from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from adaptive_agent_harness.contracts import (
    AdaptationCandidate,
    AgentProfile,
    AgentSource,
    BenchmarkResult,
    CandidateDecision,
    ChangeScope,
    CodeChangeEvidence,
    CompatibilityCheck,
    CompatibilityReport,
    CompatibilityVerdict,
    EvidenceKind,
    ExecutionMode,
    ExpectedEffect,
    Fact,
    FunctionalValidation,
    HarnessSpecification,
    HostProfile,
    IncubationIteration,
    IncubationSession,
    IncubationStep,
    Knowledge,
    OperationalRequirements,
    ResourceLimits,
    TargetProfile,
    known,
)


@pytest.mark.parametrize("kind", [EvidenceKind.SIMULATED, EvidenceKind.ESTIMATED])
def test_inferred_execution_support_is_rejected(target, kind):
    with pytest.raises(ValidationError):
        changed(target.deployment, python=known(True, "hypothesis", kind))
    with pytest.raises(ValidationError):
        changed(target.runtimes[0], version=known("3.12", "hypothesis", kind))


def test_docker_support_requires_actual_prerequisite_information(target):
    with pytest.raises(ValidationError):
        changed(target.deployment, docker=known(True, "declaration without prerequisites"))


def test_session_rejects_unrelated_host_and_cyclic_versions(agent, host, target, agent_version):
    limited = TargetProfile.simulate_resources(host, ResourceLimits(cpu_cores=1), "limited")
    with pytest.raises(ValidationError):
        IncubationSession(
            agent=agent,
            host=changed(host, host_id=uuid4()),
            target=limited,
            agent_versions=(agent_version,),
        )
    cyclic = changed(agent_version, parent_version_id=agent_version.version_id)
    with pytest.raises(ValidationError):
        IncubationSession(agent=agent, host=host, target=target, agent_versions=(cyclic,))


def changed(model, **updates):
    return type(model).model_validate({**model.model_dump(), **updates})


def candidate(agent, agent_version, benchmark, **updates):
    data = dict(
        agent_id=agent.agent_id,
        agent_version=agent_version,
        baseline_agent_sha256=agent.source.content_sha256,
        parent_harness_id=uuid4(),
        resulting_harness_id=benchmark.harness_id,
        scope=ChangeScope.HARNESS,
        changed_fields=("cpu_limit",),
        change_sha256="e" * 64,
        reason="Observed resource pressure",
        evidence_run_ids=(uuid4(),),
        expected_effects=(ExpectedEffect(description="May reduce latency"),),
        validation=benchmark,
    )
    return AdaptationCandidate(**{**data, **updates})


def test_all_nine_models_json_roundtrip(agent, host, target, agent_version, benchmark):
    report = CompatibilityReport(
        agent_id=agent.agent_id,
        target_id=target.target_id,
        verdict=CompatibilityVerdict.UNKNOWN,
        checks=(
            CompatibilityCheck(
                area="gpu", verdict=CompatibilityVerdict.UNKNOWN, reason="Not observed"
            ),
        ),
    )
    harness = HarnessSpecification(
        agent_id=agent.agent_id,
        agent_source_sha256=agent.source.content_sha256,
        target_id=target.target_id,
        version="baseline",
        execution_mode=ExecutionMode.PYTHON,
        argv=("python", "app.py"),
    )
    adaptation = candidate(agent, agent_version, benchmark)
    session = IncubationSession(
        agent=agent, host=host, target=target, agent_versions=(agent_version,)
    )
    models = (
        agent,
        target,
        host,
        agent.requirements,
        harness,
        report,
        benchmark,
        adaptation,
        session,
    )
    for model in models:
        assert type(model).model_validate_json(model.model_dump_json()) == model
        assert type(model).model_json_schema()["additionalProperties"] is False
        with pytest.raises(ValidationError):
            type(model).model_validate({**model.model_dump(), "unsupported_field": True})


@pytest.mark.parametrize(
    "model",
    [
        AgentProfile,
        TargetProfile,
        HostProfile,
        HarnessSpecification,
        CompatibilityReport,
        BenchmarkResult,
        AdaptationCandidate,
        IncubationSession,
    ],
)
def test_missing_required_fields(model):
    with pytest.raises(ValidationError):
        model.model_validate({})


def test_unknown_is_not_zero_or_false():
    fact = Fact[int]()
    assert fact.status == Knowledge.UNKNOWN and fact.value is None
    assert '"value":null' in fact.model_dump_json()
    for data in ({"value": 0}, {"status": "KNOWN"}, {"status": "UNKNOWN", "evidence": "MEASURED"}):
        with pytest.raises(ValidationError):
            Fact[int].model_validate(data)
    assert Fact[int].model_validate(known(0, "counter").model_dump()).value == 0
    assert OperationalRequirements().gpu_required.status == Knowledge.UNKNOWN


@pytest.mark.parametrize(
    "limits",
    [
        {},
        {"cpu_cores": 0},
        {"cpu_cores": -1},
        {"cpu_cores": True},
        {"cpu_cores": float("inf")},
        {"ram_bytes": -1},
        {"ram_bytes": True},
        {"ram_bytes": "1024"},
    ],
)
def test_invalid_resource_limits(limits):
    with pytest.raises(ValidationError):
        ResourceLimits(**limits)


def test_timezone_and_schema_version(agent):
    local_time = datetime(2026, 10, 14, 9, tzinfo=timezone(timedelta(hours=9)))
    result = changed(agent, created_at=local_time)
    assert result.created_at == datetime(2026, 10, 14, tzinfo=UTC)
    for data in ({"created_at": datetime(2026, 10, 14)}, {"schema_version": "2.0"}):
        with pytest.raises(ValidationError):
            changed(agent, **data)


def test_physical_host_and_simulated_target_are_separate(host, target):
    limited = TargetProfile.simulate_resources(
        host, ResourceLimits(cpu_cores=0.5, ram_bytes=512), "limited"
    )
    assert limited.hardware.resources.cpu_cores.value == 8
    assert host.hardware.resources.cpu_cores.value == 8
    assert limited.simulated_limits.cpu_cores == 0.5
    assert limited.based_on_host_id == host.host_id
    assert limited.hardware is not host.hardware
    assert target.simulated_limits is None
    for updates in ({"simulated_limits": {"cpu_cores": 1}}, {"kind": "RESOURCE_SIMULATION"}):
        with pytest.raises(ValidationError):
            changed(target, **updates)
    data = host.model_dump()
    data["hardware"]["resources"]["cpu_cores"]["evidence"] = "DECLARED"
    with pytest.raises(ValidationError):
        HostProfile.model_validate(data)
    data["hardware"]["resources"]["cpu_cores"]["evidence"] = "SIMULATED"
    with pytest.raises(ValidationError):
        TargetProfile(name="pretend", hardware=data["hardware"])


def test_python_specifier_and_gpu_requirements():
    with pytest.raises(ValidationError):
        OperationalRequirements(python_specifier=known("Python latest", "input"))
    with pytest.raises(ValidationError):
        OperationalRequirements(
            cuda_required=known(True, "input"), gpu_required=known(False, "input")
        )


def test_unmeasured_and_unexecuted_metrics(benchmark):
    metric = benchmark.metrics[0]
    assert changed(metric, value=None, evidence=EvidenceKind.UNVERIFIED).value is None
    for data in ({"value": None}, {"value": float("nan")}, {"run_id": uuid4()}):
        with pytest.raises(ValidationError):
            if "run_id" in data:
                changed(benchmark, metrics=(changed(metric, **data),))
            else:
                changed(metric, **data)
    with pytest.raises(ValidationError):
        changed(benchmark, execution_evidence=EvidenceKind.ESTIMATED)
    with pytest.raises(ValidationError):
        ExpectedEffect(description="prediction", evidence=EvidenceKind.MEASURED)


@pytest.mark.parametrize("status,degraded", [("FAIL", False), ("UNKNOWN", None), ("FAIL", True)])
def test_quality_failure_or_unknown_never_accepted(
    agent, agent_version, benchmark, status, degraded
):
    quality = changed(
        benchmark.functional_validation,
        status=status,
        quality_degraded=Fact()
        if degraded is None
        else known(degraded, "test", EvidenceKind.MEASURED),
    )
    invalid = changed(benchmark, functional_validation=quality)
    with pytest.raises(ValidationError):
        candidate(agent, agent_version, invalid, decision=CandidateDecision.ACCEPTED)
    with pytest.raises(ValidationError):
        FunctionalValidation(
            status="PASS",
            test_result_references=("report",),
            quality_degraded=known(True, "test", EvidenceKind.MEASURED),
        )


def test_acceptance_requires_real_execution_and_metrics(agent, agent_version, benchmark):
    accepted = candidate(agent, agent_version, benchmark, decision="ACCEPTED")
    assert accepted.expected_effects[0].evidence == EvidenceKind.ESTIMATED
    assert accepted.validation.execution_evidence == EvidenceKind.MEASURED
    for updates in (
        {"metrics": ()},
        {"conditions": changed(benchmark.conditions, warmup=True)},
        {
            "functional_validation": changed(
                benchmark.functional_validation, quality_degraded=known(False, "estimate")
            )
        },
    ):
        with pytest.raises(ValidationError):
            candidate(agent, agent_version, changed(benchmark, **updates), decision="ACCEPTED")


def test_harness_preserves_baseline_and_code_change_requires_approval(
    agent, agent_version, benchmark
):
    modified = changed(agent_version, content_sha256="f" * 64)
    result = changed(benchmark, agent_source_sha256=modified.content_sha256)
    with pytest.raises(ValidationError):
        candidate(agent, modified, result)
    with pytest.raises(ValidationError):
        candidate(agent, modified, result, scope="AGENT_CODE", decision="ACCEPTED")
    evidence = CodeChangeEvidence(
        approval_id=uuid4(),
        approved_original_sha256=agent.source.content_sha256,
        approved_change_sha256="e" * 64,
        before_test_references=("before",),
        after_test_references=("after",),
        rollback_test_references=("rollback",),
        rollback_source_sha256=agent.source.content_sha256,
    )
    approved = candidate(
        agent,
        modified,
        result,
        scope="AGENT_CODE",
        decision="ACCEPTED",
        code_change_evidence=evidence,
    )
    assert approved.agent_version.original_sha256 == agent.source.content_sha256
    # Later Harness trials may preserve an already approved Agent version.
    assert (
        candidate(agent, modified, result, baseline_agent_sha256="f" * 64).scope
        == ChangeScope.HARNESS
    )
    with pytest.raises(ValidationError):
        changed(approved, code_change_evidence=changed(evidence, rollback_source_sha256="0" * 64))


def test_session_records_multiple_ordered_iterations(agent, host, target, agent_version, benchmark):
    baseline_run = uuid4()
    baseline = changed(
        benchmark,
        run_id=baseline_run,
        harness_id=uuid4(),
        metrics=tuple(changed(m, run_id=baseline_run) for m in benchmark.metrics),
    )
    adaptation = candidate(
        agent,
        agent_version,
        benchmark,
        parent_harness_id=baseline.harness_id,
        evidence_run_ids=(baseline.run_id,),
    )
    steps = (
        IncubationStep(phase="BASELINE", benchmark=baseline),
        IncubationStep(phase="DIAGNOSE", diagnosis="Measured memory pressure"),
        IncubationStep(phase="ADAPT", candidate=adaptation),
        IncubationStep(phase="VALIDATE", benchmark=benchmark),
    )
    iterations = (
        IncubationIteration(number=1, steps=steps),
        IncubationIteration(number=2, steps=steps),
    )
    session = IncubationSession(
        agent=agent,
        host=host,
        target=target,
        agent_versions=(agent_version,),
        iterations=iterations,
    )
    assert IncubationSession.model_validate_json(session.model_dump_json()) == session
    with pytest.raises(ValidationError):
        IncubationIteration(number=1, steps=(steps[1], steps[0]))
    with pytest.raises(ValidationError):
        changed(session, iterations=(iterations[1],))
    with pytest.raises(ValidationError):
        changed(session, agent_versions=(changed(agent_version, original_sha256="f" * 64),))
    with pytest.raises(ValidationError):
        changed(session, target=changed(target, name="another environment"))


def test_compatibility_cannot_hide_unknown_or_failure(agent, target):
    with pytest.raises(ValidationError):
        CompatibilityReport(
            agent_id=agent.agent_id,
            target_id=target.target_id,
            verdict="FIT",
            checks=(CompatibilityCheck(area="gpu", verdict="UNKNOWN", reason="Missing"),),
        )


def test_accepted_iteration_requires_comparable_distinct_execution(agent, agent_version, benchmark):
    run_id = uuid4()
    baseline = changed(
        benchmark,
        run_id=run_id,
        benchmark_id=uuid4(),
        harness_id=uuid4(),
        metrics=tuple(changed(m, run_id=run_id) for m in benchmark.metrics),
    )
    validation = changed(
        benchmark,
        functional_validation=changed(
            benchmark.functional_validation, compared_to_benchmark_id=baseline.benchmark_id
        ),
    )
    adaptation = candidate(
        agent,
        agent_version,
        validation,
        parent_harness_id=baseline.harness_id,
        evidence_run_ids=(baseline.run_id,),
        decision="ACCEPTED",
    )
    steps = (
        IncubationStep(phase="BASELINE", benchmark=baseline),
        IncubationStep(phase="DIAGNOSE", diagnosis="Observed pressure"),
        IncubationStep(phase="ADAPT", candidate=adaptation),
        IncubationStep(phase="VALIDATE", benchmark=validation),
    )
    assert len(IncubationIteration(number=1, steps=steps).steps) == 4
    with pytest.raises(ValidationError):
        changed(adaptation, resulting_harness_id=adaptation.parent_harness_id)
    # A different evaluator/input/seed must not masquerade as a comparable trial.
    for conditions in (
        changed(validation.conditions, seed=123),
        changed(validation.conditions, input_sha256="f" * 64),
    ):
        other = changed(validation, conditions=conditions)
        other_candidate = changed(adaptation, validation=other)
        with pytest.raises(ValidationError):
            IncubationIteration(
                number=1,
                steps=(
                    *steps[:2],
                    IncubationStep(phase="ADAPT", candidate=other_candidate),
                    IncubationStep(phase="VALIDATE", benchmark=other),
                ),
            )


def test_source_paths_cannot_escape():
    for path in ("../app.py", "/app.py", "folder\\app.py"):
        with pytest.raises(ValidationError):
            AgentSource(root="/fixture", content_sha256="a" * 64, file_hashes={path: "b" * 64})
