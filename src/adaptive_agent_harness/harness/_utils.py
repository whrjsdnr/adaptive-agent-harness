"""Small content and filesystem helpers confined to container packaging."""

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath

from adaptive_agent_harness.contracts.harness import BindMount, relative_artifact_path


class HarnessError(ValueError):
    def __init__(self, code: str, message: str, *, unknowns: tuple[str, ...] = ()):
        self.code = code
        self.unknowns = unknowns
        super().__init__(f"{code}: {message}")


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def content_hash(value: object) -> str:
    def semantic(item: object) -> object:
        if isinstance(item, dict):
            return {k: semantic(v) for k, v in item.items() if k != "created_at"}
        if isinstance(item, (tuple, list)):
            return [semantic(v) for v in item]
        return item

    return hashlib.sha256(canonical_json(semantic(value))).hexdigest()


def stamp_created_at(value: object, timestamp: str) -> object:
    if isinstance(value, dict):
        return {
            k: timestamp if k == "created_at" else stamp_created_at(v, timestamp)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [stamp_created_at(v, timestamp) for v in value]
    return value


def no_symlink_ancestors(path: Path) -> None:
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise HarnessError("UNSAFE_PATH", "Symlink paths are forbidden")


def read_regular(root: Path, relative: str, max_bytes: int) -> tuple[bytes, int]:
    relative_artifact_path(relative)
    no_symlink_ancestors(root)
    descriptors: list[int] = []
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(fd)
        parts = PurePosixPath(relative).parts
        for component in parts[:-1]:
            fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        descriptors.append(file_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise HarnessError("UNSAFE_SOURCE", "Only regular source files are permitted")
        if info.st_size > max_bytes:
            raise HarnessError("PACKAGE_LIMIT", "File exceeds configured package size limit")
        with os.fdopen(os.dup(file_fd), "rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise HarnessError("PACKAGE_LIMIT", "File exceeds configured package size limit")
        return data, stat.S_IMODE(info.st_mode)
    except OSError as exc:
        raise HarnessError("UNSAFE_SOURCE", "Cannot safely read regular source file") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def validate_mounts(mounts: tuple[BindMount, ...], allowed: tuple[str, ...]) -> None:
    validate_mount_scope(mounts, allowed)
    roots = [Path(p) for p in allowed]
    if any(root == Path("/") for root in roots):
        raise HarnessError("UNSAFE_MOUNT", "Whole host filesystem access is forbidden")
    for root in roots:
        no_symlink_ancestors(root)
    for mount in mounts:
        source = Path(mount.source)
        no_symlink_ancestors(source)
        if not any(source.is_relative_to(root) for root in roots):
            raise HarnessError("UNSAFE_MOUNT", "Mount is outside the explicit host allowlist")
        if not source.exists():
            raise HarnessError(
                "UNVERIFIED_MOUNT",
                "Mount source cannot be verified on this host",
                unknowns=("Mount source existence UNKNOWN",),
            )
        entries = [source]
        if source.is_dir():

            def failed_walk(error: OSError) -> None:
                raise HarnessError("UNVERIFIED_MOUNT", "Cannot inspect mount directory") from error

            for directory, dirs, files in os.walk(source, followlinks=False, onerror=failed_walk):
                entries.extend(Path(directory) / name for name in (*dirs, *files))
                if len(entries) > 10_000:
                    raise HarnessError(
                        "UNVERIFIED_MOUNT", "Mount directory inspection limit exceeded"
                    )
        for path in entries:
            mode = path.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)) or path.name.endswith(".sock"):
                raise HarnessError(
                    "UNSAFE_MOUNT", "Sockets, devices and symlinks cannot be mounted"
                )


def validate_mount_scope(mounts: tuple[BindMount, ...], allowed: tuple[str, ...]) -> None:
    """Pure declaration check; filesystem observations belong to the packaging adapter."""
    roots = [PurePosixPath(p) for p in allowed]
    if PurePosixPath("/") in roots:
        raise HarnessError("UNSAFE_MOUNT", "Whole host filesystem access is forbidden")
    for mount in mounts:
        if not any(PurePosixPath(mount.source).is_relative_to(root) for root in roots):
            raise HarnessError("UNSAFE_MOUNT", "Mount is outside the explicit host allowlist")


def write_package(root: Path, name: str, files: dict[str, tuple[bytes, int]]) -> Path:
    """Exclusive creation via directory descriptors. Never reuse an existing package."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
        raise HarnessError("UNSAFE_PATH", "Package name must be one relative directory name")
    no_symlink_ancestors(root)
    if not root.is_dir():
        raise HarnessError("INVALID_OUTPUT", "Output root must already exist as a directory")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    package_fd: int | None = None
    written: list[str] = []
    directories: set[str] = set()
    created = False
    try:
        try:
            os.mkdir(name, mode=0o755, dir_fd=root_fd)
            created = True
        except FileExistsError:
            raise HarnessError(
                "OUTPUT_EXISTS", "Existing package directories are never overwritten"
            ) from None
        package_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        for relative, (data, mode) in sorted(files.items()):
            relative_artifact_path(relative)
            parts = PurePosixPath(relative).parts
            parent_fd = os.dup(package_fd)
            prefix: list[str] = []
            try:
                for component in parts[:-1]:
                    prefix.append(component)
                    try:
                        os.mkdir(component, mode=0o755, dir_fd=parent_fd)
                        directories.add("/".join(prefix))
                    except FileExistsError:
                        pass
                    next_fd = os.open(
                        component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
                    )
                    os.close(parent_fd)
                    parent_fd = next_fd
                fd = os.open(
                    parts[-1],
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    mode=mode,
                    dir_fd=parent_fd,
                )
                written.append(relative)
                with os.fdopen(fd, "wb") as stream:
                    os.fchmod(stream.fileno(), mode)
                    stream.write(data)
            finally:
                os.close(parent_fd)
    except Exception:
        if package_fd is not None:
            for relative in reversed(written):
                os.unlink(relative, dir_fd=package_fd)
            for directory in sorted(directories, key=lambda p: p.count("/"), reverse=True):
                os.rmdir(directory, dir_fd=package_fd)
        if created:
            os.rmdir(name, dir_fd=root_fd)
        raise
    finally:
        if package_fd is not None:
            os.close(package_fd)
        os.close(root_fd)
    return root / name
