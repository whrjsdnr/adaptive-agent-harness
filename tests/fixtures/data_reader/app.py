"""Repository test input, never a platform-generated user Agent."""

import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else os.environ["DATA_FILE"])
count = total = 0
with path.open() as stream:
    for line in stream:
        record = json.loads(line)
        total += record["id"]
        count += 1
try:
    with path.open("a") as stream:
        stream.write("unexpected write")
except PermissionError:
    readonly = True
else:
    readonly = False
assert readonly
assert not os.access(path.parent, os.W_OK)
assert path.stat().st_uid == 0
assert os.getuid() != 0
print(json.dumps({"records": count, "sum": total, "read_only": readonly}, sort_keys=True))
