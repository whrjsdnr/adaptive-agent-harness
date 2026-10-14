from uuid import uuid4

import pytest
from pydantic import ValidationError

from adaptive_agent_harness.benchmark.bottleneck import BottleneckAnalyzer
from adaptive_agent_harness.benchmark.comparison import BenchmarkComparisonEngine
from adaptive_agent_harness.contracts import (
    AdaptationCandidate,
    AgentVersion,
    BenchmarkResult,
    CandidateDecision,
    ChangeScope,
    EvidenceKind,
    FunctionalValidation,
    Metric,
    known,
)
from adaptive_agent_harness.contracts.optimization import (
    CandidateExecutionRequest,
    CandidateExecutionResult,
    ComparisonPolicy,
    ExperimentBudget,
    ExperimentPlan,
)
from adaptive_agent_harness.contracts.runtime import RuntimeRecord


@pytest.fixture
def groups(benchmark):
    """Synthetic contract inputs only; never used as empirical acceptance artifacts."""

    def make(values=(10, 10, 10), candidate_values=(9, 9, 9)):
        base_h, cand_h = uuid4(), uuid4()

        def group(values, harness, compared=None):
            result = []
            for index, value in enumerate(values, 1):
                run = uuid4()
                r = BenchmarkResult.model_validate(
                    {
                        **benchmark.model_dump(),
                        "benchmark_id": uuid4(),
                        "run_id": run,
                        "harness_id": harness,
                        "harness_sha256": ("b" if compared is None else "c") * 64,
                        "conditions": {
                            **benchmark.conditions.model_dump(),
                            "repetition": index,
                            "environment_sha256": "f" * 64,
                        },
                        "metrics": (
                            Metric(
                                name="wall_clock",
                                value=value,
                                unit="s",
                                evidence=EvidenceKind.MEASURED,
                                source="synthetic contract input",
                                run_id=run,
                            ),
                        ),
                        "functional_validation": FunctionalValidation(
                            status="PASS",
                            test_result_references=("synthetic:test",),
                            quality_degraded=known(
                                False, "synthetic quality input", EvidenceKind.MEASURED
                            ),
                            compared_to_benchmark_id=compared,
                        ),
                    }
                )
                result.append(r)
            return result

        baseline = group(values, base_h)
        candidate = group(candidate_values, cand_h, baseline[0].benchmark_id)
        return baseline, candidate

    return make


def test_stable_comparable_measured_inputs(groups):
    base, candidate = groups()
    result = BenchmarkComparisonEngine().compare(base, candidate)
    assert result.comparable
    assert result.decision == "ACCEPTED"
    assert result.improvement_fraction.value == pytest.approx(0.1)
    stats = result.baseline_statistics["wall_clock"]
    assert (
        stats.count,
        stats.mean,
        stats.median,
        stats.minimum,
        stats.maximum,
        stats.standard_deviation,
    ) == (3, 10, 10, 10, 10, 0)
    assert result.candidate_success_rate.value == 1
    assert set(result.evidence_run_ids) == {r.run_id for r in base + candidate}
    assert result.baseline_harnesses != result.candidate_harnesses
    assert type(result).model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "change",
    [
        "agent",
        "data",
        "evaluator",
        "target",
        "host",
        "seed",
        "method",
        "unmeasured",
        "harness_mix",
        "duplicate",
    ],
)
def test_incomparable_inputs_never_accepted(groups, change):
    base, candidate = groups()
    r = candidate[0]
    updates = {}
    if change == "agent":
        updates["agent_source_sha256"] = "d" * 64
    elif change == "data":
        updates["conditions"] = r.conditions.model_copy(update={"input_sha256": "d" * 64})
    elif change == "evaluator":
        updates["conditions"] = r.conditions.model_copy(update={"evaluator_sha256": "e" * 64})
    elif change == "seed":
        updates["conditions"] = r.conditions.model_copy(update={"seed": 3})
    elif change == "target":
        updates["target"] = r.target.model_copy(update={"target_id": uuid4()})
    elif change == "host":
        updates["host"] = r.host.model_copy(update={"host_id": uuid4()})
    elif change == "method":
        updates["metrics"] = (r.metrics[0].model_copy(update={"source": "different method"}),)
    elif change == "unmeasured":
        updates.update(execution_evidence=EvidenceKind.SIMULATED, metrics=())
    elif change == "harness_mix":
        updates["harness_id"] = uuid4()
    elif change == "duplicate":
        candidate[0] = base[0]
    if updates:
        candidate[0] = BenchmarkResult.model_validate({**r.model_dump(), **updates})
    result = BenchmarkComparisonEngine().compare(base, candidate)
    assert result.decision != "ACCEPTED"
    if change != "method":
        assert not result.comparable


@pytest.mark.parametrize(
    "values,candidates,reason",
    [
        ((10, 10), (9, 9), "INSUFFICIENT"),
        ((1, 10, 20), (1, 2, 3), "UNSTABLE"),
        ((0, 0, 0), (0, 0, 0), "ZERO_BASELINE"),
        ((10, 10, 10), (9.8, 9.8, 9.8), "MINIMUM"),
    ],
)
def test_uncertainty_and_no_improvement(groups, values, candidates, reason):
    base, candidate = groups(values, candidates)
    result = BenchmarkComparisonEngine().compare(base, candidate)
    assert result.decision != "ACCEPTED"
    assert any(reason in r for r in result.reasons)


def test_quality_unknown_and_degraded_block_improvement(groups):
    base, candidate = groups()
    unknown = [
        r.model_copy(
            update={
                "functional_validation": FunctionalValidation(
                    status="PASS", test_result_references=("test",)
                )
            }
        )
        for r in candidate
    ]
    result = BenchmarkComparisonEngine().compare(base, unknown)
    assert result.decision == "INCONCLUSIVE"
    degraded = [
        r.model_copy(
            update={
                "functional_validation": FunctionalValidation(
                    status="FAIL",
                    test_result_references=("test",),
                    quality_degraded=known(True, "quality evaluator", EvidenceKind.MEASURED),
                )
            }
        )
        for r in candidate
    ]
    result = BenchmarkComparisonEngine().compare(base, degraded)
    assert result.decision == "REJECTED"
    assert result.candidate_success_rate.value == 0


def test_warmups_excluded_and_no_duplicate_data_measurements(groups):
    base, candidate = groups()
    warmup = base[0].model_copy(
        update={"conditions": base[0].conditions.model_copy(update={"warmup": True})}
    )
    result = BenchmarkComparisonEngine().compare([warmup, *base], candidate)
    assert result.decision == "ACCEPTED"
    assert result.baseline_statistics["wall_clock"].count == 3
    prep_b, prep_c = uuid4(), uuid4()

    def data_group(items, prep):
        return [
            r.model_copy(
                update={
                    "conditions": r.conditions.model_copy(update={"data_preparation_run_id": prep}),
                    "metrics": (
                        Metric(
                            name="data_supply_seconds",
                            value=r.metrics[0].value,
                            unit="s",
                            source="one batch",
                            evidence=EvidenceKind.MEASURED,
                            run_id=r.run_id,
                        ),
                    ),
                }
            )
            for r in items
        ]

    result = BenchmarkComparisonEngine().compare(
        data_group(base, prep_b),
        data_group(candidate, prep_c),
        policy=ComparisonPolicy(primary_metric="data_supply_seconds"),
    )
    assert result.baseline_statistics["data_supply_seconds"].count == 1
    assert result.decision == "INCONCLUSIVE"


def test_missing_metric_and_no_runs_are_unknown(groups):
    base, candidate = groups()
    empty = [r.model_copy(update={"metrics": ()}) for r in candidate]
    assert BenchmarkComparisonEngine().compare(base, empty).decision == "INCONCLUSIVE"
    assert BenchmarkComparisonEngine().compare([], []).decision == "INCONCLUSIVE"


def test_bottleneck_evidence_and_plans_do_not_execute(groups):
    base, _ = groups()
    r = base[0]
    observed = r.model_copy(
        update={
            "metrics": r.metrics
            + (
                Metric(
                    name="cpu_percent_sample_mean",
                    value=150,
                    unit="%",
                    source="synthetic diagnostic input",
                    evidence=EvidenceKind.MEASURED,
                    run_id=r.run_id,
                ),
            )
        }
    )
    analyzer = BottleneckAnalyzer()
    findings = analyzer.analyze([observed], max_latency_seconds=5)
    assert {f.kind for f in findings} == {"CPU_PRESSURE", "LATENCY"}
    assert all(f.uncertainty == "HIGH" and f.additional_measurements for f in findings)
    h = analyzer.hypotheses(observed, findings)[0]
    assert h.scope == "HARNESS"
    assert all(e.estimated_change is None and e.evidence == "ESTIMATED" for e in h.expected_effects)
    plan = ExperimentPlan(
        hypothesis=h,
        baseline=observed,
        baseline_run_ids=(r.run_id,),
        comparison_policy=ComparisonPolicy(),
        budget=ExperimentBudget(max_runs=4, max_total_seconds=120),
        functional_criteria=("exact expected output",),
        quality_criteria=("no degraded quality",),
    )
    candidate = AdaptationCandidate(
        agent_id=r.agent_id,
        agent_version=AgentVersion(
            content_sha256=r.agent_source_sha256, original_sha256=r.agent_source_sha256
        ),
        baseline_agent_sha256=r.agent_source_sha256,
        parent_harness_id=r.harness_id,
        resulting_harness_id=uuid4(),
        scope=h.scope,
        changed_fields=h.changed_fields,
        change_sha256="e" * 64,
        reason=h.reason,
        evidence_run_ids=h.evidence_run_ids,
        expected_effects=h.expected_effects,
    )
    request = CandidateExecutionRequest(candidate=candidate, plan=plan)
    assert request.execution_authorized is False and plan.apply_changes is False
    assert CandidateExecutionRequest.model_validate_json(request.model_dump_json()) == request
    result = CandidateExecutionResult(
        candidate_id=candidate.candidate_id, plan_id=plan.plan_id, reasons=("not executed",)
    )
    assert result.decision == CandidateDecision.UNVALIDATED
    with pytest.raises(ValidationError):
        CandidateExecutionRequest.model_validate(
            {**request.model_dump(), "execution_authorized": True}
        )
    with pytest.raises(ValidationError):
        ExperimentPlan.model_validate(
            {**plan.model_dump(), "budget": {"max_runs": 1, "max_total_seconds": 5}}
        )
    with pytest.raises(ValidationError):
        type(h).model_validate({**h.model_dump(), "scope": "AGENT_CODE"})


@pytest.mark.parametrize(
    "error,kind", [("OOM", "OOM"), ("TIMEOUT", "TIMEOUT"), ("NONZERO_EXIT", "NONZERO_EXIT")]
)
def test_failure_causes_are_observed_not_guessed(groups, error, kind):
    base, _ = groups()
    r = base[0]
    runtime = RuntimeRecord(
        run_id=r.run_id,
        approval_id=uuid4(),
        harness_version="1",
        source_bundle_sha256="a" * 64,
        manifest_sha256="b" * 64,
        execution_started=True,
        error_code=error,
        timed_out=error == "TIMEOUT",
        oom_killed=known(error == "OOM", "synthetic inspect state", EvidenceKind.MEASURED),
        exit_code=known(
            137 if error != "NONZERO_EXIT" else 7, "synthetic state", EvidenceKind.MEASURED
        ),
    )
    findings = BottleneckAnalyzer().analyze([r], runtimes=[runtime])
    assert findings[0].kind == kind
    assert findings[0].evidence_run_ids == (r.run_id,)


def test_unmeasured_and_unknown_diagnosis(groups):
    base, _ = groups()
    assert BottleneckAnalyzer().analyze(base)[0].kind == "UNKNOWN"
    simulated = [
        r.model_copy(update={"execution_evidence": EvidenceKind.SIMULATED, "metrics": ()})
        for r in base
    ]
    assert BottleneckAnalyzer().analyze(simulated) == ()


def test_unknown_environment_and_data_source_block_comparison(groups):
    base, candidate = groups()
    changed = [
        r.model_copy(
            update={"conditions": r.conditions.model_copy(update={"environment_sha256": None})}
        )
        for r in candidate
    ]
    assert not BenchmarkComparisonEngine().compare(base, changed).comparable


def test_variability_requires_comparable_repetitions(groups):
    base, candidate = groups((1, 10, 20), (1, 10, 20))
    findings = BottleneckAnalyzer().analyze(base)
    assert any(f.kind == "VARIABILITY" for f in findings)
    mixed = [r.model_copy(update={"agent_id": uuid4()}) for r in base + candidate]
    assert all(f.kind != "VARIABILITY" for f in BottleneckAnalyzer().analyze(mixed))
    changed = [
        r.model_copy(
            update={
                "conditions": r.conditions.model_copy(
                    update={"data_fingerprints": {"source": "e" * 64}}
                )
            }
        )
        for r in candidate
    ]
    assert not BenchmarkComparisonEngine().compare(base, changed).comparable


def test_throughput_direction_and_data_diagnosis(groups):
    base, candidate = groups((100, 100, 100), (120, 120, 120))

    def throughput(items):
        return [
            r.model_copy(
                update={
                    "metrics": (
                        r.metrics[0].model_copy(
                            update={"name": "data_records_per_second", "unit": "records/s"}
                        ),
                    )
                }
            )
            for r in items
        ]

    base, candidate = throughput(base), throughput(candidate)
    result = BenchmarkComparisonEngine().compare(
        base,
        candidate,
        policy=ComparisonPolicy(primary_metric="data_records_per_second", direction="HIGHER"),
    )
    assert result.decision == "ACCEPTED"
    findings = BottleneckAnalyzer().analyze(base, min_throughput=110)
    assert findings[0].kind == "LOW_THROUGHPUT"
    assert BottleneckAnalyzer().hypotheses(base[0], findings)[0].scope == ChangeScope.DATA_PIPELINE
