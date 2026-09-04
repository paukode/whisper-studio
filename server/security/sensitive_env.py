"""Single source of truth for credential-shaped environment variables.

The env-var sibling of sensitive_paths.py. Two consumers with
deliberately different scopes:
  - server/mcp.py scrubs CREDENTIAL_ENV_VARS — the full list — from stdio
    MCP child environments. A third-party MCP server has no legitimate
    claim on the host app's credentials; explicit per-server ``env``
    entries in mcp_servers.json are applied AFTER the scrub, so a user
    who deliberately configured a credential for one server still
    forwards it.
  - server/sandbox.py scrubs only GITHUB_ENV_VARS from sandboxed shell
    children. GitHub auth for the git/github tools is file/keychain-based
    (~/.config/gh/hosts.yml, itself sandbox-denied), so nothing sandboxed
    legitimately needs the tokens — but the cloud keys must keep passing,
    because sandboxed code authenticates to AWS/Bedrock via inherited env
    creds (executors/code.py re-allows ~/.aws for the file-based route
    for the same reason).
"""

from collections.abc import Mapping

# GitHub tokens — scrubbed from BOTH sandboxed shell children and MCP
# stdio children (see module docstring for why the sandbox stops here).
GITHUB_ENV_VARS = frozenset({"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"})

# Cloud/API credentials the server process commonly holds (Bedrock auth,
# web search, provider SDKs) — scrubbed from MCP stdio children only.
CLOUD_ENV_VARS = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "TAVILY_API_KEY",
    }
)

CREDENTIAL_ENV_VARS = GITHUB_ENV_VARS | CLOUD_ENV_VARS


def scrub_credential_env(environ: Mapping[str, str]) -> dict[str, str]:
    """``environ`` minus every CREDENTIAL_ENV_VARS entry."""
    return {k: v for k, v in environ.items() if k not in CREDENTIAL_ENV_VARS}
