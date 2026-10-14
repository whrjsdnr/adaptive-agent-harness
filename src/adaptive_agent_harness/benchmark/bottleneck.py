"""Evidence-based diagnostic hypotheses; never assert a single causal bottleneck."""

from collections.abc import Sequence

from adaptive_agent_harness.contracts import (
    BenchmarkResult,
    ChangeScope,
    EvidenceKind,
    ExpectedEffect,
)
from adaptive_agent_harness.contracts.optimization import BottleneckFinding, OptimizationHypothesis
from adaptive_agent_harness.contracts.runtime import RuntimeRecord

from .comparison import signature, summarize


class BottleneckAnalyzer:
    def analyze(
        self,
        results: Sequence[BenchmarkResult],
        *,
        runtimes: Sequence[RuntimeRecord] = (),
        max_latency_seconds: float | None = None,
        max_data_delay_seconds: float | None = None,
        min_throughput: float | None = None,
        queue_depth_limit: float | None = None,
    ) -> tuple[BottleneckFinding, ...]:
        results = tuple(BenchmarkResult.model_validate_json(r.model_dump_json()) for r in results)
        measured = tuple(
            r
            for r in results
            if r.execution_evidence == EvidenceKind.MEASURED and not r.conditions.warmup
        )
        runtime_map = {
            r.run_id: RuntimeRecord.model_validate_json(r.model_dump_json()) for r in runtimes
        }
        output = []

        def add(kind, result, metrics, cause, uncertainty, needed):
            output.append(
                BottleneckFinding(
                    kind=kind,
                    evidence_run_ids=(result.run_id,),
                    observations=tuple(metrics),
                    possible_causes=tuple(cause),
                    uncertainty=uncertainty,
                    additional_measurements=tuple(needed),
                )
            )

        for result in measured:
            values = {
                m.name: m
                for m in result.metrics
                if m.evidence == EvidenceKind.MEASURED and m.value is not None
            }
            if (
                "cpu_percent_sample_mean" in values
                and values["cpu_percent_sample_mean"].value >= 90
            ):
                add(
                    "CPU_PRESSURE",
                    result,
                    [values["cpu_percent_sample_mean"]],
                    [
                        "CPU activity is high for at least one core; quota saturation is unproven",
                        "Data conversion or Agent computation may account for CPU usage",
                    ],
                    "HIGH",
                    ["CPU quota and throttled time", "Per-stage CPU samples"],
                )
            for key, threshold, kind, higher in (
                ("wall_clock", max_latency_seconds, "LATENCY", True),
                ("data_supply_seconds", max_data_delay_seconds, "DATA_DELAY", True),
                ("data_records_per_second", min_throughput, "LOW_THROUGHPUT", False),
                ("data_queue_depth", queue_depth_limit, "QUEUE_PRESSURE", True),
            ):
                if (
                    threshold is not None
                    and key in values
                    and (
                        (values[key].value > threshold)
                        if higher
                        else (values[key].value < threshold)
                    )
                ):
                    add(
                        kind,
                        result,
                        [values[key]],
                        [
                            "An observed metric exceeds the operator's threshold; causality is unresolved"
                        ],
                        "HIGH",
                        [
                            "Independent producer and consumer timestamps",
                            "Agent internal stage timing",
                        ],
                    )
            runtime = runtime_map.get(result.run_id)
            if runtime:
                if (
                    runtime.oom_killed.value is True
                    and runtime.oom_killed.evidence == EvidenceKind.MEASURED
                ):
                    add(
                        "OOM",
                        result,
                        [],
                        [
                            "Docker observed OOM; resource limit or workload footprint may be responsible"
                        ],
                        "MEDIUM",
                        ["Cgroup memory events and lifetime peak", "Approved memory quota"],
                    )
                elif runtime.timed_out:
                    add(
                        "TIMEOUT",
                        result,
                        [],
                        ["Execution exceeded its deadline; compute, I/O, or deadlock are possible"],
                        "HIGH",
                        ["Stage timing and bounded progress observations"],
                    )
                elif (
                    runtime.exit_code.evidence == EvidenceKind.MEASURED
                    and runtime.exit_code.value not in (None, 0)
                ):
                    add(
                        "NONZERO_EXIT",
                        result,
                        [],
                        [
                            "Abnormal exit was observed; OOM or crash cannot be inferred from exit code alone"
                        ],
                        "HIGH",
                        ["Sanitized error diagnostics and termination reason"],
                    )
        groups = {}
        for result in measured:
            groups.setdefault(
                (signature(result), result.harness_id, result.harness_sha256), []
            ).append(result)
        for group in groups.values():
            for key, summary in summarize(group).items():
                if summary.count >= 3 and (summary.coefficient_variation or 0) > 0.1:
                    output.append(
                        BottleneckFinding(
                            kind="VARIABILITY",
                            evidence_run_ids=tuple(r.run_id for r in group),
                            observations=tuple(
                                m
                                for r in group
                                for m in r.metrics
                                if m.name == key and m.evidence == EvidenceKind.MEASURED
                            ),
                            possible_causes=(
                                "Host interference, workload variation, or measurement noise",
                            ),
                            uncertainty="HIGH",
                            additional_measurements=(
                                "Controlled repetitions and environment observations",
                            ),
                        )
                    )
        if measured and not output:
            output.append(
                BottleneckFinding(
                    kind="UNKNOWN",
                    evidence_run_ids=tuple(r.run_id for r in measured),
                    possible_causes=("Existing observations do not isolate a bottleneck",),
                    uncertainty="UNKNOWN",
                    additional_measurements=(
                        "Per-stage timing, cgroup pressure and queue observations",
                    ),
                )
            )
        return tuple(output)

    def hypotheses(
        self, baseline: BenchmarkResult, findings: Sequence[BottleneckFinding]
    ) -> tuple[OptimizationHypothesis, ...]:
        output = []
        for finding in findings:
            if (
                finding.kind in ("UNKNOWN", "NONZERO_EXIT", "VARIABILITY")
                or baseline.run_id not in finding.evidence_run_ids
            ):
                continue
            data = finding.kind in ("DATA_DELAY", "QUEUE_PRESSURE", "LOW_THROUGHPUT")
            output.append(
                OptimizationHypothesis(
                    baseline_id=baseline.benchmark_id,
                    scope=ChangeScope.DATA_PIPELINE if data else ChangeScope.HARNESS,
                    reason="Test a configuration alternative suggested by observed " + finding.kind,
                    evidence_run_ids=finding.evidence_run_ids,
                    findings=(finding,),
                    changed_fields=("limits.buffer_records",) if data else ("resource_limits",),
                    expected_effects=(
                        ExpectedEffect(
                            description="Potential benefit is unmeasured; verify functional quality and controlled repetitions"
                        ),
                    ),
                )
            )
        return tuple(output)
