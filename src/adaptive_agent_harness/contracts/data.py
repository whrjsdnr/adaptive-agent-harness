"""Explicit local data contracts. No inference, filesystem access or execution."""

import re
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import AfterValidator, Field, StrictBool, model_validator

from .base import SHA256, Contract, Fact, PositiveInteger, Text
from .incubation import Metric, ValidationStatus
from .profiles import ResourceLimits


def safe_relative(value: str) -> str:
    # Import lazily: the container contracts also accept DataRequirement.
    from .harness import reject_sensitive_text, relative_source_path

    return relative_source_path(reject_sensitive_text(value))


DataPath = Annotated[Text, AfterValidator(safe_relative)]


def safe_label(value: str) -> str:
    from .harness import reject_sensitive_text

    return reject_sensitive_text(value)


def reference(value: str) -> str:
    safe_label(value)
    if not re.fullmatch(r"(?:env|secret)://[A-Za-z0-9_./-]+", value):
        raise ValueError("Credentials must be external references, never values")
    return value


DataText = Annotated[Text, AfterValidator(safe_label)]
DataReference = Annotated[Text, AfterValidator(reference)]


class DataFormat(StrEnum):
    CSV = "CSV"
    JSON = "JSON"
    JSONL = "JSONL"


class DataField(Contract):
    type: Literal["string", "integer", "number", "boolean", "object", "array"]
    required: StrictBool = True
    nullable: StrictBool = False


class DataSchema(Contract):
    fields: dict[DataText, DataField] = Field(min_length=1, max_length=256)
    allow_extra: StrictBool = False


class DataObservation(Contract):
    kind: Literal["READ", "WRITE", "FORMAT", "ENVIRONMENT", "ARGUMENT", "DYNAMIC_PATH"]
    location: Text
    name: Text | None = None
    evidence: Literal["STATIC"] = "STATIC"


class DataRequirement(Contract):
    agent_id: UUID
    agent_source_sha256: SHA256
    pipeline_specification_sha256: SHA256 | None = None
    requires_data: Fact[StrictBool] = Field(default_factory=Fact)
    input_files: Fact[tuple[Text, ...]] = Field(default_factory=Fact)
    input_directories: Fact[tuple[Text, ...]] = Field(default_factory=Fact)
    formats: Fact[tuple[DataFormat, ...]] = Field(default_factory=Fact)
    environment_paths: Fact[tuple[Text, ...]] = Field(default_factory=Fact)
    input_arguments: Fact[tuple[Text, ...]] = Field(default_factory=Fact)
    access_method: Fact[Text] = Field(default_factory=Fact)
    supply_period_seconds: Fact[float] = Field(default_factory=Fact)
    order_dependent: Fact[StrictBool] = Field(default_factory=Fact)
    input_schema: DataSchema | None = None
    output_schema: DataSchema | None = None
    credential_references: tuple[DataReference, ...] = ()
    observations: tuple[DataObservation, ...] = ()
    unknowns: tuple[Text, ...] = ()


class DataSource(Contract):
    source_id: UUID = Field(default_factory=uuid4)
    path: DataPath
    format: DataFormat
    sha256: SHA256
    version: DataText
    credential_references: tuple[DataReference, ...] = ()


class PipelineLimits(Contract):
    max_source_bytes: PositiveInteger = Field(default=64 * 1024**2, le=512 * 1024**2)
    max_output_bytes: PositiveInteger = Field(default=128 * 1024**2, le=512 * 1024**2)
    max_record_bytes: PositiveInteger = Field(default=64 * 1024, le=1024**2)
    max_records: PositiveInteger = Field(default=1_000_000, le=10_000_000)
    buffer_records: PositiveInteger = Field(default=128, le=10000)
    buffer_bytes: PositiveInteger = Field(default=1024**2, le=8 * 1024**2)

    @model_validator(mode="after")
    def bounded_records(self) -> Self:
        if self.max_record_bytes > self.buffer_bytes:
            raise ValueError("A record must fit within the byte buffer")
        return self


class DataPipelineSpecification(Contract):
    pipeline_id: UUID = Field(default_factory=uuid4)
    agent_id: UUID
    agent_source_sha256: SHA256
    source: DataSource
    input_schema: DataSchema
    output_format: DataFormat = DataFormat.JSONL
    project_fields: tuple[Text, ...] = ()
    validation_steps: tuple[Literal["REQUIRED_FIELDS", "TYPES"], ...] = ("REQUIRED_FIELDS", "TYPES")
    error_policy: Literal["REJECT", "QUARANTINE"] = "REJECT"
    delivery: Literal["FILE_ARG", "ENV_PATH"] = "FILE_ARG"
    argument_index: int | None = Field(default=None, ge=1)
    environment_name: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    limits: PipelineLimits = Field(default_factory=PipelineLimits)
    resource_limits: ResourceLimits | None = None
    version: DataText
    parent_version_id: UUID | None = None
    changes: tuple[Text, ...] = ()

    @property
    def container_path(self) -> str:
        return "/opt/data/input." + self.output_format.value.lower()

    @model_validator(mode="after")
    def explicit_delivery(self) -> Self:
        from .harness import reject_credentials

        reject_credentials(self.model_dump_json())
        if self.delivery == "FILE_ARG":
            if self.argument_index is None or self.environment_name is not None:
                raise ValueError("FILE_ARG requires only an explicit argument index")
        elif self.environment_name is None or self.argument_index is not None:
            raise ValueError("ENV_PATH requires only an environment variable name")
        if set(self.validation_steps) != {"REQUIRED_FIELDS", "TYPES"}:
            raise ValueError("Required field and type validation cannot be disabled")
        if len(set(self.project_fields)) != len(self.project_fields) or any(
            field not in self.input_schema.fields for field in self.project_fields
        ):
            raise ValueError("Projection must identify unique declared fields")
        if self.parent_version_id is not None and not self.changes:
            raise ValueError("A derived version must describe its changes")
        if self.output_format == DataFormat.CSV and (
            (self.input_schema.allow_extra and not self.project_fields)
            or any(not f.required and not f.nullable for f in self.input_schema.fields.values())
        ):
            raise ValueError("CSV delivery requires declared columns and nullable optional fields")
        return self


class DataApproval(Contract):
    approval_id: UUID
    approved_by: DataText
    source_id: UUID
    source_sha256: SHA256
    specification_sha256: SHA256


class DataPipelineVersion(Contract):
    version_id: UUID = Field(default_factory=uuid4)
    pipeline_id: UUID
    version: Text
    parent_version_id: UUID | None = None
    specification_sha256: SHA256
    source_sha256: SHA256
    output_sha256: SHA256
    changes: tuple[Text, ...] = ()


class DataValidationResult(Contract):
    validation_id: UUID = Field(default_factory=uuid4)
    pipeline_id: UUID
    source_sha256: SHA256
    schema_sha256: SHA256
    status: ValidationStatus
    valid_records: int = Field(ge=0, strict=True)
    invalid_records: int = Field(ge=0, strict=True)
    errors: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def evidence(self) -> Self:
        if self.status == ValidationStatus.PASS and not self.valid_records:
            raise ValueError("Empty data is not successful validation")
        if self.status == ValidationStatus.FAIL and not self.errors:
            raise ValueError("Failed validation requires error codes")
        return self


class DataDeliveryMetrics(Contract):
    run_id: UUID
    records: int = Field(ge=0, strict=True)
    bytes_delivered: int = Field(ge=0, strict=True)
    buffer_peak_records: int = Field(ge=0, strict=True)
    buffer_peak_bytes: int = Field(ge=0, strict=True)
    metrics: tuple[Metric, ...]
    consumer_latency_seconds: Fact[float] = Field(default_factory=Fact)

    @model_validator(mode="after")
    def metric_runs(self) -> Self:
        if any(metric.run_id != self.run_id for metric in self.metrics):
            raise ValueError("Data metrics must belong to their pipeline execution")
        return self


class ReplaySpecification(Contract):
    pipeline_version_id: UUID
    output_sha256: SHA256
    format: DataFormat
    records: PositiveInteger
    order: Literal["SOURCE_ORDER"] = "SOURCE_ORDER"
    repetitions: PositiveInteger = Field(default=1, le=100)


class DataPipelineBinding(Contract):
    specification: DataPipelineSpecification
    approval: DataApproval
    version: DataPipelineVersion
    validation: DataValidationResult
    delivery_metrics: DataDeliveryMetrics
    replay: ReplaySpecification

    @model_validator(mode="after")
    def linked_versions(self) -> Self:
        from .base import content_hash

        s, v, a, r = self.specification, self.version, self.approval, self.replay
        check = self.validation
        if (
            v.pipeline_id != s.pipeline_id
            or check.pipeline_id != s.pipeline_id
            or v.specification_sha256 != content_hash(s.model_dump(mode="json"))
            or a.specification_sha256 != v.specification_sha256
            or a.source_id != s.source.source_id
            or a.source_sha256 != s.source.sha256
            or v.source_sha256 != s.source.sha256
            or check.source_sha256 != s.source.sha256
            or check.schema_sha256 != content_hash(s.input_schema.model_dump(mode="json"))
            or check.status != ValidationStatus.PASS
            or r.pipeline_version_id != v.version_id
            or r.output_sha256 != v.output_sha256
            or r.format != s.output_format
            or r.records != check.valid_records
            or v.parent_version_id != s.parent_version_id
            or v.version != s.version
            or v.changes != s.changes
            or self.delivery_metrics.records != check.valid_records
            or self.delivery_metrics.buffer_peak_records > s.limits.buffer_records
            or self.delivery_metrics.buffer_peak_bytes > s.limits.buffer_bytes
            or check.valid_records + check.invalid_records > s.limits.max_records
            or (s.error_policy == "REJECT" and check.invalid_records)
        ):
            raise ValueError("Binding must link one approved, validated pipeline version")
        return self
