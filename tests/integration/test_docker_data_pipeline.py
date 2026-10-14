import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from adaptive_agent_harness.analyzer import AgentAnalyzer, CompatibilityEngine, TargetProfiler
from adaptive_agent_harness.contracts import (
    BaseImage,
    ContainerHarnessRequest,
    DependencyInstallation,
    OperationalRequirements,
    ResourceLimits,
    TargetProfile,
    known,
)
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
from adaptive_agent_harness.harness._utils import content_hash
from adaptive_agent_harness.orchestration.baseline import BaselineRunner
from adaptive_agent_harness.runtime.docker import DockerCLI
from adaptive_agent_harness.storage.baseline import JsonResultStore

pytestmark = pytest.mark.docker


@pytest.mark.parametrize(
    "format,text",
    [
        ("CSV", "id\n1\n2\n3\n"),
        ("JSON", '[{"id":1},{"id":2},{"id":3}]'),
        ("JSONL", '{"id":1}\n{"id":2}\n{"id":3}\n'),
    ],
)
@pytest.mark.parametrize("delivery", ["FILE_ARG", "ENV_PATH"])
def test_real_approved_data_readonly_and_record_link(tmp_path, format, text, delivery):
    docker = DockerCLI()
    docker.preflight()
    host = TargetProfiler().profile_host()
    source = Path(__file__).parents[1] / "fixtures/data_reader"
    before = (source / "app.py").read_bytes()
    agent = AgentAnalyzer().analyze(
        source,
        operational_requirements=OperationalRequirements(
            python_specifier=known(">=3.12,<3.13", "fixture contract")
        ),
    )
    target = TargetProfile(
        name="local WSL fixture host",
        hardware=host.hardware,
        runtimes=host.runtimes,
        deployment=host.deployment,
    )
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    path = dataset / ("records." + format.lower())
    path.write_text(text)
    schema = DataSchema(fields={"id": DataField(type="integer")})
    pipeline = DataPipelineSpecification(
        agent_id=agent.agent_id,
        agent_source_sha256=agent.source.content_sha256,
        source=DataSource(
            path=path.name,
            format=format,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            version="raw1",
        ),
        input_schema=schema,
        version="p1",
        delivery=delivery,
        argument_index=2 if delivery == "FILE_ARG" else None,
        environment_name="DATA_FILE" if delivery == "ENV_PATH" else None,
    )
    data_approval = DataApproval(
        approval_id=uuid4(),
        approved_by="operator --run-docker",
        source_id=pipeline.source.source_id,
        source_sha256=pipeline.source.sha256,
        specification_sha256=content_hash(pipeline.model_dump(mode="json")),
    )
    prepared = LocalFilePipeline(dataset).prepare(
        pipeline, approval=data_approval, output_root=tmp_path
    )
    requirement = DataRequirement(
        pipeline_specification_sha256=prepared.binding.version.specification_sha256,
        agent_id=agent.agent_id,
        agent_source_sha256=agent.source.content_sha256,
        requires_data=known(True, "reviewed fixture input"),
        formats=known(("JSONL",), "reviewed fixture input"),
        input_schema=schema,
    )
    platform = {"x86_64": "linux/amd64", "aarch64": "linux/arm64"}[
        host.hardware.platform.architecture.value
    ]
    request = ContainerHarnessRequest(
        image=BaseImage(
            reference="python:3.12-slim",
            platforms=(platform,),
            python_version=known("3.12", "image contract"),
        ),
        argv=("python", "/opt/agent/app.py", pipeline.container_path)
        if delivery == "FILE_ARG"
        else ("python", "/opt/agent/app.py"),
        working_directory=known("/opt/agent", "fixture"),
        resource_limits=ResourceLimits(cpu_cores=2, ram_bytes=2**30),
        network_required=known(False, "fixture"),
        source_files=agent.source.file_hashes,
        source_complete=known(True, "reviewed fixture"),
        installation=DependencyInstallation(mode="NONE"),
        data_requirement=requirement,
    )
    report = CompatibilityEngine().assess(agent, target)
    spec = HarnessSpecificationGenerator().generate(agent, target, host, report, request)
    package = ContainerHarnessBuilder().build(
        spec, agent, target, host, prepared_data=prepared, output_root=tmp_path
    )
    approval = ExecutionApproval(
        approval_id=uuid4(),
        approved_by="operator --run-docker",
        mode="TRUSTED_LOCAL_TRIAL",
        agent_source_sha256=agent.source.content_sha256,
        source_bundle_sha256=package.manifest.source_bundle_sha256,
        specification_sha256=package.manifest.specification_sha256,
        manifest_sha256=hashlib.sha256(
            (package.directory / "manifest.json").read_bytes()
        ).hexdigest(),
        reviewed_unknowns=spec.unknowns,
    )
    results = BaselineRunner(docker, JsonResultStore(tmp_path)).run(
        package.directory,
        approval=approval,
        agent=agent,
        host=host,
        target=target,
        plan=BaselinePlan(
            input_sha256=prepared.binding.version.output_sha256,
            timeout_seconds=10,
            expected_stdout='{"read_only": true, "records": 3, "sum": 6}\n',
        ),
    )
    (result,) = results
    assert result.runtime.error_code is None, result.runtime.error_cause
    assert result.runtime.exit_code.value == 0
    assert result.runtime.limits_verified.value is True and result.runtime.cleanup_verified
    assert result.runtime.cleanup_errors == ()
    assert result.benchmark.functional_validation.status == "PASS"
    assert result.data_pipeline == prepared.binding
    assert result.benchmark.conditions.data_fingerprints["source"] == pipeline.source.sha256
    assert (
        BaselineResult.model_validate_json(
            (tmp_path / f"{result.runtime.run_id}.json").read_bytes()
        )
        == result
    )
    assert path.read_text() == text and (source / "app.py").read_bytes() == before
