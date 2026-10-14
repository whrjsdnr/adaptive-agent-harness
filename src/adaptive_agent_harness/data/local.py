"""Bounded, approved file preparation and deterministic replay; never execute an Agent."""

import csv
import ctypes
import errno
import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol
from uuid import uuid4

from adaptive_agent_harness.contracts import EvidenceKind, Metric, ValidationStatus
from adaptive_agent_harness.contracts.base import content_hash
from adaptive_agent_harness.contracts.data import (
    DataApproval,
    DataDeliveryMetrics,
    DataFormat,
    DataPipelineBinding,
    DataPipelineSpecification,
    DataPipelineVersion,
    DataSchema,
    DataValidationResult,
    PipelineLimits,
    ReplaySpecification,
    safe_relative,
)
from adaptive_agent_harness.contracts.harness import reject_credentials


class DataFailure(ValueError):
    """Only safe error codes; never echo a record, schema value, or secret."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def publish_directory(root_fd: int, stage: str, name: str) -> None:
    """Linux atomic no-replace publication, including existing empty directories."""
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameat2", None)
    if rename is None:
        raise DataFailure("ATOMIC_PUBLICATION_UNSUPPORTED")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(root_fd, stage.encode(), root_fd, name.encode(), 1) != 0:
        code = ctypes.get_errno()
        raise DataFailure("OUTPUT_EXISTS" if code == errno.EEXIST else "PUBLICATION_FAILED")


class DataSourceAdapter(Protocol):
    def prepare(
        self, specification: DataPipelineSpecification, *, approval: DataApproval, output_root: Path
    ) -> "PreparedData": ...


@contextmanager
def directory_fd(path: Path) -> Iterator[int]:
    """Open every absolute component with NOFOLLOW, including the approved root."""
    path = path.absolute()
    if ".." in path.parts or os.name != "posix":
        raise DataFailure("UNSAFE_PATH_OR_PLATFORM")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            for part in path.parts[1:]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
        except OSError:
            raise DataFailure("UNSAFE_PATH") from None
        yield fd
    finally:
        os.close(fd)


@contextmanager
def regular_file(root: Path, relative: str) -> Iterator[BinaryIO]:
    safe_relative(relative)
    with directory_fd(root) as root_fd:
        parent = os.dup(root_fd)
        try:
            try:
                parts = Path(relative).parts
                for part in parts[:-1]:
                    next_fd = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                    )
                    os.close(parent)
                    parent = next_fd
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except OSError:
                raise DataFailure("UNSAFE_OR_MISSING_DATA") from None
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise DataFailure("NOT_REGULAR_FILE")
                yield stream
        finally:
            os.close(parent)


def copy_verified(source: BinaryIO, target: BinaryIO, digest: str, limit: int) -> int:
    before = os.fstat(source.fileno())
    if before.st_size > limit:
        raise DataFailure("SOURCE_SIZE_LIMIT")
    hashed, size = hashlib.sha256(), 0
    while chunk := source.read(min(64 * 1024, limit + 1 - size)):
        size += len(chunk)
        if size > limit:
            raise DataFailure("SOURCE_SIZE_LIMIT")
        hashed.update(chunk)
        target.write(chunk)
    after = os.fstat(source.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) or hashed.hexdigest() != digest:
        raise DataFailure("DATA_VERSION_MISMATCH")
    target.seek(0)
    return size


def strict_json(text: str) -> object:
    def finite(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("Nonfinite JSON number")
        return parsed

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    return json.loads(
        text,
        object_pairs_hook=pairs,
        parse_float=finite,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )


def records(stream: BinaryIO, format: DataFormat, limits: PipelineLimits) -> Iterator[object]:
    """JSON is a top-level array of records; bound a record, never load the whole array."""
    text = io.TextIOWrapper(stream, encoding="utf-8", newline="")
    try:
        if format in (DataFormat.JSONL, DataFormat.CSV):
            row_bytes = 0

            def lines():
                nonlocal row_bytes
                while line := text.readline(limits.max_record_bytes + 1):
                    row_bytes += len(line.encode())
                    if len(line.encode()) > limits.max_record_bytes or (
                        format == DataFormat.CSV and row_bytes > limits.max_record_bytes
                    ):
                        raise DataFailure("RECORD_SIZE_LIMIT")
                    yield line

            if format == DataFormat.JSONL:
                for line in lines():
                    try:
                        yield strict_json(line)
                    except (ValueError, RecursionError):
                        yield DataFailure("MALFORMED_RECORD")
            else:
                reader = csv.reader(lines(), strict=True)
                try:
                    header = next(reader, [])
                    if not header or len(set(header)) != len(header):
                        raise DataFailure("INVALID_CSV_HEADER")
                    row_bytes = 0
                    for row in reader:
                        if sum(len(value.encode()) for value in row) > limits.max_record_bytes:
                            raise DataFailure("RECORD_SIZE_LIMIT")
                        yield (
                            dict(zip(header, row, strict=True))
                            if len(row) == len(header)
                            else DataFailure("CSV_COLUMNS")
                        )
                        row_bytes = 0
                except csv.Error as exc:
                    code = (
                        "CSV_FIELD_LIMIT"
                        if "field larger than field limit" in str(exc)
                        else "MALFORMED_CSV"
                    )
                    raise DataFailure(code) from None
        else:
            buffer, ended = "", False

            def fill():
                nonlocal buffer, ended
                chunk = text.read(min(4096, limits.max_record_bytes))
                ended = not chunk
                buffer += chunk

            def whitespace():
                nonlocal buffer
                buffer = buffer.lstrip()
                while not buffer and not ended:
                    fill()
                    buffer = buffer.lstrip()

            whitespace()
            if buffer.startswith("{"):
                decoder = json.JSONDecoder()
                while True:
                    try:
                        _, end = decoder.raw_decode(buffer)
                    except (json.JSONDecodeError, RecursionError):
                        if ended or len(buffer.encode()) > limits.max_record_bytes:
                            raise DataFailure("MALFORMED_OR_OVERSIZED_JSON_RECORD") from None
                        fill()
                        continue
                    raw, buffer = buffer[:end], buffer[end:]
                    if len(raw.encode()) > limits.max_record_bytes:
                        raise DataFailure("RECORD_SIZE_LIMIT")
                    try:
                        yield strict_json(raw)
                    except (ValueError, RecursionError):
                        raise DataFailure("MALFORMED_JSON") from None
                    whitespace()
                    if buffer:
                        raise DataFailure("JSON_TRAILING_DATA")
                    return
            if not buffer.startswith("["):
                raise DataFailure("JSON_OBJECT_OR_ARRAY_REQUIRED")
            buffer = buffer[1:]
            first = True
            decoder = json.JSONDecoder()
            while True:
                whitespace()
                if buffer.startswith("]"):
                    if not first:
                        raise DataFailure("JSON_TRAILING_COMMA")
                    buffer = buffer[1:]
                    break
                while True:
                    try:
                        _, end = decoder.raw_decode(buffer)
                        raw = buffer[:end]
                        if len(raw.encode()) > limits.max_record_bytes:
                            raise DataFailure("RECORD_SIZE_LIMIT") from None
                        try:
                            yield strict_json(raw)
                        except (ValueError, RecursionError):
                            yield DataFailure("MALFORMED_RECORD")
                        buffer = buffer[end:]
                        break
                    except (json.JSONDecodeError, RecursionError):
                        if ended:
                            raise DataFailure("MALFORMED_JSON") from None
                        if len(buffer.encode()) > limits.max_record_bytes:
                            raise DataFailure("RECORD_SIZE_LIMIT") from None
                        fill()
                whitespace()
                if buffer.startswith("]"):
                    buffer = buffer[1:]
                    break
                if not buffer.startswith(","):
                    raise DataFailure("JSON_SEPARATOR_REQUIRED")
                buffer = buffer[1:]
                first = False
            whitespace()
            if buffer:
                raise DataFailure("JSON_TRAILING_DATA")
    except UnicodeError:
        raise DataFailure("INVALID_UTF8") from None
    finally:
        text.detach()


def validate_record(record: object, schema: DataSchema, *, csv_input: bool) -> dict:
    if isinstance(record, DataFailure):
        raise record
    if not isinstance(record, dict):
        raise DataFailure("OBJECT_REQUIRED")
    if not schema.allow_extra and set(record) - set(schema.fields):
        raise DataFailure("EXTRA_FIELDS")
    output = dict(record)
    for key, field in schema.fields.items():
        if key not in record:
            if field.required:
                raise DataFailure("MISSING_FIELD")
            continue
        value = record[key]
        if csv_input:
            if value == "" and field.nullable:
                value = None
            elif field.type == "integer" and re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value):
                try:
                    value = int(value)
                except ValueError:
                    raise DataFailure("FIELD_TYPE") from None
            elif field.type in ("number", "object", "array", "boolean"):
                try:
                    value = strict_json(value)
                except (ValueError, RecursionError):
                    raise DataFailure("FIELD_TYPE") from None
        if value is None and field.nullable:
            output[key] = value
            continue
        expected = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "object": dict,
            "array": list,
        }[field.type]
        if not isinstance(value, expected) or (
            isinstance(value, bool) and field.type in ("integer", "number")
        ):
            raise DataFailure("FIELD_TYPE")
        output[key] = value
    return output


@dataclass(frozen=True)
class PreparedData:
    directory: Path
    binding: DataPipelineBinding

    @classmethod
    def load(cls, directory: Path) -> "PreparedData":
        with regular_file(directory, "binding.json") as stream:
            raw = stream.read(1024**2 + 1)
        if len(raw) > 1024**2:
            raise DataFailure("METADATA_SIZE_LIMIT")
        binding = DataPipelineBinding.model_validate_json(raw)
        result = cls(directory.absolute(), binding)
        sum(1 for _ in result.replay())
        return result

    @property
    def path(self) -> Path:
        return self.directory / ("input." + self.binding.replay.format.value.lower())

    def replay(self, specification: ReplaySpecification | None = None) -> Iterator[dict]:
        binding = DataPipelineBinding.model_validate_json(self.binding.model_dump_json())
        replay = specification or binding.replay
        replay = ReplaySpecification.model_validate_json(replay.model_dump_json())
        if replay.model_dump(exclude={"created_at", "repetitions"}) != binding.replay.model_dump(
            exclude={"created_at", "repetitions"}
        ):
            raise DataFailure("REPLAY_VERSION_MISMATCH")
        # Freeze to a private descriptor before the first delivered record (TOCTOU boundary).
        with (
            regular_file(self.directory, self.path.name) as source,
            tempfile.TemporaryFile() as frozen,
        ):
            copy_verified(
                source, frozen, replay.output_sha256, binding.specification.limits.max_output_bytes
            )
            for _ in range(replay.repetitions):
                frozen.seek(0)
                count = 0
                for record in records(frozen, replay.format, binding.specification.limits):
                    spec = binding.specification
                    schema = DataSchema(
                        fields={
                            k: v
                            for k, v in spec.input_schema.fields.items()
                            if not spec.project_fields or k in spec.project_fields
                        },
                        allow_extra=spec.input_schema.allow_extra
                        if not spec.project_fields
                        else False,
                    )
                    record = validate_record(
                        record, schema, csv_input=replay.format == DataFormat.CSV
                    )
                    count += 1
                    yield record
                if count != replay.records:
                    raise DataFailure("REPLAY_COUNT_MISMATCH")


class LocalFilePipeline:
    def __init__(self, approved_root: Path):
        self.approved_root = approved_root.absolute()
        with directory_fd(self.approved_root):
            pass

    def prepare(
        self,
        specification: DataPipelineSpecification,
        *,
        approval: DataApproval,
        output_root: Path,
        previous_version: DataPipelineVersion | None = None,
    ) -> PreparedData:
        spec = DataPipelineSpecification.model_validate_json(specification.model_dump_json())
        approval = DataApproval.model_validate_json(approval.model_dump_json())
        spec_hash = content_hash(spec.model_dump(mode="json"))
        if spec.parent_version_id is not None:
            if previous_version is None:
                raise DataFailure("PARENT_VERSION_REQUIRED")
            previous_version = DataPipelineVersion.model_validate_json(
                previous_version.model_dump_json()
            )
            if (
                previous_version.version_id != spec.parent_version_id
                or previous_version.pipeline_id != spec.pipeline_id
            ):
                raise DataFailure("PARENT_VERSION_MISMATCH")
        elif previous_version is not None:
            raise DataFailure("UNLINKED_PARENT_VERSION")
        if (
            approval.source_id != spec.source.source_id
            or approval.source_sha256 != spec.source.sha256
            or approval.specification_sha256 != spec_hash
        ):
            raise DataFailure("DATA_APPROVAL_MISMATCH")
        if spec.source.credential_references:
            raise DataFailure("EXTERNAL_SOURCE_UNSUPPORTED")
        # Only record/byte bounds are enforced here; OS CPU/RAM enforcement is not claimed.
        if spec.resource_limits is not None:
            raise DataFailure("PIPELINE_OS_LIMITS_UNSUPPORTED")
        limits = spec.limits
        run_id, version_id = uuid4(), uuid4()
        output_root = output_root.absolute()
        name = "data-" + str(version_id)
        with directory_fd(output_root) as root_fd:
            stage_name = ".data-" + str(uuid4())
            os.mkdir(stage_name, mode=0o700, dir_fd=root_fd)
            stage_fd = os.open(
                stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd
            )
            # Use the pinned directory for every write, even if its visible path is replaced.
            stage_path = Path(f"/proc/self/fd/{stage_fd}")
            try:
                with (
                    regular_file(self.approved_root, spec.source.path) as source,
                    tempfile.TemporaryFile() as frozen,
                ):
                    start = time.perf_counter()
                    copy_verified(source, frozen, spec.source.sha256, limits.max_source_bytes)
                    valid = invalid = bytes_out = peak_records = peak_bytes = 0
                    delays = 0.0
                    pending: list[bytes] = []
                    pending_bytes = 0
                    fields = spec.project_fields or tuple(spec.input_schema.fields)
                    output_path = stage_path / ("input." + spec.output_format.value.lower())
                    with (
                        output_path.open("xb") as output,
                        (stage_path / "quarantine.jsonl").open("x") as quarantine,
                    ):

                        def write(raw: bytes):
                            nonlocal bytes_out
                            bytes_out += len(raw)
                            if bytes_out > limits.max_output_bytes:
                                raise DataFailure("OUTPUT_SIZE_LIMIT")
                            output.write(raw)

                        if spec.output_format == DataFormat.JSON:
                            write(b"[")
                        elif spec.output_format == DataFormat.CSV:
                            header = io.StringIO(newline="")
                            csv.writer(header).writerow(fields)
                            write(header.getvalue().encode())
                        for number, record in enumerate(
                            records(frozen, spec.source.format, limits), 1
                        ):
                            before = time.perf_counter()
                            if number > limits.max_records:
                                raise DataFailure("RECORD_COUNT_LIMIT")
                            try:
                                record = validate_record(
                                    record,
                                    spec.input_schema,
                                    csv_input=spec.source.format == DataFormat.CSV,
                                )
                                reject_credentials(
                                    json.dumps(record, ensure_ascii=False, allow_nan=False)
                                )
                            except ValueError as exc:
                                invalid += 1
                                code = (
                                    exc.code if isinstance(exc, DataFailure) else "SENSITIVE_DATA"
                                )
                                if spec.error_policy == "REJECT" or code == "SENSITIVE_DATA":
                                    raise DataFailure(code) from None
                                quarantine.write(
                                    json.dumps({"record": number, "code": code}) + "\n"
                                )
                                continue
                            if spec.project_fields:
                                record = {key: record[key] for key in fields if key in record}
                            raw = json.dumps(
                                record,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ).encode()
                            if spec.output_format == DataFormat.JSON:
                                raw = (b"," if valid else b"") + raw
                            elif spec.output_format == DataFormat.CSV:
                                row = io.StringIO(newline="")
                                csv.writer(row).writerow(
                                    ""
                                    if record.get(k) is None
                                    else record.get(k, "")
                                    if isinstance(record.get(k, ""), str)
                                    else json.dumps(record.get(k))
                                    for k in fields
                                )
                                raw = row.getvalue().encode()
                            else:
                                raw += b"\n"
                            if len(raw) > min(limits.max_record_bytes, limits.buffer_bytes):
                                raise DataFailure("RECORD_SIZE_LIMIT")
                            if pending and (
                                len(pending) >= limits.buffer_records
                                or pending_bytes + len(raw) > limits.buffer_bytes
                            ):
                                write(b"".join(pending))
                                pending.clear()
                                pending_bytes = 0
                            pending.append(raw)
                            pending_bytes += len(raw)
                            peak_records = max(peak_records, len(pending))
                            peak_bytes = max(peak_bytes, pending_bytes)
                            valid += 1
                            delays += time.perf_counter() - before
                        write(b"".join(pending))
                        if spec.output_format == DataFormat.JSON:
                            write(b"]\n")
                        output.flush()
                        os.fsync(output.fileno())
                    if not valid:
                        raise DataFailure("EMPTY_VALID_DATA")
                    output_schema = DataSchema(
                        fields={
                            k: v
                            for k, v in spec.input_schema.fields.items()
                            if not spec.project_fields or k in spec.project_fields
                        },
                        allow_extra=spec.input_schema.allow_extra
                        if not spec.project_fields
                        else False,
                    )
                    with output_path.open("rb") as output:
                        delivered = 0
                        for record in records(output, spec.output_format, limits):
                            validate_record(
                                record,
                                output_schema,
                                csv_input=spec.output_format == DataFormat.CSV,
                            )
                            delivered += 1
                        if delivered != valid:
                            raise DataFailure("DELIVERY_COUNT_MISMATCH")
                    duration = time.perf_counter() - start
                    with output_path.open("rb") as output:
                        output_hash = hashlib.file_digest(output, "sha256").hexdigest()
                    os.chmod(output_path, 0o400)
                    os.chmod(stage_path / "quarantine.jsonl", 0o600)
                    binding = DataPipelineBinding(
                        specification=spec,
                        approval=approval,
                        version=DataPipelineVersion(
                            version_id=version_id,
                            pipeline_id=spec.pipeline_id,
                            version=spec.version,
                            parent_version_id=spec.parent_version_id,
                            changes=spec.changes,
                            specification_sha256=spec_hash,
                            source_sha256=spec.source.sha256,
                            output_sha256=output_hash,
                        ),
                        validation=DataValidationResult(
                            pipeline_id=spec.pipeline_id,
                            source_sha256=spec.source.sha256,
                            schema_sha256=content_hash(spec.input_schema.model_dump(mode="json")),
                            status=ValidationStatus.PASS,
                            valid_records=valid,
                            invalid_records=invalid,
                        ),
                        delivery_metrics=DataDeliveryMetrics(
                            run_id=run_id,
                            records=valid,
                            bytes_delivered=bytes_out,
                            buffer_peak_records=peak_records,
                            buffer_peak_bytes=peak_bytes,
                            metrics=tuple(
                                Metric(
                                    name=k,
                                    value=v,
                                    unit=u,
                                    source="LocalFilePipeline preparation v1",
                                    run_id=run_id,
                                    evidence=EvidenceKind.MEASURED
                                    if v is not None
                                    else EvidenceKind.UNVERIFIED,
                                )
                                for k, v, u in (
                                    ("data_supply_seconds", duration, "s"),
                                    (
                                        "data_records_per_second",
                                        valid / duration if duration else None,
                                        "records/s",
                                    ),
                                    ("data_record_delay_mean", delays / valid, "s/record"),
                                )
                            ),
                        ),
                        replay=ReplaySpecification(
                            pipeline_version_id=version_id,
                            output_sha256=output_hash,
                            format=spec.output_format,
                            records=valid,
                        ),
                    )
                    (stage_path / "binding.json").write_text(binding.model_dump_json(indent=2))
                    os.chmod(stage_path / "binding.json", 0o600)
                # Private staging is published only after validation and serialization finish.
                with directory_fd(output_root) as current_fd:
                    old, current = os.fstat(root_fd), os.fstat(current_fd)
                    if (old.st_dev, old.st_ino) != (current.st_dev, current.st_ino):
                        raise DataFailure("UNSAFE_OUTPUT_REPLACEMENT")
                publish_directory(root_fd, stage_name, name)
                return PreparedData(output_root / name, binding)
            except Exception:
                shutil.rmtree(stage_name, dir_fd=root_fd)
                raise
            finally:
                os.close(stage_fd)
