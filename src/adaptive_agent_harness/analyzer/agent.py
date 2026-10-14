"""Bounded static analysis. Never import, install or execute Agent code."""

import ast
import hashlib
import os
import re
import tomllib
from pathlib import Path
from uuid import UUID, uuid4

from packaging.requirements import InvalidRequirement, Requirement
from pydantic import ValidationError

from adaptive_agent_harness.contracts import (
    AgentProfile,
    AgentSource,
    ConfigurationInterface,
    Dependency,
    EntryPoint,
    EnvironmentVariable,
    EvidenceKind,
    Fact,
    OperationalRequirements,
    known,
)


class AnalysisError(ValueError):
    """Safe diagnostics: no source lines, environment values or credentials."""

    def __init__(self, code: str, path: Path, message: str):
        self.code, self.path = code, str(path)
        super().__init__(f"{code}: {message} ({path})")


class AgentAnalyzer:
    EXCLUDED = frozenset(
        {
            ".git",
            ".venv",
            "venv",
            "env",
            ".cache",
            "__pycache__",
            ".aws",
            ".codex",
            ".agents",
            ".ssh",
            ".tox",
            ".nox",
            "node_modules",
            "build",
            "dist",
            ".pytest_cache",
            ".ruff_cache",
        }
    )

    def __init__(self, *, max_files: int = 2000, max_file_bytes: int = 2_000_000):
        if max_files <= 0 or max_file_bytes <= 0:
            raise ValueError("Analysis limits must be positive")
        self.max_files, self.max_file_bytes = max_files, max_file_bytes

    def analyze(
        self,
        project: str | Path,
        *,
        agent_id: UUID | None = None,
        operational_requirements: OperationalRequirements | None = None,
    ) -> AgentProfile:
        root = Path(project).resolve()
        if not root.is_dir():
            raise AnalysisError("INVALID_INPUT", root, "Expected an existing project directory")
        files: dict[str, bytes] = {}
        unknowns: list[str] = []
        dependencies: list[Dependency] = []
        entrypoints: list[EntryPoint] = []
        interfaces: list[ConfigurationInterface] = []
        variables: list[EnvironmentVariable] = []

        def read(path: Path) -> str:
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise AnalysisError(
                    "UNSAFE_PATH", path, "Source path escapes or links outside project"
                )
            relative = path.relative_to(root)
            if any(part in self.EXCLUDED or part.startswith(".env") for part in relative.parts) or (
                path.name != "pyproject.toml" and path.suffix not in (".py", ".txt", ".in")
            ):
                raise AnalysisError("UNSAFE_PATH", path, "Excluded files cannot be analyzed")
            for parent in path.parents:
                if parent == root:
                    break
                if parent.is_symlink():
                    raise AnalysisError("UNSAFE_PATH", path, "Symlink ancestors cannot be analyzed")
            path = path.resolve()
            name = path.relative_to(root).as_posix()
            if name not in files:
                if len(files) >= self.max_files:
                    raise AnalysisError("LIMIT_EXCEEDED", root, "Too many analyzed files")
                try:
                    with path.open("rb") as stream:
                        data = stream.read(self.max_file_bytes + 1)
                    if len(data) > self.max_file_bytes:
                        raise AnalysisError("LIMIT_EXCEEDED", path, "Source file is too large")
                    files[name] = data
                except OSError as exc:
                    raise AnalysisError("READ_FAILED", path, "Cannot read source file") from exc
            try:
                return files[name].decode("utf-8-sig")
            except UnicodeError as exc:
                raise AnalysisError("INVALID_INPUT", path, "Source file is not UTF-8") from exc

        def dependency(line: str, source: str) -> None:
            try:
                parsed = Requirement(line)
            except InvalidRequirement:
                unknowns.append(f"Dependency declaration cannot be resolved statically: {source}")
                return
            # URL values may contain secrets. Preserve name/specifier, never the URL.
            dependencies.append(
                Dependency(
                    name=parsed.name,
                    specifier=str(parsed.specifier),
                    extras=tuple(sorted(parsed.extras)),
                    marker=str(parsed.marker) if parsed.marker else None,
                    source=source,
                    direct_reference=parsed.url is not None,
                )
            )
            if parsed.url:
                unknowns.append(f"Direct dependency reference not verified: {source}")

        def requirements(path: Path, visited: set[Path]) -> None:
            content = read(path)
            path = path.resolve()
            if path in visited:
                unknowns.append("Requirements include cycle encountered")
                return
            if len(visited) >= 100:
                raise AnalysisError("LIMIT_EXCEEDED", path, "Too many requirement includes")
            visited.add(path)
            for number, line in enumerate(content.splitlines(), 1):
                line = line.split(" #", 1)[0].strip()
                source = f"{path.relative_to(root).as_posix()}:{number}"
                if not line or line.startswith("#"):
                    continue
                include = re.fullmatch(r"(?:-r\s*|--requirement(?:=|\s+))(.+)", line)
                if include:
                    child = path.parent / include[1].strip()
                    requirements(child, visited)
                elif line.startswith("-") or line.endswith("\\"):
                    unknowns.append(f"Pip option or multiline requirement not resolved: {source}")
                else:
                    dependency(line, source)

        req = operational_requirements or OperationalRequirements()
        name: Fact[str] = Fact(source="Project name is not statically declared")
        pyproject = root / "pyproject.toml"
        if pyproject.exists() or pyproject.is_symlink():
            try:
                document = tomllib.loads(read(pyproject))
                metadata = document.get("project", {})
                if not isinstance(metadata, dict):
                    raise ValueError
                if "name" in metadata:
                    name = known(
                        metadata["name"], "pyproject.toml:project.name", EvidenceKind.STATIC
                    )
                if "requires-python" in metadata:
                    declared = known(
                        metadata["requires-python"],
                        "pyproject.toml:project.requires-python",
                        EvidenceKind.STATIC,
                    )
                    if req.python_specifier.value not in (None, declared.value):
                        raise AnalysisError(
                            "DECLARATION_CONFLICT",
                            pyproject,
                            "Provided Python requirement conflicts with project",
                        )
                    req = OperationalRequirements.model_validate(
                        {**req.model_dump(), "python_specifier": declared}
                    )
                declared_dependencies = metadata.get("dependencies", [])
                if not isinstance(declared_dependencies, list):
                    raise ValueError
                for item in declared_dependencies:
                    if not isinstance(item, str):
                        raise ValueError
                    dependency(item, "pyproject.toml:project.dependencies")
                for group in ("scripts", "gui-scripts"):
                    scripts = metadata.get(group, {})
                    if not isinstance(scripts, dict):
                        raise ValueError
                    for script, value in scripts.items():
                        entrypoints.append(
                            EntryPoint(
                                kind=group,
                                name=script,
                                value=value,
                                source=f"pyproject.toml:project.{group}",
                            )
                        )
                if metadata.get("dynamic") or metadata.get("optional-dependencies"):
                    unknowns.append("Dynamic metadata and optional dependencies are not resolved")
                if "tool" in document:
                    interfaces.append(
                        ConfigurationInterface(
                            kind="tool_config", name="pyproject.toml", source="pyproject.toml"
                        )
                    )
            except (tomllib.TOMLDecodeError, TypeError, ValueError, ValidationError) as exc:
                if isinstance(exc, AnalysisError):
                    raise
                raise AnalysisError(
                    "INVALID_INPUT", pyproject, "Malformed project metadata"
                ) from None
        requirements_file = root / "requirements.txt"
        if requirements_file.exists() or requirements_file.is_symlink():
            requirements(requirements_file, set())

        def walk_error(error: OSError) -> None:
            raise AnalysisError("READ_FAILED", root, "Cannot traverse project") from error

        for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
            dirs[:] = sorted(d for d in dirs if d not in self.EXCLUDED)
            for folder in dirs:
                if (Path(directory) / folder).is_symlink():
                    raise AnalysisError(
                        "UNSAFE_PATH",
                        Path(directory) / folder,
                        "Symlink directories are not analyzed",
                    )
            for filename in sorted(names):
                if not filename.endswith(".py"):
                    continue
                path = Path(directory) / filename
                relative = path.relative_to(root).as_posix()
                try:
                    tree = ast.parse(read(path), filename=relative)
                except SyntaxError:
                    unknowns.append(f"Python syntax could not be analyzed: {relative}")
                    continue
                if filename == "__main__.py":
                    parts = path.relative_to(root).with_suffix("").parts[:-1]
                    if parts and parts[0] == "src":
                        parts = parts[1:]
                    if parts:
                        entrypoints.append(
                            EntryPoint(
                                kind="module",
                                name=".".join(parts),
                                value=".".join(parts),
                                source=relative,
                            )
                        )
                os_aliases, getenv_aliases, environ_aliases = {"os"}, set(), set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        os_aliases.update(a.asname or a.name for a in node.names if a.name == "os")
                    elif isinstance(node, ast.ImportFrom) and node.module == "os":
                        getenv_aliases.update(
                            a.asname or a.name for a in node.names if a.name == "getenv"
                        )
                        environ_aliases.update(
                            a.asname or a.name for a in node.names if a.name == "environ"
                        )

                def is_environ(
                    node: ast.AST,
                    environ_names: frozenset[str] = frozenset(environ_aliases),
                    os_names: frozenset[str] = frozenset(os_aliases),
                ) -> bool:
                    return (isinstance(node, ast.Name) and node.id in environ_names) or (
                        isinstance(node, ast.Attribute)
                        and node.attr == "environ"
                        and isinstance(node.value, ast.Name)
                        and node.value.id in os_names
                    )

                for node in ast.walk(tree):
                    location = f"{relative}:{getattr(node, 'lineno', 1)}"
                    env_key, access = None, None
                    if isinstance(node, ast.Call):
                        fn = node.func
                        getenv = (isinstance(fn, ast.Name) and fn.id in getenv_aliases) or (
                            isinstance(fn, ast.Attribute)
                            and fn.attr == "getenv"
                            and isinstance(fn.value, ast.Name)
                            and fn.value.id in os_aliases
                        )
                        envget = (
                            isinstance(fn, ast.Attribute)
                            and fn.attr == "get"
                            and is_environ(fn.value)
                        )
                        if getenv or envget:
                            access = "getenv" if getenv else "environ.get"
                            env_key = (
                                node.args[0]
                                if node.args
                                else next((k.value for k in node.keywords if k.arg == "key"), None)
                            )
                        if (
                            isinstance(fn, ast.Attribute)
                            and fn.attr == "add_argument"
                            and node.args
                        ):
                            for argument in node.args:
                                if isinstance(argument, ast.Constant) and isinstance(
                                    argument.value, str
                                ):
                                    interfaces.append(
                                        ConfigurationInterface(
                                            kind="cli_option", name=argument.value, source=location
                                        )
                                    )
                    elif isinstance(node, ast.Subscript) and is_environ(node.value):
                        env_key, access = node.slice, "environ[]"
                    if access is not None:
                        if (
                            isinstance(env_key, ast.Constant)
                            and isinstance(env_key.value, str)
                            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_key.value)
                        ):
                            variables.append(
                                EnvironmentVariable(
                                    name=env_key.value, source=location, access=access
                                )
                            )
                        else:
                            unknowns.append(f"Dynamic environment variable name: {location}")
                    if (
                        isinstance(node, ast.If)
                        and isinstance(node.test, ast.Compare)
                        and isinstance(node.test.left, ast.Name)
                        and node.test.left.id == "__name__"
                        and len(node.test.ops) == 1
                        and isinstance(node.test.ops[0], ast.Eq)
                        and any(
                            isinstance(c, ast.Constant) and c.value == "__main__"
                            for c in node.test.comparators
                        )
                    ):
                        entrypoints.append(
                            EntryPoint(
                                kind="script", name=relative, value=relative, source=location
                            )
                        )

        for field in OperationalRequirements.model_fields:
            value = getattr(req, field)
            if isinstance(value, Fact) and value.value is None:
                unknowns.append(f"Operational requirement UNKNOWN: {field}")
        if not entrypoints:
            unknowns.append("Executable entrypoint UNKNOWN")
        unknowns.append(
            "Dependency installation, environment necessity and runtime behavior unverified"
        )
        digest = hashlib.sha256()
        hashes = {}
        for filename, data in sorted(files.items()):
            file_hash = hashlib.sha256(data).hexdigest()
            hashes[filename] = file_hash
            digest.update(filename.encode() + b"\0" + file_hash.encode() + b"\n")
        try:
            return AgentProfile(
                agent_id=agent_id or uuid4(),
                project_name=name,
                source=AgentSource(
                    root=str(root), content_sha256=digest.hexdigest(), file_hashes=hashes
                ),
                requirements=req,
                python_files=tuple(f for f in sorted(files) if f.endswith(".py")),
                entrypoints=tuple(entrypoints),
                dependencies=tuple(dependencies),
                environment_variables=tuple(variables),
                configuration_interfaces=tuple(interfaces),
                unknowns=tuple(dict.fromkeys(unknowns)),
            )
        except ValidationError:
            raise AnalysisError(
                "INVALID_INPUT", root, "Invalid static project declarations"
            ) from None
