"""Porting Manager adapter boundary. All operations are optional and capability-gated."""

from pydantic import JsonValue

from adaptive_agent_harness.contracts.simulator import (
    SimulatorCapability,
    SimulatorOperation,
    SimulatorResponse,
)


class SimulatorAdapter:
    def __init__(self, capability: SimulatorCapability):
        self.capability = SimulatorCapability.model_validate_json(capability.model_dump_json())

    def invoke(
        self, operation: SimulatorOperation, payload: dict[str, JsonValue] | None = None
    ) -> SimulatorResponse:
        operation = SimulatorOperation(operation)
        supported = self.capability.support(operation)
        if supported.value is None:
            return SimulatorResponse(
                operation=operation, status="UNKNOWN", error_code="CAPABILITY_UNKNOWN"
            )
        if supported.value is False:
            return SimulatorResponse(
                operation=operation, status="UNSUPPORTED", error_code="OPERATION_UNSUPPORTED"
            )
        return self._perform(operation, payload or {})

    def _perform(
        self, operation: SimulatorOperation, payload: dict[str, JsonValue]
    ) -> SimulatorResponse:
        return SimulatorResponse(
            operation=operation, status="UNSUPPORTED", error_code="ADAPTER_NOT_IMPLEMENTED"
        )

    def connect(self) -> SimulatorResponse:
        return self.invoke(SimulatorOperation.CONNECT)

    def disconnect(self) -> SimulatorResponse:
        return self.invoke(SimulatorOperation.DISCONNECT)

    def reset(self) -> SimulatorResponse:
        return self.invoke(SimulatorOperation.RESET)

    def step(self) -> SimulatorResponse:
        return self.invoke(SimulatorOperation.STEP)

    def observe(self) -> SimulatorResponse:
        return self.invoke(SimulatorOperation.OBSERVE)

    def send_action(self, action: dict[str, JsonValue]) -> SimulatorResponse:
        return self.invoke(SimulatorOperation.SEND_ACTION, action)

    def get_status(self) -> SimulatorResponse:
        return self.invoke(SimulatorOperation.GET_STATUS)
