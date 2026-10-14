"""Private, atomic, no-overwrite local result storage."""

import os
import tempfile
from pathlib import Path

from adaptive_agent_harness.contracts.runtime import BaselineResult


class JsonResultStore:
    def __init__(self, directory: Path):
        self.directory = directory.resolve(strict=True)
        if not self.directory.is_dir():
            raise ValueError("Result directory must already exist")

    def save(self, result: BaselineResult) -> Path:
        path = self.directory / f"{result.runtime.run_id}.json"
        descriptor, temporary = tempfile.mkstemp(prefix=".baseline-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(result.model_dump_json(indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path)
        finally:
            os.unlink(temporary)
        return path
