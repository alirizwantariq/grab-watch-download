"""Minimal test runner (no pytest dependency):
    python3 tests/run_tests.py                 # everything
    python3 tests/run_tests.py pause cancel    # only tests whose name matches

Runs inside a throwaway sandbox (see conftest.py): the real ~/.config/mgd
and ~/Downloads/mgd are never read or written.
"""
from __future__ import annotations

import importlib
import inspect
import os
import pathlib
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

os.chdir(HERE.parent)   # tests launch bin/mgd relative to cli/
import conftest  # noqa: E402  (first: sandboxes config + download root)

conftest.assert_sandboxed()
only = [a for a in sys.argv[1:] if not a.startswith("-")]
mods = [importlib.import_module(p.stem)
        for p in sorted(HERE.glob("test_*.py"))]
passed, failed = 0, []
for mod in mods:
    for name, fn in sorted(vars(mod).items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        if only and not any(o in f"{mod.__name__}.{name}" for o in only):
            continue
        params = inspect.signature(fn).parameters
        with tempfile.TemporaryDirectory() as td:
            tmp_path = pathlib.Path(td)
            t0 = time.monotonic()
            try:
                if "tmp_path" in params:
                    fn(tmp_path)
                else:
                    fn()
                passed += 1
                took = time.monotonic() - t0
                print(f"PASS {mod.__name__}.{name}"
                      + (f"  ({took:.0f}s)" if took >= 5 else ""), flush=True)
            except Exception as exc:  # noqa: BLE001
                failed.append((f"{mod.__name__}.{name}", exc))
                print(f"FAIL {mod.__name__}.{name}: {type(exc).__name__}: {exc}")
print(f"\n{passed} passed, {len(failed)} failed")
if failed:
    raise SystemExit(1)