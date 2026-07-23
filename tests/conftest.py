"""Pytest configuration: makes the project root importable so test modules can
do `from server.security.command_validator import ...` without packaging.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def _drop_cached_bedrock_clients():
    """server.chat.infra caches one boto3 bedrock-runtime client per region for
    the whole process. A test whose code path reaches _get_bedrock_client()
    (e.g. POSTing /api/chat, whose endpoint grabs the client before the 409
    guard) leaves a REAL client in that cache; a later test that monkeypatches
    boto3.client never sees its fake because the cache short-circuits client
    creation, and with live AWS credentials the "unit" test silently calls the
    real endpoint. Drop the cache after every test so each builds clients under
    its own patches. Lazy sys.modules lookup: never import the server package
    for tests that don't touch it."""
    yield
    infra = sys.modules.get("server.chat.infra")
    if infra is not None:
        infra._reset_bedrock_client_cache()
