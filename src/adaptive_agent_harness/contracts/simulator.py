"""Future simulator boundaries; no simulator connection or hardware emulation."""

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue, StrictBool

from .base import SHA256, Contract, EvidenceKind, Fact, Text, content_hash
from .data import DataSchema
from .incubation import ValidationStatus


class SimulatorOperation(StrEnum):
    CONNECT = "connect"
    DISCONNECT = "disconnect"
    RESET = "reset"
    STEP = "step"
    OBSERVE = "observe"
    SEND_ACTION = "send_action"
    GET_STATUS = "get_status"


class SimulatorRequirement(Contract):
    agent_id: UUID
    agent_source_sha256: SHA256
    simulator_name: Fact[Text] = Field(default_factory=Fact)
    operations: dict[SimulatorOperation, Fact[StrictBool]] = Field(default_factory=dict)
    sensor_names: Fact[tuple[Text, ...]] = Field(default_factory=Fact)
    state_schema: DataSchema | None = None
    action_schema: DataSchema | None = None
    configuration_reference: Text | None = None


class SimulatorCapability(Contract):
    simulator_name: Text
    adapter_version: Text
    operations: dict[SimulatorOperation, Fact[StrictBool]] = Field(default_factory=dict)
    sensor_schema: DataSchema | None = None
    state_schema: DataSchema | None = None
    action_schema: DataSchema | None = None

    def support(self, operation: SimulatorOperation) -> Fact[StrictBool]:
        return self.operations.get(operation, Fact[StrictBool]())


class SimulatorResponse(Contract):
    operation: SimulatorOperation
    status: Literal["OK", "UNSUPPORTED", "UNKNOWN", "ERROR"]
    evidence: EvidenceKind = EvidenceKind.UNVERIFIED
    error_code: Text | None = None
    payload: dict[Text, JsonValue] = Field(default_factory=dict, max_length=128)


def assess_simulator(
    requirement: SimulatorRequirement, capability: SimulatorCapability
) -> ValidationStatus:
    """Absent operation support remains UNKNOWN; an explicit required mismatch fails."""
    if not requirement.operations:
        return ValidationStatus.UNKNOWN
    unknown = False
    for operation, required in requirement.operations.items():
        if required.value is None:
            unknown = True
        elif required.value:
            supported = capability.support(operation)
            if supported.value is False:
                return ValidationStatus.FAIL
            if supported.value is None:
                unknown = True
    # Names and schema compatibility need an explicit Porting Manager check.
    if (
        requirement.simulator_name.value is not None
        and requirement.simulator_name.value != capability.simulator_name
    ):
        return ValidationStatus.FAIL
    for name in ("state_schema", "action_schema"):
        expected = getattr(requirement, name)
        actual = getattr(capability, name)
        if expected is not None:
            if actual is None:
                unknown = True
            elif content_hash(expected.model_dump(mode="json")) != content_hash(
                actual.model_dump(mode="json")
            ):
                return ValidationStatus.FAIL
    if requirement.sensor_names.value is not None:
        if capability.sensor_schema is None:
            unknown = True
        elif not set(requirement.sensor_names.value).issubset(capability.sensor_schema.fields):
            return ValidationStatus.FAIL
    return ValidationStatus.UNKNOWN if unknown else ValidationStatus.PASS
