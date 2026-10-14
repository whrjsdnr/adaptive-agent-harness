"""Deterministic requirement checks, without optimization or runtime support guesses."""

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from adaptive_agent_harness.contracts import (
    AgentProfile,
    CompatibilityCheck,
    CompatibilityReport,
    CompatibilityVerdict,
    Fact,
    TargetProfile,
)

V = CompatibilityVerdict


class CompatibilityEngine:
    def assess(self, agent: AgentProfile, target: TargetProfile) -> CompatibilityReport:
        checks: list[CompatibilityCheck] = []
        req = agent.requirements

        def record(area: str, verdict: V, reason: str, *facts: Fact) -> None:
            checks.append(
                CompatibilityCheck(
                    area=area,
                    verdict=verdict,
                    reason=reason,
                    sources=tuple(f.source for f in facts),
                )
            )

        def equal(area: str, needed: Fact, available: Fact, aliases: dict[str, str]) -> None:
            if needed.value is None or available.value is None:
                record(
                    area,
                    V.UNKNOWN,
                    "Required or target platform information is UNKNOWN",
                    needed,
                    available,
                )
                return
            a, b = str(needed.value).lower(), str(available.value).lower()
            a, b = aliases.get(a, a), aliases.get(b, b)
            record(
                area,
                V.FIT if a == b else V.UNSUPPORTED,
                "Platform matches" if a == b else "Platform mismatch; emulation is out of scope",
                needed,
                available,
            )

        equal("os", req.os, target.hardware.platform.os, {"darwin": "macos", "win32": "windows"})
        equal(
            "architecture",
            req.architecture,
            target.hardware.platform.architecture,
            {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"},
        )

        for area, required, actual, limit in (
            (
                "cpu_capacity",
                req.min_cpu_cores,
                target.hardware.resources.cpu_cores,
                target.simulated_limits.cpu_cores if target.simulated_limits else None,
            ),
            (
                "ram_capacity",
                req.min_ram_bytes,
                target.hardware.resources.ram_bytes,
                target.simulated_limits.ram_bytes if target.simulated_limits else None,
            ),
        ):
            if required.value is None or actual.value is None:
                record(
                    area, V.UNKNOWN, "Physical requirement or capacity is UNKNOWN", required, actual
                )
            else:
                fits = actual.value >= required.value
                record(
                    area,
                    V.FIT if fits else V.UNSUPPORTED,
                    "Physical capacity satisfies requirement"
                    if fits
                    else "Physical capacity is insufficient",
                    required,
                    actual,
                )
            if limit is not None:
                if actual.value is not None and limit > actual.value:
                    record(
                        area + ":simulation_capacity",
                        V.ADAPT_REQUIRED,
                        "Simulated limit exceeds physical capacity; it cannot create hardware resources",
                        actual,
                    )
                if required.value is None:
                    record(
                        area + ":simulation",
                        V.UNKNOWN,
                        "Requirement for simulated limit is UNKNOWN",
                        required,
                    )
                else:
                    fits = limit >= required.value
                    record(
                        area + ":simulation",
                        V.FIT if fits else V.ADAPT_REQUIRED,
                        "Simulated resource limit satisfies requirement"
                        if fits
                        else "Simulated limit is too low; revise limit, not physical hardware claim",
                        required,
                    )

        for area, required, capability in (
            ("gpu", req.gpu_required, target.hardware.gpu_present),
            ("cuda", req.cuda_required, target.deployment.cuda),
        ):
            if required.value is False:
                record(area, V.FIT, "Capability explicitly not required", required)
            elif required.value is None or capability.value is None:
                record(
                    area, V.UNKNOWN, "Requirement or capability is UNKNOWN", required, capability
                )
            else:
                record(
                    area,
                    V.FIT if capability.value else V.UNSUPPORTED,
                    "Required capability is confirmed"
                    if capability.value
                    else "Required capability unavailable",
                    required,
                    capability,
                )

        python_runtimes = [r for r in target.runtimes if r.name.lower() == "python"]
        confirmed = [r for r in python_runtimes if r.available.value is True and r.version.value]
        if req.python_specifier.value is None:
            record(
                "python_runtime",
                V.UNKNOWN,
                "Agent Python requirement is UNKNOWN",
                req.python_specifier,
            )
        elif not confirmed:
            explicitly_absent = python_runtimes and all(
                r.available.value is False for r in python_runtimes
            )
            record(
                "python_runtime",
                V.ADAPT_REQUIRED if explicitly_absent else V.UNKNOWN,
                "Python runtime needs provisioning"
                if explicitly_absent
                else "Target Python runtime is UNKNOWN",
            )
        else:
            try:
                spec = SpecifierSet(req.python_specifier.value)
                matches = any(Version(r.version.value) in spec for r in confirmed)
                record(
                    "python_runtime",
                    V.FIT if matches else V.ADAPT_REQUIRED,
                    "Python version satisfies requirement"
                    if matches
                    else "A compatible Python runtime is needed",
                    req.python_specifier,
                    *[r.version for r in confirmed],
                )
            except InvalidVersion:
                record("python_runtime", V.UNKNOWN, "Target runtime version cannot be interpreted")

        if req.execution_mode.value is None:
            record(
                "deployment", V.UNKNOWN, "Required execution mode is UNKNOWN", req.execution_mode
            )
        else:
            mode = req.execution_mode.value.value.lower()
            capability = getattr(target.deployment, mode)
            record(
                "deployment",
                V.UNKNOWN
                if capability.value is None
                else V.FIT
                if capability.value
                else V.UNSUPPORTED,
                "Execution mode not confirmed"
                if capability.value is None
                else "Execution mode available"
                if capability.value
                else "Execution mode unavailable",
                req.execution_mode,
                capability,
            )
            if mode == "docker":
                for area in ("docker_cli", "docker_access"):
                    fact = getattr(target.deployment, area)
                    verdict = (
                        V.UNKNOWN
                        if fact.value is None
                        else V.FIT
                        if fact.value
                        else V.ADAPT_REQUIRED
                    )
                    record(
                        area,
                        verdict,
                        "Docker prerequisite status; separate from hardware capacity",
                        fact,
                    )
        if agent.dependencies:
            record(
                "dependencies",
                V.UNKNOWN,
                "Dependency installation and binary/ABI compatibility not tested",
            )
        priority = (V.UNSUPPORTED, V.UNKNOWN, V.ADAPT_REQUIRED, V.FIT)
        verdict = next(v for v in priority if any(c.verdict == v for c in checks))
        return CompatibilityReport(
            agent_id=agent.agent_id,
            target_id=target.target_id,
            verdict=verdict,
            checks=tuple(checks),
            unknowns=tuple(f"{c.area}: {c.reason}" for c in checks if c.verdict == V.UNKNOWN),
        )
