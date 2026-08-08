"""Toolchain smoke tests: the package imports, exposes a version, and health().

These prove the developer skeleton is wired correctly before any storage or
pipeline logic lands.
"""

import mnemoseed


def test_version_is_defined() -> None:
    assert isinstance(mnemoseed.__version__, str)
    assert mnemoseed.__version__


def test_health_stub_reports_true() -> None:
    assert mnemoseed.health() is True
