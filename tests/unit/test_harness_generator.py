from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from adaptive_agent_harness.analyzer import CompatibilityEngine
from adaptive_agent_harness.contracts import (
    BaseImage,
    BindMount,
    BuildxPlan,
    CompatibilityCheck,
    CompatibilityReport,
    ContainerHarnessSpecification,
    ContainerPlatform,
    EvidenceKind,
    ExecutionMode,
    Fact,
    HarnessSpecification,
    ResourceLimits,
    SecuritySettings,
    StaticReadiness,
    TargetProfile,
    known,
)
from adaptive_agent_harness.harness import HarnessError, HarnessSpecificationGenerator


def change(model, **updates):
    return type(model).model_validate({**model.model_dump(), **updates})


def generate(inputs, **updates):
    agent, target, host, report, request = inputs
    return HarnessSpecificationGenerator().generate(
        agent, target, host, report, change(request, **updates)
    )


def test_normal_specification_reuses_contract_and_preserves_inputs(container_inputs):
    agent, target, host, report, request = container_inputs
    before = agent.model_dump_json(), target.model_dump_json(), host.model_dump_json()
    spec = generate(container_inputs)
    assert isinstance(spec, HarnessSpecification)
    assert spec.schema_version == "1.1"
    assert spec.static_readiness == StaticReadiness.STATIC_READY
    assert not spec.execution_verified
    assert spec.execution_mode.value == "DOCKER"
    assert spec.resource_limits == request.resource_limits
    assert spec.working_directory.value == "/opt/agent"
    assert spec.container_platform.value == ContainerPlatform.LINUX_AMD64
    assert spec.agent_source_sha256 == agent.source.content_sha256
    assert before == (agent.model_dump_json(), target.model_dump_json(), host.model_dump_json())
    assert ContainerHarnessSpecification.model_validate_json(spec.model_dump_json()) == spec
    with pytest.raises(ValidationError):
        HarnessSpecification.model_validate_json(spec.model_dump_json())


def test_missing_entrypoint_has_explicit_unknown(container_inputs):
    with pytest.raises(HarnessError) as error:
        generate(container_inputs, argv=())
    assert error.value.code == "MISSING_REQUIREMENTS"
    assert "UNKNOWN" in error.value.unknowns[0]


def test_unknown_limits_workdir_and_image_are_not_guessed(container_inputs):
    image = change(
        container_inputs[-1].image,
        platforms=(),
        python_version=Fact(),
        reference="python:unverified",
    )
    spec = generate(container_inputs, resource_limits=None, working_directory=Fact(), image=image)
    assert spec.resource_limits is None
    assert spec.working_directory.value is None
    assert spec.static_readiness == StaticReadiness.REVIEW_REQUIRED
    assert any("cpu_cores limit UNKNOWN" in note for note in spec.unknowns)
    assert any("ram_bytes limit UNKNOWN" in note for note in spec.unknowns)
    assert any("image architecture support UNKNOWN" in note for note in spec.unknowns)
    assert not spec.execution_verified


def test_unsupported_report_and_stale_report_block_generation(container_inputs):
    agent, target, host, report, request = container_inputs
    blocked = CompatibilityReport(
        agent_id=agent.agent_id,
        target_id=target.target_id,
        verdict="UNSUPPORTED",
        checks=(
            CompatibilityCheck(
                area="architecture", verdict="UNSUPPORTED", reason="declared mismatch"
            ),
        ),
    )
    with pytest.raises(HarnessError, match="UNSUPPORTED"):
        HarnessSpecificationGenerator().generate(agent, target, host, blocked, request)
    mismatch = change(report, agent_id=uuid4())
    with pytest.raises(HarnessError, match="INPUT_MISMATCH"):
        HarnessSpecificationGenerator().generate(agent, target, host, mismatch, request)
    new_target = change(
        target,
        hardware=change(
            target.hardware,
            platform=change(
                target.hardware.platform, architecture=known("aarch64", "changed target")
            ),
        ),
    )
    # Target ID did not change, but recomputing compatibility catches a stale FIT report.
    with pytest.raises(HarnessError, match="UNSUPPORTED"):
        HarnessSpecificationGenerator().generate(agent, new_target, host, report, request)


def test_inherited_simulated_limits_and_explicit_allocation_bounds(container_inputs):
    agent, target, host, _, request = container_inputs
    simulated = TargetProfile.simulate_resources(
        host, ResourceLimits(cpu_cores=2, ram_bytes=2**30), "limited"
    )
    report = CompatibilityEngine().assess(agent, simulated)
    spec = HarnessSpecificationGenerator().generate(
        agent, simulated, host, report, change(request, resource_limits=None)
    )
    assert spec.resource_limits == simulated.simulated_limits
    partial = HarnessSpecificationGenerator().generate(
        agent, simulated, host, report, change(request, resource_limits=ResourceLimits(cpu_cores=2))
    )
    assert partial.resource_limits.ram_bytes == simulated.simulated_limits.ram_bytes
    assert host.hardware.resources.cpu_cores.value == 8
    with pytest.raises(HarnessError, match="RESOURCE_CONSTRAINT"):
        HarnessSpecificationGenerator().generate(
            agent,
            simulated,
            host,
            report,
            change(request, resource_limits=ResourceLimits(cpu_cores=3)),
        )
    other_host = change(host, host_id=uuid4())
    with pytest.raises(HarnessError, match="INPUT_MISMATCH"):
        HarnessSpecificationGenerator().generate(agent, simulated, other_host, report, request)


def test_additional_requirements_tighten_without_weakening_agent(container_inputs):
    agent, target, host, report, request = container_inputs
    extra = change(agent.requirements, min_cpu_cores=known(3, "operator requirement"))
    spec = HarnessSpecificationGenerator().generate(
        agent, target, host, report, request, operational_requirements=extra
    )
    assert spec.operational_requirements.min_cpu_cores.value == 3
    assert spec.static_readiness == StaticReadiness.REVIEW_REQUIRED
    extra = change(agent.requirements, min_cpu_cores=known(1, "operator request"))
    spec = HarnessSpecificationGenerator().generate(
        agent, target, host, report, request, operational_requirements=extra
    )
    assert (
        spec.operational_requirements.min_cpu_cores.value == agent.requirements.min_cpu_cores.value
    )


@pytest.mark.parametrize("version", ["3.11.9", "3.13.1"])
def test_incompatible_image_runtime_blocks(container_inputs, version):
    with pytest.raises(HarnessError, match="UNSUPPORTED"):
        generate(
            container_inputs,
            image=change(
                container_inputs[-1].image, python_version=known(version, "image metadata")
            ),
        )


def test_unknown_target_does_not_inherit_host_architecture(container_inputs):
    agent, target, host, _, request = container_inputs
    unknown = change(
        target,
        hardware=change(
            target.hardware, platform=change(target.hardware.platform, architecture=Fact())
        ),
    )
    report = CompatibilityEngine().assess(agent, unknown)
    spec = HarnessSpecificationGenerator().generate(agent, unknown, host, report, request)
    assert spec.container_platform.value is None
    assert spec.compatibility.verdict.value == "UNKNOWN"
    assert spec.static_readiness == StaticReadiness.REVIEW_REQUIRED


def test_arm64_target_and_image_architecture_are_explicit(container_inputs):
    agent, target, host, _, request = container_inputs
    portable = change(agent, requirements=change(agent.requirements, architecture=Fact()))
    arm = change(
        target,
        hardware=change(
            target.hardware,
            platform=change(
                target.hardware.platform, architecture=known("aarch64", "target declaration")
            ),
        ),
    )
    report = CompatibilityEngine().assess(portable, arm)
    spec = HarnessSpecificationGenerator().generate(portable, arm, host, report, request)
    assert spec.container_platform.value == ContainerPlatform.LINUX_ARM64
    assert host.hardware.platform.architecture.value == "x86_64"
    assert not spec.execution_verified
    with pytest.raises(HarnessError, match="UNSUPPORTED"):
        HarnessSpecificationGenerator().generate(
            portable,
            arm,
            host,
            report,
            change(
                request, image=change(request.image, platforms=(ContainerPlatform.LINUX_AMD64,))
            ),
        )
    plan = BuildxPlan(platforms=(ContainerPlatform.LINUX_AMD64, ContainerPlatform.LINUX_ARM64))
    assert "linux/amd64,linux/arm64" in plan.arguments()
    assert not plan.executed and "--push" not in plan.arguments()


def test_gpu_presence_never_promises_driver_or_cuda(container_inputs):
    agent, target, host, _, request = container_inputs
    gpu_agent = change(
        agent,
        requirements=change(agent.requirements, gpu_required=known(True, "explicit requirement")),
    )
    report = CompatibilityEngine().assess(gpu_agent, target)
    spec = HarnessSpecificationGenerator().generate(gpu_agent, target, host, report, request)
    assert any("CUDA/TensorRT" in note for note in spec.unknowns)
    with pytest.raises(HarnessError, match="UNSUPPORTED"):
        HarnessSpecificationGenerator().generate(
            gpu_agent,
            target,
            host,
            report,
            change(
                request,
                image=change(
                    request.image, gpu_runtime_compatible=known(False, "incompatible image")
                ),
            ),
        )


@pytest.mark.parametrize(
    "argv",
    [
        ("sh", "-c", "echo unsafe"),
        ("/bin/bash", "script"),
        ("python", "-c", "print('injected')"),
        ("python", "app.py", "bad\nargument"),
    ],
)
def test_shell_and_control_character_inputs_rejected(container_inputs, argv):
    with pytest.raises(ValidationError):
        change(container_inputs[-1], argv=argv)


def test_sensitive_values_rejected_without_printing_them(container_inputs):
    secret = "sk-" + "x" * 40
    with pytest.raises(ValidationError) as error:
        change(container_inputs[-1], environment_references={"API_KEY": secret})
    assert secret not in str(error.value)
    with pytest.raises(ValidationError):
        BaseImage(reference="user:password@registry/image")
    with pytest.raises(ValidationError):
        change(container_inputs[-1], argv=("python", "app.py", secret))


@pytest.mark.parametrize(
    "path",
    ["../outside.py", "/outside.py", ".env", ".aws/credentials", "cert.pem", "folder\\app.py"],
)
def test_source_allowlist_rejects_private_or_escaping_paths(container_inputs, path):
    with pytest.raises(ValidationError):
        change(container_inputs[-1], source_files={path: "a" * 64})


@pytest.mark.parametrize("source", ["/var/run/docker.sock", "/run", "/dev", "/tmp/alias.sock", "/"])
def test_docker_socket_and_runtime_mounts_rejected(source):
    with pytest.raises(ValidationError):
        BindMount(source=source, target="/data")


def test_security_flags_cannot_be_relaxed():
    for data in (
        {"user_id": 0},
        {"group_id": 0},
        {"privileged": True},
        {"cap_drop": ()},
        {"no_new_privileges": False},
    ):
        with pytest.raises(ValidationError):
            SecuritySettings(**data)


def test_spec_identity_tracks_semantics_and_parent_version(container_inputs):
    first = generate(container_inputs)
    again = generate(container_inputs)
    assert first.model_dump_json() == again.model_dump_json()
    timed = generate(
        container_inputs, created_at=container_inputs[-1].created_at + timedelta(days=1)
    )
    assert timed.harness_id == first.harness_id
    parent = generate(container_inputs, parent_harness_id=first.harness_id, version="trial-2")
    assert parent.parent_harness_id == first.harness_id and parent.harness_id != first.harness_id
    assert parent.agent_source_sha256 == first.agent_source_sha256


def test_operator_can_wrap_python_agent_in_docker(container_inputs):
    agent, target, host, report, request = container_inputs
    requirements = change(
        agent.requirements,
        execution_mode=known(ExecutionMode.DOCKER, "explicit container deployment"),
    )
    spec = HarnessSpecificationGenerator().generate(
        agent, target, host, report, request, operational_requirements=requirements
    )
    assert spec.execution_mode == ExecutionMode.DOCKER
    assert spec.operational_requirements.execution_mode.value == ExecutionMode.DOCKER


def test_specification_generation_has_no_filesystem_io(container_inputs, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Specification generation must not access the filesystem")

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr("os.open", forbidden)
    monkeypatch.setattr(Path, "stat", forbidden)
    assert generate(container_inputs).execution_verified is False


def test_vm_requirements_are_not_silently_containerized(container_inputs):
    agent, target, host, report, request = container_inputs
    vm = change(
        agent,
        requirements=change(
            agent.requirements, execution_mode=known(ExecutionMode.VM, "requires VM")
        ),
    )
    with pytest.raises(HarnessError, match="UNSUPPORTED"):
        HarnessSpecificationGenerator().generate(vm, target, host, report, request)


def test_estimated_requirements_are_not_confirmed(container_inputs):
    spec = generate(
        container_inputs,
        working_directory=known("/opt/agent", "estimated directory", EvidenceKind.ESTIMATED),
    )
    assert spec.static_readiness == StaticReadiness.REVIEW_REQUIRED
    assert any("ESTIMATED" in note for note in spec.unknowns)


@pytest.mark.parametrize(
    "reference",
    [
        "UPPERCASE",
        "repo:::tag",
        "repo:bad+tag",
        "python@sha256:bad",
        "user:password@registry/image",
    ],
)
def test_invalid_image_references_rejected(reference):
    with pytest.raises(ValidationError):
        BaseImage(reference=reference)


@pytest.mark.parametrize(
    "reference",
    ["python:3.12-slim", "example.com:5000/team/my-app:Release_2", "python@sha256:" + "a" * 64],
)
def test_registry_tag_and_digest_references(reference):
    assert BaseImage(reference=reference).reference == reference
