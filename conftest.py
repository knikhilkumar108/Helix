"""Top-level conftest. Adds the repo root to sys.path so tests
can import from services.*, runtime.*, core.* without an install
step. Belt-and-braces with pytest.ini's pythonpath setting."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
