"""Explicit local data, packaging and optional simulator integration boundary."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import JsonValue

from adaptive_agent_harness.analyzer import CompatibilityEngine
from adaptive_agent_harness.contracts import (
    AgentProfile,
    HostProfile,
    TargetProfile,
    ValidationStatus,
)
from adaptive_agent_harness.contracts.adaptation import ExecutableCandidatePlan
from adaptive_agent_harness.contracts.base import content_hash
from adaptive_agent_harness.contracts.data import (
    DataApproval,
    DataPipelineSpecification,
    DataPipelineVersion,
)
from adaptive_agent_harness.contracts.harness import ContainerHarnessSpecification
from adaptive_agent_harness.contracts.runtime import BaselinePlan
from adaptive_agent_harness.contracts.simulator import (
    SimulatorOperation,
    SimulatorRequirement,
    SimulatorResponse,
    assess_simulator,
)
from adaptive_agent_harness.data import LocalFilePipeline, PreparedData
from adaptive_agent_harness.harness import ContainerHarnessBuilder
from adaptive_agent_harness.harness._utils import read_regular
from adaptive_agent_harness.optimizer.planner import validate_patch

from .simulator import SimulatorAdapter


@dataclass(frozen=True)
class CandidatePackage:
    directory: Path
    specification: ContainerHarnessSpecification
    prepared_data: PreparedData | None = None


class PortingService(Protocol):
    def materialize(
        self,
        plan: ExecutableCandidatePlan,
        baseline: Path,
        *,
        agent: AgentProfile,
        host: HostProfile,
        target: TargetProfile,
        runtime_plan: BaselinePlan,
        output_root: Path,
        prepared_data: PreparedData | None = None,
        data_approval: DataApproval | None = None,
    ) -> CandidatePackage: ...


class PortingManager:
    def __init__(
        self,
        *,
        local_pipeline: LocalFilePipeline | None = None,
        simulator: SimulatorAdapter | None = None,
    ):
        self.local_pipeline, self.simulator = local_pipeline, simulator

    def prepare_data(
        self,
        specification: DataPipelineSpecification,
        *,
        approval: DataApproval,
        output_root: Path,
        previous_version: DataPipelineVersion | None = None,
    ) -> PreparedData:
        if self.local_pipeline is None:
            raise ValueError("DATA_ADAPTER_UNAVAILABLE")
        return self.local_pipeline.prepare(
            specification,
            approval=approval,
            output_root=output_root,
            previous_version=previous_version,
        )

    def invoke_simulator(
        self,
        operation: SimulatorOperation,
        payload: dict[str, JsonValue] | None = None,
    ) -> SimulatorResponse:
        if self.simulator is None:
            return SimulatorResponse(
                operation=operation, status="UNKNOWN", error_code="ADAPTER_UNAVAILABLE"
            )
        return self.simulator.invoke(operation, payload)

    def validate_simulator(self, requirement: SimulatorRequirement) -> ValidationStatus:
        if self.simulator is None:
            return ValidationStatus.UNKNOWN
        return assess_simulator(requirement, self.simulator.capability)

    def materialize(
        self,
        plan: ExecutableCandidatePlan,
        baseline: Path,
        *,
        agent: AgentProfile,
        host: HostProfile,
        target: TargetProfile,
        runtime_plan: BaselinePlan,
        output_root: Path,
        prepared_data: PreparedData | None = None,
        data_approval: DataApproval | None = None,
    ) -> CandidatePackage:
        plan = ExecutableCandidatePlan.model_validate_json(plan.model_dump_json())
        builder = ContainerHarnessBuilder()
        manifest = builder.verify(baseline)
        raw, _ = read_regular(baseline, "harness.yaml", 16 * 1024**2)
        if hashlib.sha256(raw).hexdigest() != manifest.specification_sha256:
            raise ValueError("SPECIFICATION_CHANGED")
        spec = ContainerHarnessSpecification.model_validate_json(raw)
        if manifest.specification_sha256 != plan.experiment.baseline.harness_sha256:
            raise ValueError("BASELINE_PACKAGE_MISMATCH")
        binding = builder.data_binding(baseline, spec)
        patch = plan.patch
        validate_patch(
            patch,
            spec,
            runtime_plan,
            agent,
            target,
            buffer_records=binding.specification.limits.buffer_records if binding else None,
        )
        if CompatibilityEngine().assess(agent, target).verdict.value == "UNSUPPORTED":
            raise ValueError("TARGET_UNSUPPORTED")
        data = spec.model_dump(mode="json")
        if patch.field.startswith("resource_limits."):
            data["resource_limits"][patch.field.split(".")[1]] = patch.after
        elif patch.field == "agent_argument":
            data["argv"][patch.configuration.argument_index] = str(patch.after)
        if binding:
            if patch.field == "limits.buffer_records":
                if prepared_data is None and plan.pipeline_specification and data_approval:
                    prepared_data = self.prepare_data(
                        plan.pipeline_specification,
                        approval=data_approval,
                        output_root=output_root,
                        previous_version=binding.version,
                    )
                if prepared_data is None:
                    raise ValueError("APPROVED_PIPELINE_REQUIRED")
                new = prepared_data.binding
                expected = binding.specification.model_dump(mode="json")
                expected["limits"]["buffer_records"] = patch.after
                actual = new.specification.model_dump(mode="json")
                # Only version ancestry/change metadata may differ besides the approved buffer.
                for field in ("version", "parent_version_id", "changes"):
                    expected[field] = actual[field]
                if (
                    content_hash(expected) != content_hash(actual)
                    or new.version.parent_version_id != binding.version.version_id
                    or new.version.output_sha256 != binding.version.output_sha256
                ):
                    raise ValueError("PIPELINE_CHANGE_MISMATCH")
                data["data_requirement"]["pipeline_specification_sha256"] = (
                    new.version.specification_sha256
                )
            else:
                if prepared_data and prepared_data.binding != binding:
                    raise ValueError("PIPELINE_VERSION_MISMATCH")
                prepared_data = PreparedData(baseline / "data", binding)
        elif prepared_data is not None or patch.field == "limits.buffer_records":
            raise ValueError("UNEXPECTED_PIPELINE")
        data.update(
            harness_id=str(plan.candidate.resulting_harness_id),
            parent_harness_id=str(spec.harness_id),
            version=f"candidate-{plan.candidate.candidate_id}",
        )
        new_spec = ContainerHarnessSpecification.model_validate(data)
        package = builder.build(
            new_spec,
            agent,
            target,
            host,
            output_root=output_root,
            package_name=f"candidate-{plan.candidate.candidate_id}",
            prepared_data=prepared_data,
        )
        return CandidatePackage(package.directory, new_spec, prepared_data)
