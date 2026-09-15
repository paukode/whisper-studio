"""server.main injects truststore at import so TLS verification consults the
OS trust store (macOS keychain) as well as certifi. Behind a TLS-intercepting
corporate proxy the packaged runtime otherwise failed every HTTPS WebFetch with
CERTIFICATE_VERIFY_FAILED while the same sites opened fine in the browser."""

import ssl


def test_main_import_switches_ssl_context_to_the_system_trust_store():
    import truststore

    import server.main  # noqa: F401  (import-time side effect under test)

    assert ssl.SSLContext is truststore.SSLContext
    # Existing callers keep working: default contexts still build, still verify,
    # and can still load an explicit CA file (botocore passes certifi's bundle).
    ctx = ssl.create_default_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    import certifi

    ctx.load_verify_locations(cafile=certifi.where())
