"""Pure evidence-driven hypotheses and allowlisted candidate planning."""

import hashlib
from collections.abc import Sequence
from uuid import UUID, uuid4

from adaptive_agent_harness.contracts import (
    AdaptationCandidate,
    AgentProfile,
    AgentVersion,
    BenchmarkResult,
    ChangeScope,
    EvidenceKind,
    ExpectedEffect,
    TargetProfile,
)
from adaptive_agent_harness.contracts.adaptation import (
    ConfigurationPatch,
    ExecutableCandidatePlan,
    NumericConfiguration,
)
from adaptive_agent_harness.contracts.base import content_hash
from adaptive_agent_harness.contracts.data import DataPipelineSpecification
from adaptive_agent_harness.contracts.harness import ContainerHarnessSpecification
from adaptive_agent_harness.contracts.optimization import (
    BottleneckFinding,
    ComparisonPolicy,
    ExperimentBudget,
    ExperimentPlan,
    OptimizationHypothesis,
)
from adaptive_agent_harness.contracts.runtime import BaselinePlan
from adaptive_agent_harness.harness._utils import canonical_json


def validate_patch(
    patch: ConfigurationPatch,
    spec: ContainerHarnessSpecification,
    plan: BaselinePlan,
    agent: AgentProfile,
    target: TargetProfile,
    *,
    buffer_records: int | None = None,
) -> None:
    patch = ConfigurationPatch.model_validate_json(patch.model_dump_json())
    if patch.field.startswith("resource_limits."):
        field = patch.field.split(".")[1]
        current = getattr(spec.resource_limits, field) if spec.resource_limits else None
        capacity = getattr(target.hardware.resources, field).value
        simulated = getattr(target.simulated_limits, field) if target.simulated_limits else None
        if capacity is None:
            raise ValueError("TARGET_CAPACITY_UNKNOWN")
        if patch.after > min(v for v in (capacity, simulated) if v is not None):
            raise ValueError("TARGET_RESOURCE_LIMIT")
    elif patch.field == "timeout_seconds":
        current = plan.timeout_seconds
        if patch.after > 86400:
            raise ValueError("TIMEOUT_LIMIT")
    elif patch.field == "limits.buffer_records":
        current = buffer_records
        if current is None or patch.after > 10000:
            raise ValueError("PIPELINE_BUFFER_UNSUPPORTED")
    else:
        cfg = patch.configuration
        index = cfg.argument_index
        if (
            index >= len(spec.argv)
            or spec.argv[index - 1] != cfg.name
            or not any(
                i.kind == "cli_option" and i.name == cfg.name
                for i in agent.configuration_interfaces
            )
            or (spec.data_requirement and spec.argv[index].startswith("/opt/data/"))
        ):
            raise ValueError("UNDECLARED_CONFIGURATION")
        try:
            current = float(spec.argv[index])
        except ValueError:
            raise ValueError("NON_NUMERIC_CONFIGURATION") from None
        if not cfg.minimum <= patch.before <= cfg.maximum:
            raise ValueError("CONFIGURATION_BOUNDS")
    if current != patch.before:
        raise ValueError("STALE_PATCH")


class HypothesisGenerator:
    def generate(
        self,
        baseline: BenchmarkResult,
        findings: Sequence[BottleneckFinding],
        spec: ContainerHarnessSpecification,
        runtime_plan: BaselinePlan,
        *,
        configurations: Sequence[NumericConfiguration] = (),
        buffer_records: int | None = None,
    ) -> tuple[tuple[OptimizationHypothesis, ConfigurationPatch], ...]:
        baseline = BenchmarkResult.model_validate_json(baseline.model_dump_json())
        if baseline.execution_evidence != EvidenceKind.MEASURED or baseline.conditions.warmup:
            return ()  # UNKNOWN: no execution evidence, never fabricate a hypothesis.
        proposals = []
        seen = set()
        for finding in findings:
            finding = BottleneckFinding.model_validate_json(finding.model_dump_json())
            if baseline.run_id not in finding.evidence_run_ids:
                continue
            operations = []
            if finding.kind == "OOM" and spec.resource_limits and spec.resource_limits.ram_bytes:
                operations.append(("resource_limits.ram_bytes", spec.resource_limits.ram_bytes))
            if (
                finding.kind == "CPU_PRESSURE"
                and spec.resource_limits
                and spec.resource_limits.cpu_cores
            ):
                operations.append(("resource_limits.cpu_cores", spec.resource_limits.cpu_cores))
            if finding.kind == "TIMEOUT":
                operations.append(("timeout_seconds", runtime_plan.timeout_seconds))
            if (
                finding.kind in ("DATA_DELAY", "QUEUE_PRESSURE", "LOW_THROUGHPUT")
                and buffer_records
            ):
                operations.append(("limits.buffer_records", buffer_records))
            patches = [ConfigurationPatch(field=f, before=v, after=v * 2) for f, v in operations]
            if finding.kind == "LATENCY":
                for cfg in configurations:
                    cfg = NumericConfiguration.model_validate_json(cfg.model_dump_json())
                    if cfg.argument_index < len(spec.argv):
                        try:
                            current = float(spec.argv[cfg.argument_index])
                        except ValueError:
                            continue
                        patches += [
                            ConfigurationPatch(
                                field="agent_argument", before=current, after=v, configuration=cfg
                            )
                            for v in cfg.alternatives
                            if v != current
                        ]
            for patch in patches:
                fingerprint = content_hash(patch.model_dump(mode="json"))
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                scope = (
                    ChangeScope.DATA_PIPELINE
                    if patch.field.startswith("limits.")
                    else ChangeScope.AGENT_CONFIGURATION
                    if patch.configuration
                    else ChangeScope.HARNESS
                )
                proposals.append(
                    (
                        OptimizationHypothesis(
                            baseline_id=baseline.benchmark_id,
                            scope=scope,
                            reason=f"Controlled trial prompted by observed {finding.kind}; causality unproven",
                            evidence_run_ids=finding.evidence_run_ids,
                            findings=(finding,),
                            changed_fields=(patch.field,),
                            expected_effects=(
                                ExpectedEffect(
                                    description="Potential benefit is unmeasured; compare identical evaluation and quality"
                                ),
                            ),
                        ),
                        patch,
                    )
                )
        return tuple(proposals)


class CandidatePlanner:
    def plan(
        self,
        hypothesis: OptimizationHypothesis,
        patch: ConfigurationPatch,
        *,
        baseline: BenchmarkResult,
        baseline_run_ids: tuple[UUID, ...],
        specification: ContainerHarnessSpecification,
        runtime_plan: BaselinePlan,
        agent: AgentProfile,
        target: TargetProfile,
        budget: ExperimentBudget,
        policy: ComparisonPolicy | None = None,
        repetitions: int = 3,
        warmups: int = 1,
        buffer_records: int | None = None,
        pipeline_specification: DataPipelineSpecification | None = None,
    ) -> ExecutableCandidatePlan:
        baseline = BenchmarkResult.model_validate_json(baseline.model_dump_json())
        spec = ContainerHarnessSpecification.model_validate_json(specification.model_dump_json())
        if (
            baseline.harness_id != spec.harness_id
            or baseline.harness_sha256
            != hashlib.sha256(canonical_json(spec.model_dump(mode="json"))).hexdigest()
            or baseline.agent_source_sha256 != agent.source.content_sha256
            or baseline.agent_id != agent.agent_id
            or baseline.target.target_id != target.target_id
        ):
            raise ValueError("BASELINE_CONTEXT_MISMATCH")
        validate_patch(patch, spec, runtime_plan, agent, target, buffer_records=buffer_records)
        if hypothesis.scope == ChangeScope.AGENT_CODE:
            raise ValueError("AGENT_CODE_UNSUPPORTED")
        if repetitions + warmups > 100 or budget.max_runs > 100 or budget.max_total_seconds > 86400:
            raise ValueError("EXPERIMENT_LIMIT")
        experiment = ExperimentPlan(
            hypothesis=hypothesis,
            baseline=baseline,
            baseline_run_ids=baseline_run_ids,
            comparison_policy=policy or ComparisonPolicy(),
            budget=budget,
            repetitions=repetitions,
            warmups=warmups,
            functional_criteria=("Approved evaluator passes every measured run",),
            quality_criteria=("Objective quality equivalence to the frozen baseline",),
        )
        candidate = AdaptationCandidate(
            agent_id=agent.agent_id,
            agent_version=AgentVersion(
                content_sha256=agent.source.content_sha256,
                original_sha256=agent.source.content_sha256,
            ),
            baseline_agent_sha256=agent.source.content_sha256,
            parent_harness_id=spec.harness_id,
            resulting_harness_id=uuid4(),
            scope=hypothesis.scope,
            changed_fields=(patch.field,),
            change_sha256=content_hash(patch.model_dump(mode="json")),
            reason=hypothesis.reason,
            evidence_run_ids=hypothesis.evidence_run_ids,
            expected_effects=hypothesis.expected_effects,
        )
        return ExecutableCandidatePlan(
            experiment=experiment,
            candidate=candidate,
            patch=patch,
            risks=(
                "Allocation can affect scheduling; runtime quality and resource observations required",
            ),
            diff=f"{patch.field}: {patch.before} -> {patch.after}",
            pipeline_specification=pipeline_specification,
        )
