"""
test_validator_gate.py — proves the bundled validator gate is wired correctly.

Runs vendor/validate_topic.py against a known-good sample enriched topic
(the dermatology atopic-dermatitis fixture, copied verbatim from the RX repo)
and asserts it reports 0 errors and exits 0 — exactly what pipeline.run_validator
relies on to gate generated topics.

Run either way:
    pytest tests/test_validator_gate.py
    python tests/test_validator_gate.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

WORKER_ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = WORKER_ROOT / "vendor" / "validate_topic.py"
FIXTURE = WORKER_ROOT / "tests" / "fixtures" / "dermatology-atopic-dermatitis.json"


def _run_validator() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), str(FIXTURE)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_validator_passes_on_sample():
    assert VALIDATOR.exists(), f"validator missing at {VALIDATOR}"
    assert FIXTURE.exists(), f"fixture missing at {FIXTURE}"

    proc = _run_validator()
    stdout = proc.stdout

    # locate the "errors:   N" summary line
    err_count = None
    for line in stdout.splitlines():
        if line.strip().startswith("errors:"):
            err_count = int(line.strip().split(":", 1)[1].strip())
            break

    assert err_count == 0, f"expected 0 errors, got {err_count}\n--- validator output ---\n{stdout}"
    assert proc.returncode == 0, f"validator exit={proc.returncode}\n{stdout}"


if __name__ == "__main__":
    p = _run_validator()
    print(p.stdout)
    if p.returncode != 0:
        print(p.stderr, file=sys.stderr)
        print("VALIDATOR GATE TEST: FAIL")
        sys.exit(1)
    # mirror the pytest assertion for the plain-script path
    errs = None
    for ln in p.stdout.splitlines():
        if ln.strip().startswith("errors:"):
            errs = int(ln.strip().split(":", 1)[1].strip())
            break
    if errs != 0:
        print(f"VALIDATOR GATE TEST: FAIL (errors={errs})")
        sys.exit(1)
    print("VALIDATOR GATE TEST: PASS (0 errors, exit 0)")
