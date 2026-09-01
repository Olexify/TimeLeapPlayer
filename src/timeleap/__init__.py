"""TimeLeapPlayer -- play video using Windows windows as pixels.

The version lives here and nowhere else: `pyproject.toml` reads it back
through setuptools' dynamic-attr support, and the CLI imports it, so the
packaged version and `timeleap --version` cannot drift apart.
"""
from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
