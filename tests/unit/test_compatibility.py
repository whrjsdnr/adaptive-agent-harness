import pytest

from adaptive_agent_harness.analyzer import CompatibilityEngine
from adaptive_agent_harness.contracts import (
    CompatibilityVerdict,
    Dependency,
    Fact,
    HardwareInfo,
    OperationalRequirements,
    ResourceLimits,
    TargetProfile,
    known,
)


def alter(model, **updates):
    return type(model).model_validate({**model.model_dump(), **updates})


def test_fit_for_explicit_matching_requirements(agent, target):
    report = CompatibilityEngine().assess(agent, target)
    assert report.verdict == CompatibilityVerdict.FIT
    assert not report.unknowns
    assert report.scope == "static_requirements_only"


def test_runtime_mismatch_requires_adaptation(agent, target):
    runtime = alter(target.runtimes[0], version=known("3.11.9", "declared target"))
    report = CompatibilityEngine().assess(agent, alter(target, runtimes=(runtime,)))
    assert report.verdict == CompatibilityVerdict.ADAPT_REQUIRED


@pytest.mark.parametrize("field,value", [("os", "Windows"), ("architecture", "aarch64")])
def test_platform_mismatch_is_unsupported(agent, target, field, value):
    platform = alter(target.hardware.platform, **{field: known(value, "target specification")})
    report = CompatibilityEngine().assess(
        agent, alter(target, hardware=alter(target.hardware, platform=platform))
    )
    assert report.verdict == CompatibilityVerdict.UNSUPPORTED
    assert any(c.area == field and "mismatch" in c.reason for c in report.checks)


def test_architecture_aliases(agent, target):
    platform = alter(target.hardware.platform, architecture=known("AMD64", "target specification"))
    assert (
        CompatibilityEngine()
        .assess(agent, alter(target, hardware=alter(target.hardware, platform=platform)))
        .verdict
        == CompatibilityVerdict.FIT
    )


def test_all_unknown_requirements_remain_unknown(agent, target):
    report = CompatibilityEngine().assess(
        alter(agent, requirements=OperationalRequirements()), target
    )
    assert report.verdict == CompatibilityVerdict.UNKNOWN
    assert report.unknowns


def test_gpu_does_not_guarantee_cuda(agent, target):
    req = alter(
        agent.requirements,
        gpu_required=known(True, "manifest"),
        cuda_required=known(True, "manifest"),
    )
    report = CompatibilityEngine().assess(alter(agent, requirements=req), target)
    checks = {c.area: c for c in report.checks}
    assert checks["gpu"].verdict == CompatibilityVerdict.FIT
    assert checks["cuda"].verdict == CompatibilityVerdict.UNKNOWN
    assert report.verdict == CompatibilityVerdict.UNKNOWN


def test_gpu_required_but_absent_is_unsupported(agent, target):
    req = alter(agent.requirements, gpu_required=known(True, "manifest"))
    target = alter(
        target, hardware=alter(target.hardware, gpu_present=known(False, "target specification"))
    )
    assert (
        CompatibilityEngine().assess(alter(agent, requirements=req), target).verdict
        == CompatibilityVerdict.UNSUPPORTED
    )


def test_docker_privileges_are_separate_from_capacity(agent, target):
    req = alter(agent.requirements, execution_mode=known("DOCKER", "manifest"))
    deployment = alter(
        target.deployment,
        docker=known(False, "execution denied"),
        docker_cli=known(True, "CLI checked"),
        docker_access=known(False, "access denied"),
    )
    report = CompatibilityEngine().assess(
        alter(agent, requirements=req), alter(target, deployment=deployment)
    )
    checks = {c.area: c for c in report.checks}
    assert checks["cpu_capacity"].verdict == CompatibilityVerdict.FIT
    assert checks["deployment"].verdict == CompatibilityVerdict.UNSUPPORTED
    assert checks["docker_access"].verdict == CompatibilityVerdict.ADAPT_REQUIRED


def test_daemon_access_alone_cannot_prove_container_execution(agent, target):
    req = alter(agent.requirements, execution_mode=known("DOCKER", "manifest"))
    deployment = alter(
        target.deployment,
        docker_cli=known(True, "checked"),
        docker_access=known(True, "checked"),
        docker=Fact(source="Not executed"),
    )
    report = CompatibilityEngine().assess(
        alter(agent, requirements=req), alter(target, deployment=deployment)
    )
    assert report.verdict == CompatibilityVerdict.UNKNOWN


def test_physical_insufficiency_and_simulated_limit_are_distinct(agent, host):
    limited = TargetProfile.simulate_resources(
        host, ResourceLimits(cpu_cores=0.5, ram_bytes=512), "tight"
    )
    assert (
        CompatibilityEngine().assess(agent, limited).verdict == CompatibilityVerdict.ADAPT_REQUIRED
    )
    req = alter(agent.requirements, min_cpu_cores=known(100, "manifest"))
    assert (
        CompatibilityEngine().assess(alter(agent, requirements=req), limited).verdict
        == CompatibilityVerdict.UNSUPPORTED
    )
    hypothetical = TargetProfile(
        name="hypothetical",
        kind="RESOURCE_SIMULATION",
        hardware=HardwareInfo(),
        simulated_limits=ResourceLimits(cpu_cores=100),
    )
    assert CompatibilityEngine().assess(agent, hypothetical).verdict == CompatibilityVerdict.UNKNOWN
    assert host.hardware.resources.cpu_cores.value == 8


def test_simulated_limits_cannot_create_physical_resources(agent, host):
    target = TargetProfile.simulate_resources(host, ResourceLimits(cpu_cores=100), "oversized")
    report = CompatibilityEngine().assess(agent, target)
    assert report.verdict == CompatibilityVerdict.ADAPT_REQUIRED
    assert any(c.area == "cpu_capacity:simulation_capacity" for c in report.checks)


def test_known_failure_has_priority_over_other_unknowns(agent, target):
    hardware = alter(
        target.hardware,
        gpu_present=Fact(),
        platform=alter(target.hardware.platform, architecture=known("arm64", "target")),
    )
    req = alter(agent.requirements, gpu_required=known(True, "manifest"))
    report = CompatibilityEngine().assess(
        alter(agent, requirements=req), alter(target, hardware=hardware)
    )
    assert report.verdict == CompatibilityVerdict.UNSUPPORTED
    assert any(item.startswith("gpu:") for item in report.unknowns)


def test_dependency_installability_is_unknown(agent, target):
    dependencies = (Dependency(name="example", source="requirements.txt"),)
    report = CompatibilityEngine().assess(alter(agent, dependencies=dependencies), target)
    assert report.verdict == CompatibilityVerdict.UNKNOWN
    assert any(c.area == "dependencies" for c in report.checks)


def test_runtime_unknown_or_invalid_does_not_use_host_runtime(agent, target):
    for version, available in [
        (Fact(), known(True, "target")),
        (known("latest", "target"), known(True, "target")),
        (known("3.12.14", "target"), Fact()),
    ]:
        runtime = alter(target.runtimes[0], version=version, available=available)
        assert (
            CompatibilityEngine().assess(agent, alter(target, runtimes=(runtime,))).verdict
            == CompatibilityVerdict.UNKNOWN
        )
