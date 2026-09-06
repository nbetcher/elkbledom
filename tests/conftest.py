"""Initialize Home Assistant's validation compatibility layer before collection."""

# HA installs its voluptuous compatibility layer during package initialization.
# Import it before test modules import the integration's service schemas.
import homeassistant  # noqa: F401
