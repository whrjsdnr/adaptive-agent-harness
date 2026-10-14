"""Actual approved fixture experiments; not final validation of a user's Agent."""

import hashlib
import time
from pathlib import Path
from uuid import uuid4

import pytest

from adaptive_agent_harness.analyzer import AgentAnalyzer, CompatibilityEngine, TargetProfiler
from adaptive_agent_harness.benchmark.bottleneck import BottleneckAnalyzer
from adaptive_agent_harness.contracts import (
    BaseImage,
    ChangeScope,
    DependencyInstallation,
    OperationalRequirements,
    ResourceLimits,
    TargetProfile,
    known,
)
from adaptive_agent_harness.contracts.adaptation import (
    AdaptationBudget,
    ApprovedCandidateRequest,
    CandidateAuthorization,
    ConfigurationPatch,
    IncubationReport,
    NumericConfiguration,
)
from adaptive_agent_harness.contracts.base import content_hash
from adaptive_agent_harness.contracts.data import (
    DataApproval,
    DataField,
    DataPipelineSpecification,
    DataRequirement,
    DataSchema,
    DataSource,
)
from adaptive_agent_harness.contracts.harness import ContainerHarnessRequest
from adaptive_agent_harness.contracts.runtime import BaselinePlan, ExecutionApproval
from adaptive_agent_harness.data import LocalFilePipeline
from adaptive_agent_harness.harness import ContainerHarnessBuilder, HarnessSpecificationGenerator
from adaptive_agent_harness.optimizer.planner import CandidatePlanner, HypothesisGenerator
from adaptive_agent_harness.orchestration.baseline import BaselineRunner
from adaptive_agent_harness.orchestration.incubation import CandidateExecutor, IncubationEngine
from adaptive_agent_harness.porting.manager import PortingManager
from adaptive_agent_harness.runtime.docker import DockerCLI, RuntimeFailure
from adaptive_agent_harness.storage.baseline import JsonResultStore
from adaptive_agent_harness.storage.experiments import JsonExperimentStore

pytestmark = pytest.mark.docker


def test_actual_adaptation_data_versions_quality_and_rollback(tmp_path):
    runtime = DockerCLI()
    runtime.preflight()
    host = TargetProfiler().profile_host()
    source = Path(__file__).parents[1] / "fixtures/configurable_reader"
    original = (source / "app.py").read_bytes()
    agent = AgentAnalyzer().analyze(
        source,
        operational_requirements=OperationalRequirements(
            python_specifier=known(">=3.12,<3.13", "fixture requirements")
        ),
    )
    target = TargetProfile(
        name="measured WSL fixture host",
        hardware=host.hardware,
        runtimes=host.runtimes,
        deployment=host.deployment,
    )
    data_root = tmp_path / "data-original"
    data_root.mkdir()
    data = data_root / "source.jsonl"
    data.write_text('{"id":1}\n{"id":2}\n{"id":3}\n')
    original_data = data.read_bytes()
    pipeline = DataPipelineSpecification(
        agent_id=agent.agent_id,
        agent_source_sha256=agent.source.content_sha256,
        source=DataSource(
            path=data.name,
            format="JSONL",
            sha256=hashlib.sha256(original_data).hexdigest(),
            version="original1",
        ),
        input_schema=DataSchema(fields={"id": DataField(type="integer")}),
        version="pipeline1",
        argument_index=7,
    )
    data_approval = DataApproval(
        approval_id=uuid4(),
        approved_by="--run-docker fixture operator",
        source_id=pipeline.source.source_id,
        source_sha256=pipeline.source.sha256,
        specification_sha256=content_hash(pipeline.model_dump(mode="json")),
    )
    manager = PortingManager(local_pipeline=LocalFilePipeline(data_root))
    prepared = manager.prepare_data(pipeline, approval=data_approval, output_root=tmp_path)
    requirement = DataRequirement(
        agent_id=agent.agent_id,
        agent_source_sha256=agent.source.content_sha256,
        requires_data=known(True, "fixture contract"),
        formats=known(("JSONL",), "fixture contract"),
        input_schema=pipeline.input_schema,
        pipeline_specification_sha256=prepared.binding.version.specification_sha256,
    )
    platform = {"x86_64": "linux/amd64", "aarch64": "linux/arm64"}[
        host.hardware.platform.architecture.value
    ]
    request = ContainerHarnessRequest(
        image=BaseImage(
            reference="python:3.12-slim",
            platforms=(platform,),
            python_version=known("3.12", "fixture image"),
        ),
        argv=(
            "python",
            "/opt/agent/app.py",
            "--delay",
            "6",
            "--mode",
            "0",
            "--data",
            pipeline.container_path,
        ),
        working_directory=known("/opt/agent", "fixture contract"),
        resource_limits=ResourceLimits(cpu_cores=1, ram_bytes=512 * 2**20),
        network_required=known(False, "fixture contract"),
        source_complete=known(True, "fixture review"),
        source_files=agent.source.file_hashes,
        installation=DependencyInstallation(mode="NONE"),
        data_requirement=requirement,
    )
    spec = HarnessSpecificationGenerator().generate(
        agent, target, host, CompatibilityEngine().assess(agent, target), request
    )
    package = ContainerHarnessBuilder().build(
        spec, agent, target, host, output_root=tmp_path, prepared_data=prepared
    )
    approval = ExecutionApproval(
        approval_id=uuid4(),
        approved_by="--run-docker fixture operator",
        mode="TRUSTED_LOCAL_TRIAL",
        agent_source_sha256=agent.source.content_sha256,
        source_bundle_sha256=package.manifest.source_bundle_sha256,
        specification_sha256=package.manifest.specification_sha256,
        manifest_sha256=hashlib.sha256(
            (package.directory / "manifest.json").read_bytes()
        ).hexdigest(),
        reviewed_unknowns=spec.unknowns,
    )
    plan = BaselinePlan(
        input_sha256=prepared.binding.version.output_sha256,
        timeout_seconds=10,
        build_timeout_seconds=60,
        expected_stdout='{"records": 3, "sum": 6}\n',
        repetitions=3,
        warmups=1,
    )
    runner = BaselineRunner(runtime, JsonResultStore(tmp_path))
    baseline = runner.run(
        package.directory, approval=approval, agent=agent, host=host, target=target, plan=plan
    )
    assert len(baseline) == 4 and all(
        r.benchmark.functional_validation.status == "PASS" for r in baseline
    )
    first = baseline[1]
    findings = BottleneckAnalyzer().analyze(
        [r.benchmark for r in baseline],
        runtimes=[r.runtime for r in baseline],
        max_latency_seconds=1,
    )
    cfg_delay = NumericConfiguration(
        name="--delay",
        argument_index=3,
        minimum=0.1,
        maximum=10,
        alternatives=(2.0,),
        reason="Fixture declares an adjustable post-read wait; exact-output must remain equivalent",
    )
    cfg_bad = NumericConfiguration(
        name="--mode",
        argument_index=5,
        minimum=0,
        maximum=1,
        alternatives=(1,),
        reason="Adversarial quality guard fixture configuration",
    )
    proposals = HypothesisGenerator().generate(
        first.benchmark, findings, spec, plan, configurations=(cfg_delay, cfg_bad)
    )
    assert len(proposals) == 2
    # Resource trials are explicit controlled hypotheses, not claims of proven CPU/RAM causality.
    resource_patches = (
        ConfigurationPatch(field="resource_limits.cpu_cores", before=1.0, after=2.0),
        ConfigurationPatch(field="resource_limits.ram_bytes", before=512 * 2**20, after=2**30),
        ConfigurationPatch(field="timeout_seconds", before=10.0, after=0.1),
    )
    hypotheses = [
        (
            proposals[0][0].model_copy(
                update={
                    "scope": ChangeScope.HARNESS,
                    "changed_fields": (p.field,),
                    "reason": "Observed delay warrants a controlled allocation/deadline trial; causality unproven",
                }
            ),
            p,
        )
        for p in resource_patches
    ]
    hypotheses += list(
        reversed(proposals)
    )  # failure first, improvement last; prior failure must not erase it.
    approved = []
    for hypothesis, patch in hypotheses:
        from adaptive_agent_harness.contracts.optimization import ExperimentBudget

        executable = CandidatePlanner().plan(
            hypothesis,
            patch,
            baseline=first.benchmark,
            baseline_run_ids=tuple(r.runtime.run_id for r in baseline),
            specification=spec,
            runtime_plan=plan,
            agent=agent,
            target=target,
            budget=ExperimentBudget(max_runs=4, max_total_seconds=180),
        )
        approved.append(
            ApprovedCandidateRequest(
                plan=executable,
                authorization=CandidateAuthorization(
                    approved_by="--run-docker exact fixture plan",
                    plan_sha256=content_hash(executable.model_dump(mode="json")),
                    baseline_approval_ids=(approval.approval_id,),
                    reviewed_candidate_unknowns=spec.unknowns,
                ),
            )
        )
    store = JsonExperimentStore(tmp_path)
    executor = CandidateExecutor(runner, manager, store)
    report = IncubationEngine(executor, store).run(
        approved,
        baseline_package=package.directory,
        baseline=baseline,
        agent=agent,
        host=host,
        target=target,
        output_root=tmp_path,
        budget=AdaptationBudget(
            max_candidates=5, max_runs=20, max_total_seconds=900, max_no_improvement=5
        ),
    )
    assert len(report.outcomes) == 5 and report.used_runs == 20
    assert all(o.status != "IMPROVED" for o in report.outcomes[:4])
    assert report.outcomes[2].status == "REGRESSED"
    assert all(r.runtime.timed_out for r in report.outcomes[2].results)
    assert report.outcomes[3].status == "REGRESSED"
    assert report.outcomes[4].status == "IMPROVED", report.outcomes[4].reasons
    assert report.selected_candidate_id == approved[4].plan.candidate.candidate_id
    assert report.automatically_deployed is False
    for o in report.outcomes:
        assert all(r.runtime.cleanup_verified and not r.runtime.cleanup_errors for r in o.results)
        assert all(r.runtime.limits_verified.value is True for r in o.results)
        assert all(r.data_pipeline.version == prepared.binding.version for r in o.results)
        assert all(
            r.benchmark.agent_source_sha256 == agent.source.content_sha256 for r in o.results
        )
    assert report.outcomes[4].comparison.quality_preserved.evidence.value == "MEASURED"
    assert (
        ContainerHarnessBuilder().verify(Path(report.final_package_reference)).harness_id
        == approved[4].plan.candidate.resulting_harness_id
    )
    assert (
        IncubationReport.model_validate_json((tmp_path / f"{report.session_id}.json").read_bytes())
        == report
    )
    ContainerHarnessBuilder().verify(package.directory)
    assert data.read_bytes() == original_data and (source / "app.py").read_bytes() == original
    reviewed = []

    def authorize_next(executable):
        reviewed.append(executable)
        return CandidateAuthorization(
            approved_by="--run-docker external exact review",
            plan_sha256=content_hash(executable.model_dump(mode="json")),
            baseline_approval_ids=(approval.approval_id,),
            reviewed_candidate_unknowns=spec.unknowns,
        )

    adaptive = IncubationEngine(executor, store).adapt(
        authorize=authorize_next,
        baseline_package=package.directory,
        baseline=baseline,
        agent=agent,
        host=host,
        target=target,
        output_root=tmp_path,
        configurations=(cfg_bad, cfg_delay),
        warmups=0,
        budget=AdaptationBudget(
            max_candidates=3,
            max_runs=9,
            max_total_seconds=240,
            max_no_improvement=3,
            target_latency_seconds=3,
        ),
    )
    assert len(adaptive.outcomes) == 2 and adaptive.used_runs == 6
    assert adaptive.outcomes[0].status == "REGRESSED"
    assert adaptive.outcomes[1].status == "IMPROVED"
    assert adaptive.stop_reason == "GOAL_REACHED"
    assert adaptive.selected_candidate_id == reviewed[1].candidate.candidate_id
    assert all(r.runtime.cleanup_verified for o in adaptive.outcomes for r in o.results)


def test_actual_build_timeout_removes_intermediate_container(container_inputs, tmp_path):
    """Backend cancellation fixture, never an executable optimization patch or Agent code edit."""
    agent, target, host, report, request = container_inputs
    spec = HarnessSpecificationGenerator().generate(agent, target, host, report, request)
    root = tmp_path / "cancelled-build"
    root.mkdir(mode=0o700)
    (root / "Dockerfile").write_text(
        "FROM python:3.12-slim\n"
        f'LABEL org.adaptive-harness.id="{spec.harness_id}"\n'
        'RUN ["python", "-c", "import time; time.sleep(30)"]\n'
    )
    runtime = DockerCLI()
    runtime.preflight()
    tag = f"adaptive-baseline:{uuid4()}"
    try:
        with pytest.raises(RuntimeFailure, match="COMMAND_TIMEOUT"):
            runtime.build(root, tag, spec, 5, False)
        deadline = time.monotonic() + 15
        while True:
            pending = runtime.command(
                [
                    "ps",
                    "--all",
                    "--quiet",
                    "--filter",
                    f"label=org.adaptive-harness.id={spec.harness_id}",
                ],
                cwd=root,
            ).stdout
            if not pending or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        assert pending == b"", "Cancelled build left its labeled intermediate container"
    finally:
        runtime.remove_image(tag)
