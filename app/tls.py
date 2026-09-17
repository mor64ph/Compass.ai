"""Make Python trust the same certificate authorities Windows does.

On a corporate machine, outbound HTTPS often goes through a TLS-inspecting proxy
that re-signs traffic with an internal root CA. Windows trusts that CA; Python's
bundled `certifi` list does not, so requests intermittently die with:

    SSLError: certificate verify failed: self-signed certificate in
    certificate chain

`truststore` points Python's `ssl` module at the OS certificate store, which
includes the corporate root. This is strictly *more* correct than the bundled
list on a managed machine - and it is emphatically not the same as disabling
verification, which would be the wrong fix.

Observed intermittently against the Gemini API on the machine this was built on:
the same URL succeeded and failed minutes apart.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_injected = False


def use_system_certificates() -> bool:
    """Idempotent. Returns True if the OS trust store is now in use."""
    global _injected
    if _injected:
        return True
    try:
        import truststore
    except ImportError:
        logger.debug("truststore not installed - using the bundled CA list")
        return False
    try:
        truststore.inject_into_ssl()
    except Exception as exc:  # pragma: no cover - platform dependent
        logger.warning("Could not use the system certificate store: %s", exc)
        return False
    _injected = True
    logger.debug("Using the system certificate store for TLS verification")
    return True
