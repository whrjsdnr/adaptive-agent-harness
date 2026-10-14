"""Pure conversion of runtime observations into the existing BenchmarkResult contract."""

from adaptive_agent_harness.contracts import (
    BenchmarkResult,
    EvidenceKind,
    ExecutionMode,
    ExperimentConditions,
    FunctionalValidation,
    HostProfile,
    Metric,
    TargetProfile,
    ValidationStatus,
)
from adaptive_agent_harness.contracts.data import DataPipelineBinding
from adaptive_agent_harness.contracts.harness import ContainerHarnessSpecification
from adaptive_agent_harness.contracts.runtime import BaselinePlan, RuntimeRecord
from adaptive_agent_harness.harness._utils import content_hash


def evaluate(
    record: RuntimeRecord,
    spec: ContainerHarnessSpecification,
    spec_hash: str,
    host: HostProfile,
    target: TargetProfile,
    plan: BaselinePlan,
    repetition: int,
    warmup: bool,
    data_binding: DataPipelineBinding | None = None,
) -> BenchmarkResult:
    values = (
        ("wall_clock", record.wall_seconds, "s", "Docker StartedAt/FinishedAt"),
        (
            "cpu_percent_sample_mean",
            sum(record.cpu_percent_samples) / len(record.cpu_percent_samples)
            if record.cpu_percent_samples
            else None,
            "%",
            "Docker stats: 100% = one CPU core",
        ),
        (
            "memory_last_sample",
            record.memory_bytes_samples[-1] if record.memory_bytes_samples else None,
            "bytes",
            "Docker stats Linux cache-subtracted memory, rounded CLI units",
        ),
        (
            "memory_sampled_peak",
            max(record.memory_bytes_samples) if record.memory_bytes_samples else None,
            "bytes",
            "Maximum sampled Docker stats memory; may miss short-lived peaks",
        ),
        ("memory_peak", None, "bytes", "UNKNOWN: kernel lifetime peak is not collected"),
    )
    functional = FunctionalValidation()
    if (
        plan.expected_stdout is not None
        and record.execution_started
        and record.exit_code.value is not None
        and record.logs_collected
    ):
        passed = (
            record.exit_code.value == 0
            and not record.timed_out
            and record.error_code is None
            and not record.cleanup_errors
            and record.logs_collected
            and not record.logs_truncated
            and record.stdout == plan.expected_stdout
        )
        functional = FunctionalValidation(
            status=ValidationStatus.PASS if passed else ValidationStatus.FAIL,
            test_result_references=(f"runtime:{record.run_id}:exact-stdout",),
        )
    metrics = tuple(
        Metric(
            name=name,
            value=value,
            unit=unit,
            source=source,
            evidence=EvidenceKind.MEASURED if value is not None else EvidenceKind.UNVERIFIED,
            run_id=record.run_id,
        )
        for name, value, unit, source in values
    )
    if data_binding and record.execution_started:
        metrics += tuple(
            Metric.model_validate({**m.model_dump(), "run_id": record.run_id})
            for m in data_binding.delivery_metrics.metrics
        )
    return BenchmarkResult(
        run_id=record.run_id,
        agent_id=spec.agent_id,
        agent_source_sha256=spec.agent_source_sha256,
        harness_id=spec.harness_id,
        harness_sha256=spec_hash,
        host=host,
        target=target,
        conditions=ExperimentConditions(
            environment_sha256=content_hash(record.daemon_identity)
            if record.daemon_identity and not spec.environment_references
            else None,
            data_preparation_run_id=data_binding.delivery_metrics.run_id if data_binding else None,
            data_fingerprints={
                "source": data_binding.version.source_sha256,
                "schema": data_binding.validation.schema_sha256,
                "evaluated": data_binding.version.output_sha256,
            }
            if data_binding
            else {},
            input_sha256=plan.input_sha256,
            evaluator_sha256=content_hash(
                {"validator": "exact-stdout-v1", "expected_stdout": plan.expected_stdout}
            ),
            seed=plan.seed,
            repetition=repetition,
            warmup=warmup,
            execution_mode=ExecutionMode.DOCKER,
            notes=(
                "TRUSTED_LOCAL_TRIAL; external security review UNVERIFIED",
                "Target hardware performance UNVERIFIED unless this is the target host",
                "Input hash and seed are caller declarations; the runner does not inject inputs",
            ),
        ),
        execution_evidence=EvidenceKind.MEASURED
        if record.execution_started
        else EvidenceKind.UNVERIFIED,
        metrics=metrics,
        functional_validation=functional,
    )
