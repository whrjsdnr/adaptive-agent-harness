"""Pure, conservative comparison of recorded repetitions; no candidate execution."""

import statistics
from collections.abc import Sequence

from adaptive_agent_harness.contracts import (
    BenchmarkResult,
    CandidateDecision,
    EvidenceKind,
    Fact,
    known,
)
from adaptive_agent_harness.contracts.optimization import (
    ComparisonPolicy,
    ExperimentComparison,
    MetricStatistics,
)
from adaptive_agent_harness.harness._utils import content_hash


def signature(result: BenchmarkResult) -> tuple:
    c = result.conditions
    return (
        result.agent_id,
        result.agent_source_sha256,
        c.input_sha256,
        c.evaluator_sha256,
        c.seed,
        c.execution_mode,
        c.environment_sha256,
        content_hash(result.host.model_dump(mode="json")),
        content_hash(result.target.model_dump(mode="json")),
        tuple(sorted(c.data_fingerprints.items())),
    )


def summarize(results: Sequence[BenchmarkResult]) -> dict[str, MetricStatistics]:
    names = (
        set.intersection(*(set(m.name for m in r.metrics) for r in results)) if results else set()
    )
    output = {}
    for name in sorted(names):
        items, seen = [], set()
        for result in results:
            matches = [m for m in result.metrics if m.name == name]
            if len(matches) != 1:
                raise ValueError("Duplicate metric names cannot be compared")
            metric = matches[0]
            if metric.evidence != EvidenceKind.MEASURED or metric.value is None or metric.value < 0:
                items = []
                break
            # One prepared batch reused by three Agent runs is one data measurement, not three.
            identity = (
                result.conditions.data_preparation_run_id
                if name.startswith("data_")
                else result.run_id
            )
            if identity is None:
                identity = result.run_id
            if identity not in seen:
                seen.add(identity)
                items.append(metric)
        if not items or len({(m.unit, m.source) for m in items}) != 1:
            continue
        values = [m.value for m in items]
        mean = statistics.mean(values)
        std = statistics.stdev(values) if len(values) > 1 else 0.0
        output[name] = MetricStatistics(
            count=len(values),
            unit=items[0].unit,
            source=items[0].source,
            mean=mean,
            median=statistics.median(values),
            minimum=min(values),
            maximum=max(values),
            standard_deviation=std,
            coefficient_variation=std / mean if mean else (0.0 if not std else None),
        )
    return output


class BenchmarkComparisonEngine:
    def compare(
        self,
        baseline: Sequence[BenchmarkResult],
        candidate: Sequence[BenchmarkResult],
        *,
        policy: ComparisonPolicy | None = None,
    ) -> ExperimentComparison:
        policy = ComparisonPolicy.model_validate_json(
            (policy or ComparisonPolicy()).model_dump_json()
        )
        baseline = tuple(BenchmarkResult.model_validate_json(r.model_dump_json()) for r in baseline)
        candidate = tuple(
            BenchmarkResult.model_validate_json(r.model_dump_json()) for r in candidate
        )
        baseline = tuple(r for r in baseline if not r.conditions.warmup)
        candidate = tuple(r for r in candidate if not r.conditions.warmup)
        all_results = baseline + candidate
        reasons = []
        comparable = True
        if not baseline or not candidate:
            reasons.append("EMPTY_MEASURED_GROUP")
            comparable = False
        if len({r.run_id for r in all_results}) != len(all_results) or len(
            {r.benchmark_id for r in all_results}
        ) != len(all_results):
            reasons.append("DUPLICATE_EXECUTION_EVIDENCE")
            comparable = False
        if len({signature(r) for r in all_results}) != 1:
            reasons.append("AGENT_DATA_EVALUATOR_TARGET_HOST_OR_METHOD_DIFFERS")
            comparable = False
        for group in (baseline, candidate):
            if len({(r.harness_id, r.harness_sha256) for r in group}) > 1:
                reasons.append("MIXED_HARNESS_VERSIONS_WITHIN_GROUP")
                comparable = False
        if any(r.execution_evidence != EvidenceKind.MEASURED for r in all_results):
            reasons.append("EXECUTION_NOT_MEASURED")
            comparable = False
        if any(r.conditions.environment_sha256 is None for r in all_results):
            reasons.append("ENVIRONMENT_EVIDENCE_UNKNOWN")
            comparable = False
        bs, cs = summarize(baseline), summarize(candidate)
        methods = {}
        for result in all_results:
            for metric in result.metrics:
                methods.setdefault(metric.name, set()).add((metric.unit, metric.source))
        for name, declared_methods in methods.items():
            if len(declared_methods) > 1:
                reasons.append("MEASUREMENT_METHOD_DIFFERS:" + name)
                comparable = False
        for name in set(bs) & set(cs):
            if (bs[name].unit, bs[name].source) != (cs[name].unit, cs[name].source):
                reasons.append("MEASUREMENT_METHOD_DIFFERS:" + name)
                comparable = False
        improvement = Fact[float]()
        decision = CandidateDecision.INCONCLUSIVE
        base_ids = {r.benchmark_id for r in baseline}
        quality = Fact[bool]()
        if (
            baseline
            and candidate
            and all(r.functional_validation.status == "PASS" for r in all_results)
            and all(
                r.functional_validation.quality_degraded.value is False
                and r.functional_validation.quality_degraded.evidence == EvidenceKind.MEASURED
                and r.functional_validation.compared_to_benchmark_id in base_ids
                for r in candidate
            )
        ):
            quality = known(
                True, "Linked measured quality comparison outcomes", EvidenceKind.MEASURED
            )
        if any(
            r.functional_validation.status == "FAIL"
            or r.functional_validation.quality_degraded.value is True
            for r in candidate
        ):
            decision = CandidateDecision.REJECTED
            reasons.append("CANDIDATE_FUNCTION_OR_QUALITY_FAILED")
        elif any(r.functional_validation.status != "PASS" for r in all_results):
            reasons.append("FUNCTIONAL_VALIDATION_UNKNOWN")
        elif any(
            r.functional_validation.quality_degraded.value is not False
            or r.functional_validation.quality_degraded.evidence != EvidenceKind.MEASURED
            or r.functional_validation.compared_to_benchmark_id not in base_ids
            for r in candidate
        ):
            reasons.append("QUALITY_EQUIVALENCE_NOT_PROVEN")
        elif comparable and policy.primary_metric in bs and policy.primary_metric in cs:
            b, c = bs[policy.primary_metric], cs[policy.primary_metric]
            if min(b.count, c.count) < policy.min_repetitions:
                reasons.append("INSUFFICIENT_REPETITIONS")
            elif (
                max(b.coefficient_variation or 0, c.coefficient_variation or 0)
                > policy.max_coefficient_variation
            ):
                reasons.append("UNSTABLE_REPETITIONS")
            elif b.median == 0:
                reasons.append("ZERO_BASELINE_DENOMINATOR")
            else:
                delta = (b.median - c.median) / abs(b.median)
                if policy.direction == "HIGHER":
                    delta = -delta
                improvement = known(
                    delta, "Derived from measured group medians", EvidenceKind.MEASURED
                )
                noise = 2 * max(b.coefficient_variation or 0, c.coefficient_variation or 0)
                if delta >= policy.min_improvement_fraction and delta > noise:
                    decision = CandidateDecision.ACCEPTED
                    reasons.append("QUALITY_PRESERVED_STABLE_MEASURED_IMPROVEMENT")
                elif delta >= policy.min_improvement_fraction:
                    reasons.append("EFFECT_WITHIN_OBSERVED_VARIATION")
                else:
                    decision = CandidateDecision.REJECTED
                    reasons.append("MINIMUM_IMPROVEMENT_NOT_REACHED")
        else:
            reasons.append("PRIMARY_METRIC_UNAVAILABLE_OR_INCOMPARABLE")

        def success(group):
            if not group or any(
                r.execution_evidence != EvidenceKind.MEASURED
                or r.functional_validation.status == "UNKNOWN"
                for r in group
            ):
                return Fact[float]()
            return known(
                sum(r.functional_validation.status == "PASS" for r in group) / len(group),
                "Measured functional outcomes",
                EvidenceKind.MEASURED,
            )

        return ExperimentComparison(
            baseline_ids=tuple(r.benchmark_id for r in baseline),
            candidate_ids=tuple(r.benchmark_id for r in candidate),
            evidence_run_ids=tuple(r.run_id for r in all_results),
            baseline_harnesses=tuple(dict.fromkeys(r.harness_id for r in baseline)),
            candidate_harnesses=tuple(dict.fromkeys(r.harness_id for r in candidate)),
            comparable=comparable,
            policy=policy,
            baseline_statistics=bs,
            candidate_statistics=cs,
            baseline_success_rate=success(baseline),
            candidate_success_rate=success(candidate),
            quality_preserved=quality,
            improvement_fraction=improvement,
            decision=decision,
            reasons=tuple(dict.fromkeys(reasons)),
        )
