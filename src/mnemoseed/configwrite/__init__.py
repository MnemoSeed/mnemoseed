"""ConfigWriteService (PRD-07 FR-7.11 / design/07 section 9, W1.1).

The daemon's single config writer: key-path registry -> validate -> surgical
TOML patch -> versioned meta-store record -> audit (actor attributed) ->
live-apply, plus the /api/v1/config REST surface the CLI codes against.
"""

from mnemoseed.configwrite.routes import router
from mnemoseed.configwrite.service import (
    CONFIG_KEY_REGISTRY,
    ConfigWriteError,
    ConfigWriteService,
)

__all__ = ["CONFIG_KEY_REGISTRY", "ConfigWriteError", "ConfigWriteService", "router"]
