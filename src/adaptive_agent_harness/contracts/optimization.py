"""Comparison and planned-candidate contracts, reusing existing version and decision models."""

from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, StrictBool, model_validator

from .base import Contract, Fact, PositiveInteger, Text
from .incubation import (
    AdaptationCandidate,
    BenchmarkResult,
    CandidateDecision,
    ChangeScope,
    ExpectedEffect,
    Metric,
)
from .runtime import BaselineResult


class ComparisonPolicy(Contract):
    primary_metric: Text = "wall_clock"
    direction: Literal["LOWER", "HIGHER"] = "LOWER"
    min_repetitions: int = Field(default=3, ge=3)
    min_improvement_fraction: float = Field(default=0.05, gt=0, le=1)
    max_coefficient_variation: float = Field(default=0.1, ge=0, le=1)


class MetricStatistics(Contract):
    count: PositiveInteger
    unit: Text
    source: Text
    mean: float
    median: float
    minimum: float
    maximum: float
    standard_deviation: float = Field(ge=0)
    coefficient_variation: float | None = Field(default=None, ge=0)


class ExperimentComparison(Contract):
    comparison_id: UUID = Field(default_factory=uuid4)
    baseline_ids: tuple[UUID, ...]
    candidate_ids: tuple[UUID, ...]
    evidence_run_ids: tuple[UUID, ...]
    baseline_harnesses: tuple[UUID, ...]
    candidate_harnesses: tuple[UUID, ...]
    comparable: bool
    policy: ComparisonPolicy
    baseline_statistics: dict[Text, MetricStatistics] = Field(default_factory=dict)
    candidate_statistics: dict[Text, MetricStatistics] = Field(default_factory=dict)
    baseline_success_rate: Fact[float] = Field(default_factory=Fact)
    candidate_success_rate: Fact[float] = Field(default_factory=Fact)
    quality_preserved: Fact[StrictBool] = Field(default_factory=Fact)
    improvement_fraction: Fact[float] = Field(default_factory=Fact)
    decision: CandidateDecision = CandidateDecision.INCONCLUSIVE
    reasons: tuple[Text, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def safe_acceptance(self) -> Self:
        if self.decision == CandidateDecision.ACCEPTED and (
            not self.comparable
            or self.quality_preserved.value is not True
            or self.quality_preserved.evidence.value != "MEASURED"
            or self.improvement_fraction.value is None
            or self.improvement_fraction.evidence.value != "MEASURED"
            or self.improvement_fraction.value < self.policy.min_improvement_fraction
        ):
            raise ValueError("Acceptance requires a comparable measured improvement")
        return self


class BottleneckFinding(Contract):
    kind: Literal[
        "CPU_PRESSURE",
        "OOM",
        "TIMEOUT",
        "NONZERO_EXIT",
        "LATENCY",
        "DATA_DELAY",
        "QUEUE_PRESSURE",
        "LOW_THROUGHPUT",
        "VARIABILITY",
        "UNKNOWN",
    ]
    evidence_run_ids: tuple[UUID, ...] = Field(min_length=1)
    observations: tuple[Metric, ...] = ()
    possible_causes: tuple[Text, ...] = Field(min_length=1)
    uncertainty: Literal["LOW", "MEDIUM", "HIGH", "UNKNOWN"]
    additional_measurements: tuple[Text, ...] = Field(min_length=1)


class OptimizationHypothesis(Contract):
    hypothesis_id: UUID = Field(default_factory=uuid4)
    baseline_id: UUID
    scope: ChangeScope
    reason: Text
    evidence_run_ids: tuple[UUID, ...] = Field(min_length=1)
    findings: tuple[BottleneckFinding, ...] = Field(min_length=1)
    expected_effects: tuple[ExpectedEffect, ...] = Field(min_length=1)
    changed_fields: tuple[Text, ...] = Field(min_length=1)
    harness_alternatives_exhausted: bool = False

    @model_validator(mode="after")
    def code_last(self) -> Self:
        if self.scope == ChangeScope.AGENT_CODE and not self.harness_alternatives_exhausted:
            raise ValueError("Agent code hypotheses require exhausted Harness alternatives")
        if any(set(f.evidence_run_ids) - set(self.evidence_run_ids) for f in self.findings):
            raise ValueError("Hypothesis evidence must cover its findings")
        return self


class ExperimentBudget(Contract):
    max_runs: PositiveInteger
    max_total_seconds: float = Field(gt=0)


class ExperimentPlan(Contract):
    plan_id: UUID = Field(default_factory=uuid4)
    hypothesis: OptimizationHypothesis
    baseline: BenchmarkResult
    baseline_run_ids: tuple[UUID, ...] = Field(min_length=1)
    comparison_policy: ComparisonPolicy
    budget: ExperimentBudget
    functional_criteria: tuple[Text, ...] = Field(min_length=1)
    quality_criteria: tuple[Text, ...] = Field(min_length=1)
    repetitions: int = Field(default=3, ge=3)
    warmups: int = Field(default=1, ge=0)
    apply_changes: Literal[False] = False

    @model_validator(mode="after")
    def enough_budget(self) -> Self:
        if (
            self.hypothesis.baseline_id != self.baseline.benchmark_id
            or self.baseline.run_id not in self.baseline_run_ids
        ):
            raise ValueError("Plan must identify its frozen baseline result")
        if (
            self.repetitions < self.comparison_policy.min_repetitions
            or self.warmups + self.repetitions > self.budget.max_runs
        ):
            raise ValueError("Plan must budget the required repetitions and warmups")
        if not set(self.hypothesis.evidence_run_ids).intersection(self.baseline_run_ids):
            raise ValueError("Hypothesis must refer to baseline evidence")
        return self


class CandidateExecutionRequest(Contract):
    candidate: AdaptationCandidate
    plan: ExperimentPlan
    execution_authorized: Literal[False] = False

    @model_validator(mode="after")
    def linked_candidate(self) -> Self:
        h = self.plan.hypothesis
        baseline = self.plan.baseline
        if (
            self.candidate.scope != h.scope
            or set(h.changed_fields) != set(self.candidate.changed_fields)
            or self.candidate.agent_id != baseline.agent_id
            or self.candidate.baseline_agent_sha256 != baseline.agent_source_sha256
            or self.candidate.parent_harness_id != baseline.harness_id
            or not set(h.evidence_run_ids).issubset(self.candidate.evidence_run_ids)
        ):
            raise ValueError("Planned change scope differs from the candidate")
        if (
            self.candidate.decision != CandidateDecision.UNVALIDATED
            or self.candidate.validation is not None
        ):
            raise ValueError("Execution requests describe unvalidated candidates only")
        return self


class CandidateExecutionResult(Contract):
    candidate_id: UUID
    plan_id: UUID
    results: tuple[BaselineResult, ...] = ()
    comparison: ExperimentComparison | None = None
    decision: CandidateDecision = CandidateDecision.UNVALIDATED
    reasons: tuple[Text, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def no_invented_execution(self) -> Self:
        if not self.results and (
            self.comparison is not None or self.decision != CandidateDecision.UNVALIDATED
        ):
            raise ValueError("Unexecuted candidates cannot have an outcome")
        if self.decision == CandidateDecision.ACCEPTED and (
            self.comparison is None
            or self.comparison.decision != CandidateDecision.ACCEPTED
            or set(self.comparison.candidate_ids)
            != {r.benchmark.benchmark_id for r in self.results}
        ):
            raise ValueError("Acceptance must link the measured comparison")
        return self
