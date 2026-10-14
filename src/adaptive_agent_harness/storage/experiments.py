"""Immutable, private experiment snapshots with atomic publication and no overwrite."""

import os
import tempfile
from pathlib import Path

from adaptive_agent_harness.contracts.adaptation import ExperimentOutcome, IncubationReport


class JsonExperimentStore:
    def __init__(self, directory: Path):
        from adaptive_agent_harness.data.local import directory_fd

        with directory_fd(directory) as root:
            self.directory = directory
            stat = os.fstat(root)
            self._identity = (stat.st_dev, stat.st_ino)

    def save(self, record: ExperimentOutcome | IncubationReport) -> Path:
        from adaptive_agent_harness.data.local import directory_fd

        model = type(record)
        record = model.model_validate_json(record.model_dump_json())
        identifier = (
            record.experiment_id if isinstance(record, ExperimentOutcome) else record.session_id
        )
        name = f"{identifier}.json"
        # Anchor writes to the reopened safe directory descriptor, never follow path replacement.
        with directory_fd(self.directory) as root:
            stat = os.fstat(root)
            if (stat.st_dev, stat.st_ino) != self._identity:
                raise ValueError("STORAGE_ROOT_REPLACED")
            descriptor, temporary = tempfile.mkstemp(
                prefix=".experiment-", dir=f"/proc/self/fd/{root}"
            )
            temporary_name = Path(temporary).name
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(record.model_dump_json(indent=2))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(
                    temporary_name, name, src_dir_fd=root, dst_dir_fd=root, follow_symlinks=False
                )
                os.fsync(root)
            finally:
                os.unlink(temporary_name, dir_fd=root)
        return self.directory / name
