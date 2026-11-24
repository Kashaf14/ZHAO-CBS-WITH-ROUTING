# Request	                            Zhao CBS	        Zhao CBS + Routing
# routing: fixed, path provided	        Eligible	        Eligible
# routing: preserve, path provided	    Eligible	        Eligible
# routing: optimize, path provided	    Not eligible	    Eligible
# routing: optimize, path missing	    Not eligible	    Eligible
import os

# Select optimizer variant through the container environment.
CBS_VARIANT = os.environ.get(
    "CBS_VARIANT", "routing"
).strip().lower()

if CBS_VARIANT not in ("routing", "fixed"):
    raise ValueError(
        "CBS_VARIANT must be 'routing' or 'fixed'"
    )

ROUTING_ENABLED = CBS_VARIANT == "routing"
REQUIRES_ROUTES = not ROUTING_ENABLED

if ROUTING_ENABLED:
    OPTIMIZER_ID = "zhao-cbs-routing"
    NAME = "Zhao CBS + Routing"
else:
    OPTIMIZER_ID = "zhao-cbs"
    NAME = "Zhao CBS"

VERSION = os.environ.get(
    "CBS_OPTIMIZER_VERSION", "unversioned"
)