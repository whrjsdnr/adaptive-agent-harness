"""Objective quality evaluation and conservative reuse of Phase 5B comparisons."""

from collections.abc import Sequence

from adaptive_agent_harness.benchmark.comparison import BenchmarkComparisonEngine, signature
from adaptive_agent_harness.contracts import CandidateDecision, EvidenceKind, known
from adaptive_agent_harness.contracts.optimization import ComparisonPolicy, ExperimentComparison
from adaptive_agent_harness.contracts.runtime import BaselineResult


class ExperimentEvaluator:
    def compare(
        self,
        baseline: Sequence[BaselineResult],
        candidate: Sequence[BaselineResult],
        *,
        policy: ComparisonPolicy,
    ) -> tuple[str, ExperimentComparison]:
        baseline = tuple(BaselineResult.model_validate_json(r.model_dump_json()) for r in baseline)
        candidate = tuple(
            BaselineResult.model_validate_json(r.model_dump_json()) for r in candidate
        )
        measured_base = [r for r in baseline if not r.benchmark.conditions.warmup]
        evaluated = []
        for result in candidate:
            b = next(
                (b for b in measured_base if signature(b.benchmark) == signature(result.benchmark)),
                None,
            )
            validation = result.benchmark.functional_validation
            if b is not None and self.complete(b) and self.complete(result):
                # Objective equality is scoped to this approved exact-output evaluator, not general quality.
                equal = (
                    result.runtime.stdout == b.runtime.stdout == b.plan.expected_stdout
                    and result.plan.expected_stdout == b.plan.expected_stdout
                )
                validation = validation.model_copy(
                    update={
                        "quality_degraded": known(
                            not equal,
                            "Measured complete exact-output equivalence v1",
                            EvidenceKind.MEASURED,
                        ),
                        "compared_to_benchmark_id": b.benchmark.benchmark_id,
                    }
                )
            benchmark = result.benchmark.model_copy(update={"functional_validation": validation})
            evaluated.append(benchmark)
        comparison = BenchmarkComparisonEngine().compare(
            [r.benchmark for r in baseline], evaluated, policy=policy
        )
        baseline_versions = {
            r.data_pipeline.version.version_id if r.data_pipeline else None for r in baseline
        }
        candidate_versions = {
            r.data_pipeline.version.version_id if r.data_pipeline else None for r in candidate
        }
        if not comparison.comparable or baseline_versions != candidate_versions:
            if baseline_versions != candidate_versions:
                comparison = comparison.model_copy(
                    update={
                        "comparable": False,
                        "decision": CandidateDecision.INCONCLUSIVE,
                        "reasons": (*comparison.reasons, "DATA_PIPELINE_VERSION_DIFFERS"),
                    }
                )
            return "INVALID", comparison
        if comparison.decision.value == "ACCEPTED":
            return "IMPROVED", comparison
        if comparison.decision.value == "REJECTED":
            return "REGRESSED", comparison
        return "INCONCLUSIVE", comparison

    @staticmethod
    def complete(result: BaselineResult) -> bool:
        r = result.runtime
        return (
            result.plan.expected_stdout is not None
            and r.execution_started
            and r.exit_code.value == 0
            and r.exit_code.evidence == EvidenceKind.MEASURED
            and r.logs_collected
            and not r.logs_truncated
            and not r.timed_out
            and r.error_code is None
            and r.cleanup_verified
            and not r.cleanup_errors
            and result.benchmark.functional_validation.status == "PASS"
        )
