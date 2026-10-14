import json
import os
import socket
from pathlib import Path
from uuid import uuid4

import pytest

from adaptive_agent_harness.analyzer import AgentAnalyzer, CompatibilityEngine
from adaptive_agent_harness.contracts import (
    BindMount,
    ContainerHarnessSpecification,
    DependencyInstallation,
    Fact,
    NetworkPolicy,
    SecuritySettings,
    known,
)
from adaptive_agent_harness.harness import (
    ContainerHarnessBuilder,
    HarnessError,
    HarnessSpecificationGenerator,
    _utils,
)


def change(model, **updates):
    return type(model).model_validate({**model.model_dump(), **updates})


def package(inputs, output_root, name="package", **updates):
    agent, target, host, report, request = inputs
    spec = HarnessSpecificationGenerator().generate(
        agent, target, host, report, change(request, **updates)
    )
    result = ContainerHarnessBuilder().build(
        spec, agent, target, host, output_root=output_root, package_name=name
    )
    return result, spec


def read_compose(result):
    return json.loads((result.directory / "compose.yaml").read_text())["services"]["agent"]


def test_package_files_security_limits_and_source_preservation(container_inputs, tmp_path):
    agent = container_inputs[0]
    root = Path(agent.source.root)
    original = {
        p.relative_to(root): (p.read_bytes(), p.stat().st_mode)
        for p in root.rglob("*")
        if p.is_file()
    }
    result, spec = package(container_inputs, tmp_path)
    assert {
        "Dockerfile",
        "compose.yaml",
        "harness.yaml",
        ".env.example",
        "README.md",
        ".dockerignore",
        "manifest.json",
    }.issubset({p.name for p in result.directory.iterdir()})
    assert (
        ContainerHarnessSpecification.model_validate_json(
            (result.directory / "harness.yaml").read_bytes()
        )
        == spec
    )
    dockerfile = (result.directory / "Dockerfile").read_text()
    assert "USER 65532:65532" in dockerfile
    assert 'COPY --chown=65532:65532 ["agent/", "/opt/agent/"]' in dockerfile
    assert 'ENTRYPOINT ["python", "/opt/agent/app.py"]' in dockerfile
    assert "CMD []" in dockerfile and "RUN " not in dockerfile
    service = read_compose(result)
    assert service["cpus"] == 2 and service["mem_limit"] == 2**30
    assert service["user"] == "65532:65532" and service["privileged"] is False
    assert service["cap_drop"] == ["ALL"] and service["security_opt"] == ["no-new-privileges:true"]
    assert service["network_mode"] == "none" and service["restart"] == "no"
    assert service["profiles"] == ["pending-validation"]
    assert service["command"] == [] and "volumes" not in service
    assert not result.manifest.execution_verified and not result.manifest.image_built
    assert result.manifest.security_review == "UNVERIFIED"
    assert original == {
        p.relative_to(root): (p.read_bytes(), p.stat().st_mode)
        for p in root.rglob("*")
        if p.is_file()
    }
    assert ContainerHarnessBuilder().verify(result.directory) == result.manifest


def test_reproducible_bytes_for_saved_inputs_and_new_output_directories(container_inputs, tmp_path):
    first, spec = package(container_inputs, tmp_path, "first")
    second, again = package(container_inputs, tmp_path, "second")
    left = {
        p.relative_to(first.directory): p.read_bytes()
        for p in first.directory.rglob("*")
        if p.is_file()
    }
    right = {
        p.relative_to(second.directory): p.read_bytes()
        for p in second.directory.rglob("*")
        if p.is_file()
    }
    assert left == right and spec == again


def test_unknown_configuration_is_preserved_without_defaults(container_inputs, tmp_path):
    result, spec = package(
        container_inputs,
        tmp_path,
        resource_limits=None,
        working_directory=Fact(),
        source_complete=Fact(),
        network_required=Fact(),
    )
    service = read_compose(result)
    assert "cpus" not in service and "mem_limit" not in service and "working_dir" not in service
    assert "WORKDIR " not in (result.directory / "Dockerfile").read_text()
    assert result.manifest.static_readiness.value == "REVIEW_REQUIRED"
    assert not result.manifest.execution_verified


def test_environment_template_contains_names_not_secret_values(container_inputs, tmp_path):
    result, _ = package(
        container_inputs,
        tmp_path,
        environment_references={"API_KEY": "secret://vault/key", "INPUT_PATH": "env://input"},
    )
    template = (result.directory / ".env.example").read_text()
    assert "API_KEY=\n" in template and "INPUT_PATH=\n" in template
    env = read_compose(result)["environment"]
    assert env["API_KEY"].startswith("${API_KEY?")
    assert "secret://" not in template
    assert (
        ".env" not in (result.directory / ".dockerignore").read_text()
    )  # blanket allowlist excludes all environment files


def test_credentials_in_source_block_all_writes(container_inputs, tmp_path):
    agent, target, host, _, request = container_inputs
    path = Path(agent.source.root) / "app.py"
    secret = "x" * 40
    path.write_text("API_KEY = " + repr(secret) + "\n", encoding="utf-8")
    agent = AgentAnalyzer().analyze(
        agent.source.root, agent_id=agent.agent_id, operational_requirements=agent.requirements
    )
    report = CompatibilityEngine().assess(agent, target)
    request = change(request, source_files=agent.source.file_hashes)
    spec = HarnessSpecificationGenerator().generate(agent, target, host, report, request)
    with pytest.raises(HarnessError, match="SENSITIVE_CONTENT") as error:
        ContainerHarnessBuilder().build(
            spec, agent, target, host, output_root=tmp_path, package_name="sensitive"
        )
    assert secret not in str(error.value) and not (tmp_path / "sensitive").exists()


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "nested/package", "..", "", "bad\\path"]
)
def test_output_name_cannot_escape_root(container_inputs, tmp_path, name):
    with pytest.raises(HarnessError, match="UNSAFE_PATH"):
        package(container_inputs, tmp_path, name)
    assert not (tmp_path.parent / "escape").exists()


def test_existing_output_and_symlink_output_are_not_overwritten(container_inputs, tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "marker"
    marker.write_text("preserve me", encoding="utf-8")
    with pytest.raises(HarnessError, match="OUTPUT_EXISTS"):
        package(container_inputs, tmp_path, "existing")
    assert marker.read_text() == "preserve me"
    alias = tmp_path / "alias"
    alias.symlink_to(existing, target_is_directory=True)
    with pytest.raises(HarnessError, match="UNSAFE_PATH"):
        package(container_inputs, alias)
    with pytest.raises(HarnessError, match="OUTPUT_EXISTS"):
        package(container_inputs, tmp_path, "alias")
    assert marker.read_text() == "preserve me"


def test_changed_source_and_symlink_source_cannot_be_packaged(container_inputs, tmp_path):
    agent, target, host, _, request = container_inputs
    spec = HarnessSpecificationGenerator().generate(*container_inputs)
    path = Path(agent.source.root) / "app.py"
    path.write_text("pass\n", encoding="utf-8")
    with pytest.raises(HarnessError, match="SOURCE_VERSION_MISMATCH"):
        ContainerHarnessBuilder().build(spec, agent, target, host, output_root=tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("pass\n", encoding="utf-8")
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(HarnessError, match="UNSAFE_SOURCE"):
        ContainerHarnessBuilder().build(spec, agent, target, host, output_root=tmp_path)


def test_mount_allowlist_readonly_and_writable_original_block(container_inputs, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "input.txt").write_text("data", encoding="utf-8")
    result, _ = package(
        container_inputs,
        tmp_path,
        mounts=(BindMount(source=str(data), target="/data"),),
        allowed_host_paths=(str(data),),
    )
    assert read_compose(result)["volumes"][0]["read_only"] is True
    assert read_compose(result)["volumes"][0]["bind"]["create_host_path"] is False
    with pytest.raises(HarnessError, match="UNSAFE_MOUNT"):
        package(
            container_inputs,
            tmp_path,
            "invalid",
            mounts=(BindMount(source=str(data), target="/data"),),
        )
    agent_root = container_inputs[0].source.root
    with pytest.raises(HarnessError, match="UNSAFE_MOUNT"):
        package(
            container_inputs,
            tmp_path,
            "write-original",
            allowed_host_paths=(agent_root,),
            mounts=(BindMount(source=agent_root, target="/data", read_only=False),),
        )


def test_directory_mount_cannot_smuggle_sockets(container_inputs, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with socket.socket(socket.AF_UNIX) as control:
        control.bind(str(data / "control"))
        with pytest.raises(HarnessError, match="UNSAFE_MOUNT"):
            package(
                container_inputs,
                tmp_path,
                mounts=(BindMount(source=str(data), target="/data"),),
                allowed_host_paths=(str(data),),
            )


@pytest.mark.parametrize(
    "mode,applied",
    [("DENY", "none"), ("UNRESTRICTED", "bridge"), ("ALLOWLIST", "none"), ("UNKNOWN", "none")],
)
def test_network_declarations_and_enforcement_are_distinct(
    container_inputs, tmp_path, mode, applied
):
    network = NetworkPolicy(
        mode=mode, allowed_destinations=("example.invalid",) if mode == "ALLOWLIST" else ()
    )
    result, spec = package(container_inputs, tmp_path, network=network)
    assert read_compose(result)["network_mode"] == applied
    if mode in ("ALLOWLIST", "UNKNOWN"):
        assert spec.network.enforcement == "DECLARATIVE_WITH_DENY_FALLBACK"
        assert any("declarative only" in note for note in spec.unknowns)


@pytest.mark.parametrize("compatible,enabled", [(True, True), (False, False), (None, False)])
def test_readonly_rootfs_requires_explicit_compatibility(
    container_inputs, tmp_path, compatible, enabled
):
    value = (
        known(compatible, "operator verified compatibility") if compatible is not None else Fact()
    )
    result, _ = package(
        container_inputs, tmp_path, security=SecuritySettings(read_only_rootfs_compatible=value)
    )
    assert read_compose(result)["read_only"] is enabled


def test_requirements_installation_is_exec_form_not_shell(container_inputs, tmp_path):
    agent, target, host, _, request = container_inputs
    path = Path(agent.source.root) / "requirements.txt"
    path.write_text("example>=1\n", encoding="utf-8")
    agent = AgentAnalyzer().analyze(
        agent.source.root, agent_id=agent.agent_id, operational_requirements=agent.requirements
    )
    request = change(
        request,
        source_files=agent.source.file_hashes,
        installation=DependencyInstallation(
            mode="REQUIREMENTS", python_executable="python", requirements_file="requirements.txt"
        ),
    )
    result, _ = package(
        (agent, target, host, CompatibilityEngine().assess(agent, target), request), tmp_path
    )
    dockerfile = (result.directory / "Dockerfile").read_text()
    assert (
        'RUN ["python", "-m", "pip", "install", "--no-cache-dir", "--requirement", "/opt/agent/requirements.txt"]'
        in dockerfile
    )
    assert "RUN sh" not in dockerfile and "RUN bash" not in dockerfile
    assert result.manifest.static_readiness.value == "REVIEW_REQUIRED"


def test_literal_shell_metacharacters_remain_argv_data(container_inputs, tmp_path):
    result, _ = package(
        container_inputs,
        tmp_path,
        argv=("python", "/opt/agent/app.py", "literal; $(not_a_command)", "$LITERAL"),
    )
    assert read_compose(result)["entrypoint"][-1] == "$$LITERAL"
    assert '"$LITERAL"' in (result.directory / "Dockerfile").read_text()


def test_arm_package_metadata_keeps_host_cpu_separate(container_inputs, tmp_path):
    agent, target, host, _, request = container_inputs
    agent = change(agent, requirements=change(agent.requirements, architecture=Fact()))
    target = change(
        target,
        hardware=change(
            target.hardware,
            platform=change(
                target.hardware.platform, architecture=known("arm64", "target declaration")
            ),
        ),
    )
    report = CompatibilityEngine().assess(agent, target)
    result, spec = package((agent, target, host, report, request), tmp_path)
    assert result.manifest.host_architecture.value == "x86_64"
    assert result.manifest.target_architecture.value == "arm64"
    assert result.manifest.container_platform.value.value == "linux/arm64"
    assert read_compose(result)["platform"] == "linux/arm64"
    arguments = result.manifest.build_plan.arguments()
    assert arguments[arguments.index("--platform") + 1] == "linux/arm64"
    assert not result.manifest.execution_verified
    assert "ARM64" in (result.directory / "README.md").read_text()


@pytest.mark.parametrize("tamper", ["content", "mode", "extra", "identity"])
def test_verifier_detects_modified_packages(container_inputs, tmp_path, tamper):
    result, _ = package(container_inputs, tmp_path)
    if tamper == "content":
        (result.directory / "Dockerfile").write_text("tampered", encoding="utf-8")
    elif tamper == "mode":
        (result.directory / "Dockerfile").chmod(0o600)
    elif tamper == "extra":
        (result.directory / "agent" / "unexpected.py").write_text("pass", encoding="utf-8")
    else:
        path = result.directory / "manifest.json"
        data = json.loads(path.read_text())
        data["harness_id"] = str(uuid4())
        path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(HarnessError, match="PACKAGE_INTEGRITY"):
        ContainerHarnessBuilder().verify(result.directory)


def test_size_limits_and_stale_input_profiles(container_inputs, tmp_path):
    agent, target, host, _, _ = container_inputs
    spec = HarnessSpecificationGenerator().generate(*container_inputs)
    with pytest.raises(HarnessError, match="PACKAGE_LIMIT"):
        ContainerHarnessBuilder(max_file_bytes=1).build(
            spec, agent, target, host, output_root=tmp_path
        )
    with pytest.raises(HarnessError, match="INPUT_MISMATCH"):
        ContainerHarnessBuilder().build(
            spec, agent, change(target, name="changed target"), host, output_root=tmp_path
        )


def test_write_failure_removes_only_new_owned_files(tmp_path, monkeypatch):
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "marker").write_text("keep", encoding="utf-8")
    original_open = os.open

    def fail_second(path, *args, **kwargs):
        if path == "second":
            raise OSError("simulated write failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(_utils.os, "open", fail_second)
    with pytest.raises(OSError):
        _utils.write_package(
            tmp_path, "new", {"agent/first": (b"created", 0o644), "agent/second": (b"fail", 0o644)}
        )
    assert not (tmp_path / "new").exists()
    assert (existing / "marker").read_text() == "keep"
