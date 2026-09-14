#!/usr/bin/env python3
"""Convenience entry point: `uv run diligence.py "Datadog" --domain datadog.com`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from diligence.cli import app  # noqa: E402

if __name__ == "__main__":
    app()
