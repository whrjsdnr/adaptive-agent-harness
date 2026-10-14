"""Container preparation contracts; no image build or execution is performed."""

import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, Field, StrictBool, StringConstraints, model_validator

from .base import SHA256, Contract, EvidenceKind, Fact, PositiveInteger, Text
from .data import DataRequirement
from .incubation import HarnessSpecification
from .profiles import CompatibilityReport, OperationalRequirements, ResourceLimits, TargetKind


def reject_credentials(value: str) -> str:
    """Reject common credential forms without putting their values in diagnostics."""
    patterns = (
        r"-----BEGIN (?:[A-Z ]+)?(?:PRIVATE KEY|CERTIFICATE)-----",
        r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
        r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b",
        r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b",
        r"https?://[^\s/:]+:[^\s/@]+@",
        r"(?i)(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|client[_-]?secret|password|aws_secret_access_key)"
        r"[\"']?\s*[=:]\s*[\"']?[A-Za-z0-9_+/=-]{16,}",
    )
    if any(re.search(pattern, value) for pattern in patterns):
        raise ValueError("Suspected credential content is forbidden")
    return value


def reject_sensitive_text(value: str) -> str:
    reject_credentials(value)
    if re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError("Control characters are forbidden")
    return value


def relative_artifact_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or value in (".", "..")
        or ".." in path.parts
        or "\\" in value
    ):
        raise ValueError("Expected a normalized relative artifact path")
    return value


def relative_source_path(value: str) -> str:
    relative_artifact_path(value)
    path = PurePosixPath(value)
    forbidden = {
        ".git",
        ".aws",
        ".codex",
        ".agents",
        ".ssh",
        ".venv",
        "venv",
        "__pycache__",
        ".cache",
    }
    if (
        path.is_absolute()
        or str(path) != value
        or value in (".", "..")
        or ".." in path.parts
        or "\\" in value
        or any(p in forbidden or p.startswith(".env") for p in path.parts)
        or path.name.lower().startswith(("id_rsa", "id_ed25519", "credentials", "secrets", "token"))
        or path.suffix.lower() in (".pem", ".key", ".crt", ".cer", ".p12", ".pfx", ".sock")
    ):
        raise ValueError("Expected a safe relative source path without private files")
    return value


def absolute_path(value: str) -> str:
    path = PurePosixPath(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts or "\\" in value:
        raise ValueError("Expected a normalized absolute path")
    return value


SafeText = Annotated[Text, AfterValidator(reject_sensitive_text)]
SourcePath = Annotated[SafeText, AfterValidator(relative_source_path)]
ArtifactPath = Annotated[SafeText, AfterValidator(relative_artifact_path)]
HostPath = Annotated[SafeText, AfterValidator(absolute_path)]
ContainerPath = Annotated[HostPath, StringConstraints(pattern=r"^/[A-Za-z0-9_./-]*$")]
EnvName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", strict=True)]
SecretReference = Annotated[
    SafeText, StringConstraints(pattern=r"^(?:secret|env)://[A-Za-z0-9_./-]+$")
]
ImageReference = Annotated[SafeText, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/:@+-]*$")]


class ContainerPlatform(StrEnum):
    LINUX_AMD64 = "linux/amd64"
    LINUX_ARM64 = "linux/arm64"


class BaseImage(Contract):
    reference: ImageReference
    platforms: tuple[ContainerPlatform, ...] = ()
    python_version: Fact[Text] = Field(default_factory=Fact)
    dependencies_preinstalled: Fact[StrictBool] = Field(default_factory=Fact)
    gpu_runtime_compatible: Fact[StrictBool] = Field(default_factory=Fact)

    @model_validator(mode="after")
    def explicit_image_reference(self) -> Self:
        if "@" in self.reference and not re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", self.reference):
            raise ValueError(
                "Image digest must be explicit SHA-256; embedded credentials are forbidden"
            )
        if "://" in self.reference:
            raise ValueError("Image reference must not be a credential-bearing URL")
        name = self.reference.split("@", 1)[0]
        component = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
        registry = r"(?:[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*(?::[0-9]+)?/)"
        tag = r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
        if not re.fullmatch(rf"(?:{registry})?(?:{component}/)*{component}{tag}", name):
            raise ValueError("Invalid image repository or tag syntax")
        return self


class NetworkMode(StrEnum):
    DENY = "DENY"
    UNRESTRICTED = "UNRESTRICTED"
    ALLOWLIST = "ALLOWLIST"
    UNKNOWN = "UNKNOWN"


class NetworkPolicy(Contract):
    mode: NetworkMode = NetworkMode.DENY
    allowed_destinations: tuple[SafeText, ...] = ()

    @model_validator(mode="after")
    def destination_scope(self) -> Self:
        if (self.mode == NetworkMode.ALLOWLIST) != bool(self.allowed_destinations):
            raise ValueError("Only ALLOWLIST requires destination declarations")
        return self

    @property
    def effective_network_mode(self) -> str:
        # Compose cannot enforce an egress hostname/IP allowlist. Fail closed.
        return "bridge" if self.mode == NetworkMode.UNRESTRICTED else "none"

    @property
    def enforcement(self) -> str:
        return (
            "DECLARATIVE_WITH_DENY_FALLBACK"
            if self.mode in (NetworkMode.ALLOWLIST, NetworkMode.UNKNOWN)
            else "COMPOSE_NETWORK_MODE"
        )


class BindMount(Contract):
    source: HostPath
    target: ContainerPath
    read_only: StrictBool = True

    @model_validator(mode="after")
    def no_runtime_sockets(self) -> Self:
        for name in (self.source, self.target):
            path = PurePosixPath(name)
            if path == PurePosixPath("/") or path.suffix == ".sock" or "docker.sock" in path.parts:
                raise ValueError("Root filesystem and socket mounts are forbidden")
            for reserved in ("/run", "/var/run", "/proc", "/sys", "/dev"):
                if path.is_relative_to(reserved) or PurePosixPath(reserved).is_relative_to(path):
                    raise ValueError("Runtime control filesystem mounts are forbidden")
        if PurePosixPath("/opt/agent").is_relative_to(self.target):
            raise ValueError("Mount cannot replace packaged Agent source")
        return self


class SecuritySettings(Contract):
    user_id: PositiveInteger = 65532
    group_id: PositiveInteger = 65532
    pids_limit: PositiveInteger = 256
    privileged: Literal[False] = False
    cap_drop: tuple[Literal["ALL"], ...] = ("ALL",)
    no_new_privileges: Literal[True] = True
    read_only_rootfs_compatible: Fact[StrictBool] = Field(default_factory=Fact)

    @model_validator(mode="after")
    def verified_security_options(self) -> Self:
        if self.cap_drop != ("ALL",):
            raise ValueError("All Linux capabilities must be dropped")
        if self.read_only_rootfs_compatible.evidence in (
            EvidenceKind.ESTIMATED,
            EvidenceKind.SIMULATED,
        ):
            raise ValueError("Read-only compatibility requires evidence or an explicit declaration")
        return self


class InstallationMode(StrEnum):
    UNRESOLVED = "UNRESOLVED"
    NONE = "NONE"
    REQUIREMENTS = "REQUIREMENTS"
    PROJECT = "PROJECT"
    PREINSTALLED = "PREINSTALLED"


class DependencyInstallation(Contract):
    mode: InstallationMode = InstallationMode.UNRESOLVED
    python_executable: SafeText | None = None
    requirements_file: SourcePath | None = None

    @model_validator(mode="after")
    def explicit_installer(self) -> Self:
        if self.mode in (InstallationMode.REQUIREMENTS, InstallationMode.PROJECT):
            if self.python_executable is None or not re.fullmatch(
                r"[A-Za-z0-9_./-]+", self.python_executable
            ):
                raise ValueError("Installation requires an explicit Python executable")
            if not re.fullmatch(
                r"python(?:[0-9]+(?:\.[0-9]+)*)?", PurePosixPath(self.python_executable).name
            ):
                raise ValueError("Installer executable must be a Python interpreter")
            if ".." in PurePosixPath(self.python_executable).parts:
                raise ValueError("Installer path cannot contain traversal")
        if (self.mode == InstallationMode.REQUIREMENTS) != (self.requirements_file is not None):
            raise ValueError("Only REQUIREMENTS requires a requirements file")
        return self


class ContainerHarnessRequest(Contract):
    data_requirement: DataRequirement | None = Field(default=None, exclude_if=lambda v: v is None)
    image: BaseImage
    argv: tuple[SafeText, ...] = ()
    working_directory: Fact[ContainerPath] = Field(default_factory=Fact)
    resource_limits: ResourceLimits | None = None
    environment_references: dict[EnvName, SecretReference] = Field(default_factory=dict)
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    network_required: Fact[StrictBool] = Field(default_factory=Fact)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    allowed_host_paths: tuple[HostPath, ...] = ()
    mounts: tuple[BindMount, ...] = ()
    source_files: dict[SourcePath, SHA256] = Field(default_factory=dict)
    source_complete: Fact[StrictBool] = Field(default_factory=Fact)
    installation: DependencyInstallation = Field(default_factory=DependencyInstallation)
    version: SafeText = "baseline-1"
    parent_harness_id: UUID | None = None

    @model_validator(mode="after")
    def no_shell_entrypoint(self) -> Self:
        if self.argv:
            executable = PurePosixPath(self.argv[0]).name.lower()
            if executable in (
                "sh",
                "bash",
                "dash",
                "zsh",
                "ash",
                "ksh",
                "fish",
                "cmd",
                "cmd.exe",
                "powershell",
            ):
                raise ValueError("Shell entrypoints are not supported")
            if executable.startswith("python"):
                for argument in self.argv[1:]:
                    if argument in ("-c", "--eval"):
                        raise ValueError("Inline executable code is not supported")
                    if argument == "-m" or not argument.startswith("-"):
                        break
        if self.installation.mode == InstallationMode.REQUIREMENTS:
            if self.installation.requirements_file not in self.source_files:
                raise ValueError("Requirements file must be in the explicit source allowlist")
        if self.installation.mode == InstallationMode.PROJECT:
            if not {"pyproject.toml", "setup.py"}.intersection(self.source_files):
                raise ValueError("Project installation requires explicit project metadata")
        return self


class StaticReadiness(StrEnum):
    STATIC_READY = "STATIC_READY"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class ContainerHarnessSpecification(HarnessSpecification):
    data_requirement: DataRequirement | None = Field(default=None, exclude_if=lambda v: v is None)
    schema_version: Literal["1.1"] = "1.1"
    host_id: UUID
    input_fingerprints: dict[Literal["agent", "target", "host"], SHA256]
    target_kind: TargetKind
    target_os: Fact[Text]
    target_architecture: Fact[Text]
    container_platform: Fact[ContainerPlatform]
    image: BaseImage
    working_directory: Fact[ContainerPath]
    network: NetworkPolicy
    network_required: Fact[StrictBool]
    security: SecuritySettings
    allowed_host_paths: tuple[HostPath, ...]
    mounts: tuple[BindMount, ...]
    source_files: dict[SourcePath, SHA256]
    source_complete: Fact[StrictBool]
    installation: DependencyInstallation
    operational_requirements: OperationalRequirements
    compatibility: CompatibilityReport
    unknowns: tuple[SafeText, ...] = ()
    static_readiness: StaticReadiness
    execution_verified: Literal[False] = False
    environment_references: dict[EnvName, SecretReference] = Field(default_factory=dict)
    argv: tuple[SafeText, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def linked_specification(self) -> Self:
        if set(self.input_fingerprints) != {"agent", "target", "host"}:
            raise ValueError("All three input profile fingerprints are required")
        if (
            self.compatibility.agent_id != self.agent_id
            or self.compatibility.target_id != self.target_id
            or self.compatibility.verdict.value == "UNSUPPORTED"
            or self.execution_mode.value != "DOCKER"
        ):
            raise ValueError("Container specification must preserve compatible input identities")
        if self.base_image.value != self.image.reference:
            raise ValueError("Base image references must agree")
        expected = (
            StaticReadiness.REVIEW_REQUIRED
            if self.unknowns or self.compatibility.verdict.value != "FIT"
            else StaticReadiness.STATIC_READY
        )
        if self.static_readiness != expected:
            raise ValueError("Static readiness cannot hide uncertainty")
        ContainerHarnessRequest.model_validate(
            {
                "image": self.image,
                "data_requirement": self.data_requirement,
                "argv": self.argv,
                "working_directory": self.working_directory,
                "resource_limits": self.resource_limits,
                "environment_references": self.environment_references,
                "network": self.network,
                "network_required": self.network_required,
                "security": self.security,
                "allowed_host_paths": self.allowed_host_paths,
                "mounts": self.mounts,
                "source_files": self.source_files,
                "source_complete": self.source_complete,
                "installation": self.installation,
                "version": self.version,
                "parent_harness_id": self.parent_harness_id,
            }
        )
        return self


class BuildxPlan(Contract):
    platforms: tuple[ContainerPlatform, ...] = ()
    executed: Literal[False] = False
    output: Literal["image.oci.tar"] = "image.oci.tar"

    def arguments(self) -> tuple[str, ...]:
        if not self.platforms:
            raise ValueError("Container platform is UNKNOWN")
        return (
            "docker",
            "buildx",
            "build",
            "--platform",
            ",".join(self.platforms),
            "--output",
            f"type=oci,dest={self.output}",
            ".",
        )


class PackageFile(Contract):
    path: ArtifactPath
    sha256: SHA256
    size_bytes: Annotated[int, Field(ge=0, strict=True)]
    mode: Literal[0o400, 0o444, 0o600, 0o644, 0o755] = 0o644


class ContainerPackageManifest(Contract):
    schema_version: Literal["1.1"] = "1.1"
    harness_id: UUID
    agent_id: UUID
    agent_source_sha256: SHA256
    source_bundle_sha256: SHA256
    target_id: UUID
    host_id: UUID
    target_architecture: Fact[Text]
    host_architecture: Fact[Text]
    container_platform: Fact[ContainerPlatform]
    base_image_platforms: tuple[ContainerPlatform, ...]
    specification_sha256: SHA256
    files: tuple[PackageFile, ...]
    build_plan: BuildxPlan
    static_readiness: StaticReadiness
    unknowns: tuple[SafeText, ...]
    execution_verified: Literal[False] = False
    image_built: Literal[False] = False
    security_review: Literal["UNVERIFIED"] = "UNVERIFIED"
