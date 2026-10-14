from uuid import uuid4

from adaptive_agent_harness.contracts import EvidenceKind, known
from adaptive_agent_harness.contracts.simulator import (
    SimulatorCapability,
    SimulatorOperation,
    SimulatorRequirement,
    SimulatorResponse,
    assess_simulator,
)
from adaptive_agent_harness.porting.simulator import SimulatorAdapter


class MockSimulator(SimulatorAdapter):
    def _perform(self, operation, payload):
        return SimulatorResponse(
            operation=operation,
            status="OK",
            evidence=EvidenceKind.SIMULATED,
            payload={"connected": True}
            if operation == SimulatorOperation.CONNECT
            else {"sensor": 42},
        )


def test_optional_operations_and_mock_evidence():
    capability = SimulatorCapability(
        simulator_name="mock",
        adapter_version="1",
        operations={
            SimulatorOperation.CONNECT: known(True, "mock contract", EvidenceKind.SIMULATED),
            SimulatorOperation.OBSERVE: known(True, "mock contract", EvidenceKind.SIMULATED),
            SimulatorOperation.RESET: known(False, "mock lacks reset"),
        },
    )
    adapter = MockSimulator(capability)
    assert adapter.connect().status == "OK"
    assert adapter.observe().payload == {"sensor": 42}
    assert adapter.observe().evidence == "SIMULATED"
    assert adapter.reset().status == "UNSUPPORTED"
    for response in (
        adapter.disconnect(),
        adapter.step(),
        adapter.send_action({"command": 1}),
        adapter.get_status(),
    ):
        assert response.status == "UNKNOWN"
        assert response.evidence == "UNVERIFIED"
    assert SimulatorCapability.model_validate_json(capability.model_dump_json()) == capability


def test_declared_capability_does_not_fabricate_connection():
    adapter = SimulatorAdapter(
        SimulatorCapability(
            simulator_name="future",
            adapter_version="1",
            operations={SimulatorOperation.CONNECT: known(True, "operator declaration")},
        )
    )
    assert adapter.connect().status == "UNSUPPORTED"


def test_requirement_support_unknown_and_failure():
    req = SimulatorRequirement(
        agent_id=uuid4(),
        agent_source_sha256="a" * 64,
        operations={SimulatorOperation.STEP: known(True, "Agent contract")},
    )
    unknown = SimulatorCapability(simulator_name="mock", adapter_version="1")
    assert assess_simulator(req, unknown) == "UNKNOWN"
    no = unknown.model_copy(
        update={"operations": {SimulatorOperation.STEP: known(False, "unsupported")}}
    )
    assert assess_simulator(req, no) == "FAIL"
    yes = unknown.model_copy(
        update={
            "operations": {SimulatorOperation.STEP: known(True, "mock", EvidenceKind.SIMULATED)}
        }
    )
    assert assess_simulator(req, yes) == "PASS"
