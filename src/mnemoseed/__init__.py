"""MnemoSeed — cross-model neutral AI memory layer.

Five-stage memory pipeline (Capture / Consolidate / Retrieve / Reconcile / Decay)
derived from neuroscience. See docs/design/ for the full design.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("mnemoseed")
except PackageNotFoundError:
    __version__ = "0.0.0+dev"


def health() -> bool:
    """Minimal liveness check for the core package."""
    return True
