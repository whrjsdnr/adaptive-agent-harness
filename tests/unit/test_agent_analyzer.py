import hashlib
import subprocess

import pytest

from adaptive_agent_harness.analyzer import AgentAnalyzer, AnalysisError
from adaptive_agent_harness.contracts import Knowledge, OperationalRequirements, known


def test_python_project_metadata_ast_and_original_preservation(tmp_path, monkeypatch):
    sentinel = tmp_path / "executed"
    source = tmp_path / "src" / "sample" / "__main__.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        f'from pathlib import Path\nPath({str(sentinel)!r}).write_text("should not run")\n'
        "import os as operating\nfrom os import getenv as read_env\n"
        'name = operating.environ["INPUT_PATH"]\n'
        'endpoint = read_env("SERVICE_ENDPOINT")\n'
        'optional = operating.environ.get("OPTIONAL_NAME")\n'
        'parser.add_argument("--config")\n'
        'if __name__ == "__main__":\n    raise RuntimeError("never execute")\n',
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="existing-sample"\nrequires-python=">=3.11,<3.13"\n'
        "dependencies=[\"requests>=2; python_version >= '3.11'\"]\n"
        '[project.scripts]\nsample="sample:main"\n[tool.example]\noption=true\n',
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("-r extra.txt\n", encoding="utf-8")
    (tmp_path / "extra.txt").write_text("pydantic[email]>=2\n", encoding="utf-8")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    def forbid_execution(*args, **kwargs):
        pytest.fail("Static analyzer attempted subprocess execution")

    monkeypatch.setattr(subprocess, "run", forbid_execution)
    result = AgentAnalyzer().analyze(tmp_path)
    assert not sentinel.exists()
    assert before == {
        p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }
    assert result.project_name.value == "existing-sample"
    assert result.requirements.python_specifier.value == ">=3.11,<3.13"
    assert result.requirements.gpu_required.status == Knowledge.UNKNOWN
    assert {d.name for d in result.dependencies} == {"requests", "pydantic"}
    assert next(d for d in result.dependencies if d.name == "pydantic").extras == ("email",)
    assert {e.kind for e in result.entrypoints} == {"scripts", "module", "script"}
    assert all(not e.execution_verified for e in result.entrypoints)
    assert {v.name for v in result.environment_variables} == {
        "INPUT_PATH",
        "SERVICE_ENDPOINT",
        "OPTIONAL_NAME",
    }
    assert all(v.required.status == Knowledge.UNKNOWN for v in result.environment_variables)
    assert {i.name for i in result.configuration_interfaces} == {"--config", "pyproject.toml"}
    assert (
        result.source.file_hashes["src/sample/__main__.py"]
        == hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert result.source.fingerprint_scope == "analyzed_files"
    assert result.source.content_sha256 == AgentAnalyzer().analyze(tmp_path).source.content_sha256
    source.write_text(source.read_text() + "\n# revised\n", encoding="utf-8")
    revised = AgentAnalyzer().analyze(tmp_path, agent_id=result.agent_id)
    assert result.agent_id == revised.agent_id
    assert result.source.content_sha256 != revised.source.content_sha256


def test_unknown_dynamic_declarations_and_empty_project(tmp_path):
    empty = AgentAnalyzer().analyze(tmp_path)
    assert not empty.entrypoints
    assert empty.requirements.python_specifier.status == Knowledge.UNKNOWN
    assert "Executable entrypoint UNKNOWN" in empty.unknowns
    (tmp_path / "app.py").write_text("import os\nvalue = os.getenv(name)\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text(
        "--index-url ignored\nnot valid ??\n", encoding="utf-8"
    )
    (tmp_path / "broken.py").write_text("def broken(:", encoding="utf-8")
    result = AgentAnalyzer().analyze(tmp_path)
    assert any("Dynamic environment" in item for item in result.unknowns)
    assert any("syntax" in item for item in result.unknowns)
    assert any("Dependency declaration" in item for item in result.unknowns)
    assert not result.environment_variables


@pytest.mark.parametrize(
    "declaration",
    [
        "[broken",
        '[project]\nrequires-python="3.x"',
        '[project]\ndependencies="not a list"',
        "[project.scripts]\nmain=123",
    ],
)
def test_malformed_metadata_has_safe_error(tmp_path, declaration):
    (tmp_path / "pyproject.toml").write_text(declaration, encoding="utf-8")
    with pytest.raises(AnalysisError) as error:
        AgentAnalyzer().analyze(tmp_path)
    assert error.value.code == "INVALID_INPUT"
    assert declaration not in str(error.value)


def test_missing_directory_and_file_input(tmp_path):
    for path in (tmp_path / "missing", tmp_path / "file.txt"):
        if path.name == "file.txt":
            path.write_text("data", encoding="utf-8")
        with pytest.raises(AnalysisError, match="INVALID_INPUT"):
            AgentAnalyzer().analyze(path)


def test_requirements_cycles_relative_paths_and_private_references(tmp_path):
    nested = tmp_path / "requirements"
    nested.mkdir()
    (tmp_path / "requirements.txt").write_text("-r requirements/base.txt\n", encoding="utf-8")
    (nested / "base.txt").write_text(
        "-r ../requirements.txt\nexample @ https://example.invalid/archive.whl\n", encoding="utf-8"
    )
    result = AgentAnalyzer().analyze(tmp_path)
    assert result.dependencies[0].direct_reference is True
    assert any("cycle" in item for item in result.unknowns)
    assert "https://" not in result.model_dump_json()


def test_requirements_cannot_escape_or_read_excluded_files(tmp_path):
    root = tmp_path / "agent"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("example>=1", encoding="utf-8")
    (root / ".env").write_text("private local values", encoding="utf-8")
    for path in ("../outside.txt", ".aws/credentials.txt", ".env"):
        hidden = root / ".aws"
        hidden.mkdir(exist_ok=True)
        (hidden / "credentials.txt").write_text("private>=1", encoding="utf-8")
        (root / "requirements.txt").write_text(f"-r {path}\n", encoding="utf-8")
        with pytest.raises(AnalysisError, match="UNSAFE_PATH"):
            AgentAnalyzer().analyze(root)


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_symlink_sources_rejected(tmp_path, kind):
    root = tmp_path / "agent"
    root.mkdir()
    outside = tmp_path / ("outside.py" if kind == "file" else "outside")
    if kind == "file":
        outside.write_text("pass", encoding="utf-8")
    else:
        outside.mkdir()
    (root / ("linked.py" if kind == "file" else "linked")).symlink_to(
        outside, target_is_directory=kind == "directory"
    )
    with pytest.raises(AnalysisError, match="UNSAFE_PATH"):
        AgentAnalyzer().analyze(root)


def test_analysis_limits_and_non_utf8(tmp_path):
    source = tmp_path / "app.py"
    source.write_bytes(b"#" * 20)
    with pytest.raises(AnalysisError, match="LIMIT_EXCEEDED"):
        AgentAnalyzer(max_file_bytes=10).analyze(tmp_path)
    (tmp_path / "second.py").write_text("pass", encoding="utf-8")
    with pytest.raises(AnalysisError, match="LIMIT_EXCEEDED"):
        AgentAnalyzer(max_files=1).analyze(tmp_path)
    source.write_bytes(b"\xff")
    with pytest.raises(AnalysisError, match="INVALID_INPUT"):
        AgentAnalyzer().analyze(tmp_path)


def test_excluded_environments_are_not_read(tmp_path):
    hidden = tmp_path / ".venv"
    hidden.mkdir()
    (hidden / "broken.py").write_bytes(b"\xff")
    (tmp_path / ".env").write_text("private local values", encoding="utf-8")
    result = AgentAnalyzer().analyze(tmp_path)
    assert result.source.file_hashes == {}


def test_python_requirement_conflict_is_explicit(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python=">=3.12"', encoding="utf-8"
    )
    with pytest.raises(AnalysisError, match="DECLARATION_CONFLICT"):
        AgentAnalyzer().analyze(
            tmp_path,
            operational_requirements=OperationalRequirements(
                python_specifier=known("<3.10", "user declaration")
            ),
        )
