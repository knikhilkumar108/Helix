"""Pytest configuration."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the project importable without installation.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Default config for the in-process test environment.
os.environ.setdefault("AUTOMATA_ENV", "test")
os.environ.setdefault("AUTOMATA_LOG_LEVEL", "WARNING")

# pytest-asyncio: per-test loop, no auto mode required.
import pytest

def pytest_collection_modifyitems(config, items):
    for item in items:
        if "asyncio" in item.keywords:
            continue
        if getattr(item, "obj", None) and getattr(item.obj, "__code__", None):
            if item.obj.__code__.co_flags & 0x100:  # CO_COROUTINE
                item.add_marker(pytest.mark.asyncio)
