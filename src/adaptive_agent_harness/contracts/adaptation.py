"""Approved, bounded execution contracts; Phase 5B planned-only APIs remain unchanged."""

from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, StrictFloat, StrictInt, model_validator

from .base import SHA256, Contract, Text, content_hash
from .data import DataApproval, DataPipelineSpecification
from .harness import ContainerHarnessSpecification, reject_credentials
from .incubation import AdaptationCandidate
from .optimization import ExperimentComparison, ExperimentPlan
from .runtime import BaselineResult


class NumericConfiguration(Contract):
    """Operator-reviewed numeric CLI slot, never an executable or arbitrary argument."""

    name: Text
    argument_index: int = Field(ge=2)
    minimum: float
    maximum: float
    alternatives: tuple[StrictInt | StrictFloat, ...] = Field(min_length=1, max_length=8)
    reason: Text

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.maximum < self.minimum or any(
            not self.minimum <= v <= self.maximum for v in self.alternatives
        ):
            raise ValueError("Configuration alternatives exceed reviewed bounds")
        return self


class ConfigurationPatch(Contract):
    field: Literal[
        "resource_limits.cpu_cores",
        "resource_limits.ram_bytes",
        "timeout_seconds",
        "agent_argument",
        "limits.buffer_records",
    ]
    before: StrictInt | StrictFloat
    after: StrictInt | StrictFloat
    configuration: NumericConfiguration | None = None

    @model_validator(mode="after")
    def safe_operation(self) -> Self:
        if self.before == self.after:
            raise ValueError("No-op patches are not candidates")
        if self.field != "agent_argument" and min(self.before, self.after) <= 0:
            raise ValueError("Limits must remain positive")
        if self.field in ("resource_limits.ram_bytes", "limits.buffer_records") and (
            type(self.before) is not int or type(self.after) is not int
        ):
            raise ValueError("Byte and record limits require integers")
        if (self.field == "agent_argument") != (self.configuration is not None):
            raise ValueError("Only declared numeric arguments accept configuration interfaces")
        if self.configuration and self.after not in self.configuration.alternatives:
            raise ValueError("Argument value is not a reviewed alternative")
        return self


class ExecutableCandidatePlan(Contract):
    """A precise patch supplements, rather than activates, the planned-only contract."""

    experiment: ExperimentPlan
    candidate: AdaptationCandidate
    patch: ConfigurationPatch
    risks: tuple[Text, ...] = Field(min_length=1)
    diff: Text
    concurrency: Literal[1] = 1
    pipeline_specification: DataPipelineSpecification | None = None

    @model_validator(mode="after")
    def exact_links(self) -> Self:
        from .optimization import CandidateExecutionRequest

        CandidateExecutionRequest(candidate=self.candidate, plan=self.experiment)
        if self.candidate.change_sha256 != content_hash(self.patch.model_dump(mode="json")):
            raise ValueError("Candidate must identify its exact structured patch")
        if self.candidate.changed_fields != (self.patch.field,):
            raise ValueError("Patch and candidate fields differ")
        expected_scope = (
            "DATA_PIPELINE"
            if self.patch.field.startswith("limits.")
            else "AGENT_CONFIGURATION"
            if self.patch.configuration
            else "HARNESS"
        )
        if self.candidate.scope.value != expected_scope:
            raise ValueError("Patch must preserve its declared modification scope")
        if (self.patch.field == "limits.buffer_records") != (
            self.pipeline_specification is not None
        ):
            raise ValueError("Pipeline patches require their exact derived specification")
        if self.pipeline_specification and (
            self.pipeline_specification.agent_id != self.candidate.agent_id
            or self.pipeline_specification.agent_source_sha256
            != self.candidate.baseline_agent_sha256
            or self.pipeline_specification.limits.buffer_records != self.patch.after
        ):
            raise ValueError("Derived pipeline does not match the candidate")
        if (
            self.experiment.budget.max_runs > 100
            or self.experiment.repetitions + self.experiment.warmups > 100
            or self.experiment.budget.max_total_seconds > 86400
        ):
            raise ValueError("Executable experiments must remain bounded")
        expected = f"{self.patch.field}: {self.patch.before} -> {self.patch.after}"
        if self.diff != expected:
            raise ValueError("Diff must describe the actual patch")
        reject_credentials(self.model_dump_json())
        return self


class CandidateAuthorization(Contract):
    approval_id: UUID = Field(default_factory=uuid4)
    approved_by: Text
    plan_sha256: SHA256
    baseline_approval_ids: tuple[UUID, ...] = Field(min_length=1)
    mode: Literal["TRUSTED_LOCAL_TRIAL"] = "TRUSTED_LOCAL_TRIAL"
    reviewed_candidate_unknowns: tuple[Text, ...]


class ApprovedCandidateRequest(Contract):
    plan: ExecutableCandidatePlan
    authorization: CandidateAuthorization
    execution_authorized: Literal[True] = True
    data_approval: DataApproval | None = None

    @model_validator(mode="after")
    def exact_approval(self) -> Self:
        if self.authorization.plan_sha256 != content_hash(self.plan.model_dump(mode="json")):
            raise ValueError("Authorization does not cover this plan")
        reject_credentials(self.authorization.approved_by)
        specification = self.plan.pipeline_specification
        if (specification is not None) != (self.data_approval is not None):
            raise ValueError("Pipeline execution needs a separate exact data approval")
        if specification and (
            self.data_approval.specification_sha256
            != content_hash(specification.model_dump(mode="json"))
            or self.data_approval.source_id != specification.source.source_id
            or self.data_approval.source_sha256 != specification.source.sha256
        ):
            raise ValueError("Data approval does not cover this derived pipeline")
        return self


class ExperimentOutcome(Contract):
    experiment_id: UUID = Field(default_factory=uuid4)
    request: ApprovedCandidateRequest
    status: Literal["IMPROVED", "REGRESSED", "INCONCLUSIVE", "INVALID", "UNSUPPORTED"]
    results: tuple[BaselineResult, ...] = ()
    comparison: ExperimentComparison | None = None
    package_reference: Text | None = None
    package_sha256: SHA256 | None = None
    specification: ContainerHarnessSpecification | None = None
    validated_candidate: AdaptationCandidate | None = None
    reasons: tuple[Text, ...] = Field(min_length=1)
    discarded: bool
    elapsed_seconds: float = Field(ge=0)

    @model_validator(mode="after")
    def measured_acceptance(self) -> Self:
        if self.status == "IMPROVED" and (
            not self.results
            or self.comparison is None
            or self.comparison.decision.value != "ACCEPTED"
            or self.discarded
            or self.package_reference is None
            or self.package_sha256 is None
            or self.validated_candidate is None
            or self.validated_candidate.decision.value != "ACCEPTED"
        ):
            raise ValueError("Improved requires a validated, retained package and comparison")
        if self.validated_candidate and (
            self.validated_candidate.candidate_id != self.request.plan.candidate.candidate_id
            or self.validated_candidate.change_sha256 != self.request.plan.candidate.change_sha256
        ):
            raise ValueError("Validated candidate must identify its approved patch")
        if self.comparison and set(self.comparison.candidate_ids) != {
            r.benchmark.benchmark_id for r in self.results if not r.benchmark.conditions.warmup
        }:
            raise ValueError("Comparison evidence differs from candidate measurements")
        return self


class AdaptationBudget(Contract):
    max_candidates: int = Field(default=5, ge=1, le=20)
    max_runs: int = Field(default=20, ge=1, le=100)
    max_total_seconds: float = Field(default=3600, gt=0, le=86400)
    max_no_improvement: int = Field(default=3, ge=1, le=20)
    target_latency_seconds: float | None = Field(default=None, gt=0)


class ModificationAssessment(Contract):
    status: Literal["NEEDS_INVESTIGATION", "AGENT_MODIFICATION_REQUIRED"]
    unresolved_problems: tuple[Text, ...] = Field(min_length=1)
    attempted_candidate_ids: tuple[UUID, ...]
    evidence_run_ids: tuple[UUID, ...]
    reasons: tuple[Text, ...] = Field(min_length=1)
    expected_scope: tuple[Text, ...] = ()
    approval_required: Literal[True] = True
    # A trusted diagnostic adapter must prove an Agent-internal limitation, not infer it from CPU.
    diagnostic_reference: Text | None = None

    @model_validator(mode="after")
    def no_speculative_escalation(self) -> Self:
        if self.status == "AGENT_MODIFICATION_REQUIRED" and (
            not self.diagnostic_reference
            or not self.attempted_candidate_ids
            or not self.evidence_run_ids
            or not self.expected_scope
        ):
            raise ValueError("Code escalation requires explicit diagnostics and failed trials")
        return self


class IncubationReport(Contract):
    session_id: UUID = Field(default_factory=uuid4)
    baseline_run_ids: tuple[UUID, ...] = Field(min_length=1)
    outcomes: tuple[ExperimentOutcome, ...]
    selected_candidate_id: UUID | None = None
    final_package_reference: Text
    final_package_sha256: SHA256
    stop_reason: Text
    used_runs: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)
    pending_plan_hashes: tuple[SHA256, ...] = ()
    assessment: ModificationAssessment | None = None
    automatically_deployed: Literal[False] = False

    @model_validator(mode="after")
    def valid_selection(self) -> Self:
        if self.selected_candidate_id is not None and not any(
            o.request.plan.candidate.candidate_id == self.selected_candidate_id
            and o.status == "IMPROVED"
            and o.package_reference == self.final_package_reference
            and o.package_sha256 == self.final_package_sha256
            for o in self.outcomes
        ):
            raise ValueError("Final candidate must be validated by this session")
        if self.used_runs != sum(len(o.results) for o in self.outcomes):
            raise ValueError("Run budget accounting must cover every attempt")
        return self
