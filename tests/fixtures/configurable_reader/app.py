"""Repository-owned input fixture with explicit knobs; not a platform-generated Agent."""

import argparse
import json
import os
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--delay", type=float, default=3)
parser.add_argument("--mode", type=int, default=0)
parser.add_argument("--data", required=True)
args = parser.parse_args()
path = Path(args.data)
assert os.getuid() != 0 and path.stat().st_uid == 0
assert not os.access(path, os.W_OK) and not os.access(path.parent, os.W_OK)
total = count = 0
with path.open() as stream:
    for line in stream:
        total += json.loads(line)["id"]
        count += 1
time.sleep(args.delay)
print(json.dumps({"records": count, "sum": total if args.mode == 0 else -1}, sort_keys=True))
