"""Trace schemas only: no execution, candidate generation or optimization policy."""

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, StrictBool, StrictInt, model_validator

from .base import SHA256, Contract, EvidenceKind, Fact, PositiveInteger, Text
from .profiles import AgentProfile, ExecutionMode, HostProfile, ResourceLimits, TargetProfile


class HarnessSpecification(Contract):
    harness_id: UUID = Field(default_factory=uuid4)
    agent_id: UUID
    agent_source_sha256: SHA256
    target_id: UUID
    version: Text
    parent_harness_id: UUID | None = None
    execution_mode: ExecutionMode
    argv: tuple[Text, ...] = Field(min_length=1)
    base_image: Fact[Text] = Field(default_factory=Fact)
    resource_limits: ResourceLimits | None = None
    environment_references: dict[Text, Text] = Field(default_factory=dict)


class Metric(Contract):
    name: Text
    value: float | None
    unit: Text
    evidence: EvidenceKind
    source: Text
    run_id: UUID

    @model_validator(mode="after")
    def missing_measurement(self) -> Self:
        if (self.evidence == EvidenceKind.UNVERIFIED) != (self.value is None):
            raise ValueError("Unmeasured metrics require null; observed metrics require a value")
        return self


class ValidationStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class FunctionalValidation(Contract):
    status: ValidationStatus = ValidationStatus.UNKNOWN
    test_result_references: tuple[Text, ...] = ()
    quality_degraded: Fact[StrictBool] = Field(default_factory=Fact)
    compared_to_benchmark_id: UUID | None = None

    @model_validator(mode="after")
    def validation_evidence(self) -> Self:
        if self.status != ValidationStatus.UNKNOWN and not self.test_result_references:
            raise ValueError("PASS or FAIL requires functional test evidence")
        if self.status == ValidationStatus.PASS and self.quality_degraded.value is True:
            raise ValueError("Degraded quality cannot be recorded as PASS")
        return self


class ExperimentConditions(Contract):
    input_sha256: SHA256
    evaluator_sha256: SHA256
    seed: StrictInt | None = None
    repetition: PositiveInteger
    warmup: StrictBool = False
    execution_mode: ExecutionMode
    notes: tuple[Text, ...] = ()


class BenchmarkResult(Contract):
    benchmark_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    agent_id: UUID
    agent_source_sha256: SHA256
    harness_id: UUID
    harness_sha256: SHA256
    host: HostProfile
    target: TargetProfile
    conditions: ExperimentConditions
    execution_evidence: EvidenceKind
    metrics: tuple[Metric, ...] = ()
    functional_validation: FunctionalValidation = Field(default_factory=FunctionalValidation)

    @model_validator(mode="after")
    def traceable_run(self) -> Self:
        if any(m.run_id != self.run_id for m in self.metrics):
            raise ValueError("Metrics must refer to the owning run")
        if self.target.based_on_host_id not in (None, self.host.host_id):
            raise ValueError("Resource simulation belongs to a different host")
        if self.execution_evidence != EvidenceKind.MEASURED:
            if any(m.evidence == EvidenceKind.MEASURED for m in self.metrics):
                raise ValueError("Unexecuted experiments cannot contain actual measurements")
        return self


class ChangeScope(StrEnum):
    HARNESS = "HARNESS"
    AGENT_CODE = "AGENT_CODE"


class AgentVersion(Contract):
    version_id: UUID = Field(default_factory=uuid4)
    content_sha256: SHA256
    original_sha256: SHA256
    parent_version_id: UUID | None = None


class ExpectedEffect(Contract):
    description: Text
    metric_name: Text | None = None
    estimated_change: float | None = None
    unit: Text | None = None
    evidence: Literal[EvidenceKind.ESTIMATED] = EvidenceKind.ESTIMATED

    @model_validator(mode="after")
    def explained_estimate(self) -> Self:
        if self.estimated_change is not None and (self.metric_name is None or self.unit is None):
            raise ValueError("Numeric estimates require metric name and unit")
        return self


class CodeChangeEvidence(Contract):
    approval_id: UUID
    approved_original_sha256: SHA256
    approved_change_sha256: SHA256
    before_test_references: tuple[Text, ...] = Field(min_length=1)
    after_test_references: tuple[Text, ...] = Field(min_length=1)
    rollback_test_references: tuple[Text, ...] = Field(min_length=1)
    rollback_source_sha256: SHA256


class CandidateDecision(StrEnum):
    UNVALIDATED = "UNVALIDATED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class AdaptationCandidate(Contract):
    candidate_id: UUID = Field(default_factory=uuid4)
    agent_id: UUID
    agent_version: AgentVersion
    baseline_agent_sha256: SHA256
    parent_harness_id: UUID
    resulting_harness_id: UUID
    scope: ChangeScope
    changed_fields: tuple[Text, ...] = Field(min_length=1)
    change_sha256: SHA256
    reason: Text
    evidence_run_ids: tuple[UUID, ...] = Field(min_length=1)
    expected_effects: tuple[ExpectedEffect, ...] = Field(min_length=1)
    validation: BenchmarkResult | None = None
    decision: CandidateDecision = CandidateDecision.UNVALIDATED
    code_change_evidence: CodeChangeEvidence | None = None

    @model_validator(mode="after")
    def preserve_original_and_quality(self) -> Self:
        if self.parent_harness_id == self.resulting_harness_id:
            raise ValueError("Adaptation must identify a new Harness version")
        if self.scope == ChangeScope.HARNESS:
            if self.agent_version.content_sha256 != self.baseline_agent_sha256:
                raise ValueError("Harness-only changes must preserve their baseline Agent version")
            if self.code_change_evidence is not None:
                raise ValueError("Harness-only changes cannot carry Agent patch approval")
        if self.validation is not None:
            v = self.validation
            if (
                v.agent_id != self.agent_id
                or v.harness_id != self.resulting_harness_id
                or v.agent_source_sha256 != self.agent_version.content_sha256
            ):
                raise ValueError("Validation must refer to this candidate's Agent and Harness")
        if self.decision == CandidateDecision.ACCEPTED:
            v = self.validation
            if v is None or v.execution_evidence != EvidenceKind.MEASURED:
                raise ValueError("Acceptance requires actual execution evidence")
            quality = v.functional_validation
            if (
                quality.status != ValidationStatus.PASS
                or quality.quality_degraded.value is not False
                or quality.quality_degraded.evidence != EvidenceKind.MEASURED
                or quality.compared_to_benchmark_id is None
                or v.conditions.warmup
                or not any(m.evidence == EvidenceKind.MEASURED for m in v.metrics)
            ):
                raise ValueError(
                    "Acceptance requires measured metrics and proven functional quality"
                )
            if self.scope == ChangeScope.AGENT_CODE:
                approval = self.code_change_evidence
                if (
                    approval is None
                    or approval.approved_change_sha256 != self.change_sha256
                    or approval.approved_original_sha256 != self.agent_version.original_sha256
                    or approval.rollback_source_sha256 != self.agent_version.original_sha256
                ):
                    raise ValueError("Agent changes require matching approval, tests and rollback")
        return self


class IncubationPhase(StrEnum):
    BASELINE = "BASELINE"
    DIAGNOSE = "DIAGNOSE"
    ADAPT = "ADAPT"
    VALIDATE = "VALIDATE"


class IncubationStep(Contract):
    phase: IncubationPhase
    benchmark: BenchmarkResult | None = None
    diagnosis: Text | None = None
    candidate: AdaptationCandidate | None = None

    @model_validator(mode="after")
    def phase_payload(self) -> Self:
        expected = {
            IncubationPhase.BASELINE: "benchmark",
            IncubationPhase.DIAGNOSE: "diagnosis",
            IncubationPhase.ADAPT: "candidate",
            IncubationPhase.VALIDATE: "benchmark",
        }
        for field in ("benchmark", "diagnosis", "candidate"):
            if (getattr(self, field) is not None) != (field == expected[self.phase]):
                raise ValueError("Each phase requires exactly its associated payload")
        return self


class IncubationIteration(Contract):
    number: PositiveInteger
    steps: tuple[IncubationStep, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def ordered_phases(self) -> Self:
        expected = list(IncubationPhase)[: len(self.steps)]
        if [s.phase for s in self.steps] != expected:
            raise ValueError("Iterations follow Baseline -> Diagnose -> Adapt -> Validate")
        baseline = self.steps[0].benchmark
        if len(self.steps) >= 3:
            candidate = self.steps[2].candidate
            if (
                candidate.parent_harness_id != baseline.harness_id
                or candidate.baseline_agent_sha256 != baseline.agent_source_sha256
                or baseline.run_id not in candidate.evidence_run_ids
            ):
                raise ValueError("Adaptation must refer to its baseline version, Harness and run")
        if len(self.steps) == 4:
            candidate, validation = self.steps[2].candidate, self.steps[3].benchmark
            if validation.run_id == baseline.run_id:
                raise ValueError("Candidate validation requires a separate execution from baseline")
            if validation.harness_id != candidate.resulting_harness_id:
                raise ValueError("Validation phase must refer to its adaptation")
            if candidate.validation is not None and candidate.validation != validation:
                raise ValueError("Candidate and phase validation records differ")
            if (
                validation.agent_id != candidate.agent_id
                or validation.agent_source_sha256 != candidate.agent_version.content_sha256
            ):
                raise ValueError("Validation phase must preserve candidate Agent identity")
            if candidate.decision == CandidateDecision.ACCEPTED:
                if (
                    validation.host != baseline.host
                    or validation.target != baseline.target
                    or validation.conditions.input_sha256 != baseline.conditions.input_sha256
                    or validation.conditions.evaluator_sha256
                    != baseline.conditions.evaluator_sha256
                    or validation.conditions.seed != baseline.conditions.seed
                    or validation.functional_validation.compared_to_benchmark_id
                    != baseline.benchmark_id
                ):
                    raise ValueError("Accepted validation must be comparable to its baseline")
        return self


class IncubationSession(Contract):
    session_id: UUID = Field(default_factory=uuid4)
    agent: AgentProfile
    host: HostProfile
    target: TargetProfile
    agent_versions: tuple[AgentVersion, ...] = Field(min_length=1)
    iterations: tuple[IncubationIteration, ...] = ()
    notes: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def session_trace(self) -> Self:
        if self.target.based_on_host_id not in (None, self.host.host_id):
            raise ValueError("Session resource simulation belongs to a different host")
        original = self.agent.source.content_sha256
        hashes = {v.content_sha256 for v in self.agent_versions}
        ids = {v.version_id for v in self.agent_versions}
        if (
            original not in hashes
            or len(ids) != len(self.agent_versions)
            or any(v.original_sha256 != original for v in self.agent_versions)
        ):
            raise ValueError("Session must preserve one original and unique tracked Agent versions")
        if any(
            v.parent_version_id is not None and v.parent_version_id not in ids
            for v in self.agent_versions
        ):
            raise ValueError("Version parent is not tracked in this session")
        parents = {v.version_id: v.parent_version_id for v in self.agent_versions}
        for version_id in parents:
            visited = set()
            current = version_id
            while current is not None:
                if current in visited:
                    raise ValueError("Agent version ancestry must not contain cycles")
                visited.add(current)
                current = parents[current]
        if [i.number for i in self.iterations] != list(range(1, len(self.iterations) + 1)):
            raise ValueError("Iteration numbers must be consecutive")
        for iteration in self.iterations:
            for step in iteration.steps:
                if step.benchmark is not None:
                    b = step.benchmark
                    if (
                        b.agent_id != self.agent.agent_id
                        or b.agent_source_sha256 not in hashes
                        or b.target != self.target
                        or b.host != self.host
                    ):
                        raise ValueError(
                            "Benchmark is outside this session's version or environment"
                        )
                if step.candidate is not None:
                    c = step.candidate
                    if (
                        c.agent_id != self.agent.agent_id
                        or c.agent_version not in self.agent_versions
                    ):
                        raise ValueError(
                            "Candidate is outside this session's tracked Agent versions"
                        )
        return self
