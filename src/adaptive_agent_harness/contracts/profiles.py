"""Independent agent, target, host, runtime and deployment contracts."""

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import Field, StrictBool, field_validator, model_validator

from .base import SHA256, Contract, EvidenceKind, Fact, Knowledge, PositiveInteger, Text


class ExecutionMode(StrEnum):
    PYTHON = "PYTHON"
    DOCKER = "DOCKER"
    VM = "VM"


class PlatformInfo(Contract):
    os: Fact[Text] = Field(default_factory=Fact)
    architecture: Fact[Text] = Field(default_factory=Fact)
    abi: Fact[Text] = Field(default_factory=Fact)


class ResourceCapacity(Contract):
    cpu_cores: Fact[PositiveInteger] = Field(default_factory=Fact)
    ram_bytes: Fact[PositiveInteger] = Field(default_factory=Fact)


class HardwareInfo(Contract):
    platform: PlatformInfo = Field(default_factory=PlatformInfo)
    resources: ResourceCapacity = Field(default_factory=ResourceCapacity)
    gpu_present: Fact[StrictBool] = Field(default_factory=Fact)

    @model_validator(mode="after")
    def physical_facts(self) -> Self:
        facts = (
            self.platform.os,
            self.platform.architecture,
            self.platform.abi,
            self.resources.cpu_cores,
            self.resources.ram_bytes,
            self.gpu_present,
        )
        if any(f.evidence in (EvidenceKind.SIMULATED, EvidenceKind.ESTIMATED) for f in facts):
            raise ValueError("Hardware facts cannot contain simulated or estimated capacities")
        return self


class ResourceLimits(Contract):
    cpu_cores: Annotated[float, Field(gt=0, strict=True)] | None = None
    ram_bytes: PositiveInteger | None = None

    @model_validator(mode="after")
    def nonempty_limits(self) -> Self:
        if self.cpu_cores is None and self.ram_bytes is None:
            raise ValueError("At least one resource limit is required")
        return self


class RuntimeInfo(Contract):
    name: Text
    version: Fact[Text] = Field(default_factory=Fact)
    available: Fact[StrictBool] = Field(default_factory=Fact)

    @model_validator(mode="after")
    def factual_runtime(self) -> Self:
        if any(
            f.evidence in (EvidenceKind.SIMULATED, EvidenceKind.ESTIMATED)
            for f in (self.version, self.available)
        ):
            raise ValueError("Runtime support cannot be inferred from simulation or estimates")
        return self


class DeploymentCapabilities(Contract):
    python: Fact[StrictBool] = Field(default_factory=Fact)
    docker_cli: Fact[StrictBool] = Field(default_factory=Fact)
    docker_access: Fact[StrictBool] = Field(default_factory=Fact)
    docker: Fact[StrictBool] = Field(default_factory=Fact)
    vm: Fact[StrictBool] = Field(default_factory=Fact)
    cuda: Fact[StrictBool] = Field(default_factory=Fact)

    @model_validator(mode="after")
    def factual_capabilities(self) -> Self:
        for name in ("python", "docker_cli", "docker_access", "docker", "vm", "cuda"):
            if getattr(self, name).evidence in (EvidenceKind.SIMULATED, EvidenceKind.ESTIMATED):
                raise ValueError(
                    "Execution capability cannot be inferred from simulation or estimates"
                )
        if self.docker.value is True and (
            self.docker_cli.value is not True or self.docker_access.value is not True
        ):
            raise ValueError("Confirmed Docker execution requires confirmed CLI and access")
        return self


class HostProfile(Contract):
    host_id: UUID = Field(default_factory=uuid4)
    hardware: HardwareInfo
    runtimes: tuple[RuntimeInfo, ...] = ()
    deployment: DeploymentCapabilities = Field(default_factory=DeploymentCapabilities)

    @model_validator(mode="after")
    def measured_host(self) -> Self:
        facts = (
            self.hardware.platform.os,
            self.hardware.platform.architecture,
            self.hardware.platform.abi,
            self.hardware.resources.cpu_cores,
            self.hardware.resources.ram_bytes,
            self.hardware.gpu_present,
            *[
                getattr(self.deployment, f)
                for f in DeploymentCapabilities.model_fields
                if f not in Contract.model_fields
            ],
            *[f for r in self.runtimes for f in (r.available, r.version)],
        )
        if any(f.status == Knowledge.KNOWN and f.evidence != EvidenceKind.MEASURED for f in facts):
            raise ValueError("Known host facts must be measured, not user declarations")
        return self


class TargetKind(StrEnum):
    HARDWARE = "HARDWARE"
    RESOURCE_SIMULATION = "RESOURCE_SIMULATION"


class TargetProfile(Contract):
    target_id: UUID = Field(default_factory=uuid4)
    name: Text
    kind: TargetKind = TargetKind.HARDWARE
    hardware: HardwareInfo
    runtimes: tuple[RuntimeInfo, ...] = ()
    deployment: DeploymentCapabilities = Field(default_factory=DeploymentCapabilities)
    simulated_limits: ResourceLimits | None = None
    based_on_host_id: UUID | None = None

    @model_validator(mode="after")
    def separate_simulation(self) -> Self:
        if (self.kind == TargetKind.RESOURCE_SIMULATION) != (self.simulated_limits is not None):
            raise ValueError("Only RESOURCE_SIMULATION targets require simulated_limits")
        return self

    @classmethod
    def simulate_resources(cls, host: HostProfile, limits: ResourceLimits, name: str) -> Self:
        """Describe limits on a host without applying limits or emulating hardware."""
        return cls(
            name=name,
            kind=TargetKind.RESOURCE_SIMULATION,
            hardware=host.hardware.model_copy(deep=True),
            runtimes=host.runtimes,
            deployment=host.deployment.model_copy(deep=True),
            simulated_limits=limits,
            based_on_host_id=host.host_id,
        )


class OperationalRequirements(Contract):
    os: Fact[Text] = Field(default_factory=Fact)
    architecture: Fact[Text] = Field(default_factory=Fact)
    python_specifier: Fact[Text] = Field(default_factory=Fact)
    min_cpu_cores: Fact[PositiveInteger] = Field(default_factory=Fact)
    min_ram_bytes: Fact[PositiveInteger] = Field(default_factory=Fact)
    gpu_required: Fact[StrictBool] = Field(default_factory=Fact)
    cuda_required: Fact[StrictBool] = Field(default_factory=Fact)
    execution_mode: Fact[ExecutionMode] = Field(default_factory=Fact)

    @field_validator("python_specifier")
    @classmethod
    def valid_specifier(cls, value: Fact[str]) -> Fact[str]:
        if value.value is not None:
            try:
                if not str(SpecifierSet(value.value)):
                    raise ValueError("Python specifier cannot be empty")
            except InvalidSpecifier as exc:
                raise ValueError("Invalid Python version specifier") from exc
        return value

    @model_validator(mode="after")
    def coherent_gpu(self) -> Self:
        if self.cuda_required.value is True and self.gpu_required.value is False:
            raise ValueError("CUDA requirement conflicts with declared no-GPU requirement")
        return self


class AgentSource(Contract):
    root: Text
    content_sha256: SHA256
    file_hashes: dict[Text, SHA256]
    fingerprint_scope: Literal["analyzed_files"] = "analyzed_files"

    @field_validator("file_hashes")
    @classmethod
    def safe_relative_paths(cls, value: dict[str, str]) -> dict[str, str]:
        for name in value:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name:
                raise ValueError("Source hashes require relative paths inside the project")
        return value


class Dependency(Contract):
    name: Text
    specifier: str = ""
    extras: tuple[Text, ...] = ()
    marker: Text | None = None
    source: Text
    direct_reference: bool = False


class EntryPoint(Contract):
    kind: Text
    name: Text
    value: Text
    source: Text
    execution_verified: bool = False

    @model_validator(mode="after")
    def static_only(self) -> Self:
        if self.execution_verified:
            raise ValueError("Static entrypoints cannot claim verified execution")
        return self


class EnvironmentVariable(Contract):
    name: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    source: Text
    access: Text
    required: Fact[StrictBool] = Field(default_factory=Fact)


class ConfigurationInterface(Contract):
    kind: Text
    name: Text
    source: Text


class AgentProfile(Contract):
    agent_id: UUID = Field(default_factory=uuid4)
    project_name: Fact[Text] = Field(default_factory=Fact)
    source: AgentSource
    requirements: OperationalRequirements
    python_files: tuple[Text, ...] = ()
    entrypoints: tuple[EntryPoint, ...] = ()
    dependencies: tuple[Dependency, ...] = ()
    environment_variables: tuple[EnvironmentVariable, ...] = ()
    configuration_interfaces: tuple[ConfigurationInterface, ...] = ()
    unknowns: tuple[Text, ...] = ()


class CompatibilityVerdict(StrEnum):
    FIT = "FIT"
    ADAPT_REQUIRED = "ADAPT_REQUIRED"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


class CompatibilityCheck(Contract):
    area: Text
    verdict: CompatibilityVerdict
    reason: Text
    sources: tuple[Text, ...] = ()


class CompatibilityReport(Contract):
    agent_id: UUID
    target_id: UUID
    verdict: CompatibilityVerdict
    checks: tuple[CompatibilityCheck, ...] = Field(min_length=1)
    unknowns: tuple[Text, ...] = ()
    scope: Literal["static_requirements_only"] = "static_requirements_only"

    @model_validator(mode="after")
    def coherent_verdict(self) -> Self:
        priority = (
            CompatibilityVerdict.UNSUPPORTED,
            CompatibilityVerdict.UNKNOWN,
            CompatibilityVerdict.ADAPT_REQUIRED,
            CompatibilityVerdict.FIT,
        )
        expected = next(v for v in priority if any(c.verdict == v for c in self.checks))
        if self.verdict != expected:
            raise ValueError("Overall verdict must preserve failures and uncertainty")
        return self
