"""Minimal test runner (no pytest dependency):
    python3 tests/run_tests.py
"""
from __future__ import annotations

import importlib
import inspect
import pathlib
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

mods = [importlib.import_module(p.stem)
        for p in sorted(HERE.glob("test_*.py"))]
passed, failed = 0, []
for mod in mods:
    for name, fn in sorted(vars(mod).items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        params = inspect.signature(fn).parameters
        with tempfile.TemporaryDirectory() as td:
            tmp_path = pathlib.Path(td)
            try:
                if "tmp_path" in params:
                    fn(tmp_path)
                else:
                    fn()
                passed += 1
                print(f"PASS {mod.__name__}.{name}")
            except Exception as exc:  # noqa: BLE001
                failed.append((f"{mod.__name__}.{name}", exc))
                print(f"FAIL {mod.__name__}.{name}: {type(exc).__name__}: {exc}")
print(f"\n{passed} passed, {len(failed)} failed")
if failed:
    raise SystemExit(1)