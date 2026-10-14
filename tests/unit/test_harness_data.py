import hashlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest

from adaptive_agent_harness.contracts import known
from adaptive_agent_harness.contracts.data import (
    DataApproval,
    DataField,
    DataPipelineSpecification,
    DataRequirement,
    DataSchema,
    DataSource,
)
from adaptive_agent_harness.contracts.runtime import BaselinePlan, BaselineResult, ExecutionApproval
from adaptive_agent_harness.data import LocalFilePipeline
from adaptive_agent_harness.harness import ContainerHarnessBuilder, HarnessSpecificationGenerator
from adaptive_agent_harness.harness._utils import HarnessError, content_hash
from adaptive_agent_harness.orchestration.baseline import BaselineRunner
from adaptive_agent_harness.runtime.docker import CommandResult, RuntimeFailure
from adaptive_agent_harness.storage.baseline import JsonResultStore


@pytest.fixture
def data_case(container_inputs, tmp_path):
    agent, target, host, report, request = container_inputs
    source = tmp_path / "dataset"
    source.mkdir()
    (source / "records.jsonl").write_text('{"id":1}\n')
    schema = DataSchema(fields={"id": DataField(type="integer")})
    pipeline = DataPipelineSpecification(
        agent_id=agent.agent_id,
        agent_source_sha256=agent.source.content_sha256,
        source=DataSource(
            path="records.jsonl",
            format="JSONL",
            sha256=hashlib.sha256((source / "records.jsonl").read_bytes()).hexdigest(),
            version="raw1",
        ),
        input_schema=schema,
        version="p1",
        argument_index=2,
    )
    approval = DataApproval(
        approval_id=uuid4(),
        approved_by="fixture operator",
        source_id=pipeline.source.source_id,
        source_sha256=pipeline.source.sha256,
        specification_sha256=content_hash(pipeline.model_dump(mode="json")),
    )
    prepared = LocalFilePipeline(source).prepare(pipeline, approval=approval, output_root=tmp_path)
    req = DataRequirement(
        pipeline_specification_sha256=prepared.binding.version.specification_sha256,
        agent_id=agent.agent_id,
        agent_source_sha256=agent.source.content_sha256,
        requires_data=known(True, "operator"),
        formats=known(("JSONL",), "operator"),
        input_schema=schema,
    )
    request = request.model_copy(
        update={
            "data_requirement": req,
            "argv": ("python", "/opt/agent/app.py", pipeline.container_path),
        }
    )
    spec = HarnessSpecificationGenerator().generate(agent, target, host, report, request)
    return agent, target, host, report, request, spec, prepared


def trial(data_case, tmp_path):
    agent, target, host, _, _, spec, prepared = data_case
    package = ContainerHarnessBuilder().build(
        spec, agent, target, host, prepared_data=prepared, output_root=tmp_path
    )
    approval = ExecutionApproval(
        approval_id=uuid4(),
        approved_by="fixture operator",
        mode="TRUSTED_LOCAL_TRIAL",
        agent_source_sha256=agent.source.content_sha256,
        source_bundle_sha256=package.manifest.source_bundle_sha256,
        specification_sha256=package.manifest.specification_sha256,
        manifest_sha256=hashlib.sha256(
            (package.directory / "manifest.json").read_bytes()
        ).hexdigest(),
        reviewed_unknowns=spec.unknowns,
    )
    runtime = Mock()
    runtime.preflight.return_value = {"Architecture": "x86_64"}
    runtime.build.return_value = "sha256:" + "d" * 64
    runtime.create.return_value = "container"
    now = datetime.now(UTC)
    runtime.inspect.return_value = {
        "State": {
            "Running": False,
            "ExitCode": 0,
            "OOMKilled": False,
            "Status": "exited",
            "StartedAt": (now - timedelta(seconds=1)).isoformat(),
            "FinishedAt": now.isoformat(),
        }
    }
    runtime.logs.return_value = CommandResult(b"ok\n", b"")
    runtime.stats.return_value = (20.0, 1024.0)
    plan = BaselinePlan(
        input_sha256=prepared.binding.version.output_sha256,
        timeout_seconds=1,
        expected_stdout="ok\n",
    )

    def run(plan_override=None):
        return BaselineRunner(runtime, JsonResultStore(tmp_path)).run(
            package.directory,
            approval=approval,
            agent=agent,
            target=target,
            host=host,
            plan=plan_override or plan,
        )

    return run, package, runtime, plan


def test_builder_embeds_read_only_approved_data_and_runner_persists_link(data_case, tmp_path):
    run, package, runtime, _ = trial(data_case, tmp_path)
    assert ContainerHarnessBuilder().verify(package.directory) == package.manifest
    data = package.directory / "data/input.jsonl"
    assert data.stat().st_mode & 0o777 == 0o444
    assert package.directory.stat().st_mode & 0o777 == 0o700
    dockerfile = (package.directory / "Dockerfile").read_text()
    assert "COPY --chown=0:0" in dockerfile
    assert '"/opt/data/input.jsonl"' in dockerfile
    (result,) = run()
    assert result.data_pipeline == data_case[-1].binding
    assert result.benchmark.conditions.input_sha256 == result.data_pipeline.version.output_sha256
    assert (
        result.benchmark.conditions.data_preparation_run_id
        == result.data_pipeline.delivery_metrics.run_id
    )
    assert result.benchmark.functional_validation.status == "PASS"
    assert {m.name for m in result.benchmark.metrics} >= {"wall_clock", "data_supply_seconds"}
    assert (
        BaselineResult.model_validate_json(
            (tmp_path / f"{result.runtime.run_id}.json").read_bytes()
        )
        == result
    )
    assert runtime.create.call_args.args[2].data_requirement.requires_data.value is True


@pytest.mark.parametrize("required", [True, None])
def test_missing_and_unknown_data_block_before_packaging(data_case, tmp_path, required):
    agent, target, host, report, request, _, _ = data_case
    if required is None:
        req = DataRequirement(
            agent_id=agent.agent_id, agent_source_sha256=agent.source.content_sha256
        )
        request = request.model_copy(update={"data_requirement": req})
    spec = HarnessSpecificationGenerator().generate(agent, target, host, report, request)
    with pytest.raises(HarnessError, match="DATA_REQUIRED|DATA_REQUIREMENT_UNKNOWN"):
        ContainerHarnessBuilder().build(spec, agent, target, host, output_root=tmp_path)


def test_missing_or_modified_packaged_data_block_before_runtime(data_case, tmp_path):
    run, package, runtime, _ = trial(data_case, tmp_path)
    data = package.directory / "data/input.jsonl"
    data.chmod(0o600)
    data.write_text('{"id":"bad"}\n')
    with pytest.raises(HarnessError, match="INTEGRITY"):
        run()
    runtime.preflight.assert_not_called()
    data.unlink()
    with pytest.raises(HarnessError):
        run()


def test_data_plan_hash_mismatch_blocks_before_runtime(data_case, tmp_path):
    run, _, runtime, plan = trial(data_case, tmp_path)
    with pytest.raises(RuntimeFailure, match="DATA_INPUT_HASH"):
        run(plan.model_copy(update={"input_sha256": "e" * 64}))
    runtime.preflight.assert_not_called()


def test_data_preparation_is_not_agent_execution_evidence(data_case, tmp_path):
    run, _, runtime, _ = trial(data_case, tmp_path)
    runtime.build.side_effect = RuntimeFailure("BUILD_FAILURE", "injected failure")
    (result,) = run()
    assert result.benchmark.execution_evidence == "UNVERIFIED"
    assert all(m.value is None for m in result.benchmark.metrics)
    assert all(m.evidence == "MEASURED" for m in result.data_pipeline.delivery_metrics.metrics)
    assert result.benchmark.functional_validation.status == "UNKNOWN"


def test_environment_delivery_and_other_agent_block(data_case, tmp_path):
    agent, target, host, report, request, _, prepared = data_case
    spec_data = prepared.binding.specification.model_copy(
        update={"delivery": "ENV_PATH", "argument_index": None, "environment_name": "DATA_FILE"}
    )
    approval = prepared.binding.approval.model_copy(
        update={"specification_sha256": content_hash(spec_data.model_dump(mode="json"))}
    )
    data = LocalFilePipeline(tmp_path / "dataset").prepare(
        spec_data, approval=approval, output_root=tmp_path
    )
    spec = HarnessSpecificationGenerator().generate(
        agent,
        target,
        host,
        report,
        request.model_copy(
            update={
                "argv": ("python", "/opt/agent/app.py"),
                "data_requirement": request.data_requirement.model_copy(
                    update={
                        "pipeline_specification_sha256": data.binding.version.specification_sha256
                    }
                ),
            }
        ),
    )
    package = ContainerHarnessBuilder().build(
        spec, agent, target, host, prepared_data=data, output_root=tmp_path
    )
    assert "ENV DATA_FILE=/opt/data/input.jsonl" in (package.directory / "Dockerfile").read_text()
    changed = prepared.binding.specification.model_copy(update={"agent_id": uuid4()})
    approval = prepared.binding.approval.model_copy(
        update={"specification_sha256": content_hash(changed.model_dump(mode="json"))}
    )
    other = LocalFilePipeline(tmp_path / "dataset").prepare(
        changed, approval=approval, output_root=tmp_path
    )
    with pytest.raises(HarnessError, match="DATA_CONTRACT"):
        ContainerHarnessBuilder().build(
            spec, agent, target, host, prepared_data=other, output_root=tmp_path
        )


def test_forged_pass_does_not_skip_actual_schema_validation(data_case, tmp_path):
    from dataclasses import replace

    agent, target, host, _, _, spec, prepared = data_case
    prepared.path.chmod(0o600)
    prepared.path.write_text('{"id":"invalid"}\n')
    digest = hashlib.sha256(prepared.path.read_bytes()).hexdigest()
    binding = type(prepared.binding).model_validate(
        {
            **prepared.binding.model_dump(),
            "version": {**prepared.binding.version.model_dump(), "output_sha256": digest},
            "replay": {**prepared.binding.replay.model_dump(), "output_sha256": digest},
            "delivery_metrics": {
                **prepared.binding.delivery_metrics.model_dump(),
                "bytes_delivered": len(prepared.path.read_bytes()),
            },
        }
    )
    forged = replace(prepared, binding=binding)
    with pytest.raises(HarnessError, match="DATA_VALIDATION_FAILED"):
        ContainerHarnessBuilder().build(
            spec, agent, target, host, prepared_data=forged, output_root=tmp_path
        )


def test_historical_baseline_json_remains_readable(benchmark):
    from adaptive_agent_harness.contracts.runtime import RuntimeRecord

    approval = ExecutionApproval(
        approval_id=uuid4(),
        approved_by="contract fixture",
        mode="TRUSTED_LOCAL_TRIAL",
        agent_source_sha256=benchmark.agent_source_sha256,
        source_bundle_sha256="c" * 64,
        specification_sha256=benchmark.harness_sha256,
        manifest_sha256="d" * 64,
        reviewed_unknowns=(),
    )
    old = {
        "benchmark": benchmark.model_dump(mode="json"),
        "runtime": RuntimeRecord(
            run_id=benchmark.run_id,
            approval_id=approval.approval_id,
            harness_version="baseline-1",
            source_bundle_sha256=approval.source_bundle_sha256,
            manifest_sha256=approval.manifest_sha256,
            execution_started=True,
        ).model_dump(mode="json"),
        "approval": approval.model_dump(mode="json"),
        "plan": BaselinePlan(
            input_sha256=benchmark.conditions.input_sha256, timeout_seconds=30
        ).model_dump(mode="json"),
    }
    result = BaselineResult.model_validate_json(json.dumps(old))
    assert result.data_pipeline is None
    assert "data_pipeline" not in result.model_dump(mode="json")
    assert result.benchmark.run_id == result.runtime.run_id
