"""Offline test data; these fixtures are not platform-created Agents."""

import socket
import subprocess
from uuid import uuid4

import pytest

from adaptive_agent_harness.contracts import (
    AgentProfile,
    AgentSource,
    AgentVersion,
    BenchmarkResult,
    DeploymentCapabilities,
    EvidenceKind,
    ExecutionMode,
    ExperimentConditions,
    FunctionalValidation,
    HardwareInfo,
    HostProfile,
    Metric,
    OperationalRequirements,
    PlatformInfo,
    ResourceCapacity,
    RuntimeInfo,
    TargetProfile,
    ValidationStatus,
    known,
)

SOURCE_HASH = "a" * 64


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Tests must not invoke external commands or network connections")

    monkeypatch.setattr(subprocess, "run", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def measured(value):
    return known(value, "fixture: observed", EvidenceKind.MEASURED)


@pytest.fixture
def agent():
    return AgentProfile(
        source=AgentSource(
            root="/fixture/agent", content_sha256=SOURCE_HASH, file_hashes={"app.py": SOURCE_HASH}
        ),
        requirements=OperationalRequirements(
            os=known("Linux", "fixture manifest"),
            architecture=known("x86_64", "fixture manifest"),
            python_specifier=known(">=3.12,<3.13", "fixture manifest"),
            min_cpu_cores=known(2, "fixture manifest"),
            min_ram_bytes=known(2**30, "fixture manifest"),
            gpu_required=known(False, "fixture manifest"),
            cuda_required=known(False, "fixture manifest"),
            execution_mode=known(ExecutionMode.PYTHON, "fixture manifest"),
        ),
    )


@pytest.fixture
def host():
    return HostProfile(
        hardware=HardwareInfo(
            platform=PlatformInfo(os=measured("Linux"), architecture=measured("x86_64")),
            resources=ResourceCapacity(cpu_cores=measured(8), ram_bytes=measured(8 * 2**30)),
            gpu_present=measured(True),
        ),
        runtimes=(
            RuntimeInfo(name="python", version=measured("3.12.14"), available=measured(True)),
        ),
        deployment=DeploymentCapabilities(python=measured(True)),
    )


@pytest.fixture
def target(host):
    return TargetProfile(
        name="test physical target",
        hardware=host.hardware,
        runtimes=host.runtimes,
        deployment=host.deployment,
    )


@pytest.fixture
def agent_version(agent):
    return AgentVersion(
        content_sha256=agent.source.content_sha256, original_sha256=agent.source.content_sha256
    )


@pytest.fixture
def benchmark(agent, host, target):
    run_id = uuid4()
    return BenchmarkResult(
        run_id=run_id,
        agent_id=agent.agent_id,
        agent_source_sha256=agent.source.content_sha256,
        harness_id=uuid4(),
        harness_sha256="b" * 64,
        host=host,
        target=target,
        conditions=ExperimentConditions(
            input_sha256="c" * 64,
            evaluator_sha256="d" * 64,
            seed=42,
            repetition=1,
            execution_mode=ExecutionMode.PYTHON,
        ),
        execution_evidence=EvidenceKind.MEASURED,
        metrics=(
            Metric(
                name="elapsed",
                value=1.5,
                unit="s",
                evidence=EvidenceKind.MEASURED,
                source="fixture clock",
                run_id=run_id,
            ),
        ),
        functional_validation=FunctionalValidation(
            status=ValidationStatus.PASS,
            test_result_references=("fixture:test-report",),
            quality_degraded=measured(False),
            compared_to_benchmark_id=uuid4(),
        ),
    )
