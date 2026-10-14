import hashlib
import json
import os
import tracemalloc
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from adaptive_agent_harness.analyzer import AgentAnalyzer
from adaptive_agent_harness.analyzer.data import AgentDataRequirementAnalyzer
from adaptive_agent_harness.contracts import known
from adaptive_agent_harness.contracts.data import (
    DataApproval,
    DataField,
    DataFormat,
    DataPipelineBinding,
    DataPipelineSpecification,
    DataRequirement,
    DataSchema,
    DataSource,
    PipelineLimits,
    ReplaySpecification,
)
from adaptive_agent_harness.data import DataFailure, LocalFilePipeline
from adaptive_agent_harness.harness._utils import content_hash


@pytest.fixture
def pipeline_case(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    schema = DataSchema(fields={"id": DataField(type="integer"), "name": DataField(type="string")})

    def prepare(
        text,
        *,
        format="JSONL",
        output_format="JSONL",
        policy="REJECT",
        limits=None,
        fields=(),
        approval_change=None,
    ):
        path = source / ("records." + format.lower())
        path.write_text(text)
        original = path.read_bytes()
        spec = DataPipelineSpecification(
            agent_id=uuid4(),
            agent_source_sha256="a" * 64,
            source=DataSource(
                path=path.name,
                format=format,
                sha256=hashlib.sha256(original).hexdigest(),
                version="raw-v1",
            ),
            input_schema=schema,
            output_format=output_format,
            argument_index=2,
            version="v1",
            error_policy=policy,
            limits=limits or PipelineLimits(),
            project_fields=fields,
        )
        approval = DataApproval(
            approval_id=uuid4(),
            approved_by="fixture operator",
            source_id=spec.source.source_id,
            source_sha256=spec.source.sha256,
            specification_sha256=content_hash(spec.model_dump(mode="json")),
        )
        if approval_change:
            approval = approval.model_copy(update=approval_change)
        data = LocalFilePipeline(source).prepare(spec, approval=approval, output_root=output)
        assert path.read_bytes() == original
        return data, spec, approval, path

    return prepare, source, output


@pytest.mark.parametrize(
    "format,text",
    [
        ("CSV", "id,name\n1,가\n2,나\n"),
        ("JSON", '[{"id":1,"name":"가"},{"id":2,"name":"나"}]'),
        ("JSONL", '{"id":1,"name":"가"}\n{"id":2,"name":"나"}\n'),
    ],
)
@pytest.mark.parametrize("output_format", list(DataFormat))
def test_formats_and_replay(pipeline_case, format, text, output_format):
    data, _, _, _ = pipeline_case[0](text, format=format, output_format=output_format)
    expected = [{"id": 1, "name": "가"}, {"id": 2, "name": "나"}]
    assert list(data.replay()) == expected
    replay = ReplaySpecification.model_validate(
        {**data.binding.replay.model_dump(), "repetitions": 2}
    )
    assert list(data.replay(replay)) == expected * 2
    assert data.path.stat().st_mode & 0o777 == 0o400
    assert data.directory.stat().st_mode & 0o777 == 0o700
    assert DataPipelineBinding.model_validate_json(data.binding.model_dump_json()) == data.binding
    assert all(
        m.evidence == "MEASURED" and m.value >= 0 for m in data.binding.delivery_metrics.metrics
    )
    assert data.binding.delivery_metrics.consumer_latency_seconds.value is None


@pytest.mark.parametrize(
    "bad,code",
    [
        ('{"name":"bad"}', "MISSING_FIELD"),
        ('{"id":true,"name":"bad"}', "FIELD_TYPE"),
        ('{"id":"1","name":"bad"}', "FIELD_TYPE"),
        ('{"id":1,"name":null}', "FIELD_TYPE"),
        ('{"id":1,"name":"bad","other":2}', "EXTRA_FIELDS"),
        ('{"id":1,"id":2,"name":"bad"}', "MALFORMED_RECORD"),
        ('{"id":NaN,"name":"bad"}', "MALFORMED_RECORD"),
        ("broken", "MALFORMED_RECORD"),
        ("[1,2]", "OBJECT_REQUIRED"),
    ],
)
def test_invalid_records_are_rejected_or_quarantined(pipeline_case, bad, code):
    text = '{"id":1,"name":"ok"}\n' + bad + "\n"
    with pytest.raises(DataFailure, match=code):
        pipeline_case[0](text)
    assert list(pipeline_case[2].iterdir()) == []
    data, _, _, _ = pipeline_case[0](text, policy="QUARANTINE")
    assert list(data.replay()) == [{"id": 1, "name": "ok"}]
    assert data.binding.validation.invalid_records == 1
    quarantined = (data.directory / "quarantine.jsonl").read_text()
    assert json.loads(quarantined) == {"record": 2, "code": code}
    assert "bad" not in quarantined


@pytest.mark.parametrize(
    "text",
    ["[]", "{}", '[{"id":1,"name":"a"},]', '[{"id":1,"name":"a"}] garbage', '[{"id":1,"name":"a"}'],
)
def test_malformed_or_empty_json_is_not_published(pipeline_case, text):
    with pytest.raises(DataFailure):
        pipeline_case[0](text, format="JSON")
    assert list(pipeline_case[2].iterdir()) == []


@pytest.mark.parametrize(
    "change",
    [{"source_sha256": "f" * 64}, {"source_id": uuid4()}, {"specification_sha256": "e" * 64}],
)
def test_exact_data_approval_required(pipeline_case, change):
    with pytest.raises(DataFailure, match="APPROVAL"):
        pipeline_case[0]('{"id":1,"name":"a"}\n', approval_change=change)
    assert list(pipeline_case[2].iterdir()) == []


@pytest.mark.parametrize(
    "path",
    [
        "../outside.json",
        "/etc/passwd",
        "a/../b.json",
        "a\\b.json",
        ".env",
        ".git/data.json",
        "secrets.json",
    ],
)
def test_unsafe_source_paths_rejected(path):
    with pytest.raises(ValidationError):
        DataSource(path=path, format="JSON", sha256="a" * 64, version="v1")


def test_links_and_source_hash_changes_block_before_publication(pipeline_case, tmp_path):
    data, spec, approval, path = pipeline_case[0]('{"id":1,"name":"a"}\n')
    path.write_text('{"id":2,"name":"changed"}\n')
    with pytest.raises(DataFailure, match="VERSION"):
        LocalFilePipeline(pipeline_case[1]).prepare(
            spec, approval=approval, output_root=pipeline_case[2]
        )
    path.unlink()
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"id":1,"name":"a"}\n')
    path.symlink_to(outside)
    with pytest.raises(DataFailure, match="UNSAFE"):
        LocalFilePipeline(pipeline_case[1]).prepare(
            spec, approval=approval, output_root=pipeline_case[2]
        )
    linked = tmp_path / "linked"
    linked.symlink_to(pipeline_case[1], target_is_directory=True)
    with pytest.raises(DataFailure, match="UNSAFE"):
        LocalFilePipeline(linked)
    assert list(pipeline_case[2].iterdir()) == [data.directory]


def test_replay_tampering_detected_before_first_record(pipeline_case):
    data, _, _, _ = pipeline_case[0]('{"id":1,"name":"a"}\n')
    data.path.chmod(0o600)
    data.path.write_text('{"id":2,"name":"tampered"}\n')
    with pytest.raises(DataFailure, match="VERSION"):
        next(data.replay())


@pytest.mark.parametrize(
    "limit",
    [
        {"max_source_bytes": 2},
        {"max_output_bytes": 2},
        {"max_record_bytes": 5},
        {"max_records": 1},
    ],
)
def test_size_and_record_limits(pipeline_case, limit):
    with pytest.raises(DataFailure, match="LIMIT"):
        pipeline_case[0](
            '{"id":1,"name":"a"}\n{"id":2,"name":"b"}\n', limits=PipelineLimits(**limit)
        )
    assert list(pipeline_case[2].iterdir()) == []


def test_streaming_large_json_and_buffer_bounds(pipeline_case):
    text = json.dumps([{"id": i, "name": "x" * 1024} for i in range(18000)])
    assert len(text) > 16 * 1024**2
    _, spec, approval, path = pipeline_case[0](
        '[{"id":1,"name":"a"}]',
        format="JSON",
        limits=PipelineLimits(buffer_records=4, buffer_bytes=8192, max_record_bytes=2048),
    )
    path.write_text(text)
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    spec = DataPipelineSpecification.model_validate(
        {**spec.model_dump(), "source": {**spec.source.model_dump(), "sha256": digest}}
    )
    approval = DataApproval.model_validate(
        {
            **approval.model_dump(),
            "source_sha256": digest,
            "specification_sha256": content_hash(spec.model_dump(mode="json")),
        }
    )
    del text
    tracemalloc.start()
    try:
        data = LocalFilePipeline(pipeline_case[1]).prepare(
            spec, approval=approval, output_root=pipeline_case[2]
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert sum(1 for _ in data.replay()) == 18000
    assert data.binding.delivery_metrics.buffer_peak_records <= 4
    assert data.binding.delivery_metrics.buffer_peak_bytes <= 8192
    assert peak < 5 * 1024**2


def test_projection_and_deterministic_output(pipeline_case):
    text = '{"id":2,"name":"a"}\n{"id":1,"name":"b"}\n'
    first, spec, approval, _ = pipeline_case[0](text, fields=("id",))
    second = LocalFilePipeline(pipeline_case[1]).prepare(
        spec, approval=approval, output_root=pipeline_case[2]
    )
    assert list(first.replay()) == [{"id": 2}, {"id": 1}]
    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.binding.version.output_sha256 == second.binding.version.output_sha256
    assert first.binding.version.version_id != second.binding.version.version_id


def test_sensitive_data_is_never_quarantined_or_published(pipeline_case):
    secret = "sk-" + "s" * 32
    with pytest.raises(DataFailure, match="SENSITIVE") as error:
        pipeline_case[0](json.dumps({"id": 1, "name": secret}) + "\n", policy="QUARANTINE")
    assert secret not in str(error.value)
    assert list(pipeline_case[2].iterdir()) == []


def test_failure_during_binding_save_removes_partial_result(pipeline_case, monkeypatch):
    original = Path.write_text

    def fail(path, *args, **kwargs):
        if path.name == "binding.json":
            raise OSError("injected write failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail)
    with pytest.raises(OSError):
        pipeline_case[0]('{"id":1,"name":"a"}\n')
    assert list(pipeline_case[2].iterdir()) == []


def test_multiline_csv_record_bound(pipeline_case):
    with pytest.raises(DataFailure, match="RECORD_SIZE"):
        pipeline_case[0](
            'id,name\n1,"' + "a\n" * 100 + '"\n',
            format="CSV",
            limits=PipelineLimits(max_record_bytes=32),
        )


def test_static_analysis_is_observation_not_execution(tmp_path):
    code = """import os, json
from pathlib import Path
raise RuntimeError("Agent must never execute")
open("input.json", "r")
Path("output.json").write_text("data")
Path("created.json").open("w")
open(os.getenv("DATA_PATH"))
json.load(None)
"""
    (tmp_path / "app.py").write_text(code)
    before = hashlib.sha256((tmp_path / "app.py").read_bytes()).hexdigest()
    agent = AgentAnalyzer().analyze(tmp_path)
    requirement = AgentDataRequirementAnalyzer().analyze(agent)
    assert requirement.requires_data.value is None
    assert requirement.input_schema is None
    assert requirement.order_dependent.value is None
    assert requirement.input_files.value == ("input.json",)
    assert {o.kind for o in requirement.observations} >= {
        "READ",
        "WRITE",
        "FORMAT",
        "DYNAMIC_PATH",
        "ENVIRONMENT",
    }
    assert hashlib.sha256((tmp_path / "app.py").read_bytes()).hexdigest() == before
    declared = DataRequirement(
        agent_id=agent.agent_id,
        agent_source_sha256=agent.source.content_sha256,
        requires_data=known(True, "operator"),
    )
    assert (
        AgentDataRequirementAnalyzer().analyze(agent, declared=declared).requires_data.value is True
    )
    with pytest.raises(DataFailure, match="AGENT"):
        AgentDataRequirementAnalyzer().analyze(
            agent, declared=declared.model_copy(update={"agent_id": uuid4()})
        )
    (tmp_path / "app.py").write_text('print("changed")')
    with pytest.raises(DataFailure, match="VERSION"):
        AgentDataRequirementAnalyzer().analyze(agent)


def test_schema_field_named_created_at_is_hashed():
    a = DataSchema(fields={"created_at": DataField(type="string")})
    b = DataSchema(fields={"created_at": DataField(type="integer")})
    assert content_hash(a.model_dump(mode="json")) != content_hash(b.model_dump(mode="json"))


def test_single_json_object_is_one_record(pipeline_case):
    data, _, _, _ = pipeline_case[0]('{"id":1,"name":"single"}', format="JSON")
    assert list(data.replay()) == [{"id": 1, "name": "single"}]


def test_unsupported_csv_field_size_never_publishes_pass(pipeline_case):
    record = json.dumps({"id": 1, "name": "x" * 140000})
    with pytest.raises(DataFailure, match="CSV_FIELD_LIMIT"):
        pipeline_case[0](
            record,
            format="JSON",
            output_format="CSV",
            limits=PipelineLimits(max_record_bytes=1024**2),
        )
    assert list(pipeline_case[2].iterdir()) == []


def test_even_existing_empty_version_directory_is_never_replaced(pipeline_case, monkeypatch):
    identifier = uuid4()
    target = pipeline_case[2] / ("data-" + str(identifier))
    target.mkdir(mode=0o700)
    before = target.stat().st_ino
    monkeypatch.setattr("adaptive_agent_harness.data.local.uuid4", lambda: identifier)
    with pytest.raises(DataFailure, match="OUTPUT_EXISTS"):
        pipeline_case[0]('{"id":1,"name":"a"}\n')
    assert target.stat().st_ino == before and list(target.iterdir()) == []
    assert list(pipeline_case[2].iterdir()) == [target]


def test_restore_and_derived_version_preserve_previous_replay(pipeline_case):
    from adaptive_agent_harness.data import PreparedData

    first, spec, approval, _ = pipeline_case[0]('{"id":1,"name":"a"}\n')
    restored = PreparedData.load(first.directory)
    assert restored.binding == first.binding
    derived = DataPipelineSpecification.model_validate(
        {
            **spec.model_dump(),
            "version": "v2",
            "parent_version_id": first.binding.version.version_id,
            "changes": ("limits.buffer_records",),
            "limits": {**spec.limits.model_dump(), "buffer_records": 1},
        }
    )
    new_approval = approval.model_copy(
        update={"specification_sha256": content_hash(derived.model_dump(mode="json"))}
    )
    with pytest.raises(DataFailure, match="PARENT_VERSION_REQUIRED"):
        LocalFilePipeline(pipeline_case[1]).prepare(
            derived, approval=new_approval, output_root=pipeline_case[2]
        )
    second = LocalFilePipeline(pipeline_case[1]).prepare(
        derived,
        approval=new_approval,
        output_root=pipeline_case[2],
        previous_version=first.binding.version,
    )
    assert second.binding.version.parent_version_id == first.binding.version.version_id
    assert second.binding.version.specification_sha256 != first.binding.version.specification_sha256
    assert list(first.replay()) == list(second.replay())


def test_numeric_overflow_is_invalid_data(pipeline_case):
    with pytest.raises(DataFailure, match="MALFORMED_RECORD"):
        pipeline_case[0]('{"id":1e999,"name":"x"}\n')


def test_csv_nullable_and_structured_fields_roundtrip(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    text = '[{"s":null,"n":null,"a":[1,2],"b":true}]'
    (source / "data.json").write_text(text)
    schema = DataSchema(
        fields={
            "s": DataField(type="string", nullable=True),
            "n": DataField(type="number", nullable=True),
            "a": DataField(type="array"),
            "b": DataField(type="boolean"),
        }
    )
    spec = DataPipelineSpecification(
        agent_id=uuid4(),
        agent_source_sha256="a" * 64,
        source=DataSource(
            path="data.json",
            format="JSON",
            sha256=hashlib.sha256(text.encode()).hexdigest(),
            version="raw1",
        ),
        input_schema=schema,
        version="v1",
        output_format="CSV",
        argument_index=2,
    )
    approval = DataApproval(
        approval_id=uuid4(),
        approved_by="operator",
        source_id=spec.source.source_id,
        source_sha256=spec.source.sha256,
        specification_sha256=content_hash(spec.model_dump(mode="json")),
    )
    data = LocalFilePipeline(source).prepare(spec, approval=approval, output_root=tmp_path)
    assert list(data.replay()) == [{"s": None, "n": None, "a": [1, 2], "b": True}]


def test_output_root_replacement_cannot_redirect_writes(pipeline_case, monkeypatch, tmp_path):
    source, output = pipeline_case[1:]
    outside = tmp_path / "outside"
    outside.mkdir()
    renamed = tmp_path / "original-output"
    original = os.mkdir

    def replace(name, *args, **kwargs):
        if isinstance(name, str) and name.startswith(".data-"):
            output.rename(renamed)
            output.symlink_to(outside, target_is_directory=True)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", replace)
    with pytest.raises(DataFailure, match="UNSAFE"):
        pipeline_case[0]('{"id":1,"name":"a"}\n')
    assert list(outside.iterdir()) == []
    assert list(renamed.iterdir()) == []
