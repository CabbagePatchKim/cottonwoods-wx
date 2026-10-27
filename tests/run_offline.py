"""Tiny runner for machines without pytest: python tests/run_offline.py"""
import sys
import traceback
from pathlib import Path

# Make `import cottonwoods` work even if the package isn't pip-installed, and make the test files importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import importlib  # noqa: E402

# Every tests/test_*.py file, so a new test file can't be forgotten here
modules = [importlib.import_module(p.stem) for p in sorted(Path(__file__).resolve().parent.glob("test_*.py"))]
failed = 0
# Find every function whose name starts with "test_" (the same rule pytest uses) and run each one.
# A test passes if it finishes; any failed `assert` or error raises an exception and marks it FAIL.
cases = [(m, n) for m in modules for n in sorted(dir(m)) if n.startswith("test_")]
for mod, name in cases:
    try:
        getattr(mod, name)()
        print(f"PASS {name}")
    except Exception:  # noqa: BLE001
        failed += 1
        print(f"FAIL {name}")
        traceback.print_exc()
sys.exit(1 if failed else 0)  # non-zero exit = something failed (useful for scripts and CI)
