"""Trusted host adapter. Only Docker CLI processes are launched on the host."""

import json
import math
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Thread

from adaptive_agent_harness.contracts.harness import ContainerHarnessSpecification


class RuntimeFailure(Exception):
    def __init__(self, code: str, cause: str):
        self.code, self.cause = code, cause
        super().__init__(f"{code}: {cause}")


@dataclass(frozen=True)
class CommandResult:
    stdout: bytes
    stderr: bytes
    truncated: bool = False


class DockerCLI:
    def __init__(self, endpoint: str = "unix:///var/run/docker.sock"):
        if not endpoint.startswith("unix:///"):
            raise ValueError("Only an explicitly local Unix Docker socket is supported")
        self.endpoint = endpoint
        self._captures: dict[str, tuple[Thread, list]] = {}

    def command(
        self,
        args: list[str],
        *,
        cwd: Path,
        timeout: float = 10,
        limit: int = 1048576,
        allow_nonzero: bool = False,
    ) -> CommandResult:
        env = {k: v for k, v in os.environ.items() if not k.startswith("DOCKER_")}
        buffers = [bytearray(), bytearray()]
        truncated = [False, False]

        def drain(pipe, index):
            with pipe:
                while chunk := pipe.read(8192):
                    remaining = max(0, limit - len(buffers[index]))
                    buffers[index].extend(chunk[:remaining])
                    truncated[index] |= len(chunk) > remaining

        try:
            process = subprocess.Popen(
                ["docker", "--host", self.endpoint, *args],
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise RuntimeFailure("DOCKER_UNAVAILABLE", str(exc)) from exc
        threads = [
            Thread(target=drain, args=(process.stdout, 0)),
            Thread(target=drain, args=(process.stderr, 1)),
        ]
        for thread in threads:
            thread.start()
        try:
            process.wait(timeout=timeout)
        except BaseException as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            if isinstance(exc, subprocess.TimeoutExpired):
                raise RuntimeFailure(
                    "COMMAND_TIMEOUT", f"Docker {args[0]} exceeded {timeout}s"
                ) from exc
            raise
        finally:
            for thread in threads:
                thread.join()
        result = CommandResult(bytes(buffers[0]), bytes(buffers[1]), any(truncated))
        if process.returncode and not allow_nonzero:
            raise RuntimeFailure(
                "DOCKER_COMMAND_FAILED",
                f"Docker {args[0]} exited {process.returncode}: "
                + result.stderr.decode("utf-8", errors="replace"),
            )
        return result

    def preflight(self) -> dict:
        result = self.command(["info", "--format", "{{json .}}"], cwd=Path("/"))
        info = json.loads(result.stdout)
        if info.get("OSType") != "linux":
            raise RuntimeFailure("UNSUPPORTED_DAEMON", "Linux Docker Engine is required")
        return info

    def build(
        self,
        root: Path,
        tag: str,
        spec: ContainerHarnessSpecification,
        timeout: float,
        network: bool,
    ) -> str:
        self.command(
            [
                "build",
                "--force-rm",
                "--network",
                "default" if network else "none",
                "--platform",
                spec.container_platform.value.value,
                "--tag",
                tag,
                ".",
            ],
            cwd=root,
            timeout=timeout,
        )
        result = self.command(["image", "inspect", tag, "--format", "{{.Id}}"], cwd=root)
        image_id = result.stdout.decode().strip()
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise RuntimeFailure("INVALID_IMAGE_ID", "Docker returned an invalid image ID")
        image = json.loads(self.command(["image", "inspect", image_id], cwd=root).stdout)[0]
        if image["Config"].get("Volumes"):
            raise RuntimeFailure("UNSUPPORTED_IMAGE", "Inherited image volumes are forbidden")
        return image_id

    def create(
        self,
        name: str,
        image: str,
        spec: ContainerHarnessSpecification,
        environment: dict[str, str],
    ) -> str:
        limits, security = spec.resource_limits, spec.security
        args = [
            "create",
            "--name",
            name,
            "--label",
            "org.adaptive-harness.run=" + name,
            "--network",
            spec.network.effective_network_mode,
            "--user",
            f"{security.user_id}:{security.group_id}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(security.pids_limit),
            "--cpus",
            str(limits.cpu_cores),
            "--memory",
            str(limits.ram_bytes),
            "--memory-swap",
            str(limits.ram_bytes),
            "--log-driver",
            "none",
            "--no-healthcheck",
            "--workdir",
            spec.working_directory.value,
            "--entrypoint",
            spec.argv[0],
        ]
        if security.read_only_rootfs_compatible.value is True:
            args.append("--read-only")
        for key, value in sorted(environment.items()):
            args += ["--env", f"{key}={value}"]
        result = self.command([*args, image, *spec.argv[1:]], cwd=Path("/"))
        return result.stdout.decode().strip()

    def inspect(self, name: str) -> dict:
        data = json.loads(self.command(["inspect", name], cwd=Path("/")).stdout)[0]
        # Docker may publish the exit event before its OOM event updates inspect.
        # Re-observe briefly; exit 137 alone remains a nonzero exit, never OOM evidence.
        deadline = time.monotonic() + 1
        while (
            data.get("State", {}).get("Status") == "exited"
            and data["State"].get("ExitCode") == 137
            and not data["State"].get("OOMKilled")
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
            data = json.loads(self.command(["inspect", name], cwd=Path("/")).stdout)[0]
        return data

    def verify_policy(self, name: str, spec: ContainerHarnessSpecification) -> None:
        data = self.inspect(name)
        host, config = data["HostConfig"], data["Config"]
        checks = [
            host.get("NanoCpus") == round(spec.resource_limits.cpu_cores * 1e9),
            host.get("Memory") == spec.resource_limits.ram_bytes,
            host.get("MemorySwap") == spec.resource_limits.ram_bytes,
            host.get("PidsLimit") == spec.security.pids_limit,
            host.get("Privileged") is False,
            host.get("CapDrop") == ["ALL"],
            not host.get("CapAdd"),
            "no-new-privileges:true" in host.get("SecurityOpt", []),
            host.get("NetworkMode") == spec.network.effective_network_mode,
            config.get("User") == f"{spec.security.user_id}:{spec.security.group_id}",
            config.get("WorkingDir") == spec.working_directory.value,
            config.get("Entrypoint") == [spec.argv[0]],
            (config.get("Cmd") or []) == list(spec.argv[1:]),
            not data.get("Mounts"),
            not host.get("Devices"),
            host.get("LogConfig", {}).get("Type") == "none",
            host.get("ReadonlyRootfs") == (spec.security.read_only_rootfs_compatible.value is True),
        ]
        if not all(checks):
            raise RuntimeFailure("POLICY_MISMATCH", "Docker inspect differs from required policy")

    def start(self, name: str, timeout: float, limit: int) -> None:
        # Attach before execution so even very short runs and arbitrarily large output
        # are drained without relying on a rotated daemon log file.
        captured: list[CommandResult | Exception] = []

        def capture():
            try:
                captured.append(
                    self.command(
                        ["start", "--attach", name],
                        cwd=Path("/"),
                        timeout=timeout + 15,
                        limit=limit,
                        allow_nonzero=True,
                    )
                )
            except Exception as exc:
                captured.append(exc)

        thread = Thread(target=capture)
        self._captures[name] = thread, captured
        thread.start()
        deadline = time.monotonic() + min(10, timeout)
        while time.monotonic() < deadline:
            state = self.inspect(name)["State"]
            if state.get("StartedAt") and not state["StartedAt"].startswith("0001-"):
                return
            if captured:
                if isinstance(captured[0], Exception):
                    raise RuntimeFailure("START_FAILED", str(captured[0]))
                raise RuntimeFailure("START_FAILED", captured[0].stderr.decode(errors="replace"))
            time.sleep(0.02)
        raise RuntimeFailure("START_TIMEOUT", "Container did not start before the deadline")

    def wait(self, name: str, timeout: float) -> None:
        self.command(["wait", name], cwd=Path("/"), timeout=timeout)

    def stop(self, name: str) -> None:
        try:
            self.command(["stop", "--time", "1", name], cwd=Path("/"), timeout=5)
        except RuntimeFailure:
            self.command(["kill", name], cwd=Path("/"), timeout=5)

    def stats(self, name: str) -> tuple[float, float]:
        raw = self.command(
            ["stats", "--no-stream", "--format", "{{json .}}", name], cwd=Path("/"), timeout=3
        )
        data = json.loads(raw.stdout)
        memory = data["MemUsage"].split("/")[0].strip()
        match = re.fullmatch(r"([0-9.]+)\s*(B|KiB|MiB|GiB|TiB|kB|MB|GB)", memory)
        if not match:
            raise ValueError("Unknown Docker memory unit")
        units = {
            "B": 1,
            "KiB": 1024,
            "MiB": 1024**2,
            "GiB": 1024**3,
            "TiB": 1024**4,
            "kB": 1000,
            "MB": 1000**2,
            "GB": 1000**3,
        }
        cpu, memory = float(data["CPUPerc"].rstrip("%")), float(match[1]) * units[match[2]]
        if not all(math.isfinite(value) and value >= 0 for value in (cpu, memory)):
            raise ValueError("Docker resource samples must be finite and nonnegative")
        return cpu, memory

    def logs(self, name: str, limit: int) -> CommandResult:
        if name not in self._captures:
            raise RuntimeFailure("LOGS_UNAVAILABLE", "Container start capture was not established")
        thread, captured = self._captures[name]
        thread.join(timeout=5)
        if thread.is_alive() or not captured:
            raise RuntimeFailure("LOGS_UNAVAILABLE", "Attached stream did not finish")
        if isinstance(captured[0], Exception):
            raise RuntimeFailure("LOGS_UNAVAILABLE", str(captured[0]))
        return captured[0]

    def remove(self, name: str) -> None:
        # A generated name is known before create, so ambiguous create failures can be cleaned.
        result = self.command(["ps", "-aq", "--filter", "name=^/" + name + "$"], cwd=Path("/"))
        if result.stdout.strip():
            self.command(["rm", "--force", "--volumes", name], cwd=Path("/"))
        result = self.command(["ps", "-aq", "--filter", "name=^/" + name + "$"], cwd=Path("/"))
        if result.stdout.strip():
            raise RuntimeFailure("CLEANUP_FAILED", "Container remains after removal")
        if name in self._captures:
            thread, _ = self._captures[name]
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeFailure("CLEANUP_FAILED", "Docker attach process is still running")
            del self._captures[name]

    def remove_image(self, tag: str) -> None:
        result = self.command(["image", "ls", "-q", tag], cwd=Path("/"))
        if result.stdout.strip():
            self.command(["image", "rm", tag], cwd=Path("/"))
