"""Approved single-host candidate execution and bounded evidence-driven adaptation."""

import hashlib
import re
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Lock
from typing import Protocol

from adaptive_agent_harness.benchmark.bottleneck import BottleneckAnalyzer
from adaptive_agent_harness.contracts import (
    AdaptationCandidate,
    AgentProfile,
    CandidateDecision,
    EvidenceKind,
    HostProfile,
    TargetProfile,
    known,
)
from adaptive_agent_harness.contracts.adaptation import (
    AdaptationBudget,
    ApprovedCandidateRequest,
    CandidateAuthorization,
    ExecutableCandidatePlan,
    ExperimentOutcome,
    IncubationReport,
    ModificationAssessment,
    NumericConfiguration,
)
from adaptive_agent_harness.contracts.base import content_hash
from adaptive_agent_harness.contracts.harness import ContainerHarnessSpecification
from adaptive_agent_harness.contracts.optimization import ComparisonPolicy, ExperimentBudget
from adaptive_agent_harness.contracts.runtime import BaselinePlan, BaselineResult, ExecutionApproval
from adaptive_agent_harness.data import PreparedData
from adaptive_agent_harness.harness import ContainerHarnessBuilder
from adaptive_agent_harness.optimizer.evaluator import ExperimentEvaluator
from adaptive_agent_harness.optimizer.planner import CandidatePlanner, HypothesisGenerator
from adaptive_agent_harness.porting.manager import PortingService
from adaptive_agent_harness.runtime.docker import RuntimeFailure

from .baseline import BaselineRunner, integrate


class ExperimentStore(Protocol):
    def save(self, record: ExperimentOutcome | IncubationReport) -> Path: ...


def baseline_context(
    package: Path,
    baseline: Sequence[BaselineResult],
    agent: AgentProfile,
    host: HostProfile,
    target: TargetProfile,
) -> tuple[BaselineResult, ContainerHarnessSpecification]:
    if not baseline:
        raise ValueError("BASELINE_REQUIRED")
    baseline = tuple(BaselineResult.model_validate_json(r.model_dump_json()) for r in baseline)
    if len({r.runtime.run_id for r in baseline}) != len(baseline):
        raise ValueError("DUPLICATE_BASELINE")
    spec = None
    for result in baseline:
        spec, spec_hash, _ = integrate(package, result.approval, agent, host, target, {})
        b = result.benchmark
        if (
            b.harness_id != spec.harness_id
            or b.harness_sha256 != spec_hash
            or content_hash(b.host.model_dump(mode="json"))
            != content_hash(host.model_dump(mode="json"))
            or content_hash(b.target.model_dump(mode="json"))
            != content_hash(target.model_dump(mode="json"))
            or b.agent_id != agent.agent_id
            or b.agent_source_sha256 != agent.source.content_sha256
        ):
            raise ValueError("BASELINE_PROFILE_MISMATCH")
    return baseline[0], spec


class CandidateExecutor:
    def __init__(self, runner: BaselineRunner, porting: PortingService, store: ExperimentStore):
        self.runner, self.porting, self.store = runner, porting, store
        self._lock = Lock()

    def execute(
        self,
        request: ApprovedCandidateRequest,
        *,
        baseline_package: Path,
        baseline: Sequence[BaselineResult],
        agent: AgentProfile,
        host: HostProfile,
        target: TargetProfile,
        output_root: Path,
        max_total_seconds: float | None = None,
        prepared_data: PreparedData | None = None,
        comparison_baseline: Sequence[BaselineResult] | None = None,
    ) -> ExperimentOutcome:
        if not self._lock.acquire(blocking=False):
            raise ValueError("CANDIDATE_ALREADY_RUNNING")
        try:
            return self._execute(
                request,
                baseline_package=baseline_package,
                baseline=baseline,
                agent=agent,
                host=host,
                target=target,
                output_root=output_root,
                max_total_seconds=max_total_seconds,
                prepared_data=prepared_data,
                comparison_baseline=comparison_baseline,
            )
        finally:
            self._lock.release()

    def _execute(
        self,
        request: ApprovedCandidateRequest,
        *,
        baseline_package: Path,
        baseline: Sequence[BaselineResult],
        agent: AgentProfile,
        host: HostProfile,
        target: TargetProfile,
        output_root: Path,
        max_total_seconds: float | None,
        prepared_data,
        comparison_baseline,
    ) -> ExperimentOutcome:
        started = time.monotonic()
        request = ApprovedCandidateRequest.model_validate_json(request.model_dump_json())
        baseline = tuple(BaselineResult.model_validate_json(r.model_dump_json()) for r in baseline)
        first, original_spec = baseline_context(baseline_package, baseline, agent, host, target)
        plan, authorization = request.plan, request.authorization
        if (
            set(authorization.baseline_approval_ids) != {r.approval.approval_id for r in baseline}
            or set(plan.experiment.baseline_run_ids) != {r.runtime.run_id for r in baseline}
            or not any(r.benchmark == plan.experiment.baseline for r in baseline)
        ):
            raise ValueError("BASELINE_APPROVAL_MISMATCH")
        if set(plan.experiment.hypothesis.evidence_run_ids) - {r.runtime.run_id for r in baseline}:
            raise ValueError("FOREIGN_HYPOTHESIS_EVIDENCE")
        metrics = {r.runtime.run_id: r.benchmark.metrics for r in baseline}
        if any(
            m not in metrics.get(m.run_id, ()) or m.evidence.value != "MEASURED"
            for finding in plan.experiment.hypothesis.findings
            for m in finding.observations
        ):
            raise ValueError("HYPOTHESIS_OBSERVATION_MISMATCH")
        seconds = plan.experiment.budget.max_total_seconds
        if max_total_seconds is not None:
            if max_total_seconds <= 0:
                raise ValueError("EXPERIMENT_BUDGET_EXHAUSTED")
            seconds = min(seconds, max_total_seconds)
        deadline = started + seconds
        results, comparison, package, manifest = (), None, None, None
        status, reasons = "INVALID", ("EXECUTION_NOT_STARTED",)
        try:
            package = self.porting.materialize(
                plan,
                baseline_package,
                agent=agent,
                host=host,
                target=target,
                runtime_plan=first.plan,
                output_root=output_root,
                prepared_data=prepared_data,
                data_approval=request.data_approval,
            )
            if package.specification.harness_id != plan.candidate.resulting_harness_id:
                raise ValueError("CANDIDATE_PACKAGE_MISMATCH")
            expected = original_spec.model_dump(mode="json")
            actual = package.specification.model_dump(mode="json")
            expected.update(
                harness_id=str(plan.candidate.resulting_harness_id),
                parent_harness_id=str(original_spec.harness_id),
                version=f"candidate-{plan.candidate.candidate_id}",
            )
            if plan.patch.field.startswith("resource_limits."):
                expected["resource_limits"][plan.patch.field.split(".")[1]] = plan.patch.after
            elif plan.patch.field == "agent_argument":
                expected["argv"][plan.patch.configuration.argument_index] = str(plan.patch.after)
            elif plan.patch.field == "limits.buffer_records":
                if package.prepared_data is None:
                    raise ValueError("APPROVED_PIPELINE_REQUIRED")
                expected["data_requirement"]["pipeline_specification_sha256"] = (
                    package.prepared_data.binding.version.specification_sha256
                )
            if content_hash(expected) != content_hash(actual):
                raise ValueError("UNAPPROVED_PACKAGE_CHANGE")
            # New unknowns are not silently approved by reuse of the baseline approval.
            if set(authorization.reviewed_candidate_unknowns) != set(
                package.specification.unknowns
            ):
                raise ValueError("CANDIDATE_REVIEW_REQUIRED")
            manifest = ContainerHarnessBuilder().verify(package.directory)
            if manifest.source_bundle_sha256 != first.runtime.source_bundle_sha256:
                raise ValueError("AGENT_SOURCE_CHANGED")
            approval = ExecutionApproval(
                approval_id=authorization.approval_id,
                approved_by=authorization.approved_by,
                mode=authorization.mode,
                agent_source_sha256=agent.source.content_sha256,
                source_bundle_sha256=manifest.source_bundle_sha256,
                specification_sha256=manifest.specification_sha256,
                manifest_sha256=hashlib.sha256(
                    (package.directory / "manifest.json").read_bytes()
                ).hexdigest(),
                reviewed_unknowns=package.specification.unknowns,
                allow_build_network=first.approval.allow_build_network,
                allow_runtime_network=first.approval.allow_runtime_network,
            )
            runtime_plan = first.plan.model_dump()
            runtime_plan.update(
                repetitions=plan.experiment.repetitions, warmups=plan.experiment.warmups
            )
            if plan.patch.field == "timeout_seconds":
                runtime_plan["timeout_seconds"] = plan.patch.after
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("EXPERIMENT_BUDGET_EXHAUSTED")
            results = self.runner.run(
                package.directory,
                approval=approval,
                agent=agent,
                host=host,
                target=target,
                plan=BaselinePlan.model_validate(runtime_plan),
                max_total_seconds=remaining,
            )
            if results:
                status, comparison = ExperimentEvaluator().compare(
                    comparison_baseline if comparison_baseline is not None else baseline,
                    results,
                    policy=plan.experiment.comparison_policy,
                )
                reasons = comparison.reasons
                if len(results) != plan.experiment.repetitions + plan.experiment.warmups:
                    status, reasons = "INCONCLUSIVE", (*reasons, "INCOMPLETE_BUDGET_OR_CLEANUP")
            else:
                reasons = ("EXPERIMENT_BUDGET_EXHAUSTED",)
        except (ValueError, OSError, RuntimeFailure) as exc:
            # Never persist arbitrary exception payloads, source data, provider contexts or secrets.
            status = "UNSUPPORTED" if "UNSUPPORTED" in str(exc) else "INVALID"
            code = str(exc)
            reasons = (
                getattr(exc, "code", None)
                or (code if re.fullmatch(r"[A-Z_]{1,80}", code) else type(exc).__name__),
            )
        # Verify that immutable rollback anchors were never modified, even on failures.
        baseline_context(baseline_package, baseline, agent, host, target)
        validated = None
        if status == "IMPROVED":
            measurement = next(r.benchmark for r in results if not r.benchmark.conditions.warmup)
            quality = measurement.functional_validation.model_copy(
                update={
                    "quality_degraded": known(
                        False, comparison.quality_preserved.source, EvidenceKind.MEASURED
                    ),
                    "compared_to_benchmark_id": comparison.baseline_ids[0],
                }
            )
            validation = measurement.model_copy(update={"functional_validation": quality})
            validated = AdaptationCandidate.model_validate(
                {
                    **plan.candidate.model_dump(),
                    "decision": CandidateDecision.ACCEPTED,
                    "validation": validation,
                }
            )
        outcome = ExperimentOutcome(
            request=request,
            status=status,
            results=results,
            comparison=comparison,
            package_reference=str(package.directory) if package else None,
            package_sha256=manifest.specification_sha256 if manifest else None,
            specification=package.specification if package else None,
            validated_candidate=validated,
            reasons=reasons,
            discarded=status != "IMPROVED",
            elapsed_seconds=time.monotonic() - started,
        )
        self.store.save(outcome)
        return outcome


class IncubationEngine:
    """Sequential, explicitly approved trials; proposed follow-ups never authorize themselves."""

    def __init__(self, executor: CandidateExecutor, store: ExperimentStore):
        self.executor, self.store = executor, store
        self._lock = Lock()

    def run(
        self,
        requests: Sequence[ApprovedCandidateRequest],
        *,
        baseline_package: Path,
        baseline: Sequence[BaselineResult],
        agent: AgentProfile,
        host: HostProfile,
        target: TargetProfile,
        output_root: Path,
        budget: AdaptationBudget,
    ) -> IncubationReport:
        if not self._lock.acquire(blocking=False):
            raise ValueError("INCUBATION_ALREADY_RUNNING")
        try:
            return self._run(
                requests,
                baseline_package=baseline_package,
                baseline=baseline,
                agent=agent,
                host=host,
                target=target,
                output_root=output_root,
                budget=budget,
            )
        finally:
            self._lock.release()

    def adapt(
        self,
        *,
        authorize: Callable[[ExecutableCandidatePlan], CandidateAuthorization | None],
        baseline_package: Path,
        baseline: Sequence[BaselineResult],
        agent: AgentProfile,
        host: HostProfile,
        target: TargetProfile,
        output_root: Path,
        budget: AdaptationBudget,
        configurations: Sequence[NumericConfiguration] = (),
        policy: ComparisonPolicy | None = None,
        repetitions: int = 3,
        warmups: int = 1,
    ) -> IncubationReport:
        """Regenerate from measurements; only a trusted external approval callback can gate trials."""
        if not self._lock.acquire(blocking=False):
            raise ValueError("INCUBATION_ALREADY_RUNNING")
        try:
            return self._adapt(
                authorize=authorize,
                baseline_package=baseline_package,
                baseline=baseline,
                agent=agent,
                host=host,
                target=target,
                output_root=output_root,
                budget=budget,
                configurations=configurations,
                policy=policy,
                repetitions=repetitions,
                warmups=warmups,
            )
        finally:
            self._lock.release()

    def _adapt(
        self,
        *,
        authorize,
        baseline_package,
        baseline,
        agent,
        host,
        target,
        output_root,
        budget,
        configurations,
        policy,
        repetitions,
        warmups,
    ) -> IncubationReport:
        budget = AdaptationBudget.model_validate_json(budget.model_dump_json())
        policy = ComparisonPolicy.model_validate_json(
            (policy or ComparisonPolicy()).model_dump_json()
        )
        if repetitions < policy.min_repetitions or repetitions + warmups > 100 or warmups < 0:
            raise ValueError("EXPERIMENT_LIMIT")
        started = time.monotonic()
        original_first, current_spec = baseline_context(
            baseline_package, baseline, agent, host, target
        )
        current_package, current_results = baseline_package, tuple(baseline)
        final_sha, selected, best = original_first.benchmark.harness_sha256, None, 0.0
        seen, outcomes, pending = set(), [], set()
        runs, stop = 0, "NO_SUPPORTED_HYPOTHESIS"
        no_improvement = 0
        while len(outcomes) < budget.max_candidates:
            remaining = budget.max_total_seconds - (time.monotonic() - started)
            if remaining <= 0 or runs + repetitions + warmups > budget.max_runs:
                stop = "EXPERIMENT_BUDGET_EXHAUSTED"
                break
            measured = tuple(r for r in current_results if not r.benchmark.conditions.warmup)
            if not measured:
                stop = "BASELINE_MEASUREMENTS_UNKNOWN"
                break
            if (
                budget.target_latency_seconds
                and len(measured) >= policy.min_repetitions
                and all(
                    ExperimentEvaluator.complete(r)
                    and r.runtime.wall_seconds is not None
                    and r.runtime.wall_seconds <= budget.target_latency_seconds
                    for r in measured
                )
            ):
                stop = "GOAL_REACHED"
                break
            first = measured[0]
            findings = BottleneckAnalyzer().analyze(
                [r.benchmark for r in current_results],
                runtimes=[r.runtime for r in current_results],
                max_latency_seconds=budget.target_latency_seconds,
            )
            proposals = HypothesisGenerator().generate(
                first.benchmark,
                findings,
                current_spec,
                first.plan,
                configurations=configurations,
                buffer_records=first.data_pipeline.specification.limits.buffer_records
                if first.data_pipeline
                else None,
            )
            chosen = None
            for hypothesis, patch in proposals:
                # Deduplicate the resulting effective setting, independent of random candidate/version IDs.
                effective = current_spec.model_dump(mode="json")
                for field in ("created_at", "harness_id", "parent_harness_id", "version"):
                    effective.pop(field, None)
                if patch.field.startswith("resource_limits."):
                    effective["resource_limits"][patch.field.split(".")[1]] = patch.after
                elif patch.configuration:
                    effective["argv"][patch.configuration.argument_index] = str(patch.after)
                else:
                    effective[patch.field] = patch.after
                key = content_hash(effective)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    plan = CandidatePlanner().plan(
                        hypothesis,
                        patch,
                        baseline=first.benchmark,
                        baseline_run_ids=tuple(r.runtime.run_id for r in current_results),
                        specification=current_spec,
                        runtime_plan=first.plan,
                        agent=agent,
                        target=target,
                        budget=ExperimentBudget(
                            max_runs=repetitions + warmups, max_total_seconds=remaining
                        ),
                        policy=policy,
                        repetitions=repetitions,
                        warmups=warmups,
                        buffer_records=first.data_pipeline.specification.limits.buffer_records
                        if first.data_pipeline
                        else None,
                    )
                except ValueError:
                    continue  # Unsupported operations are not executed or escalated to source edits.
                approval = authorize(plan)
                if approval is None:
                    pending.add(content_hash(plan.model_dump(mode="json")))
                    continue
                chosen = ApprovedCandidateRequest(plan=plan, authorization=approval)
                break
            if chosen is None:
                stop = "APPROVAL_REQUIRED" if pending else "NO_SUPPORTED_HYPOTHESIS"
                break
            remaining = budget.max_total_seconds - (time.monotonic() - started)
            if remaining <= 0:
                stop = "EXPERIMENT_BUDGET_EXHAUSTED"
                break
            outcome = self.executor.execute(
                chosen,
                baseline_package=current_package,
                baseline=current_results,
                comparison_baseline=baseline,
                agent=agent,
                host=host,
                target=target,
                output_root=output_root,
                max_total_seconds=remaining,
            )
            outcomes.append(outcome)
            runs += len(outcome.results)
            if (
                outcome.status == "IMPROVED"
                and outcome.comparison.improvement_fraction.value > best
            ):
                best = outcome.comparison.improvement_fraction.value
                selected = chosen.plan.candidate.candidate_id
                current_package, current_results = Path(outcome.package_reference), outcome.results
                current_spec, final_sha = outcome.specification, outcome.package_sha256
                no_improvement = 0
            else:
                no_improvement += 1
            if any(r.runtime.cleanup_errors for r in outcome.results):
                stop = "CLEANUP_FAILED"
                break
            if no_improvement >= budget.max_no_improvement:
                stop = "NO_IMPROVEMENT_LIMIT"
                break
        else:
            stop = "EXPERIMENT_BUDGET_EXHAUSTED"
        baseline_context(baseline_package, baseline, agent, host, target)
        ContainerHarnessBuilder().verify(current_package)
        report = IncubationReport(
            baseline_run_ids=tuple(r.runtime.run_id for r in baseline),
            outcomes=tuple(outcomes),
            selected_candidate_id=selected,
            final_package_reference=str(current_package),
            final_package_sha256=final_sha,
            stop_reason=stop,
            used_runs=runs,
            elapsed_seconds=time.monotonic() - started,
            pending_plan_hashes=tuple(sorted(pending)),
            assessment=None
            if selected
            else ModificationAssessment(
                status="NEEDS_INVESTIGATION",
                unresolved_problems=("No supported approved quality-preserving improvement",),
                attempted_candidate_ids=tuple(
                    o.request.plan.candidate.candidate_id for o in outcomes
                ),
                evidence_run_ids=tuple(r.runtime.run_id for o in outcomes for r in o.results),
                reasons=(
                    "Source modification requires independent Agent-internal diagnostic evidence",
                ),
            ),
        )
        self.store.save(report)
        return report

    def _run(
        self,
        requests,
        *,
        baseline_package,
        baseline,
        agent,
        host,
        target,
        output_root,
        budget,
    ) -> IncubationReport:
        budget = AdaptationBudget.model_validate_json(budget.model_dump_json())
        if len(requests) > 100:
            raise ValueError("REQUEST_LIMIT")
        requests = tuple(
            ApprovedCandidateRequest.model_validate_json(r.model_dump_json()) for r in requests
        )
        started = time.monotonic()
        first, spec = baseline_context(baseline_package, baseline, agent, host, target)
        final_path = str(baseline_package)
        final_sha = first.benchmark.harness_sha256
        selected, best, runs, no_improvement = None, None, 0, 0
        outcomes, seen, pending = [], set(), set()
        stop = "APPROVED_CANDIDATES_EXHAUSTED"
        measured_baseline = tuple(r for r in baseline if not r.benchmark.conditions.warmup)
        if (
            budget.target_latency_seconds
            and len(measured_baseline) >= 3
            and all(
                r.benchmark.functional_validation.status == "PASS"
                and r.runtime.wall_seconds is not None
                and r.runtime.wall_seconds <= budget.target_latency_seconds
                for r in measured_baseline
            )
        ):
            requests, stop = (), "BASELINE_GOAL_REACHED"
        for request in requests:
            key = content_hash(
                {
                    "parent": str(request.plan.candidate.parent_harness_id),
                    "patch": request.plan.patch.model_dump(mode="json"),
                    "evaluation": request.plan.experiment.comparison_policy.model_dump(mode="json"),
                }
            )
            if key in seen:
                continue
            required = request.plan.experiment.repetitions + request.plan.experiment.warmups
            remaining = budget.max_total_seconds - (time.monotonic() - started)
            if (
                len(outcomes) >= budget.max_candidates
                or runs + required > budget.max_runs
                or remaining <= 0
            ):
                stop = "EXPERIMENT_BUDGET_EXHAUSTED"
                break
            seen.add(key)
            outcome = self.executor.execute(
                request,
                baseline_package=baseline_package,
                baseline=baseline,
                agent=agent,
                host=host,
                target=target,
                output_root=output_root,
                max_total_seconds=remaining,
            )
            outcomes.append(outcome)
            runs += len(outcome.results)
            findings = BottleneckAnalyzer().analyze(
                [r.benchmark for r in outcome.results],
                runtimes=[r.runtime for r in outcome.results],
                max_latency_seconds=budget.target_latency_seconds,
            )
            # Subsequent evidence can create new plans, but these need their own exact approval.
            observed = next((r for r in outcome.results if not r.benchmark.conditions.warmup), None)
            if observed and outcome.specification:
                proposed = HypothesisGenerator().generate(
                    observed.benchmark, findings, outcome.specification, observed.plan
                )
                pending.update(content_hash(h.model_dump(mode="json")) for h, _ in proposed)
            if outcome.status == "IMPROVED":
                score = outcome.comparison.improvement_fraction.value
                if best is None or score > best:
                    selected, best = request.plan.candidate.candidate_id, score
                    final_path, final_sha = outcome.package_reference, outcome.package_sha256
                no_improvement = 0
                median = outcome.comparison.candidate_statistics.get("wall_clock")
                if (
                    budget.target_latency_seconds
                    and median
                    and median.median <= budget.target_latency_seconds
                ):
                    stop = "GOAL_REACHED"
                    break
            else:
                no_improvement += 1
            if any(r.runtime.cleanup_errors for r in outcome.results):
                stop = "CLEANUP_FAILED"
                break
            if no_improvement >= budget.max_no_improvement:
                stop = "NO_IMPROVEMENT_LIMIT"
                break
        baseline_context(baseline_package, baseline, agent, host, target)
        if final_path != str(baseline_package):
            ContainerHarnessBuilder().verify(Path(final_path))
        assessment = None
        if selected is None:
            assessment = ModificationAssessment(
                status="NEEDS_INVESTIGATION",
                unresolved_problems=(
                    "No approved trial demonstrated a stable quality-preserving improvement",
                ),
                attempted_candidate_ids=tuple(
                    o.request.plan.candidate.candidate_id for o in outcomes
                ),
                evidence_run_ids=tuple(r.runtime.run_id for o in outcomes for r in o.results),
                reasons=("Harness trial failure alone does not prove an Agent source defect",),
            )
        report = IncubationReport(
            baseline_run_ids=tuple(r.runtime.run_id for r in baseline),
            outcomes=tuple(outcomes),
            selected_candidate_id=selected,
            final_package_reference=final_path,
            final_package_sha256=final_sha,
            stop_reason=stop,
            used_runs=runs,
            elapsed_seconds=time.monotonic() - started,
            pending_plan_hashes=tuple(sorted(pending)),
            assessment=assessment,
        )
        self.store.save(report)
        return report
