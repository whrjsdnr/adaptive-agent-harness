import json
import subprocess

import pytest

import adaptive_agent_harness.analyzer.host as host_module
from adaptive_agent_harness.analyzer import LocalHostProbe, TargetProfiler
from adaptive_agent_harness.contracts import Knowledge, ResourceLimits, TargetProfile


class FakeProbe:
    def os_name(self):
        return "Linux"

    def architecture(self):
        return "x86_64"

    def abi(self):
        return "glibc 2.39"

    def cpu_cores(self):
        return 8

    def ram_bytes(self):
        return 8 * 2**30

    def gpu_present(self):
        return None

    def docker_cli(self):
        return False

    def docker_access(self):
        pytest.fail("Missing Docker must not be queried")

    def python_version(self):
        return "3.12.14"


def test_offline_host_collection_and_separate_target():
    host = TargetProfiler(FakeProbe()).profile_host()
    assert host.hardware.resources.cpu_cores.value == 8
    assert host.hardware.resources.ram_bytes.value == 8 * 2**30
    assert host.hardware.gpu_present.status == Knowledge.UNKNOWN
    assert host.deployment.cuda.status == Knowledge.UNKNOWN
    assert host.deployment.docker_cli.value is False
    assert host.deployment.docker_access.value is False
    assert host.deployment.python.value is True
    assert host.runtimes[0].version.value == "3.12.14"
    target = TargetProfile.simulate_resources(host, ResourceLimits(cpu_cores=1), "virtual limit")
    assert target.simulated_limits.cpu_cores == 1
    assert host.hardware.resources.cpu_cores.value == 8


def test_accessible_docker_and_gpu_do_not_prove_execution():
    class AvailableProbe(FakeProbe):
        def docker_cli(self):
            return True

        def docker_access(self):
            return True

        def gpu_present(self):
            return True

    host = TargetProfiler(AvailableProbe()).profile_host()
    assert host.hardware.gpu_present.value is True
    assert host.deployment.docker_access.value is True
    assert host.deployment.docker.status == Knowledge.UNKNOWN
    assert host.deployment.cuda.status == Knowledge.UNKNOWN


def test_probe_failures_and_invalid_capacity_are_unknown():
    class BrokenProbe(FakeProbe):
        def ram_bytes(self):
            raise OSError("unavailable")

        def cpu_cores(self):
            return 0

        def docker_cli(self):
            return None

        def python_version(self):
            return None

    host = TargetProfiler(BrokenProbe()).profile_host()
    assert host.hardware.resources.cpu_cores.status == Knowledge.UNKNOWN
    assert host.hardware.resources.ram_bytes.status == Knowledge.UNKNOWN
    assert host.deployment.docker_access.status == Knowledge.UNKNOWN
    assert host.deployment.python.status == Knowledge.UNKNOWN


def test_remote_docker_endpoint_is_not_queried(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://example.invalid:2375")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    probe = LocalHostProbe()
    monkeypatch.setattr(probe, "docker_cli", lambda: True)

    def reject_query(argv):
        pytest.fail("Remote endpoint must not be contacted")

    monkeypatch.setattr(probe, "_docker_query", reject_query)
    assert probe.docker_access() is None


@pytest.mark.parametrize("outcome", ["ok", "denied", "timeout", "invalid"])
def test_local_docker_access_probe_without_docker(monkeypatch, outcome):
    monkeypatch.setenv("DOCKER_HOST", "unix:///fixture/docker.sock")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    probe = LocalHostProbe()
    monkeypatch.setattr(probe, "docker_cli", lambda: True)

    def query(argv):
        assert argv == [
            "docker",
            "--host",
            "unix:///fixture/docker.sock",
            "version",
            "--format",
            "{{json .Server}}",
        ]
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, 3)
        return subprocess.CompletedProcess(
            argv,
            1 if outcome == "denied" else 0,
            json.dumps({"Version": "fixture"}) if outcome == "ok" else "invalid",
        )

    monkeypatch.setattr(probe, "_docker_query", query)
    assert probe.docker_access() is (
        True if outcome == "ok" else False if outcome == "denied" else None
    )


@pytest.mark.parametrize(
    "pci_class,expected", [("0x030000", True), ("0x020000", None), ("invalid", None)]
)
def test_gpu_sysfs_detection_is_conservative(tmp_path, monkeypatch, pci_class, expected):
    device = tmp_path / "device"
    device.mkdir()
    (device / "class").write_text(pci_class, encoding="utf-8")
    monkeypatch.setattr(host_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(host_module, "Path", lambda path: tmp_path)
    assert LocalHostProbe().gpu_present() is expected
