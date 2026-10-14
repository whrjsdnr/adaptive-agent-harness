"""Conservative AST observations; never import or execute the supplied Agent."""

import ast
import hashlib

from adaptive_agent_harness.contracts import AgentProfile, EvidenceKind, known
from adaptive_agent_harness.contracts.data import DataObservation, DataRequirement, safe_relative
from adaptive_agent_harness.data.local import DataFailure
from adaptive_agent_harness.harness._utils import read_regular


class AgentDataRequirementAnalyzer:
    def analyze(
        self, agent: AgentProfile, *, declared: DataRequirement | None = None
    ) -> DataRequirement:
        agent = AgentProfile.model_validate_json(agent.model_dump_json())
        observations = []
        files = []
        directories = []
        unknowns = [
            "Required inputs, directories, schemas, access, cadence and ordering need an explicit contract"
        ]
        for filename in agent.python_files:
            from pathlib import Path

            data, _ = read_regular(Path(agent.source.root), filename, 2_000_000)
            if hashlib.sha256(data).hexdigest() != agent.source.file_hashes.get(filename):
                raise DataFailure("AGENT_VERSION_MISMATCH")
            try:
                tree = ast.parse(data, filename=filename)
            except SyntaxError:
                unknowns.append("Unparseable source: " + filename)
                continue
            aliases = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    aliases.update({n.asname or n.name: n.name for n in node.names})
                elif isinstance(node, ast.ImportFrom) and node.module:
                    aliases.update(
                        {n.asname or n.name: node.module + "." + n.name for n in node.names}
                    )

            def call_name(node, aliases=aliases):
                if isinstance(node, ast.Name):
                    return aliases.get(node.id, node.id)
                if isinstance(node, ast.Attribute):
                    return call_name(node.value) + "." + node.attr
                return ""

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                location = f"{filename}:{node.lineno}"
                name = call_name(node.func)
                if name in ("json.load", "json.loads", "csv.reader", "csv.DictReader"):
                    observations.append(
                        DataObservation(
                            kind="FORMAT", location=location, name=name.split(".")[0].upper()
                        )
                    )
                path = None
                access = "READ"
                if name in ("open", "io.open", "pathlib.Path") and node.args:
                    # Path construction alone does not establish data access.
                    if name != "pathlib.Path":
                        path = node.args[0]
                        mode = (
                            node.args[1]
                            if len(node.args) > 1
                            else next((k.value for k in node.keywords if k.arg == "mode"), None)
                        )
                        if (
                            isinstance(mode, ast.Constant)
                            and isinstance(mode.value, str)
                            and any(c in mode.value for c in "wax+")
                        ):
                            access = "WRITE"
                elif isinstance(node.func, ast.Attribute) and node.func.attr in (
                    "read_text",
                    "read_bytes",
                    "write_text",
                    "write_bytes",
                    "open",
                    "iterdir",
                    "glob",
                ):
                    receiver = node.func.value
                    if (
                        isinstance(receiver, ast.Call)
                        and call_name(receiver.func) == "pathlib.Path"
                        and receiver.args
                    ):
                        path = receiver.args[0]
                        access = "WRITE" if node.func.attr.startswith("write") else "READ"
                        if node.func.attr == "open":
                            mode = (
                                node.args[0]
                                if node.args
                                else next((k.value for k in node.keywords if k.arg == "mode"), None)
                            )
                            if (
                                isinstance(mode, ast.Constant)
                                and isinstance(mode.value, str)
                                and any(c in mode.value for c in "wax+")
                            ):
                                access = "WRITE"
                if path is not None:
                    if isinstance(path, ast.Constant) and isinstance(path.value, str):
                        try:
                            relative = safe_relative(path.value)
                        except ValueError:
                            observations.append(
                                DataObservation(kind="DYNAMIC_PATH", location=location)
                            )
                        else:
                            observations.append(
                                DataObservation(kind=access, location=location, name=relative)
                            )
                            if access == "READ":
                                if isinstance(node.func, ast.Attribute) and node.func.attr in (
                                    "glob",
                                    "iterdir",
                                ):
                                    directories.append(relative)
                                else:
                                    files.append(relative)
                    else:
                        observations.append(DataObservation(kind="DYNAMIC_PATH", location=location))
        observations.extend(
            DataObservation(kind="ENVIRONMENT", location=v.source, name=v.name)
            for v in agent.environment_variables
        )
        observations.extend(
            DataObservation(kind="ARGUMENT", location=v.source, name=v.name)
            for v in agent.configuration_interfaces
            if v.kind == "cli_option"
        )
        if declared is not None:
            declared = DataRequirement.model_validate_json(declared.model_dump_json())
            if (
                declared.agent_id != agent.agent_id
                or declared.agent_source_sha256 != agent.source.content_sha256
            ):
                raise DataFailure("DATA_REQUIREMENT_AGENT_MISMATCH")
            return DataRequirement.model_validate(
                {**declared.model_dump(), "observations": tuple(observations)}
            )
        values = dict(
            agent_id=agent.agent_id,
            agent_source_sha256=agent.source.content_sha256,
            observations=tuple(observations),
            unknowns=tuple(unknowns),
        )
        if files:
            values["input_files"] = known(
                tuple(dict.fromkeys(files)),
                "Literal read sites; runtime necessity unverified",
                EvidenceKind.STATIC,
            )
        if directories:
            values["input_directories"] = known(
                tuple(dict.fromkeys(directories)),
                "Literal directory read sites; runtime necessity unverified",
                EvidenceKind.STATIC,
            )
        return DataRequirement(**values)
