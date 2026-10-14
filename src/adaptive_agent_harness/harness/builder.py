"""Render and safely write a container build context. Never invoke Docker or an Agent."""

import hashlib
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path

from adaptive_agent_harness.contracts import AgentProfile, HostProfile, TargetProfile
from adaptive_agent_harness.contracts.data import DataPipelineBinding
from adaptive_agent_harness.contracts.harness import (
    BuildxPlan,
    ContainerHarnessSpecification,
    ContainerPackageManifest,
    InstallationMode,
    PackageFile,
    reject_credentials,
)
from adaptive_agent_harness.data.local import PreparedData, copy_verified, regular_file

from ._utils import (
    HarnessError,
    canonical_json,
    content_hash,
    read_regular,
    validate_mounts,
    write_package,
)
from .data import check_data, validate_payload


@dataclass(frozen=True)
class ContainerPackage:
    directory: Path
    manifest: ContainerPackageManifest


class ContainerHarnessBuilder:
    def __init__(self, *, max_file_bytes: int = 16 * 1024**2, max_total_bytes: int = 64 * 1024**2):
        if max_file_bytes <= 0 or max_total_bytes <= 0:
            raise ValueError("Package size limits must be positive")
        self.max_file_bytes, self.max_total_bytes = max_file_bytes, max_total_bytes

    def build(
        self,
        specification: ContainerHarnessSpecification,
        agent: AgentProfile,
        target: TargetProfile,
        host: HostProfile,
        *,
        output_root: str | Path,
        package_name: str | None = None,
        prepared_data: PreparedData | None = None,
    ) -> ContainerPackage:
        spec = ContainerHarnessSpecification.model_validate_json(specification.model_dump_json())
        for name, profile in (("agent", agent), ("target", target), ("host", host)):
            if spec.input_fingerprints[name] != content_hash(profile.model_dump(mode="json")):
                raise HarnessError(
                    "INPUT_MISMATCH", "Profile changed after specification generation"
                )
        if agent.source.content_sha256 != spec.agent_source_sha256:
            raise HarnessError("SOURCE_VERSION_MISMATCH", "Agent version changed after generation")
        validate_mounts(spec.mounts, spec.allowed_host_paths)
        source_root = Path(agent.source.root)
        for mount in spec.mounts:
            path = Path(mount.source)
            if not mount.read_only and (
                path.is_relative_to(source_root) or source_root.is_relative_to(path)
            ):
                raise HarnessError("UNSAFE_MOUNT", "Writable mount exposes original Agent source")
        files: dict[str, tuple[bytes, int]] = {}
        binding = (
            DataPipelineBinding.model_validate_json(prepared_data.binding.model_dump_json())
            if prepared_data
            else None
        )
        check_data(spec, binding)
        if prepared_data is not None and binding is not None:
            buffer = io.BytesIO()
            with regular_file(prepared_data.directory, prepared_data.path.name) as stream:
                copy_verified(stream, buffer, binding.version.output_sha256, self.max_file_bytes)
            validate_payload(buffer.getvalue(), binding)
            files["data/" + prepared_data.path.name] = (buffer.getvalue(), 0o444)
            files["data-pipeline.json"] = (canonical_json(binding.model_dump(mode="json")), 0o600)
        source_hashes = {}
        total = 0
        for relative, expected in sorted(spec.source_files.items()):
            data, original_mode = read_regular(
                Path(agent.source.root), relative, self.max_file_bytes
            )
            mode = 0o755 if original_mode & 0o111 else 0o644
            digest = hashlib.sha256(data).hexdigest()
            if digest != expected:
                raise HarnessError(
                    "SOURCE_VERSION_MISMATCH",
                    "Source bytes changed after analysis or explicit hashing",
                )
            files["agent/" + relative] = (data, mode)
            source_hashes[relative] = {"sha256": digest, "mode": mode}
            total += len(data)
            if total > self.max_total_bytes:
                raise HarnessError(
                    "PACKAGE_LIMIT", "Source payload exceeds configured package size limit"
                )
        dockerfile = [
            f"FROM {spec.image.reference}",
            f"LABEL org.adaptive-harness.id={json.dumps(str(spec.harness_id))}",
        ]
        if spec.source_files:
            dockerfile.append(
                f"COPY --chown={spec.security.user_id}:{spec.security.group_id} "
                '["agent/", "/opt/agent/"]'
            )
        data_instructions = []
        if binding is not None:
            data_instructions.append("USER 0:0")
            for command in (
                ("test", "!", "-L", "/opt"),
                ("mkdir", "-p", "/opt/data"),
                ("test", "!", "-L", "/opt/data"),
                ("chown", "0:0", "/opt", "/opt/data"),
                ("chmod", "0755", "/opt"),
                ("chmod", "0555", "/opt/data"),
            ):
                data_instructions.append("RUN " + json.dumps(command))
            data_instructions.append(
                "COPY --chown=0:0 "
                + json.dumps(
                    [
                        "data/input." + binding.replay.format.value.lower(),
                        binding.specification.container_path,
                    ]
                )
            )
            if binding.specification.delivery == "ENV_PATH":
                data_instructions.append(
                    "ENV "
                    + binding.specification.environment_name
                    + "="
                    + binding.specification.container_path
                )
        installer = spec.installation
        if installer.mode in (InstallationMode.REQUIREMENTS, InstallationMode.PROJECT):
            argv = [installer.python_executable, "-m", "pip", "install", "--no-cache-dir"]
            if installer.mode == InstallationMode.REQUIREMENTS:
                argv += ["--requirement", "/opt/agent/" + installer.requirements_file]
            else:
                argv += ["/opt/agent"]
            dockerfile.append("RUN " + json.dumps(argv))
        dockerfile.extend(data_instructions)
        if spec.working_directory.value is not None:
            dockerfile.append("WORKDIR " + spec.working_directory.value)
        dockerfile.extend(
            [
                f"USER {spec.security.user_id}:{spec.security.group_id}",
                "ENTRYPOINT " + json.dumps(list(spec.argv)),
                "CMD []",
            ]
        )
        service = {
            "build": {"context": ".", "dockerfile": "Dockerfile"},
            "image": "adaptive-harness:" + str(spec.harness_id),
            "entrypoint": [arg.replace("$", "$$") for arg in spec.argv],
            "command": [],
            "user": f"{spec.security.user_id}:{spec.security.group_id}",
            "privileged": False,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "pids_limit": spec.security.pids_limit,
            "read_only": spec.security.read_only_rootfs_compatible.value is True,
            "network_mode": spec.network.effective_network_mode,
            "profiles": ["pending-validation"],
            "restart": "no",
        }
        if spec.container_platform.value is not None:
            service["platform"] = spec.container_platform.value.value
            service["build"]["platforms"] = [spec.container_platform.value.value]
        if spec.working_directory.value is not None:
            service["working_dir"] = spec.working_directory.value
        if spec.resource_limits:
            if spec.resource_limits.cpu_cores is not None:
                service["cpus"] = spec.resource_limits.cpu_cores
            if spec.resource_limits.ram_bytes is not None:
                service["mem_limit"] = spec.resource_limits.ram_bytes
        if spec.environment_references:
            service["environment"] = {
                name: "${" + name + "?Provide externally or remove this binding}"
                for name in sorted(spec.environment_references)
            }
        if spec.mounts:
            service["volumes"] = [
                {
                    "type": "bind",
                    "source": mount.source.replace("$", "$$"),
                    "target": mount.target,
                    "read_only": mount.read_only,
                    "bind": {"create_host_path": False},
                }
                for mount in spec.mounts
            ]
        template = "# No secret values are included. Set values outside the generated package.\n"
        template += "# Blank placeholders are not confirmed Agent defaults.\n"
        template += "".join(f"{name}=\n" for name in sorted(spec.environment_references))
        notes = (
            "\n".join("- " + note for note in spec.unknowns)
            or "- No unresolved static input checks."
        )
        readme = f"""# Container Harness preparation package

Static status: {spec.static_readiness.value}
Image built: false. Agent executed: false. Security review: UNVERIFIED.
Hardware execution (including ARM64), driver/CUDA/TensorRT and performance are not validated.

Only explicitly allowlisted source files were copied; the original Agent was not modified.
The analyzed-files fingerprint and copied-source bundle hash have different scopes.
Review manifest.json checksums and harness.yaml before building. YAML files use JSON syntax,
which is valid YAML 1.2. Preserve the manifest outside any untrusted build context for comparison.

## Required review and verification

{notes}

Runtime network mode: {spec.network.effective_network_mode}.
Network policy enforcement: {spec.network.enforcement}.
An allowlist declaration does not configure a firewall; deny fallback remains until external enforcement.
Read-only root filesystem is enabled only when compatibility was explicitly confirmed.
UID/GID and PID limits are security policies, not measured Agent requirements.
Build-time dependency downloads are separate from runtime network restrictions.
Installer RUN instructions are fixed exec-form argv; project/dependency build hooks still need review.
Secrets and environment values must be supplied externally. .env.example has names and blank placeholders only.
Resolve UNKNOWN and ADAPT_REQUIRED inputs, source completeness, image platform/runtime, writable paths,
mount permissions and external security review on the actual deployment host before attempting execution.

## Commands for a separately verified Docker host

The platform has not executed these commands. Check Docker/Compose/Buildx support first.
From this directory, after the above review:

```bash
docker compose --env-file /absolute/path/to/private.env config
docker compose --profile pending-validation --env-file /absolute/path/to/private.env build
docker compose --profile pending-validation --env-file /absolute/path/to/private.env up --abort-on-container-exit
```

The explicit profile prevents default compose up from starting a pending package.
BuildxPlan in manifest.json exports an OCI archive; it does not build, load, push or emulate here.
Validate exit status, functional quality, actual limits and source/image hashes in the future Baseline Runner.
Rollback: stop/remove the trial containers and retain the original Agent and prior package; source is untouched.
"""
        files.update(
            {
                "Dockerfile": (("\n".join(dockerfile) + "\n").encode(), 0o644),
                "compose.yaml": (canonical_json({"services": {"agent": service}}), 0o644),
                "harness.yaml": (canonical_json(spec.model_dump(mode="json")), 0o644),
                ".env.example": (template.encode(), 0o644),
                ".dockerignore": (
                    b"**\n!Dockerfile\n!agent/\n!agent/**\n"
                    + (b"!data/\n!data/**\n" if binding else b""),
                    0o644,
                ),
                "README.md": (readme.encode(), 0o644),
            }
        )
        self._check_payload(files)
        serialized_spec = files["harness.yaml"][0]
        created_at = spec.created_at
        manifest = ContainerPackageManifest(
            created_at=created_at,
            harness_id=spec.harness_id,
            agent_id=spec.agent_id,
            agent_source_sha256=spec.agent_source_sha256,
            source_bundle_sha256=content_hash(source_hashes),
            target_id=spec.target_id,
            host_id=spec.host_id,
            target_architecture=spec.target_architecture,
            host_architecture=host.hardware.platform.architecture,
            container_platform=spec.container_platform,
            base_image_platforms=spec.image.platforms,
            specification_sha256=hashlib.sha256(serialized_spec).hexdigest(),
            files=tuple(
                PackageFile(
                    created_at=created_at,
                    path=name,
                    sha256=hashlib.sha256(data).hexdigest(),
                    size_bytes=len(data),
                    mode=mode,
                )
                for name, (data, mode) in sorted(files.items())
            ),
            build_plan=BuildxPlan(
                created_at=created_at,
                platforms=(spec.container_platform.value,) if spec.container_platform.value else (),
            ),
            static_readiness=spec.static_readiness,
            unknowns=spec.unknowns,
        )
        files["manifest.json"] = (canonical_json(manifest.model_dump(mode="json")), 0o644)
        self._check_payload(files)
        output = Path(output_root).absolute()
        directory = write_package(
            output,
            package_name if package_name is not None else "harness-" + str(spec.harness_id),
            files,
            private=binding is not None,
        )
        return ContainerPackage(directory=directory, manifest=manifest)

    def _check_payload(self, files: dict[str, tuple[bytes, int]]) -> None:
        if sum(len(data) for data, _ in files.values()) > self.max_total_bytes:
            raise HarnessError("PACKAGE_LIMIT", "Generated package exceeds configured size limit")
        for data, _ in files.values():
            try:
                reject_credentials(data.decode("utf-8", errors="replace"))
            except ValueError:
                raise HarnessError(
                    "SENSITIVE_CONTENT", "Suspected credential content blocks packaging"
                ) from None

    def verify(self, directory: str | Path) -> ContainerPackageManifest:
        root = Path(directory).absolute()
        data, _ = read_regular(root, "manifest.json", self.max_file_bytes)
        manifest = ContainerPackageManifest.model_validate_json(data)
        if len({f.path for f in manifest.files}) != len(manifest.files):
            raise HarnessError("PACKAGE_INTEGRITY", "Manifest contains duplicate file paths")
        actual_files = set()
        for parent, dirs, names in os.walk(root, followlinks=False):
            if any((Path(parent) / name).is_symlink() for name in dirs):
                raise HarnessError("PACKAGE_INTEGRITY", "Package directory contains a symlink")
            actual_files.update(
                (Path(parent) / name).relative_to(root).as_posix() for name in names
            )
        if actual_files != {"manifest.json", *[f.path for f in manifest.files]}:
            raise HarnessError("PACKAGE_INTEGRITY", "Unexpected or missing package files")
        source_hashes = {}
        for entry in manifest.files:
            data, mode = read_regular(root, entry.path, self.max_file_bytes)
            if (
                hashlib.sha256(data).hexdigest() != entry.sha256
                or len(data) != entry.size_bytes
                or mode != entry.mode
            ):
                raise HarnessError(
                    "PACKAGE_INTEGRITY", "Package file content or permissions changed"
                )
            if entry.path.startswith("agent/"):
                source_hashes[entry.path[6:]] = {"sha256": entry.sha256, "mode": entry.mode}
        data, _ = read_regular(root, "harness.yaml", self.max_file_bytes)
        if hashlib.sha256(data).hexdigest() != manifest.specification_sha256:
            raise HarnessError("PACKAGE_INTEGRITY", "Harness specification checksum mismatch")
        spec = ContainerHarnessSpecification.model_validate_json(data)
        binding = self.data_binding(root, spec)
        if binding is not None:
            entries = {entry.path: entry for entry in manifest.files}
            entry = entries.get("data/input." + binding.replay.format.value.lower())
            if (
                entry is None
                or entry.sha256 != binding.version.output_sha256
                or entry.mode != 0o444
            ):
                raise HarnessError(
                    "DATA_VERSION_MISMATCH", "Data bytes do not match validated input"
                )
            if root.stat().st_mode & 0o777 != 0o700:
                raise HarnessError("DATA_PACKAGE_PRIVACY", "Data package must remain private")
            payload, _ = read_regular(root, entry.path, self.max_file_bytes)
            validate_payload(payload, binding)
        if (
            spec.harness_id != manifest.harness_id
            or spec.agent_id != manifest.agent_id
            or spec.target_id != manifest.target_id
            or spec.host_id != manifest.host_id
            or spec.agent_source_sha256 != manifest.agent_source_sha256
            or source_hashes.keys() != spec.source_files.keys()
            or any(
                source_hashes[path]["sha256"] != digest
                for path, digest in spec.source_files.items()
            )
            or content_hash(source_hashes) != manifest.source_bundle_sha256
        ):
            raise HarnessError(
                "PACKAGE_INTEGRITY",
                "Manifest and specification identities or source snapshots differ",
            )
        return manifest

    def data_binding(
        self, root: Path, spec: ContainerHarnessSpecification
    ) -> DataPipelineBinding | None:
        path = root / "data-pipeline.json"
        binding = None
        if path.exists() or path.is_symlink():
            data, _ = read_regular(root, "data-pipeline.json", self.max_file_bytes)
            binding = DataPipelineBinding.model_validate_json(data)
        check_data(spec, binding)
        return binding
