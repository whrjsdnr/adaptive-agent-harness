import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from adaptive_agent_harness.contracts.runtime import BaselinePlan, BaselineResult, ExecutionApproval
from adaptive_agent_harness.harness import ContainerHarnessBuilder, HarnessSpecificationGenerator
from adaptive_agent_harness.orchestration.baseline import BaselineRunner
from adaptive_agent_harness.runtime.docker import CommandResult, DockerCLI, RuntimeFailure
from adaptive_agent_harness.storage.baseline import JsonResultStore


class FakeDocker:
    """Lifecycle fault injection only; never evidence of a real Docker execution."""

    def __init__(self, failure=None, exit_code=0, oom=False, logs=b"ok\n"):
        self.failure, self.exit_code, self.oom, self.output = failure, exit_code, oom, logs
        self.calls = []

    def step(self, name):
        self.calls.append(name)
        if self.failure == name:
            raise RuntimeFailure("INJECTED_FAILURE", name)

    def preflight(self):
        self.step("preflight")
        return {"Architecture": "x86_64"}

    def build(self, *args):
        self.step("build")
        return "sha256:" + "d" * 64

    def create(self, *args):
        self.step("create")
        return "container-id"

    def verify_policy(self, *args):
        self.step("policy")

    def start(self, *args):
        self.step("start")

    def wait(self, *args):
        self.step("wait")
        if self.failure == "timeout":
            raise RuntimeFailure("COMMAND_TIMEOUT", "injected timeout")

    def stop(self, *args):
        self.step("stop")

    def inspect(self, *args):
        self.step("inspect")
        now = datetime.now(UTC)
        if "start" not in self.calls or self.failure == "start":
            return {
                "State": {
                    "Status": "created",
                    "Running": False,
                    "ExitCode": 0,
                    "OOMKilled": False,
                    "StartedAt": "0001-01-01T00:00:00Z",
                    "FinishedAt": "0001-01-01T00:00:00Z",
                }
            }
        return {
            "State": {
                "Status": "exited",
                "Running": False,
                "ExitCode": self.exit_code,
                "OOMKilled": self.oom,
                "StartedAt": (now - timedelta(seconds=1)).isoformat(),
                "FinishedAt": now.isoformat(),
            }
        }

    def stats(self, *args):
        self.step("stats")
        return 75.0, 1024.0

    def logs(self, name, limit):
        self.step("logs")
        return CommandResult(self.output[:limit], b"", len(self.output) > limit)

    def remove(self, *args):
        self.step("remove")

    def remove_image(self, *args):
        self.step("remove_image")


@pytest.fixture
def run_case(container_inputs, tmp_path):
    agent, target, host, report, request = container_inputs
    spec = HarnessSpecificationGenerator().generate(agent, target, host, report, request)
    package = ContainerHarnessBuilder().build(spec, agent, target, host, output_root=tmp_path)
    approval = ExecutionApproval(
        approval_id=uuid4(),
        approved_by="unit test",
        mode="TRUSTED_LOCAL_TRIAL",
        agent_source_sha256=agent.source.content_sha256,
        source_bundle_sha256=package.manifest.source_bundle_sha256,
        specification_sha256=package.manifest.specification_sha256,
        manifest_sha256=hashlib.sha256(
            (package.directory / "manifest.json").read_bytes()
        ).hexdigest(),
        reviewed_unknowns=spec.unknowns,
    )
    store = JsonResultStore(tmp_path)

    def run(runtime, *, plan=None, approve=None):
        return BaselineRunner(runtime, store).run(
            package.directory,
            approval=approve or approval,
            agent=agent,
            host=host,
            target=target,
            plan=plan
            or BaselinePlan(input_sha256="a" * 64, timeout_seconds=1, expected_stdout="ok\n"),
        )

    return run, package, approval, store


def test_success_version_json_and_cleanup(run_case):
    run, package, approval, store = run_case
    docker = FakeDocker()
    (result,) = run(docker)
    assert result.benchmark.functional_validation.status == "PASS"
    assert result.benchmark.harness_sha256 == approval.specification_sha256
    assert result.runtime.source_bundle_sha256 == package.manifest.source_bundle_sha256
    assert result.runtime.harness_version == "baseline-1"
    assert result.runtime.cleanup_verified
    path = store.directory / f"{result.runtime.run_id}.json"
    assert BaselineResult.model_validate_json(path.read_bytes()) == result
    assert docker.calls.index("policy") < docker.calls.index("start")
    assert docker.calls[-2:] == ["remove", "remove_image"]
    with pytest.raises(FileExistsError):
        store.save(result)


@pytest.mark.parametrize("stage", ["preflight", "build", "create", "policy", "start", "wait"])
def test_failures_preserved(run_case, stage):
    docker = FakeDocker(failure=stage)
    (result,) = run_case[0](docker)
    assert result.runtime.error_code == "INJECTED_FAILURE"
    assert result.benchmark.functional_validation.status == (
        "FAIL" if stage == "wait" else "UNKNOWN"
    )
    if stage in ("create", "policy", "start", "wait"):
        assert "remove" in docker.calls
    if stage in ("preflight", "build", "create", "policy", "start"):
        assert result.benchmark.execution_evidence == "UNVERIFIED"
        assert all(m.value is None for m in result.benchmark.metrics)


@pytest.mark.parametrize("code,oom,expected", [(7, False, "NONZERO_EXIT"), (137, True, "OOM")])
def test_exit_and_oom(run_case, code, oom, expected):
    (result,) = run_case[0](FakeDocker(exit_code=code, oom=oom))
    assert result.runtime.error_code == expected
    assert result.runtime.exit_code.value == code
    assert result.runtime.oom_killed.value is oom


def test_timeout_stops_and_cleans(run_case):
    docker = FakeDocker(failure="timeout")
    (result,) = run_case[0](docker)
    assert result.runtime.timed_out
    assert result.runtime.error_code == "TIMEOUT"
    assert "stop" in docker.calls
    assert result.runtime.cleanup_verified


@pytest.mark.parametrize("expected,status", [(None, "UNKNOWN"), ("wrong", "FAIL")])
def test_exit_zero_is_not_functional_success(run_case, expected, status):
    (result,) = run_case[0](
        FakeDocker(),
        plan=BaselinePlan(input_sha256="a" * 64, timeout_seconds=1, expected_stdout=expected),
    )
    assert result.benchmark.functional_validation.status == status


def test_bounded_logs(run_case):
    (result,) = run_case[0](
        FakeDocker(logs=b"x" * 1000),
        plan=BaselinePlan(
            input_sha256="a" * 64, timeout_seconds=1, log_limit_bytes=10, expected_stdout="x" * 10
        ),
    )
    assert len(result.runtime.stdout) == 10
    assert result.runtime.logs_truncated
    assert result.benchmark.functional_validation.status == "FAIL"


def test_repetitions_and_warmups(run_case):
    results = run_case[0](
        FakeDocker(),
        plan=BaselinePlan(input_sha256="a" * 64, timeout_seconds=1, warmups=1, repetitions=3),
    )
    assert [r.benchmark.conditions.warmup for r in results] == [True, False, False, False]
    assert [r.benchmark.conditions.repetition for r in results] == [1, 1, 2, 3]
    assert len({r.runtime.run_id for r in results}) == 4


def test_cleanup_failure_stops_repetition(run_case):
    results = run_case[0](
        FakeDocker(failure="remove"),
        plan=BaselinePlan(input_sha256="a" * 64, timeout_seconds=1, repetitions=3),
    )
    assert len(results) == 1
    assert results[0].runtime.cleanup_errors
    assert not results[0].runtime.cleanup_verified


def test_approval_and_tampering_block_before_docker(run_case):
    run, package, approval, _ = run_case
    docker = FakeDocker()
    with pytest.raises(RuntimeFailure, match="APPROVAL_MISMATCH"):
        run(docker, approve=approval.model_copy(update={"manifest_sha256": "f" * 64}))
    (package.directory / "Dockerfile").write_text("FROM malicious\n")
    with pytest.raises(Exception, match="integrity|hash|checksum|changed|match"):
        run(docker)
    assert docker.calls == []


def test_docker_policy_and_stats(monkeypatch, container_inputs):
    agent, target, host, report, request = container_inputs
    spec = HarnessSpecificationGenerator().generate(agent, target, host, report, request)
    docker = DockerCLI()
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        return CommandResult(b"id", b"")

    monkeypatch.setattr(docker, "command", command)
    docker.create("trial", "sha256:" + "d" * 64, spec, {})
    args = calls[0]
    for option, value in (
        ("--network", "none"),
        ("--cap-drop", "ALL"),
        ("--memory", str(2**30)),
        ("--cpus", "2.0"),
        ("--user", "65532:65532"),
    ):
        assert args[args.index(option) + 1] == value
    assert "--privileged" not in args and "--volume" not in args
    monkeypatch.setattr(
        docker,
        "command",
        lambda *a, **k: CommandResult(
            json.dumps({"CPUPerc": "150.00%", "MemUsage": "2MiB / 1GiB"}).encode(), b""
        ),
    )
    assert docker.stats("trial") == (150.0, 2 * 1024**2)


def test_stop_falls_back_to_kill(monkeypatch):
    docker = DockerCLI()
    calls = []

    def command(args, **kwargs):
        calls.append(args[0])
        if args[0] == "stop":
            raise RuntimeFailure("COMMAND_TIMEOUT", "stop timed out")
        return CommandResult(b"", b"")

    monkeypatch.setattr(docker, "command", command)
    docker.stop("trial")
    assert calls == ["stop", "kill"]


@pytest.mark.parametrize("failure", ["inspect", "logs", "stats"])
def test_missing_telemetry_preserves_execution(run_case, failure):
    (result,) = run_case[0](FakeDocker(failure=failure))
    assert result.benchmark.execution_evidence == "MEASURED"
    assert result.runtime.telemetry_errors
    assert result.runtime.cleanup_verified
    if failure == "inspect":
        assert result.runtime.started_at is None
        assert result.runtime.exit_code.status == "UNKNOWN"


def test_unknown_review_not_implicitly_approved(run_case):
    run, _, approval, _ = run_case
    docker = FakeDocker()
    with pytest.raises(RuntimeFailure, match="REVIEW_REQUIRED"):
        run(docker, approve=approval.model_copy(update={"reviewed_unknowns": ("stale review",)}))
    assert docker.calls == []


def test_cli_drains_both_streams_with_bounded_memory(monkeypatch, tmp_path):
    import io
    import subprocess
    from types import SimpleNamespace

    observed = []

    def popen(argv, **kwargs):
        observed.append((argv, kwargs))
        return SimpleNamespace(
            stdout=io.BytesIO(b"x" * 100000),
            stderr=io.BytesIO(b"y" * 200000),
            wait=lambda **kw: 0,
            returncode=0,
        )

    monkeypatch.setattr(subprocess, "Popen", popen)
    result = DockerCLI().command(["logs", "fixture"], cwd=tmp_path, limit=100)
    assert result.stdout == b"x" * 100
    assert result.stderr == b"y" * 100
    assert result.truncated
    argv, options = observed[0]
    assert argv == ["docker", "--host", "unix:///var/run/docker.sock", "logs", "fixture"]
    assert options["cwd"] == tmp_path
    assert options["start_new_session"] is True


def test_cli_timeout_kills_process_group(monkeypatch, tmp_path):
    import io
    import os
    import subprocess
    from types import SimpleNamespace

    calls = []

    def wait(**kwargs):
        if kwargs:
            raise subprocess.TimeoutExpired("docker", 1)
        calls.append("reaped")

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *a, **k: SimpleNamespace(
            stdout=io.BytesIO(b""), stderr=io.BytesIO(b""), wait=wait, pid=123
        ),
    )
    monkeypatch.setattr(os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    with pytest.raises(RuntimeFailure, match="COMMAND_TIMEOUT"):
        DockerCLI().command(["wait", "fixture"], cwd=tmp_path, timeout=1)
    assert calls[0][0] == 123
    assert calls[-1] == "reaped"


@pytest.mark.parametrize(
    "field,value",
    [
        ("Privileged", True),
        ("Memory", 0),
        ("NanoCpus", 0),
        ("CapDrop", []),
        ("NetworkMode", "host"),
        ("SecurityOpt", []),
        ("MemorySwap", -1),
    ],
)
def test_inspect_policy_mismatch_blocks_start(monkeypatch, container_inputs, field, value):
    agent, target, host, report, request = container_inputs
    spec = HarnessSpecificationGenerator().generate(agent, target, host, report, request)
    host_config = {
        "NanoCpus": 2000000000,
        "Memory": 2**30,
        "MemorySwap": 2**30,
        "PidsLimit": 256,
        "Privileged": False,
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
        "NetworkMode": "none",
        "ReadonlyRootfs": False,
        "LogConfig": {"Type": "none"},
    }
    config = {
        "User": "65532:65532",
        "WorkingDir": "/opt/agent",
        "Entrypoint": ["python"],
        "Cmd": ["/opt/agent/app.py"],
    }
    docker = DockerCLI()
    monkeypatch.setattr(docker, "inspect", lambda _: {"HostConfig": host_config, "Config": config})
    docker.verify_policy("fixture", spec)
    host_config[field] = value
    with pytest.raises(RuntimeFailure, match="POLICY_MISMATCH"):
        docker.verify_policy("fixture", spec)


def test_remote_engine_rejected():
    with pytest.raises(ValueError, match="local Unix"):
        DockerCLI("tcp://remote:2375")


def test_attach_captures_short_run_and_cleanup(monkeypatch):
    docker = DockerCLI()
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        if args[:2] == ["start", "--attach"]:
            assert kwargs["allow_nonzero"] is True
            return CommandResult(b"short output", b"error output")
        return CommandResult(b"", b"")

    monkeypatch.setattr(docker, "command", command)
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda _: {"State": {"StartedAt": "2026-10-14T00:00:00Z", "Status": "exited"}},
    )
    docker.start("trial", 1, 100)
    logs = docker.logs("trial", 100)
    assert logs.stdout == b"short output"
    assert logs.stderr == b"error output"
    docker.remove("trial")
    assert "trial" not in docker._captures
    assert not any(args[0] == "logs" for args in calls)


def test_attach_start_failure_is_not_success(monkeypatch):
    docker = DockerCLI()
    monkeypatch.setattr(docker, "command", lambda *a, **k: CommandResult(b"", b"start rejected"))
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda _: {"State": {"StartedAt": "0001-01-01T00:00:00Z", "Status": "created"}},
    )
    with pytest.raises(RuntimeFailure, match="START_FAILED"):
        docker.start("trial", 1, 100)


def test_daemon_architecture_unknown_blocks_build(run_case):
    docker = FakeDocker()
    docker.preflight = lambda: {"Architecture": "unknown"}
    (result,) = run_case[0](docker)
    assert result.runtime.error_code == "DAEMON_PLATFORM_MISMATCH"
    assert "build" not in docker.calls
