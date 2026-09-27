"""Process-wide boto3 clients that never pin a missing credential.

botocore resolves credentials once, when a client is created. A client built
before the user has any AWS credentials (a fresh Mac, before ``aws configure``
or ``aws sso login``) therefore holds none for its whole life, and every call
on it raises ``NoCredentialsError`` even after the credentials exist. The
cached getters (the chat client, memory recall, the data-retention control
plane, Cohere embeddings) all build through ``cached_client`` here: a client
is cached only when it was built WITH credentials, so the next call after the
user adds them builds one that works. A client built without them still fails
at signing, before it opens a socket, so skipping the cache for it does not
bring back the file-descriptor exhaustion the caches exist to prevent.

Each build uses a FRESH botocore session, which re-reads the environment and
``~/.aws/config`` / ``~/.aws/credentials``; the process-wide default session
caches its parsed profile config, so an SSO profile added after launch would
stay invisible to it.
"""

from __future__ import annotations

import threading

import boto3
import botocore.session

# Bedrock API keys (AWS_BEARER_TOKEN_BEDROCK) authenticate bedrock and
# bedrock-runtime without SigV4 credentials; their signing name is "bedrock".
_BEARER_SIGNING_NAME = "bedrock"


def _fresh_session() -> botocore.session.Session:
    return botocore.session.get_session()


def resolve_credentials():
    """SigV4 credentials from botocore's real provider chain (env, shared
    files, SSO, credential_process, container, instance metadata), read fresh.
    Returns None when nothing resolves. Raises what botocore raises for a
    broken configuration (for example a malformed profile)."""
    return _fresh_session().get_credentials()


def bedrock_api_key_present() -> bool:
    """Whether a Bedrock API key (``AWS_BEARER_TOKEN_BEDROCK``) is configured."""
    return _fresh_session().get_auth_token(signing_name=_BEARER_SIGNING_NAME) is not None


def _authenticated(core: botocore.session.Session) -> bool:
    if core.get_credentials() is not None:
        return True
    return core.get_auth_token(signing_name=_BEARER_SIGNING_NAME) is not None


def cached_client(cache: dict, lock: threading.Lock, key, service: str, **client_kwargs):
    """The client cached under ``key``, building it on a miss. The build is
    cached only when credentials resolved; otherwise the uncached client is
    returned and the next call resolves again."""
    with lock:
        client = cache.get(key)
        if client is not None:
            return client
        core = _fresh_session()
        client = boto3.session.Session(botocore_session=core).client(service, **client_kwargs)
        if _authenticated(core):
            cache[key] = client
        return client
