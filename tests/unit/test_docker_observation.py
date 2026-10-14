import json

from adaptive_agent_harness.runtime.docker import CommandResult, DockerCLI


def test_late_oom_state_is_observed_not_inferred(monkeypatch):
    docker = DockerCLI()
    states = iter(
        [
            {"State": {"Status": "exited", "ExitCode": 137, "OOMKilled": False}},
            {"State": {"Status": "exited", "ExitCode": 137, "OOMKilled": True}},
        ]
    )
    monkeypatch.setattr(
        docker, "command", lambda *a, **k: CommandResult(json.dumps([next(states)]).encode(), b"")
    )
    monkeypatch.setattr("adaptive_agent_harness.runtime.docker.time.sleep", lambda _: None)
    clock = iter([0, 0.1])
    monkeypatch.setattr("adaptive_agent_harness.runtime.docker.time.monotonic", lambda: next(clock))
    assert docker.inspect("fixture")["State"]["OOMKilled"] is True


def test_exit_137_without_oom_observation_remains_false(monkeypatch):
    docker = DockerCLI()
    state = {"State": {"Status": "exited", "ExitCode": 137, "OOMKilled": False}}
    monkeypatch.setattr(
        docker, "command", lambda *a, **k: CommandResult(json.dumps([state]).encode(), b"")
    )
    clock = iter([0, 2])
    monkeypatch.setattr("adaptive_agent_harness.runtime.docker.time.monotonic", lambda: next(clock))
    assert docker.inspect("fixture")["State"]["OOMKilled"] is False
