"""Entrypoint module for `kopf run -m fleet_operator.handlers`.

Kopf discovers handlers via decorator side-effects at import time, so this
module only needs to import every controller submodule.
"""

from . import (  # noqa: F401
    adaptation_controller,
    lifecycle_controller,
    robotfleet_controller,
    rosmodule_controller,
)
