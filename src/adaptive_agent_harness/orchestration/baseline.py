"""Integrate approved packages and coordinate injected runtime and storage adapters."""

import hashlib
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import Protocol
from uuid import uuid4

from adaptive_agent_harness.benchmark.baseline import evaluate
from adaptive_agent_harness.contracts import (
    AgentProfile,
    EvidenceKind,
    HostProfile,
    TargetProfile,
    known,
)
from adaptive_agent_harness.contracts.harness import ContainerHarnessSpecification, InstallationMode
from adaptive_agent_harness.contracts.runtime import (
    BaselinePlan,
    BaselineResult,
    ExecutionApproval,
    RuntimeRecord,
)
from adaptive_agent_harness.harness import ContainerHarnessBuilder
from adaptive_agent_harness.harness._utils import content_hash
from adaptive_agent_harness.runtime.docker import CommandResult, RuntimeFailure


class Runtime(Protocol):
    def preflight(self) -> dict: ...
    def build(
        self,
        root: Path,
        tag: str,
        spec: ContainerHarnessSpecification,
        timeout: float,
        network: bool,
    ) -> str: ...
    def create(
        self,
        name: str,
        image: str,
        spec: ContainerHarnessSpecification,
        environment: dict[str, str],
    ) -> str: ...
    def verify_policy(self, name: str, spec: ContainerHarnessSpecification) -> None: ...
    def start(self, name: str, timeout: float, limit: int) -> None: ...
    def wait(self, name: str, timeout: float) -> None: ...
    def stop(self, name: str) -> None: ...
    def inspect(self, name: str) -> dict: ...
    def stats(self, name: str) -> tuple[float, float]: ...
    def logs(self, name: str, limit: int) -> CommandResult: ...
    def remove(self, name: str) -> None: ...
    def remove_image(self, tag: str) -> None: ...


class ResultStore(Protocol):
    def save(self, result: BaselineResult) -> Path: ...


def integrate(
    root: Path,
    approval: ExecutionApproval,
    agent: AgentProfile,
    host: HostProfile,
    target: TargetProfile,
    bindings: dict[str, str],
) -> tuple[ContainerHarnessSpecification, str, dict[str, str]]:
    manifest = ContainerHarnessBuilder().verify(root)
    manifest_hash = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    if (
        approval.manifest_sha256 != manifest_hash
        or approval.specification_sha256 != manifest.specification_sha256
        or approval.source_bundle_sha256 != manifest.source_bundle_sha256
        or approval.agent_source_sha256 != manifest.agent_source_sha256
    ):
        raise RuntimeFailure("APPROVAL_MISMATCH", "Approval does not cover this package version")
    spec = ContainerHarnessSpecification.model_validate_json((root / "harness.yaml").read_bytes())
    for name, profile in (("agent", agent), ("host", host), ("target", target)):
        if spec.input_fingerprints[name] != content_hash(profile.model_dump(mode="json")):
            raise RuntimeFailure(
                "PROFILE_MISMATCH", f"{name} profile differs from generated package"
            )
    if set(approval.reviewed_unknowns) != set(spec.unknowns):
        raise RuntimeFailure("REVIEW_REQUIRED", "All static unknowns require explicit review")
    if (
        not spec.source_files
        or spec.source_complete.value is not True
        or spec.working_directory.value is None
        or spec.container_platform.value is None
        or spec.installation.mode == InstallationMode.UNRESOLVED
        or spec.network_required.value is None
        or spec.resource_limits is None
        or spec.resource_limits.cpu_cores is None
        or spec.resource_limits.ram_bytes is None
    ):
        raise RuntimeFailure("MISSING_REQUIREMENTS", "Execution inputs must be explicit")
    if any(
        fact.evidence in (EvidenceKind.ESTIMATED, EvidenceKind.SIMULATED)
        for fact in (spec.source_complete, spec.working_directory, spec.network_required)
    ):
        raise RuntimeFailure("MISSING_REQUIREMENTS", "Estimated inputs cannot authorize execution")
    if (
        spec.installation.mode == InstallationMode.PREINSTALLED
        and spec.image.dependencies_preinstalled.value is not True
    ):
        raise RuntimeFailure("MISSING_REQUIREMENTS", "Preinstalled dependencies must be confirmed")
    if spec.mounts:
        raise RuntimeFailure(
            "UNSUPPORTED_MOUNTS", "Baseline local trials do not accept host mounts"
        )
    if spec.network.effective_network_mode != "none" and not approval.allow_runtime_network:
        raise RuntimeFailure("NETWORK_NOT_APPROVED", "Runtime network requires explicit approval")
    if spec.network_required.value and spec.network.effective_network_mode == "none":
        raise RuntimeFailure("NETWORK_UNAVAILABLE", "Agent requires a network blocked by policy")
    architecture = {
        "x86_64": "linux/amd64",
        "amd64": "linux/amd64",
        "aarch64": "linux/arm64",
        "arm64": "linux/arm64",
    }
    if architecture.get(host.hardware.platform.architecture.value) != spec.container_platform.value:
        raise RuntimeFailure(
            "CROSS_EXECUTION_FORBIDDEN", "Native host architecture must be confirmed"
        )
    if set(bindings) != set(spec.environment_references.values()):
        raise RuntimeFailure(
            "MISSING_ENVIRONMENT", "Provide exactly the declared environment references"
        )
    if any(not isinstance(v, str) or "\x00" in v for v in bindings.values()):
        raise RuntimeFailure("INVALID_ENVIRONMENT", "Environment values must be NUL-free strings")
    return (
        spec,
        manifest.specification_sha256,
        {name: bindings[reference] for name, reference in spec.environment_references.items()},
    )


class BaselineRunner:
    def __init__(self, runtime: Runtime, store: ResultStore):
        self.runtime, self.store = runtime, store

    def run(
        self,
        package: Path,
        *,
        approval: ExecutionApproval,
        agent: AgentProfile,
        host: HostProfile,
        target: TargetProfile,
        plan: BaselinePlan,
        bindings: dict[str, str] | None = None,
    ) -> tuple[BaselineResult, ...]:
        approval = ExecutionApproval.model_validate_json(approval.model_dump_json())
        plan = BaselinePlan.model_validate_json(plan.model_dump_json())
        # Freeze the approved context before build; never run a mutable caller directory.
        with tempfile.TemporaryDirectory(prefix="harness-baseline-") as temporary:
            root = Path(temporary) / "package"
            ContainerHarnessBuilder().verify(package)
            shutil.copytree(package, root, symlinks=True)
            spec, spec_hash, environment = integrate(
                root, approval, agent, host, target, bindings or {}
            )
            data_binding = ContainerHarnessBuilder().data_binding(root, spec)
            if data_binding is not None and plan.input_sha256 != data_binding.version.output_sha256:
                raise RuntimeFailure(
                    "DATA_INPUT_HASH_MISMATCH", "Plan must pin the evaluated data bytes"
                )
            results = []
            for index in range(plan.warmups + plan.repetitions):
                record = self._execute(root, spec, approval, plan, environment)
                warmup = index < plan.warmups
                benchmark = evaluate(
                    record,
                    spec,
                    spec_hash,
                    host,
                    target,
                    plan,
                    index + 1 if warmup else index - plan.warmups + 1,
                    warmup,
                    data_binding,
                )
                result = BaselineResult(
                    benchmark=benchmark,
                    runtime=record,
                    plan=plan,
                    approval=approval,
                    data_pipeline=data_binding,
                )
                self.store.save(result)
                results.append(result)
                if record.cleanup_errors:
                    break  # Do not compound leaked resources.
            return tuple(results)

    def _execute(
        self,
        root: Path,
        spec: ContainerHarnessSpecification,
        approval: ExecutionApproval,
        plan: BaselinePlan,
        environment: dict[str, str],
    ) -> RuntimeRecord:
        run_id = uuid4()
        name, tag = f"aah-{run_id}", f"adaptive-baseline:{run_id}"
        data = dict(
            run_id=run_id,
            approval_id=approval.approval_id,
            harness_version=spec.version,
            source_bundle_sha256=approval.source_bundle_sha256,
            manifest_sha256=approval.manifest_sha256,
        )
        cpu, memory, telemetry_errors, cleanup_errors = [], [], [], []
        done = Event()
        worker = None
        build_attempted = create_attempted = start_attempted = started = False

        def redact(text: str) -> str:
            for value in sorted(environment.values(), key=len, reverse=True):
                if value:
                    text = text.replace(value, "[REDACTED]")
            return text

        def sample():
            while not done.is_set():
                try:
                    c, m = self.runtime.stats(name)
                    # Exclude stats from containers which stopped during the sampling interval.
                    if not done.is_set():
                        cpu.append(c)
                        memory.append(m)
                except (RuntimeFailure, ValueError, KeyError) as exc:
                    if len(telemetry_errors) < 10:
                        telemetry_errors.append(redact(str(exc)))
                done.wait(0.2)

        try:
            info = self.runtime.preflight()
            native_platform = {
                "x86_64": "linux/amd64",
                "amd64": "linux/amd64",
                "aarch64": "linux/arm64",
                "arm64": "linux/arm64",
            }.get(info.get("Architecture"))
            if native_platform != spec.container_platform.value:
                raise RuntimeFailure(
                    "DAEMON_PLATFORM_MISMATCH",
                    "Docker Engine native architecture differs or is unknown",
                )
            data["daemon_identity"] = {
                key: info[key]
                for key in (
                    "ID",
                    "Name",
                    "Architecture",
                    "NCPU",
                    "MemTotal",
                    "ServerVersion",
                    "KernelVersion",
                )
                if key in info and isinstance(info[key], (str, int))
            }
            build_attempted = True
            data["image_id"] = self.runtime.build(
                root, tag, spec, plan.build_timeout_seconds, approval.allow_build_network
            )
            create_attempted = True
            data["container_id"] = self.runtime.create(name, data["image_id"], spec, environment)
            self.runtime.verify_policy(name, spec)
            data["limits_verified"] = known(
                True, "Docker inspect before start", EvidenceKind.MEASURED
            )
            start_attempted = True
            deadline = time.monotonic() + plan.timeout_seconds
            self.runtime.start(name, plan.timeout_seconds, plan.log_limit_bytes)
            started = True
            data["execution_started"] = True
            worker = Thread(target=sample)
            worker.start()
            try:
                self.runtime.wait(name, max(0.001, deadline - time.monotonic()))
            except RuntimeFailure as exc:
                if exc.code != "COMMAND_TIMEOUT":
                    raise
                data["timed_out"] = True
                data["error_code"], data["error_cause"] = "TIMEOUT", exc.cause
                self.runtime.stop(name)
        except (RuntimeFailure, OSError, ValueError, KeyError) as exc:
            data["error_code"] = exc.code if isinstance(exc, RuntimeFailure) else "RUNTIME_ERROR"
            data["error_cause"] = redact(str(exc))
        finally:
            done.set()
            if worker:
                worker.join()
            if create_attempted:
                try:
                    state = self.runtime.inspect(name)["State"]
                    if state.get("Running"):
                        self.runtime.stop(name)
                        state = self.runtime.inspect(name)["State"]
                    for key, source in (
                        ("exit_code", "ExitCode"),
                        ("oom_killed", "OOMKilled"),
                        ("container_state", "Status"),
                    ):
                        if source in state:
                            data[key] = known(
                                state[source], "Docker inspect State", EvidenceKind.MEASURED
                            )
                    if start_attempted:
                        for field, key in (
                            ("started_at", "StartedAt"),
                            ("finished_at", "FinishedAt"),
                        ):
                            if state.get(key) and not state[key].startswith("0001-"):
                                data[field] = datetime.fromisoformat(
                                    state[key].replace("Z", "+00:00")
                                ).astimezone(UTC)
                        if data.get("started_at"):
                            data["execution_started"] = True
                        if data.get("started_at") and data.get("finished_at"):
                            elapsed = (data["finished_at"] - data["started_at"]).total_seconds()
                            if elapsed >= 0:
                                data["wall_seconds"] = elapsed
                            else:
                                data["finished_at"] = None
                                telemetry_errors.append("Docker finish time precedes start time")
                    if state.get("OOMKilled"):
                        data["error_code"], data["error_cause"] = "OOM", "Docker reported OOMKilled"
                    elif started and state.get("ExitCode", 0) != 0 and not data.get("error_code"):
                        data["error_code"], data["error_cause"] = (
                            "NONZERO_EXIT",
                            "Agent exited unsuccessfully",
                        )
                    logs = self.runtime.logs(name, plan.log_limit_bytes)
                    data["logs_truncated"] = logs.truncated
                    for stream, raw in (("stdout", logs.stdout), ("stderr", logs.stderr)):
                        encoded = redact(raw.decode("utf-8", errors="replace")).encode()
                        data[stream] = encoded[: plan.log_limit_bytes].decode(
                            "utf-8", errors="ignore"
                        )
                        data["logs_truncated"] |= len(encoded) > plan.log_limit_bytes
                    data["logs_collected"] = True
                except (RuntimeFailure, ValueError, KeyError) as exc:
                    telemetry_errors.append(redact(str(exc)))
                try:
                    self.runtime.remove(name)
                    data["cleanup_verified"] = True
                except RuntimeFailure as exc:
                    cleanup_errors.append(redact(str(exc)))
            if build_attempted:
                try:
                    self.runtime.remove_image(tag)
                except RuntimeFailure as exc:
                    cleanup_errors.append(redact(str(exc)))
        data.update(
            cpu_percent_samples=tuple(cpu),
            memory_bytes_samples=tuple(memory),
            telemetry_errors=tuple(telemetry_errors),
            cleanup_errors=tuple(cleanup_errors),
        )
        return RuntimeRecord(**data)
