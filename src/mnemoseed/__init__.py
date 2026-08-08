"""MnemoSeed — cross-model neutral AI memory layer.

Five-stage memory pipeline (Capture / Consolidate / Retrieve / Reconcile / Decay)
derived from neuroscience. See docs/design/ for the full design.
"""

__version__ = "0.0.1"


def health() -> bool:
    """Minimal liveness check for the core package."""
    return True
