"""Versioned, immutable boundary data with explicit uncertainty and provenance."""

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$", strict=True)]
PositiveInteger = Annotated[int, Field(gt=0, strict=True)]


def content_hash(value: object) -> str:
    """Semantic contract hash, retaining business fields named created_at."""

    def semantic(item: object) -> object:
        if isinstance(item, dict):
            metadata = isinstance(item.get("schema_version"), str)
            return {k: semantic(v) for k, v in item.items() if k != "created_at" or not metadata}
        if isinstance(item, (tuple, list)):
            return [semantic(v) for v in item]
        return item

    raw = (
        json.dumps(semantic(value), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode()
    return hashlib.sha256(raw).hexdigest()


class EvidenceKind(StrEnum):
    MEASURED = "MEASURED"
    STATIC = "STATIC"
    DECLARED = "DECLARED"
    SIMULATED = "SIMULATED"
    ESTIMATED = "ESTIMATED"
    UNVERIFIED = "UNVERIFIED"


class Knowledge(StrEnum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )

    schema_version: Literal["1.0"] = "1.0"
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class Fact[T](Contract):
    status: Knowledge = Knowledge.UNKNOWN
    value: T | None = None
    source: Text = "Not observed"
    evidence: EvidenceKind = EvidenceKind.UNVERIFIED

    @model_validator(mode="after")
    def consistent_observation(self) -> Self:
        if self.status == Knowledge.UNKNOWN:
            if self.value is not None or self.evidence != EvidenceKind.UNVERIFIED:
                raise ValueError("UNKNOWN requires a null value and UNVERIFIED evidence")
        elif self.value is None or self.evidence == EvidenceKind.UNVERIFIED:
            raise ValueError("KNOWN requires a value and identified evidence")
        return self


def known[T](value: T, source: str, evidence: EvidenceKind = EvidenceKind.DECLARED) -> Fact[T]:
    return Fact(status=Knowledge.KNOWN, value=value, source=source, evidence=evidence)
