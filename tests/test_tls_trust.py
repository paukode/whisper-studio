"""server.infrastructure.tls: truststore injection must not break the original
ssl.SSLContext. Regression for "Failed to start the turn: maximum recursion
depth exceeded" on every chat turn after v2.6.0-22 injected truststore: botocore
builds the original class, whose setters recursed through the rebound global."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from server.infrastructure import tls

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# A miniature of CPython's ssl.py shape: a C-like base whose descriptor does the
# real work, a Python class whose setter names ITSELF through a module global,
# and an injected subclass that rebinds that global.
class _Base:
    def __init__(self):
        self._raw = 0

    def _get(self):
        return self._raw

    def _set(self, value):
        self._raw = value

    options = property(_get, _set)


class _Original(_Base):
    @property
    def options(self):
        return super().options

    @options.setter
    def options(self, value):
        super(SSLContext, SSLContext).options.__set__(self, value)


SSLContext = _Original  # the module global the setter resolves at call time


class _Injected(_Original):
    pass


def test_rebinding_pins_the_setter_to_the_original_class(monkeypatch):
    monkeypatch.setitem(globals(), "SSLContext", _Injected)  # what inject_into_ssl does
    ctx = _Original()
    with pytest.raises(RecursionError):
        ctx.options = 4
    assert tls.rebind_original_sslcontext_setters(_Original) == 1
    ctx.options = 4
    assert ctx.options == 4
    # Idempotent: a second pass finds nothing left to rebind.
    assert tls.rebind_original_sslcontext_setters(_Original) == 0
    # The injected subclass keeps working too (its MRO now ends in a pinned setter).
    sub = _Injected()
    sub.options = 8
    assert sub.options == 8


def test_bedrock_client_survives_server_import(tmp_path):
    """The real thing, in a subprocess so the test process's ssl module stays
    untouched: import the app the way uvicorn does (which injects truststore),
    then build a Bedrock client exactly like every chat turn does."""
    env = dict(os.environ)
    env["WHISPER_USER_DIR"] = str(tmp_path / "user")
    env["WHISPER_HOME"] = str(tmp_path / "home")
    env["WHISPER_DATA_DIR"] = str(tmp_path / "data")
    env.pop("AWS_PROFILE", None)
    code = (
        "import ssl\n"
        "import server.main\n"
        "assert ssl.SSLContext.__module__ == 'truststore._api', 'truststore not injected'\n"
        "import boto3\n"
        "boto3.client('bedrock-runtime', region_name='us-east-1')\n"
        "probe = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)\n"
        "probe.options |= ssl.OP_NO_COMPRESSION\n"
        "print('BEDROCK_CLIENT_OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=240
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "BEDROCK_CLIENT_OK" in proc.stdout
    assert "RecursionError" not in proc.stderr
