"""Local host observations behind an injectable probe, without Agent/container execution."""

import json
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from adaptive_agent_harness.contracts import (
    DeploymentCapabilities,
    EvidenceKind,
    Fact,
    HardwareInfo,
    HostProfile,
    PlatformInfo,
    ResourceCapacity,
    RuntimeInfo,
    known,
)


class HostProbe(Protocol):
    def os_name(self) -> str | None: ...
    def architecture(self) -> str | None: ...
    def abi(self) -> str | None: ...
    def cpu_cores(self) -> int | None: ...
    def ram_bytes(self) -> int | None: ...
    def gpu_present(self) -> bool | None: ...
    def docker_cli(self) -> bool | None: ...
    def docker_access(self) -> bool | None: ...
    def python_version(self) -> str | None: ...


class LocalHostProbe:
    def os_name(self) -> str | None:
        return platform.system() or None

    def architecture(self) -> str | None:
        return platform.machine() or None

    def abi(self) -> str | None:
        name, version = platform.libc_ver()
        return f"{name} {version}" if name and version else None

    def cpu_cores(self) -> int | None:
        return os.cpu_count()

    def ram_bytes(self) -> int | None:
        try:
            return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (ValueError, OSError, AttributeError):
            return None

    def gpu_present(self) -> bool | None:
        if platform.system() == "Linux":
            devices = Path("/sys/bus/pci/devices")
            if devices.is_dir():
                for path in devices.glob("*/class"):
                    try:
                        # PCI display controllers are evidence of GPU hardware, not CUDA support.
                        if int(path.read_text().strip(), 16) >> 16 == 0x03:
                            return True
                    except (OSError, ValueError):
                        continue
        # No visible PCI display device does not prove physical absence (VM/container/SoC).
        return None

    def docker_cli(self) -> bool:
        return shutil.which("docker") is not None

    def _docker_query(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        environment = {
            k: v for k, v in os.environ.items() if k not in ("DOCKER_HOST", "DOCKER_CONTEXT")
        }
        return subprocess.run(
            argv,
            cwd=Path.cwd().anchor,
            env=environment,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )

    def docker_access(self) -> bool | None:
        """Query only a local Unix socket; remote daemons are not local host evidence."""
        if not self.docker_cli():
            return False
        try:
            endpoint = os.environ.get("DOCKER_HOST")
            if not endpoint or os.environ.get("DOCKER_CONTEXT"):
                argv = ["docker", "context", "inspect"]
                if os.environ.get("DOCKER_CONTEXT"):
                    argv.append(os.environ["DOCKER_CONTEXT"])
                result = self._docker_query([*argv, "--format", "{{json .Endpoints.docker.Host}}"])
                if result.returncode:
                    return None
                endpoint = json.loads(result.stdout)
            if not isinstance(endpoint, str) or not endpoint.startswith("unix://"):
                return None
            result = self._docker_query(
                ["docker", "--host", endpoint, "version", "--format", "{{json .Server}}"]
            )
            if result.returncode:
                return False
            server = json.loads(result.stdout)
            return True if isinstance(server, dict) and server.get("Version") else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None

    def python_version(self) -> str:
        return platform.python_version()


class TargetProfiler:
    def __init__(self, probe: HostProbe | None = None):
        self.probe = probe if probe is not None else LocalHostProbe()

    def profile_host(self) -> HostProfile:
        """Collect host facts only. Never substitute or mutate a requested target."""

        def observe(method: str) -> Fact:
            source = f"local_host:{method}"
            try:
                value = getattr(self.probe, method)()
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                value = None
            if method in ("cpu_cores", "ram_bytes"):
                if type(value) is not int or value <= 0:
                    value = None
            if value is None or value == "":
                return Fact(source=f"{source}: not confirmed")
            return known(value, source, EvidenceKind.MEASURED)

        runtime_version = observe("python_version")
        python_available = (
            known(True, "Current profiler Python interpreter", EvidenceKind.MEASURED)
            if runtime_version.value
            else Fact(source="Python runtime not confirmed")
        )
        cli = observe("docker_cli")
        access = (
            known(False, "Docker CLI absent", EvidenceKind.MEASURED)
            if cli.value is False
            else observe("docker_access")
            if cli.value is True
            else Fact(source="Docker CLI not confirmed")
        )
        docker = (
            known(False, "Local Docker currently inaccessible", EvidenceKind.MEASURED)
            if access.value is False
            else Fact(source="Container execution not tested")
        )
        return HostProfile(
            hardware=HardwareInfo(
                platform=PlatformInfo(
                    os=observe("os_name"), architecture=observe("architecture"), abi=observe("abi")
                ),
                resources=ResourceCapacity(
                    cpu_cores=observe("cpu_cores"), ram_bytes=observe("ram_bytes")
                ),
                gpu_present=observe("gpu_present"),
            ),
            runtimes=(
                RuntimeInfo(name="python", version=runtime_version, available=python_available),
            ),
            deployment=DeploymentCapabilities(
                python=python_available,
                docker_cli=cli,
                docker_access=access,
                docker=docker,
                cuda=Fact(source="CUDA execution not tested"),
                vm=Fact(source="VM execution not tested"),
            ),
        )
