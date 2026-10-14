"""Pure binding checks shared by packaging and approved execution."""

import io

from adaptive_agent_harness.contracts.data import DataPipelineBinding, DataSchema
from adaptive_agent_harness.contracts.harness import ContainerHarnessSpecification
from adaptive_agent_harness.data.local import DataFailure, records, validate_record

from ._utils import HarnessError, content_hash


def validate_payload(payload: bytes, binding: DataPipelineBinding) -> None:
    if len(payload) != binding.delivery_metrics.bytes_delivered:
        raise HarnessError("DATA_SIZE_MISMATCH", "Delivered bytes differ from pipeline metrics")
    s = binding.specification
    schema = DataSchema(
        fields={
            k: v
            for k, v in s.input_schema.fields.items()
            if not s.project_fields or k in s.project_fields
        },
        allow_extra=s.input_schema.allow_extra if not s.project_fields else False,
    )
    count = 0
    try:
        for record in records(io.BytesIO(payload), s.output_format, s.limits):
            validate_record(record, schema, csv_input=s.output_format.value == "CSV")
            count += 1
            if count > s.limits.max_records:
                raise DataFailure("RECORD_COUNT_LIMIT")
    except DataFailure as exc:
        raise HarnessError("DATA_VALIDATION_FAILED", exc.code) from None
    if count != binding.validation.valid_records:
        raise HarnessError("DATA_COUNT_MISMATCH", "Delivered records differ from validation")


def check_data(spec: ContainerHarnessSpecification, binding: DataPipelineBinding | None) -> None:
    req = spec.data_requirement
    if req is None:
        if binding is not None:
            raise HarnessError("DATA_REQUIREMENT_MISSING", "Declare the Agent data contract first")
        return
    if req.agent_id != spec.agent_id or req.agent_source_sha256 != spec.agent_source_sha256:
        raise HarnessError("DATA_AGENT_MISMATCH", "Data requirement belongs to another Agent")
    if req.requires_data.value is None:
        raise HarnessError("DATA_REQUIREMENT_UNKNOWN", "Confirm whether input data is required")
    if req.requires_data.value is False:
        if binding is not None:
            raise HarnessError("DATA_NOT_REQUESTED", "The contract declares no data input")
        return
    if binding is None:
        raise HarnessError("DATA_REQUIRED", "A validated, approved data pipeline is required")
    pipeline = binding.specification
    output_schema = DataSchema(
        fields={
            k: v
            for k, v in pipeline.input_schema.fields.items()
            if not pipeline.project_fields or k in pipeline.project_fields
        },
        allow_extra=pipeline.input_schema.allow_extra if not pipeline.project_fields else False,
    )
    if (
        pipeline.agent_id != spec.agent_id
        or pipeline.agent_source_sha256 != spec.agent_source_sha256
        or req.input_schema is None
        or req.formats.value is None
        or binding.replay.format not in req.formats.value
        or content_hash(req.input_schema.model_dump(mode="json"))
        != content_hash(output_schema.model_dump(mode="json"))
    ):
        raise HarnessError(
            "DATA_CONTRACT_MISMATCH", "Confirm input format, schema and Agent version"
        )
    if req.supply_period_seconds.value not in (None, 0) or req.credential_references:
        raise HarnessError("DATA_DELIVERY_UNSUPPORTED", "Only local batch files are supported")
    if req.pipeline_specification_sha256 != binding.version.specification_sha256:
        raise HarnessError(
            "DATA_PIPELINE_VERSION_MISMATCH",
            "Pin the approved pipeline specification in the Harness request",
        )
    if pipeline.delivery == "FILE_ARG":
        index = pipeline.argument_index
        if index is None or index >= len(spec.argv) or spec.argv[index] != pipeline.container_path:
            raise HarnessError("DATA_ARGUMENT_MISMATCH", "Declare the approved data path in argv")
    elif pipeline.environment_name in spec.environment_references:
        raise HarnessError("DATA_ENV_CONFLICT", "Data path cannot override a supplied environment")
