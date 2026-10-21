"""Single source of truth for the Expra Connect harness package version.

Packaging metadata derives the distribution version from this constant (see
``pyproject.toml`` ``[tool.setuptools.dynamic]``), and the release tooling
bumps it automatically (see ``maintenance._release``). Kept as its own leaf
module so setuptools can read it at build time without importing Qt or runtime
dependencies.
"""

__version__ = "1.7.0.1"
