"""Repository-owned pipeline fixture, not a replacement for user Agent acceptance."""

import json
import os
import signal
import sys
import time
from pathlib import Path

mode = sys.argv[1]
if mode == "normal":
    # Stay alive long enough for Docker's stats interval; exercise CPU and RAM.
    payload = bytearray(8 * 1024**2)
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        sum(range(10000))
    print("ok", flush=True)
elif mode == "nonzero":
    print("expected error", file=sys.stderr, flush=True)
    sys.exit(7)
elif mode == "timeout":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)
elif mode == "oom":
    payloads = []
    while True:
        payloads.append(bytearray(8 * 1024**2))
elif mode == "wrong":
    print("wrong", flush=True)
elif mode == "logs":
    print("x" * 100000, flush=True)
    print("y" * 100000, file=sys.stderr, flush=True)
elif mode == "policy":
    assert os.getuid() != 0
    status = Path("/proc/self/status").read_text()
    assert "CapEff:\t0000000000000000" in status
    assert "NoNewPrivs:\t1" in status
    assert sorted(p.name for p in Path("/sys/class/net").iterdir()) == ["lo"]
    print(
        json.dumps(
            {
                "cpu": Path("/sys/fs/cgroup/cpu.max").read_text().strip(),
                "memory": Path("/sys/fs/cgroup/memory.max").read_text().strip(),
            }
        ),
        flush=True,
    )
