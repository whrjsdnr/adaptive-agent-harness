"""Explicit opt-in tests: daemon failure is a failure, never a skip or simulated pass."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from adaptive_agent_harness.analyzer import AgentAnalyzer, CompatibilityEngine, TargetProfiler
from adaptive_agent_harness.contracts import (
    BaseImage,
    ContainerHarnessRequest,
    ContainerPlatform,
    DependencyInstallation,
    OperationalRequirements,
    ResourceLimits,
    TargetProfile,
    known,
)
from adaptive_agent_harness.contracts.runtime import BaselinePlan, BaselineResult, ExecutionApproval
from adaptive_agent_harness.harness import ContainerHarnessBuilder, HarnessSpecificationGenerator
from adaptive_agent_harness.orchestration.baseline import BaselineRunner
from adaptive_agent_harness.runtime.docker import DockerCLI
from adaptive_agent_harness.storage.baseline import JsonResultStore

pytestmark = pytest.mark.docker


@pytest.fixture
def docker():
    runtime = DockerCLI()
    runtime.preflight()  # Explicit opt-in with unavailable Docker must fail.
    return runtime


@pytest.fixture
def execute(tmp_path, docker):
    host = TargetProfiler().profile_host()
    agent = AgentAnalyzer().analyze(
        Path(__file__).parents[1] / "fixtures/reference_agent",
        operational_requirements=OperationalRequirements(
            python_specifier=known(">=3.12,<3.13", "fixture requirement")
        ),
    )
    source_before = (Path(agent.source.root) / "app.py").read_bytes()
    target = TargetProfile(
        name="local fixture target",
        hardware=host.hardware,
        runtimes=host.runtimes,
        deployment=host.deployment,
    )
    report = CompatibilityEngine().assess(agent, target)
    platforms = {"x86_64": ContainerPlatform.LINUX_AMD64, "aarch64": ContainerPlatform.LINUX_ARM64}

    def run(mode, *, repetitions=1, expected="ok\n", image="python:3.12-slim"):
        request = ContainerHarnessRequest(
            image=BaseImage(
                reference=image,
                platforms=(platforms[host.hardware.platform.architecture.value],),
                python_version=known("3.12", "fixture image requirement"),
            ),
            argv=("python", "/opt/agent/app.py", mode),
            working_directory=known("/opt/agent", "fixture working directory"),
            resource_limits=ResourceLimits(
                cpu_cores=2, ram_bytes=64 * 1024**2 if mode == "oom" else 2**30
            ),
            network_required=known(False, "fixture requires no network"),
            source_files=agent.source.file_hashes,
            source_complete=known(True, "reviewed repository-owned fixture"),
            installation=DependencyInstallation(mode="NONE"),
        )
        spec = HarnessSpecificationGenerator().generate(agent, target, host, report, request)
        package = ContainerHarnessBuilder().build(spec, agent, target, host, output_root=tmp_path)
        approval = ExecutionApproval(
            approval_id=uuid4(),
            approved_by="operator --run-docker",
            mode="TRUSTED_LOCAL_TRIAL",
            agent_source_sha256=agent.source.content_sha256,
            source_bundle_sha256=package.manifest.source_bundle_sha256,
            specification_sha256=package.manifest.specification_sha256,
            manifest_sha256=hashlib.sha256(
                (package.directory / "manifest.json").read_bytes()
            ).hexdigest(),
            reviewed_unknowns=spec.unknowns,
        )
        results = BaselineRunner(docker, JsonResultStore(tmp_path)).run(
            package.directory,
            approval=approval,
            agent=agent,
            host=host,
            target=target,
            plan=BaselinePlan(
                input_sha256=hashlib.sha256(mode.encode()).hexdigest(),
                repetitions=repetitions,
                timeout_seconds=1 if mode == "timeout" else 30,
                expected_stdout=expected,
                log_limit_bytes=4096,
            ),
        )
        for result in results:
            saved = tmp_path / f"{result.runtime.run_id}.json"
            assert BaselineResult.model_validate_json(saved.read_bytes()) == result
            assert result.runtime.cleanup_errors == ()
        assert (Path(agent.source.root) / "app.py").read_bytes() == source_before
        return results

    return run


def test_real_normal_repeated(execute):
    results = execute("normal", repetitions=3)
    for result in results:
        assert result.runtime.error_code is None, result.runtime.error_cause
        assert result.runtime.exit_code.value == 0
        assert result.runtime.limits_verified.value is True
        assert result.runtime.cleanup_verified
        assert result.runtime.wall_seconds >= 4
        assert result.runtime.cpu_percent_samples
        assert result.runtime.memory_bytes_samples
        assert result.benchmark.functional_validation.status == "PASS"


@pytest.mark.parametrize(
    "mode,code", [("nonzero", "NONZERO_EXIT"), ("timeout", "TIMEOUT"), ("oom", "OOM")]
)
def test_real_failure_scenarios(execute, mode, code):
    (result,) = execute(mode)
    assert result.runtime.error_code == code, result.runtime.error_cause
    assert result.runtime.cleanup_verified
    assert result.benchmark.functional_validation.status == "FAIL"
    if mode == "oom":
        assert result.runtime.oom_killed.value is True
    if mode == "timeout":
        assert result.runtime.timed_out
        assert result.runtime.exit_code.value == 137


def test_real_functional_failure(execute):
    (result,) = execute("wrong")
    assert result.runtime.exit_code.value == 0
    assert result.benchmark.functional_validation.status == "FAIL"


def test_real_limits_network_and_privileges(execute):
    (result,) = execute("policy", expected=None)
    assert result.runtime.exit_code.value == 0, result.runtime.stderr
    limits = json.loads(result.runtime.stdout)
    quota, period = map(int, limits["cpu"].split())
    assert quota / period == 2
    assert int(limits["memory"]) == 2**30


def test_real_log_bounds(execute):
    (result,) = execute("logs")
    assert len(result.runtime.stdout.encode()) <= 4096
    assert len(result.runtime.stderr.encode()) <= 4096
    assert result.runtime.logs_truncated


def test_real_build_failure(execute):
    (result,) = execute("normal", image="python:phase4-intentionally-nonexistent-tag")
    assert result.runtime.error_code is not None
    assert result.runtime.image_id is None
    assert result.runtime.started_at is None
    assert result.benchmark.execution_evidence == "UNVERIFIED"
