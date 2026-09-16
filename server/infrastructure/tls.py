"""System trust store injection that keeps the original ``ssl.SSLContext`` usable.

``truststore.inject_into_ssl()`` rebinds the module global ``ssl.SSLContext``
to truststore's subclass so urllib, requests and httpx verify against the OS
keychain. CPython's own ``ssl.SSLContext`` property setters (``options``,
``verify_mode``, ``verify_flags``, ``minimum_version``, ``maximum_version``,
``_msg_callback``) reach their C base through ``super(SSLContext,
SSLContext)``, resolving that name in ssl's module globals at CALL time. After
injection the name is the subclass, whose MRO leads straight back to the same
Python property, so setting ``.options`` on an instance of the ORIGINAL class
recursed until ``RecursionError``.

botocore deliberately builds the original class (urllib3's
``orig_util_SSLContext``) for every client, so with truststore injected every
Bedrock client creation, and therefore every chat turn and title request,
died with "maximum recursion depth exceeded".

The repair rebinds each affected setter to a copy whose globals pin
``SSLContext`` to the original class. The setter bodies run unchanged (the
SSLv3 special case in ``minimum_version``, the callback wrapping in
``_msg_callback``), only the class they name is fixed.
"""

from __future__ import annotations

import logging
import types

log = logging.getLogger("whisper-studio")

GLOBAL_NAME = "SSLContext"


def rebind_original_sslcontext_setters(original: type, name: str = GLOBAL_NAME) -> int:
    """Pin ``name`` to ``original`` inside every property setter of ``original``
    that references it. Returns the number of setters rebound. Idempotent."""
    fixed = 0
    for attr, prop in list(vars(original).items()):
        if not isinstance(prop, property) or prop.fset is None:
            continue
        fset = prop.fset
        code = getattr(fset, "__code__", None)
        if code is None or name not in code.co_names:
            continue
        if getattr(fset, "__whisper_pinned__", False):
            continue  # already rebound by an earlier pass
        pinned = dict(fset.__globals__)
        pinned[name] = original
        new_fset = types.FunctionType(
            code, pinned, fset.__name__, fset.__defaults__, fset.__closure__
        )
        new_fset.__kwdefaults__ = fset.__kwdefaults__
        new_fset.__doc__ = fset.__doc__
        new_fset.__whisper_pinned__ = True  # type: ignore[attr-defined]
        setattr(original, attr, property(prop.fget, new_fset, prop.fdel, prop.__doc__))
        fixed += 1
    return fixed


def install_system_trust() -> bool:
    """Inject truststore into ``ssl`` and repair the original class. Returns
    True when the system store is active. Fails open: if truststore is missing
    the bundled CA list stays in use; if the repair fails the injection is
    undone so nothing is left half-patched."""
    import ssl

    try:
        import truststore
    except Exception as exc:  # noqa: BLE001 - optional dependency
        log.warning("system trust store unavailable, using the bundled CA list: %s", exc)
        return False
    original = ssl.SSLContext
    try:
        truststore.inject_into_ssl()
    except Exception as exc:  # noqa: BLE001
        log.warning("system trust store injection failed, using the bundled CA list: %s", exc)
        return False
    if ssl.SSLContext is original:
        return True  # nothing was rebound (already injected, or a no-op build)
    try:
        fixed = rebind_original_sslcontext_setters(original)
        # Prove the repair on a throwaway instance of the ORIGINAL class: the
        # exact operation botocore performs when it builds a client.
        probe = original(ssl.PROTOCOL_TLS_CLIENT)
        probe.options |= ssl.OP_NO_COMPRESSION
        probe.verify_mode = ssl.CERT_REQUIRED
        log.info("system trust store active (truststore); %d ssl setters rebound", fixed)
        return True
    except Exception as exc:  # noqa: BLE001 - includes RecursionError
        try:
            truststore.extract_from_ssl()
        finally:
            log.warning(
                "system trust store disabled: the original ssl.SSLContext could not be "
                "repaired after injection (%s); using the bundled CA list",
                type(exc).__name__,
            )
        return False


__all__ = ["GLOBAL_NAME", "install_system_trust", "rebind_original_sslcontext_setters"]
