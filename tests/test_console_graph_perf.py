"""Console Graph View performance smoke (NFR-7.2 v2, CI-skip-safe).

The >= 30 fps @5k-nodes floor needs a real GPU + display (headed Chrome),
which CI does not have, so this test is SKIPPED by default. Set
``MNEMOSEED_GRAPH_PERF=1`` to run it: it serves the console static dir on a
spare port, drives the page's built-in perf mode (``#/graph?perf=1``, 5k
nodes / ~19k edges, 20 s decay animation) through the bench's Playwright
install, and prints the measured fps. The gate assertion (avg >= 30) lives in
the node harness; the test only requires the harness to complete and log a
well-formed result, so a GPU-less environment cannot fail CI.

Documented commands:

    $env:MNEMOSEED_GRAPH_PERF=1; uv run --no-sync pytest tests/test_console_graph_perf.py -s
    node scripts/graphview_perf.mjs
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "scripts" / "graphview_perf.mjs"
STATIC = REPO / "src" / "mnemoseed" / "console" / "static"

NEEDS_GPU = pytest.mark.skipif(
    not os.environ.get("MNEMOSEED_GRAPH_PERF"),
    reason=(
        "set MNEMOSEED_GRAPH_PERF=1 to run the GPU Graph View fps smoke "
        "(headed browser + real display required; CI is GPU-less by design)"
    ),
)


def _spare_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@NEEDS_GPU
def test_graph_view_perf_mode_reports_fps() -> None:
    if not HARNESS.exists():
        pytest.skip("scripts/graphview_perf.mjs missing")
    if not (STATIC / "vendor" / "three.module.js").exists():
        pytest.fail("vendored three.module.js missing from /console/vendor")
    env = dict(os.environ)
    env["MNEMOSEED_PERF_PORT"] = str(_spare_port())
    proc = subprocess.run(
        ["node", str(HARNESS)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    output = proc.stdout + proc.stderr
    print(output)
    assert proc.returncode == 0, f"perf harness failed:\n{output}"
    match = re.search(r"result=\{.*\}", output)
    assert match, f"no result= line in harness output:\n{output}"
    result = json.loads(match.group(0).replace("result=", ""))
    assert result["nodes"] >= 5000, result
    assert result["avgFps"] > 0 and result["p5Fps"] > 0, result
