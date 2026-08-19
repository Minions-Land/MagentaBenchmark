"""Project entrypoint for the digest-bound Apptainer backend factory."""

from __future__ import annotations

import hashlib
from pathlib import Path

from MagentaBench.runner.backend.apptainer import (
    ApptainerBackendFactory as _CoreFactory,
)


class ApptainerBackendFactory(_CoreFactory):
    """Bind the project plugin declaration to the shared runtime factory."""

    digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


__all__ = ["ApptainerBackendFactory"]
