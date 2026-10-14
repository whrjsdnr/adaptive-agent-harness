"""Generate versioned container specifications from explicit, observed inputs."""

from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from adaptive_agent_harness.analyzer import CompatibilityEngine
from adaptive_agent_harness.contracts import (
    AgentProfile,
    CompatibilityReport,
    CompatibilityVerdict,
    EvidenceKind,
    ExecutionMode,
    Fact,
    HostProfile,
    Knowledge,
    OperationalRequirements,
    ResourceLimits,
    TargetProfile,
)
from adaptive_agent_harness.contracts.harness import (
    ContainerHarnessRequest,
    ContainerHarnessSpecification,
    ContainerPlatform,
    InstallationMode,
    NetworkMode,
    StaticReadiness,
)

from ._utils import HarnessError, content_hash, stamp_created_at, validate_mount_scope

V = CompatibilityVerdict


class HarnessSpecificationGenerator:
    def generate(
        self,
        agent: AgentProfile,
        target: TargetProfile,
        host: HostProfile,
        compatibility: CompatibilityReport,
        request: ContainerHarnessRequest,
        *,
        operational_requirements: OperationalRequirements | None = None,
    ) -> ContainerHarnessSpecification:
        # Revalidate wire snapshots; do not trust mutable dictionaries or model_copy(update=...).
        agent = AgentProfile.model_validate_json(agent.model_dump_json())
        target = TargetProfile.model_validate_json(target.model_dump_json())
        host = HostProfile.model_validate_json(host.model_dump_json())
        compatibility = CompatibilityReport.model_validate_json(compatibility.model_dump_json())
        request = ContainerHarnessRequest.model_validate_json(request.model_dump_json())
        if compatibility.agent_id != agent.agent_id or compatibility.target_id != target.target_id:
            raise HarnessError("INPUT_MISMATCH", "Compatibility report belongs to different inputs")
        if compatibility.verdict == V.UNSUPPORTED:
            raise HarnessError("UNSUPPORTED", "Compatibility report blocks generation")
        if target.based_on_host_id not in (None, host.host_id):
            raise HarnessError("INPUT_MISMATCH", "Resource simulation belongs to a different host")
        if not request.argv:
            raise HarnessError(
                "MISSING_REQUIREMENTS",
                "An explicit entrypoint is required",
                unknowns=("argv UNKNOWN; static declarations are not a confirmed command",),
            )
        timestamp = request.created_at.isoformat()

        def fact(value, source):
            return Fact(
                status=Knowledge.KNOWN,
                value=value,
                source=source,
                evidence=EvidenceKind.DECLARED,
                created_at=request.created_at,
            )

        # Additional requirements may tighten minima or fill unknowns, never silently weaken them.
        requirements = agent.requirements
        if operational_requirements is not None:
            operational_requirements = OperationalRequirements.model_validate_json(
                operational_requirements.model_dump_json()
            )
            data = requirements.model_dump()
            for field in OperationalRequirements.model_fields:
                current, extra = (
                    getattr(requirements, field),
                    getattr(operational_requirements, field),
                )
                if (
                    not isinstance(current, Fact)
                    or not isinstance(extra, Fact)
                    or extra.value is None
                ):
                    continue
                if current.value is None or current.value == extra.value:
                    data[field] = extra
                elif field in ("min_cpu_cores", "min_ram_bytes"):
                    data[field] = extra if extra.value > current.value else current
                elif field in ("gpu_required", "cuda_required"):
                    data[field] = extra if extra.value else current
                elif field == "python_specifier":
                    data[field] = fact(
                        str(SpecifierSet(f"{current.value},{extra.value}")),
                        "Intersection of Agent and operational Python requirements",
                    )
                elif (
                    field == "execution_mode"
                    and current.value == ExecutionMode.PYTHON
                    and extra.value == ExecutionMode.DOCKER
                ):
                    data[field] = extra
                elif (
                    field in ("os", "architecture")
                    and str(current.value).lower() == str(extra.value).lower()
                ):
                    data[field] = extra
                else:
                    raise HarnessError(
                        "REQUIREMENT_CONFLICT",
                        "Operational requirements contradict Agent declarations",
                    )
            requirements = OperationalRequirements.model_validate(data)
        if requirements.execution_mode.value == ExecutionMode.VM:
            raise HarnessError(
                "UNSUPPORTED", "VM execution requirements are outside container generation scope"
            )
        effective_agent = AgentProfile.model_validate(
            {**agent.model_dump(), "requirements": requirements}
        )
        engine = CompatibilityEngine()
        reports = (
            compatibility,
            engine.assess(agent, target),
            engine.assess(effective_agent, target),
        )
        if any(r.verdict == V.UNSUPPORTED for r in reports):
            raise HarnessError("UNSUPPORTED", "Current requirements or target block generation")
        checks = []
        seen = set()
        for report in reports:
            for check in report.checks:
                key = (check.area, check.verdict, check.reason, check.sources)
                if key not in seen:
                    checks.append(check)
                    seen.add(key)
        verdict = next(
            v for v in (V.UNKNOWN, V.ADAPT_REQUIRED, V.FIT) if any(c.verdict == v for c in checks)
        )
        report = CompatibilityReport.model_validate(
            stamp_created_at(
                {
                    **compatibility.model_dump(mode="json"),
                    "verdict": verdict,
                    "checks": [c.model_dump(mode="json") for c in checks],
                    "unknowns": [f"{c.area}: {c.reason}" for c in checks if c.verdict == V.UNKNOWN],
                },
                timestamp,
            )
        )
        unknowns = list(report.unknowns)
        for field in OperationalRequirements.model_fields:
            observation = getattr(requirements, field)
            if isinstance(observation, Fact) and observation.evidence in (
                EvidenceKind.ESTIMATED,
                EvidenceKind.SIMULATED,
            ):
                unknowns.append(
                    f"Operational requirement {field} is {observation.evidence.value}; confirmation pending"
                )
        for field in ("working_directory", "network_required"):
            observation = getattr(request, field)
            if observation.evidence in (EvidenceKind.ESTIMATED, EvidenceKind.SIMULATED):
                unknowns.append(
                    f"Requested {field} is {observation.evidence.value}; Agent requirement not confirmed"
                )
        platform = Fact(source="Target OS or architecture UNKNOWN", created_at=request.created_at)
        target_os, architecture = (
            target.hardware.platform.os.value,
            target.hardware.platform.architecture.value,
        )
        if target_os is not None and target_os.lower() != "linux":
            raise HarnessError("UNSUPPORTED", "Only Linux container targets are supported")
        architectures = {
            "x86_64": ContainerPlatform.LINUX_AMD64,
            "amd64": ContainerPlatform.LINUX_AMD64,
            "x64": ContainerPlatform.LINUX_AMD64,
            "arm64": ContainerPlatform.LINUX_ARM64,
            "aarch64": ContainerPlatform.LINUX_ARM64,
        }
        if architecture is not None and architecture.lower() not in architectures:
            raise HarnessError("UNSUPPORTED", "Target CPU architecture is unsupported")
        if target_os is not None and architecture is not None:
            platform = fact(
                architectures[architecture.lower()],
                "Mapping from declared target OS and CPU architecture",
            )
        else:
            unknowns.append(
                "Container image platform UNKNOWN; host architecture is not substituted"
            )
        if not request.image.platforms:
            unknowns.append("Base image architecture support UNKNOWN; registry was not inspected")
        elif platform.value is not None and platform.value not in request.image.platforms:
            raise HarnessError(
                "UNSUPPORTED", "Base image does not declare the target container architecture"
            )
        if "@sha256:" not in request.image.reference:
            unknowns.append("Base image digest is not pinned; tag contents may change")
        python = request.image.python_version
        if python.value is None or python.evidence in (
            EvidenceKind.ESTIMATED,
            EvidenceKind.SIMULATED,
        ):
            unknowns.append("Base image Python runtime compatibility UNKNOWN")
        elif requirements.python_specifier.value is None:
            unknowns.append("Agent Python requirement UNKNOWN")
        else:
            try:
                if Version(python.value) not in SpecifierSet(requirements.python_specifier.value):
                    raise HarnessError(
                        "UNSUPPORTED", "Base image Python runtime violates Agent requirements"
                    )
            except InvalidVersion:
                unknowns.append("Base image Python version cannot be interpreted")
        if (
            request.image.gpu_runtime_compatible.value is False
            and request.image.gpu_runtime_compatible.evidence
            in (EvidenceKind.DECLARED, EvidenceKind.STATIC, EvidenceKind.MEASURED)
            and (
                requirements.gpu_required.value is True or requirements.cuda_required.value is True
            )
        ):
            raise HarnessError("UNSUPPORTED", "Image declares incompatible GPU runtime")
        if requirements.gpu_required.value is True or requirements.cuda_required.value is True:
            unknowns.append(
                "GPU device mapping, CUDA/TensorRT and host driver compatibility require separate verification"
            )
        limits = request.resource_limits or target.simulated_limits
        if request.resource_limits is not None and target.simulated_limits is not None:
            limits = ResourceLimits(
                created_at=request.created_at,
                cpu_cores=request.resource_limits.cpu_cores
                if request.resource_limits.cpu_cores is not None
                else target.simulated_limits.cpu_cores,
                ram_bytes=request.resource_limits.ram_bytes
                if request.resource_limits.ram_bytes is not None
                else target.simulated_limits.ram_bytes,
            )
        capacities = target.hardware.resources
        for field in ("cpu_cores", "ram_bytes"):
            value = getattr(limits, field) if limits else None
            if value is None:
                unknowns.append(
                    f"Container {field} limit UNKNOWN; no default allocation is assumed"
                )
                continue
            ceilings = [getattr(capacities, field).value]
            if target.simulated_limits is not None:
                ceilings.append(getattr(target.simulated_limits, field))
            if any(ceiling is not None and value > ceiling for ceiling in ceilings):
                raise HarnessError(
                    "RESOURCE_CONSTRAINT",
                    "Requested allocation exceeds declared target limit or capacity",
                )
            minimum = getattr(requirements, "min_" + field).value
            if minimum is not None and value < minimum:
                unknowns.append(
                    f"Configured {field} limit is below Agent minimum; revision is required"
                )
        if request.working_directory.value is None:
            unknowns.append("Agent working directory UNKNOWN; no WORKDIR is inferred")
        if request.source_complete.value is not True or request.source_complete.evidence in (
            EvidenceKind.ESTIMATED,
            EvidenceKind.SIMULATED,
        ):
            unknowns.append(
                "Source allowlist completeness UNKNOWN; analyzed-files fingerprint is not a complete backup"
            )
        if not request.source_files:
            unknowns.append("Agent source payload missing; package is preparation material only")
        for name, digest in request.source_files.items():
            if name in agent.source.file_hashes and agent.source.file_hashes[name] != digest:
                raise HarnessError(
                    "SOURCE_VERSION_MISMATCH",
                    "Source allowlist conflicts with analyzed Agent version",
                )
            if name not in agent.source.file_hashes:
                unknowns.append(
                    f"Additional payload tracked separately from analyzed fingerprint: {name}"
                )
            if name.endswith((".so", ".dll", ".dylib", ".whl", ".bin", ".a", ".o")):
                unknowns.append(f"Binary payload target ABI/architecture UNKNOWN: {name}")
        if request.installation.mode == InstallationMode.UNRESOLVED:
            unknowns.append("Dependency installation strategy UNKNOWN")
        elif request.installation.mode == InstallationMode.NONE and agent.dependencies:
            raise HarnessError(
                "REQUIREMENT_CONFLICT", "Declared dependencies cannot be silently omitted"
            )
        elif request.installation.mode == InstallationMode.PREINSTALLED:
            if (
                request.image.dependencies_preinstalled.value is not True
                or request.image.dependencies_preinstalled.evidence
                in (EvidenceKind.ESTIMATED, EvidenceKind.SIMULATED)
            ):
                unknowns.append("Dependencies in base image are not confirmed")
        elif request.installation.mode in (InstallationMode.REQUIREMENTS, InstallationMode.PROJECT):
            unknowns.append(
                "Installer availability, dependency fetch/build hooks and target ABI require build validation"
            )
        for variable in agent.environment_variables:
            if variable.name not in request.environment_references:
                unknowns.append(f"Environment binding missing: {variable.name}")
            if variable.required.value is None:
                unknowns.append(f"Environment necessity UNKNOWN: {variable.name}")
        if request.network_required.value is None:
            unknowns.append(
                "Agent network requirement UNKNOWN; default security policy denies access"
            )
        elif (
            request.network_required.value is True
            and request.network.mode != NetworkMode.UNRESTRICTED
        ):
            unknowns.append(
                "Requested network access is blocked until the declared policy can be enforced"
            )
        if request.network.mode in (NetworkMode.ALLOWLIST, NetworkMode.UNKNOWN):
            unknowns.append(
                "Selective network policy is declarative only; effective runtime network is none"
            )
        if request.security.read_only_rootfs_compatible.value is None:
            unknowns.append(
                "Read-only root filesystem compatibility UNKNOWN; option is not applied"
            )
        if target.deployment.docker.value is not True:
            unknowns.append(
                "Target container execution not confirmed; Docker provisioning or runtime validation pending"
            )
        validate_mount_scope(request.mounts, request.allowed_host_paths)
        source_root = Path(agent.source.root)
        for mount in request.mounts:
            source = Path(mount.source)
            if not mount.read_only and (
                source.is_relative_to(source_root) or source_root.is_relative_to(source)
            ):
                raise HarnessError(
                    "UNSAFE_MOUNT", "Writable mounts must not expose original Agent source"
                )
        if request.mounts:
            unknowns.append(
                "Bind-mount permissions and socket/symlink checks must be repeated on deployment host"
            )
        unknowns = tuple(dict.fromkeys(unknowns))
        identity = content_hash(
            {
                "agent": agent.model_dump(mode="json"),
                "target": target.model_dump(mode="json"),
                "host": host.model_dump(mode="json"),
                "request": request.model_dump(mode="json"),
                "requirements": requirements.model_dump(mode="json"),
                "compatibility": report.model_dump(mode="json"),
            }
        )
        return ContainerHarnessSpecification(
            data_requirement=request.data_requirement,
            created_at=request.created_at,
            harness_id=uuid5(NAMESPACE_URL, "adaptive-harness:" + identity),
            agent_id=agent.agent_id,
            agent_source_sha256=agent.source.content_sha256,
            input_fingerprints={
                "agent": content_hash(agent.model_dump(mode="json")),
                "target": content_hash(target.model_dump(mode="json")),
                "host": content_hash(host.model_dump(mode="json")),
            },
            target_id=target.target_id,
            host_id=host.host_id,
            target_kind=target.kind,
            version=request.version,
            parent_harness_id=request.parent_harness_id,
            execution_mode=ExecutionMode.DOCKER,
            argv=request.argv,
            base_image=fact(request.image.reference, "Explicit base image request"),
            image=request.image,
            target_os=target.hardware.platform.os,
            target_architecture=target.hardware.platform.architecture,
            container_platform=platform,
            working_directory=request.working_directory,
            resource_limits=limits,
            environment_references=request.environment_references,
            network=request.network,
            network_required=request.network_required,
            security=request.security,
            allowed_host_paths=request.allowed_host_paths,
            mounts=request.mounts,
            source_files=request.source_files,
            source_complete=request.source_complete,
            installation=request.installation,
            operational_requirements=requirements,
            compatibility=report,
            unknowns=unknowns,
            static_readiness=StaticReadiness.REVIEW_REQUIRED
            if unknowns or verdict != V.FIT
            else StaticReadiness.STATIC_READY,
        )
