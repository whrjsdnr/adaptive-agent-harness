"""Synthetic lifecycle evidence tests; fake timings are not optimization measurements."""

import hashlib
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from tests.unit.test_baseline_runner import FakeDocker

from adaptive_agent_harness.benchmark.bottleneck import BottleneckAnalyzer
from adaptive_agent_harness.contracts import EvidenceKind, TargetProfile, known
from adaptive_agent_harness.contracts.adaptation import (
    AdaptationBudget,
    ApprovedCandidateRequest,
    CandidateAuthorization,
    ConfigurationPatch,
    IncubationReport,
    ModificationAssessment,
    NumericConfiguration,
)
from adaptive_agent_harness.contracts.base import content_hash
from adaptive_agent_harness.contracts.harness import ContainerHarnessSpecification
from adaptive_agent_harness.contracts.optimization import BottleneckFinding, ExperimentBudget
from adaptive_agent_harness.contracts.runtime import BaselinePlan, ExecutionApproval
from adaptive_agent_harness.harness import ContainerHarnessBuilder, HarnessSpecificationGenerator
from adaptive_agent_harness.optimizer.evaluator import ExperimentEvaluator
from adaptive_agent_harness.optimizer.planner import (
    CandidatePlanner,
    HypothesisGenerator,
    validate_patch,
)
from adaptive_agent_harness.orchestration.baseline import BaselineRunner
from adaptive_agent_harness.orchestration.incubation import CandidateExecutor, IncubationEngine
from adaptive_agent_harness.porting.manager import PortingManager
from adaptive_agent_harness.runtime.docker import RuntimeFailure
from adaptive_agent_harness.storage.baseline import JsonResultStore
from adaptive_agent_harness.storage.experiments import JsonExperimentStore


@pytest.fixture
def adaptation_case(container_inputs, tmp_path):
    agent, target, host, report, request = container_inputs
    spec = HarnessSpecificationGenerator().generate(agent, target, host, report, request)
    package = ContainerHarnessBuilder().build(spec, agent, target, host, output_root=tmp_path)
    approval = ExecutionApproval(
        approval_id=uuid4(),
        approved_by="synthetic test",
        mode="TRUSTED_LOCAL_TRIAL",
        agent_source_sha256=agent.source.content_sha256,
        source_bundle_sha256=package.manifest.source_bundle_sha256,
        specification_sha256=package.manifest.specification_sha256,
        manifest_sha256=hashlib.sha256(
            (package.directory / "manifest.json").read_bytes()
        ).hexdigest(),
        reviewed_unknowns=spec.unknowns,
    )
    runtime = FakeDocker()
    runtime.preflight = lambda: {"Architecture": "x86_64", "ID": "synthetic-daemon"}
    runner = BaselineRunner(runtime, JsonResultStore(tmp_path))
    runtime_plan = BaselinePlan(
        input_sha256="a" * 64, timeout_seconds=1, expected_stdout="ok\n", repetitions=3
    )
    baseline = runner.run(
        package.directory,
        approval=approval,
        agent=agent,
        host=host,
        target=target,
        plan=runtime_plan,
    )
    store = JsonExperimentStore(tmp_path)
    executor = CandidateExecutor(runner, PortingManager(), store)

    def plan(patch=None):
        patch = patch or ConfigurationPatch(field="timeout_seconds", before=1, after=2)
        finding = BottleneckFinding(
            kind="TIMEOUT",
            evidence_run_ids=(baseline[0].runtime.run_id,),
            possible_causes=("Synthetic test deadline observation",),
            uncertainty="HIGH",
            additional_measurements=("Controlled repetitions",),
        )
        hypothesis, _ = HypothesisGenerator().generate(
            baseline[0].benchmark, [finding], spec, runtime_plan
        )[0]
        if patch.field != "timeout_seconds":
            hypothesis = hypothesis.model_copy(update={"changed_fields": (patch.field,)})
        return CandidatePlanner().plan(
            hypothesis,
            patch,
            baseline=baseline[0].benchmark,
            baseline_run_ids=tuple(r.runtime.run_id for r in baseline),
            specification=spec,
            runtime_plan=runtime_plan,
            agent=agent,
            target=target,
            budget=ExperimentBudget(max_runs=4, max_total_seconds=60),
        )

    def authorize(p):
        return ApprovedCandidateRequest(
            plan=p,
            authorization=CandidateAuthorization(
                approved_by="synthetic test exact patch",
                plan_sha256=content_hash(p.model_dump(mode="json")),
                baseline_approval_ids=tuple({r.approval.approval_id for r in baseline}),
                reviewed_candidate_unknowns=spec.unknowns,
            ),
        )

    context = dict(
        baseline_package=package.directory,
        baseline=baseline,
        agent=agent,
        host=host,
        target=target,
        output_root=tmp_path,
    )
    return context, executor, store, plan, authorize, runtime, spec


def test_preserved_versions_json_and_duplicate_candidates(adaptation_case):
    ctx, executor, store, plan, approve, _, spec = adaptation_case
    request = approve(plan())
    before = (ctx["baseline_package"] / "manifest.json").read_bytes()
    report = IncubationEngine(executor, store).run(
        [request, request], **ctx, budget=AdaptationBudget(max_no_improvement=3)
    )
    assert len(report.outcomes) == 1 and report.used_runs == 4
    outcome = report.outcomes[0]
    assert outcome.status == "REGRESSED" and outcome.discarded
    child = ContainerHarnessSpecification.model_validate_json(
        (Path(outcome.package_reference) / "harness.yaml").read_bytes()
    )
    assert child.parent_harness_id == spec.harness_id
    assert child.agent_source_sha256 == spec.agent_source_sha256
    assert child.security == spec.security and child.network == spec.network
    assert report.final_package_reference == str(ctx["baseline_package"])
    assert report.assessment.status == "NEEDS_INVESTIGATION"
    assert (ctx["baseline_package"] / "manifest.json").read_bytes() == before
    assert IncubationReport.model_validate_json(
        store.save(report.model_copy(update={"session_id": uuid4()})).read_bytes()
    )
    with pytest.raises(FileExistsError):
        store.save(report)


@pytest.mark.parametrize(
    "field", ["privileged", "network", "argv", "security.pids_limit", "mounts", "python_code"]
)
def test_unapproved_fields_rejected(field):
    with pytest.raises(ValidationError):
        ConfigurationPatch(field=field, before=1, after=2)


@pytest.mark.parametrize("values", [(1, 1), (0, 1), (-1, 2), (1, float("inf")), (True, 2)])
def test_invalid_numeric_patches(values):
    with pytest.raises(ValidationError):
        ConfigurationPatch(field="timeout_seconds", before=values[0], after=values[1])


def test_stale_excess_unknown_and_argument_bounds(adaptation_case):
    ctx, _, _, plan, _, _, spec = adaptation_case
    with pytest.raises(ValueError, match="STALE_PATCH"):
        plan(ConfigurationPatch(field="timeout_seconds", before=3, after=4))
    with pytest.raises(ValueError, match="TARGET_RESOURCE_LIMIT"):
        plan(ConfigurationPatch(field="resource_limits.cpu_cores", before=2, after=900))
    target = TargetProfile.model_validate(
        {
            **ctx["target"].model_dump(),
            "hardware": {**ctx["target"].hardware.model_dump(), "resources": {}},
        }
    )
    with pytest.raises(ValueError, match="TARGET_CAPACITY_UNKNOWN"):
        validate_patch(
            ConfigurationPatch(field="resource_limits.cpu_cores", before=2, after=4),
            spec,
            ctx["baseline"][0].plan,
            ctx["agent"],
            target,
        )
    cfg = NumericConfiguration(
        name="--pretend",
        argument_index=2,
        minimum=0,
        maximum=2,
        alternatives=(1,),
        reason="reviewed",
    )
    with pytest.raises(ValueError, match="UNDECLARED_CONFIGURATION"):
        validate_patch(
            ConfigurationPatch(field="agent_argument", before=0, after=1, configuration=cfg),
            spec,
            ctx["baseline"][0].plan,
            ctx["agent"],
            ctx["target"],
        )


def test_exact_approval_cannot_reuse_changed_patch(adaptation_case):
    _, _, _, plan, approve, _, _ = adaptation_case
    request = approve(plan())
    wire = request.model_dump(mode="json")
    wire["plan"]["patch"]["after"] = 8
    with pytest.raises(ValidationError):
        ApprovedCandidateRequest.model_validate(wire)


@pytest.mark.parametrize("stage", ["build", "create", "wait", "timeout", "remove"])
def test_failed_candidate_and_original_retained(adaptation_case, stage):
    ctx, executor, store, plan, approve, runtime, _ = adaptation_case
    runtime.failure = stage
    report = IncubationEngine(executor, store).run(
        [approve(plan())], **ctx, budget=AdaptationBudget()
    )
    assert report.outcomes[0].status != "IMPROVED"
    assert report.selected_candidate_id is None
    assert report.final_package_reference == str(ctx["baseline_package"])
    assert len(report.outcomes[0].results) == (1 if stage == "remove" else 4)
    assert report.stop_reason == (
        "CLEANUP_FAILED" if stage == "remove" else "APPROVED_CANDIDATES_EXHAUSTED"
    )


@pytest.mark.parametrize(
    "budget", [AdaptationBudget(max_runs=1), AdaptationBudget(max_total_seconds=1e-12)]
)
def test_no_execution_when_budget_exhausted(adaptation_case, budget):
    ctx, executor, store, plan, approve, runtime, _ = adaptation_case
    before = len(runtime.calls)
    report = IncubationEngine(executor, store).run([approve(plan())], **ctx, budget=budget)
    assert report.used_runs == 0 and len(runtime.calls) == before
    assert report.stop_reason == "EXPERIMENT_BUDGET_EXHAUSTED"


def test_no_information_no_hypothesis_and_foreign_evidence(adaptation_case):
    ctx, _, _, _, _, _, spec = adaptation_case
    b = ctx["baseline"][0]
    unknown = BottleneckAnalyzer().analyze([b.benchmark])
    assert HypothesisGenerator().generate(b.benchmark, unknown, spec, b.plan) == ()
    foreign = BottleneckFinding(
        kind="OOM",
        evidence_run_ids=(uuid4(),),
        possible_causes=("unknown",),
        uncertainty="UNKNOWN",
        additional_measurements=("diagnostic",),
    )
    assert HypothesisGenerator().generate(b.benchmark, [foreign], spec, b.plan) == ()


def test_bad_quality_and_environment_cannot_improve(adaptation_case):
    ctx, executor, _, plan, approve, runtime, _ = adaptation_case
    runtime.output = b"bad\n"
    outcome = executor.execute(approve(plan()), **ctx)
    assert outcome.status == "REGRESSED"
    assert outcome.comparison.candidate_success_rate.value == 0
    assert outcome.comparison.quality_preserved.value is None
    candidate = outcome.results
    altered = ctx["baseline"][0].benchmark.model_copy(
        update={
            "conditions": ctx["baseline"][0].benchmark.conditions.model_copy(
                update={"environment_sha256": "e" * 64}
            )
        }
    )
    different = ctx["baseline"][0].model_copy(update={"benchmark": altered})
    assert (
        ExperimentEvaluator().compare(
            [different], candidate, policy=plan().experiment.comparison_policy
        )[0]
        == "INVALID"
    )


def test_code_escalation_requires_diagnostics():
    with pytest.raises(ValidationError):
        ModificationAssessment(
            status="AGENT_MODIFICATION_REQUIRED",
            unresolved_problems=("latency",),
            attempted_candidate_ids=(),
            evidence_run_ids=(),
            reasons=("high CPU is insufficient",),
        )


def test_manager_simulator_unknown_and_data_missing():
    manager = PortingManager()
    assert manager.invoke_simulator("connect").status == "UNKNOWN"
    with pytest.raises(ValueError, match="DATA_ADAPTER_UNAVAILABLE"):
        manager.prepare_data(None, approval=None, output_root=Path("/unused"))


def test_runner_stops_before_first_build(adaptation_case):
    ctx, executor, _, _, _, runtime, _ = adaptation_case
    first = ctx["baseline"][0]
    before = len(runtime.calls)
    results = executor.runner.run(
        ctx["baseline_package"],
        agent=ctx["agent"],
        host=ctx["host"],
        target=ctx["target"],
        approval=first.approval,
        plan=first.plan,
        max_total_seconds=1e-12,
    )
    assert results == () and len(runtime.calls) == before


def test_best_candidate_retained_after_later_failure(adaptation_case):
    ctx, executor, store, plan, approve, runtime, _ = adaptation_case
    inspect = runtime.inspect

    def faster(*args):
        state = inspect(*args)
        state["State"]["StartedAt"] = (
            datetime.fromisoformat(state["State"]["FinishedAt"]) - timedelta(seconds=0.5)
        ).isoformat()
        return state

    runtime.inspect = faster
    good = approve(plan())
    bad = approve(plan(ConfigurationPatch(field="timeout_seconds", before=1, after=3)))
    original_execute = executor.execute

    def execute(request, **kwargs):
        if request == bad:
            runtime.output = b"wrong\n"
        return original_execute(request, **kwargs)

    executor.execute = execute
    report = IncubationEngine(executor, store).run([good, bad], **ctx, budget=AdaptationBudget())
    assert [o.status for o in report.outcomes] == ["IMPROVED", "REGRESSED"]
    assert report.selected_candidate_id == good.plan.candidate.candidate_id
    assert report.final_package_reference == report.outcomes[0].package_reference
    assert report.outcomes[0].comparison.improvement_fraction.value == pytest.approx(0.5)
    assert report.outcomes[0].comparison.quality_preserved.value is True


def test_measured_adaptive_loop_regenerates_new_parent(adaptation_case):
    ctx, executor, store, _, _, runtime, spec = adaptation_case
    # Synthetic CPU observations exercise the closed loop; never report these as actual performance.
    baseline = []
    for result in ctx["baseline"]:
        metrics = tuple(
            m.model_copy(update={"value": 100.0, "evidence": EvidenceKind.MEASURED})
            if m.name == "cpu_percent_sample_mean"
            else m
            for m in result.benchmark.metrics
        )
        baseline.append(
            result.model_copy(
                update={"benchmark": result.benchmark.model_copy(update={"metrics": metrics})}
            )
        )
    ctx["baseline"] = tuple(baseline)
    cpu = 2.0
    create, inspect, wait = runtime.create, runtime.inspect, runtime.wait

    def configured_create(*args):
        nonlocal cpu
        cpu = args[2].resource_limits.cpu_cores
        return create(*args)

    def measured_state(*args):
        state = inspect(*args)
        state["State"]["StartedAt"] = (
            datetime.fromisoformat(state["State"]["FinishedAt"])
            - timedelta(seconds=0.5 if cpu == 4 else 0.2)
        ).isoformat()
        return state

    def bounded_wait(*args):
        wait(*args)
        time.sleep(0.005)

    runtime.create, runtime.inspect, runtime.wait = configured_create, measured_state, bounded_wait
    runtime.stats = lambda *_: (100.0, 1024.0)
    approved_plans = []

    def authorize(plan):
        approved_plans.append(plan)
        current = ctx["baseline"] if not approved_plans[:-1] else latest[0]
        return CandidateAuthorization(
            approved_by="synthetic callback exact review",
            plan_sha256=content_hash(plan.model_dump(mode="json")),
            baseline_approval_ids=tuple({r.approval.approval_id for r in current}),
            reviewed_candidate_unknowns=spec.unknowns,
        )

    latest = [()]
    original_execute = executor.execute

    def remember(request, **kwargs):
        outcome = original_execute(request, **kwargs)
        latest[0] = outcome.results
        return outcome

    executor.execute = remember
    report = IncubationEngine(executor, store).adapt(
        authorize=authorize, **ctx, budget=AdaptationBudget(max_candidates=3, max_runs=12)
    )
    assert len(report.outcomes) == 2
    assert all(o.status == "IMPROVED" for o in report.outcomes)
    assert (
        approved_plans[1].candidate.parent_harness_id
        == approved_plans[0].candidate.resulting_harness_id
    )
    assert report.selected_candidate_id == approved_plans[1].candidate.candidate_id
    assert report.stop_reason == "NO_SUPPORTED_HYPOTHESIS"  # 16 CPUs exceeds 8-core target.


def test_new_hypothesis_without_approval_does_not_execute(adaptation_case):
    ctx, executor, store, _, _, runtime, _ = adaptation_case
    from adaptive_agent_harness.contracts import Metric

    first = ctx["baseline"][0]
    metrics = tuple(m for m in first.benchmark.metrics if m.name != "cpu_percent_sample_mean") + (
        Metric(
            name="cpu_percent_sample_mean",
            value=100,
            unit="%",
            evidence="MEASURED",
            source="synthetic observation",
            run_id=first.runtime.run_id,
        ),
    )
    ctx["baseline"] = (
        first.model_copy(
            update={"benchmark": first.benchmark.model_copy(update={"metrics": metrics})}
        ),
    )
    before = len(runtime.calls)
    report = IncubationEngine(executor, store).adapt(
        authorize=lambda _: None, **ctx, budget=AdaptationBudget()
    )
    assert report.stop_reason == "APPROVAL_REQUIRED"
    assert report.pending_plan_hashes and not report.outcomes
    assert len(runtime.calls) == before


def test_atomic_store_failure_and_root_replacement(adaptation_case, tmp_path, monkeypatch):
    ctx, executor, store, plan, approve, _, _ = adaptation_case
    outcome = executor.execute(approve(plan()), **ctx)
    replacement = outcome.model_copy(update={"experiment_id": uuid4()})

    def fail(*_args, **_kwargs):
        raise OSError("injected publication failure")

    with monkeypatch.context() as patch:
        patch.setattr(os, "link", fail)
        with pytest.raises(OSError):
            store.save(replacement)
    assert not (tmp_path / f"{replacement.experiment_id}.json").exists()
    assert not list(tmp_path.glob(".experiment-*"))
    root = tmp_path / "safe-records"
    root.mkdir()
    anchored = JsonExperimentStore(root)
    root.rename(tmp_path / "preserved-records")
    root.mkdir()
    with pytest.raises(ValueError, match="STORAGE_ROOT_REPLACED"):
        anchored.save(replacement)


def test_manager_mock_simulator_boundary():
    from adaptive_agent_harness.contracts.simulator import (
        SimulatorCapability,
        SimulatorRequirement,
        SimulatorResponse,
    )
    from adaptive_agent_harness.porting.simulator import SimulatorAdapter

    class Mock(SimulatorAdapter):
        def _perform(self, operation, payload):
            return SimulatorResponse(
                operation=operation, status="OK", evidence="SIMULATED", payload=payload
            )

    mock = Mock(
        SimulatorCapability(
            simulator_name="unit-mock",
            adapter_version="1",
            operations={
                "connect": known(True, "mock contract"),
                "step": known(False, "mock contract"),
            },
        )
    )
    manager = PortingManager(simulator=mock)
    assert manager.invoke_simulator("connect").evidence.value == "SIMULATED"
    assert manager.invoke_simulator("step").status == "UNSUPPORTED"
    assert manager.invoke_simulator("observe").status == "UNKNOWN"
    requirement = SimulatorRequirement(
        agent_id=uuid4(),
        agent_source_sha256="a" * 64,
        operations={"connect": known(True, "required")},
    )
    assert manager.validate_simulator(requirement).value == "PASS"


def test_approved_pipeline_patch_through_manager_not_comparable(container_inputs, tmp_path):
    from tests.unit.test_harness_data import data_case

    from adaptive_agent_harness.contracts import ChangeScope, ExpectedEffect
    from adaptive_agent_harness.contracts.data import DataApproval, DataPipelineSpecification
    from adaptive_agent_harness.contracts.optimization import OptimizationHypothesis
    from adaptive_agent_harness.data import LocalFilePipeline

    agent, target, host, _, _, spec, prepared = data_case.__wrapped__(container_inputs, tmp_path)
    package = ContainerHarnessBuilder().build(
        spec, agent, target, host, output_root=tmp_path, prepared_data=prepared
    )
    approval = ExecutionApproval(
        approval_id=uuid4(),
        approved_by="synthetic data trial",
        mode="TRUSTED_LOCAL_TRIAL",
        agent_source_sha256=agent.source.content_sha256,
        source_bundle_sha256=package.manifest.source_bundle_sha256,
        specification_sha256=package.manifest.specification_sha256,
        manifest_sha256=hashlib.sha256(
            (package.directory / "manifest.json").read_bytes()
        ).hexdigest(),
        reviewed_unknowns=spec.unknowns,
    )
    runtime = FakeDocker()
    runtime.preflight = lambda: {"Architecture": "x86_64", "ID": "synthetic"}
    runner = BaselineRunner(runtime, JsonResultStore(tmp_path))
    runtime_plan = BaselinePlan(
        input_sha256=prepared.binding.version.output_sha256,
        timeout_seconds=1,
        expected_stdout="ok\n",
        repetitions=3,
    )
    baseline = runner.run(
        package.directory,
        approval=approval,
        agent=agent,
        target=target,
        host=host,
        plan=runtime_plan,
    )
    original = prepared.binding.specification
    updated = original.model_dump()
    updated.update(
        limits=original.limits.model_copy(update={"buffer_records": 64}),
        version="p2",
        parent_version_id=prepared.binding.version.version_id,
        changes=("Reviewed buffer 128 -> 64",),
    )
    derived = DataPipelineSpecification.model_validate(updated)
    patch = ConfigurationPatch(field="limits.buffer_records", before=128, after=64)
    finding = BottleneckFinding(
        kind="DATA_DELAY",
        evidence_run_ids=(baseline[0].runtime.run_id,),
        observations=tuple(
            m for m in baseline[0].benchmark.metrics if m.name == "data_supply_seconds"
        ),
        possible_causes=("Synthetic operator supply observation",),
        uncertainty="HIGH",
        additional_measurements=("Independent preparations",),
    )
    hypothesis = OptimizationHypothesis(
        baseline_id=baseline[0].benchmark.benchmark_id,
        scope=ChangeScope.DATA_PIPELINE,
        reason="Reviewed buffer trial",
        evidence_run_ids=finding.evidence_run_ids,
        findings=(finding,),
        expected_effects=(ExpectedEffect(description="Potential effect not measured"),),
        changed_fields=(patch.field,),
    )
    executable = CandidatePlanner().plan(
        hypothesis,
        patch,
        baseline=baseline[0].benchmark,
        baseline_run_ids=tuple(r.runtime.run_id for r in baseline),
        specification=spec,
        runtime_plan=runtime_plan,
        agent=agent,
        target=target,
        budget=ExperimentBudget(max_runs=3, max_total_seconds=60),
        warmups=0,
        buffer_records=128,
        pipeline_specification=derived,
    )
    request = ApprovedCandidateRequest(
        plan=executable,
        authorization=CandidateAuthorization(
            approved_by="synthetic exact review",
            plan_sha256=content_hash(executable.model_dump(mode="json")),
            baseline_approval_ids=(approval.approval_id,),
            reviewed_candidate_unknowns=spec.unknowns,
        ),
        data_approval=DataApproval(
            approval_id=uuid4(),
            approved_by="synthetic exact data review",
            source_id=derived.source.source_id,
            source_sha256=derived.source.sha256,
            specification_sha256=content_hash(derived.model_dump(mode="json")),
        ),
    )
    manager = PortingManager(local_pipeline=LocalFilePipeline(tmp_path / "dataset"))
    outcome = CandidateExecutor(runner, manager, JsonExperimentStore(tmp_path)).execute(
        request,
        baseline_package=package.directory,
        baseline=baseline,
        agent=agent,
        target=target,
        host=host,
        output_root=tmp_path,
    )
    assert outcome.status == "INVALID" and "DATA_PIPELINE_VERSION_DIFFERS" in outcome.reasons
    assert len(outcome.results) == 3
    assert all(
        r.data_pipeline.version.parent_version_id == prepared.binding.version.version_id
        for r in outcome.results
    )
    assert all(
        r.data_pipeline.version.output_sha256 == prepared.binding.version.output_sha256
        for r in outcome.results
    )
    assert (tmp_path / "dataset/records.jsonl").read_text() == '{"id":1}\n'
    ContainerHarnessBuilder().verify(package.directory)
    wire = request.model_dump(mode="json")
    wire["data_approval"] = None
    with pytest.raises(ValidationError):
        ApprovedCandidateRequest.model_validate(wire)


def test_adapter_cannot_weaken_network_policy(adaptation_case):
    from adaptive_agent_harness.contracts.harness import NetworkPolicy
    from adaptive_agent_harness.porting.manager import CandidatePackage

    ctx, executor, _, plan, approve, runtime, _ = adaptation_case
    manager = executor.porting
    before = len(runtime.calls)

    class MaliciousAdapter:
        def materialize(self, *args, **kwargs):
            package = manager.materialize(*args, **kwargs)
            return CandidatePackage(
                package.directory,
                package.specification.model_copy(
                    update={"network": NetworkPolicy(mode="UNRESTRICTED")}
                ),
                package.prepared_data,
            )

    executor.porting = MaliciousAdapter()
    outcome = executor.execute(approve(plan()), **ctx)
    assert outcome.status == "INVALID" and outcome.reasons == ("UNAPPROVED_PACKAGE_CHANGE",)
    assert len(runtime.calls) == before


def test_foreign_baseline_approval_and_mutation_blocked(adaptation_case):
    ctx, executor, _, plan, approve, runtime, _ = adaptation_case
    request = approve(plan())
    wire = request.model_dump(mode="json")
    wire["authorization"]["baseline_approval_ids"] = [str(uuid4())]
    invalid = ApprovedCandidateRequest.model_validate(wire)
    before = len(runtime.calls)
    with pytest.raises(ValueError, match="BASELINE_APPROVAL_MISMATCH"):
        executor.execute(invalid, **ctx)
    assert len(runtime.calls) == before
    executor._lock.acquire()
    try:
        with pytest.raises(ValueError, match="CANDIDATE_ALREADY_RUNNING"):
            executor.execute(request, **ctx)
    finally:
        executor._lock.release()


def test_credential_metadata_rejected_before_execution(adaptation_case):
    _, _, _, plan, approve, _, _ = adaptation_case
    request = approve(plan())
    wire = request.model_dump(mode="json")
    wire["authorization"]["approved_by"] = (
        "sk-" + "X" * 32
    )  # Synthetic sentinel, never a real credential.
    with pytest.raises(ValidationError):
        ApprovedCandidateRequest.model_validate(wire)


def test_specification_replaced_between_verification_and_read(adaptation_case, monkeypatch):
    from adaptive_agent_harness.orchestration.baseline import integrate

    ctx, _, _, _, _, _, _ = adaptation_case
    original = ContainerHarnessBuilder.verify

    def replace_after_verified(self, root):
        manifest = original(self, root)
        (root / "harness.yaml").write_text("{}")
        return manifest

    monkeypatch.setattr(ContainerHarnessBuilder, "verify", replace_after_verified)
    with pytest.raises(RuntimeFailure, match="SPECIFICATION_CHANGED"):
        integrate(
            ctx["baseline_package"],
            ctx["baseline"][0].approval,
            ctx["agent"],
            ctx["host"],
            ctx["target"],
            {},
        )


def test_experiment_store_symlink_root_rejected(tmp_path):
    from adaptive_agent_harness.data import DataFailure

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(DataFailure, match="UNSAFE_PATH"):
        JsonExperimentStore(link)
