"""Explicit local-trial authorization and baseline execution records."""

from datetime import UTC
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, StrictBool, field_validator, model_validator

from .base import SHA256, Contract, Fact, Text
from .data import DataPipelineBinding
from .incubation import BenchmarkResult


class ExecutionApproval(Contract):
    approval_id: UUID
    approved_by: Text
    agent_source_sha256: SHA256
    source_bundle_sha256: SHA256
    specification_sha256: SHA256
    manifest_sha256: SHA256
    # This is an operator-selected test mode, never an external security verdict.
    mode: Literal["TRUSTED_LOCAL_TRIAL"]
    reviewed_unknowns: tuple[str, ...]
    allow_runtime_network: StrictBool = False
    allow_build_network: StrictBool = False


class BaselinePlan(Contract):
    input_sha256: SHA256
    seed: int | None = None
    repetitions: int = Field(default=1, ge=1, le=100)
    warmups: int = Field(default=0, ge=0, le=100)
    timeout_seconds: float = Field(gt=0, le=86400)
    build_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    log_limit_bytes: int = Field(default=65536, ge=1, le=1048576)
    expected_stdout: str | None = Field(default=None, max_length=1048576)


class RuntimeRecord(Contract):
    run_id: UUID
    approval_id: UUID
    harness_version: Text
    source_bundle_sha256: SHA256
    manifest_sha256: SHA256
    image_id: str | None = None
    container_id: str | None = None
    execution_started: bool = False
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    wall_seconds: float | None = Field(default=None, ge=0)
    exit_code: Fact[int] = Field(default_factory=Fact)
    timed_out: bool = False
    oom_killed: Fact[bool] = Field(default_factory=Fact)
    container_state: Fact[str] = Field(default_factory=Fact)
    limits_verified: Fact[bool] = Field(default_factory=Fact)
    cpu_percent_samples: tuple[float, ...] = ()
    memory_bytes_samples: tuple[float, ...] = ()
    stdout: str = ""
    stderr: str = ""
    logs_truncated: bool = False
    logs_collected: bool = False
    daemon_identity: dict[str, str | int] = Field(default_factory=dict)
    error_code: str | None = None
    error_cause: str | None = None
    cleanup_errors: tuple[str, ...] = ()
    telemetry_errors: tuple[str, ...] = ()
    cleanup_verified: bool = False

    @field_validator("started_at", "finished_at")
    @classmethod
    def utc_observation(cls, value):
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def consistent_measurements(self) -> Self:
        if not self.execution_started and (
            self.started_at is not None
            or self.wall_seconds is not None
            or self.cpu_percent_samples
            or self.memory_bytes_samples
        ):
            raise ValueError("Unexecuted runs cannot contain runtime measurements")
        if any(value < 0 for value in (*self.cpu_percent_samples, *self.memory_bytes_samples)):
            raise ValueError("Resource samples cannot be negative")
        if self.started_at and self.finished_at and self.finished_at < self.started_at:
            raise ValueError("Finish time precedes start time")
        return self


class BaselineResult(Contract):
    data_pipeline: DataPipelineBinding | None = Field(default=None, exclude_if=lambda v: v is None)
    benchmark: BenchmarkResult
    runtime: RuntimeRecord
    plan: BaselinePlan
    approval: ExecutionApproval

    @model_validator(mode="after")
    def linked_evidence(self) -> Self:
        if (
            self.runtime.run_id != self.benchmark.run_id
            or self.runtime.approval_id != self.approval.approval_id
            or self.runtime.manifest_sha256 != self.approval.manifest_sha256
            or self.runtime.source_bundle_sha256 != self.approval.source_bundle_sha256
            or self.benchmark.harness_sha256 != self.approval.specification_sha256
            or self.benchmark.agent_source_sha256 != self.approval.agent_source_sha256
        ):
            raise ValueError("Baseline records must refer to the same approved execution")
        if self.data_pipeline is not None:
            d = self.data_pipeline
            if (
                d.specification.agent_id != self.benchmark.agent_id
                or d.specification.agent_source_sha256 != self.benchmark.agent_source_sha256
                or self.plan.input_sha256 != d.version.output_sha256
                or self.benchmark.conditions.data_preparation_run_id != d.delivery_metrics.run_id
                or self.benchmark.conditions.data_fingerprints
                != {
                    "source": d.version.source_sha256,
                    "schema": d.validation.schema_sha256,
                    "evaluated": d.version.output_sha256,
                }
            ):
                raise ValueError("Baseline must pin the validated data execution and version")
        return self
