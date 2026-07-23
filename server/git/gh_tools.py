"""Tool schemas for the GitHub hybrid tools (verb plane + raw API plane).

These are deferred tools (activated via tool_search), so their descriptions
carry the usage contract the model needs on activation.
"""

GITHUB_TOOLS = [
    {
        "name": "github",
        "description": (
            "[GitHub] Run a GitHub CLI (gh) subcommand using the server's authenticated gh. "
            'Pass argv WITHOUT the leading \'gh\', e.g. ["pr","list","--state","open"] or '
            '["pr","close","2"]. Covers pr, issue, run, checks, release, repo, workflow, label, '
            "gist, project, and search. For raw REST/GraphQL, use github_api / github_api_write "
            "instead. Reads run immediately; mutations show the exact command for approval and are "
            "verified afterward. argv only — no shell syntax, pipes, or redirects. auth/config/"
            "secret/extension/alias are blocked. Deleting or transferring a repo, or changing its "
            "visibility, is NEVER permitted (the user must do it on GitHub). Never tell the user a "
            "PR/issue is closed, merged, or open unless a tool result this turn confirmed that state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "gh argv without the leading 'gh'.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Optional, clamped 5-120. Default 30.",
                },
            },
            "required": ["args"],
        },
    },
    {
        "name": "github_api",
        "description": (
            "[GitHub] Read any GitHub REST or GraphQL resource via the server's authenticated gh "
            "(GET/HEAD only; mutations must use github_api_write). Endpoint is a path like "
            '"repos/{owner}/{repo}/pulls" or "user"; {owner}/{repo} resolve to the connected '
            "workspace repo. For GraphQL, pass a single read query in graphql_query. Credential/"
            "secret/key endpoints are blocked. Runs immediately (read-only)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "endpoint": {
                    "type": "string",
                    "description": 'REST path (e.g. "repos/{owner}/{repo}/issues") or "graphql".',
                },
                "method": {
                    "type": "string",
                    "enum": ["GET", "HEAD"],
                    "description": "Optional; defaults to GET.",
                },
                "fields": {
                    "type": "object",
                    "description": "Optional query parameters, passed as -f key=value.",
                },
                "graphql_query": {
                    "type": "string",
                    "description": "A single GraphQL read query (must begin with 'query' or '{').",
                },
                "timeout_seconds": {"type": "integer", "description": "Optional, clamped 5-120."},
            },
            "required": ["endpoint"],
        },
    },
    {
        "name": "github_api_write",
        "description": (
            "[GitHub] Mutate a GitHub resource via the server's authenticated gh REST/GraphQL API. "
            "Use for POST/PATCH/PUT/DELETE when no gh verb (the github tool) fits. Endpoint is a "
            "path; {owner}/{repo} resolve to the workspace repo. Pass the JSON request body in "
            "body. Shows the request for approval, then verifies the result (re-reads the resource; "
            "DELETE expects a 404). Credential/secret/key endpoints are blocked, and repo delete/"
            "transfer/visibility plus adding/removing collaborators or teams are NEVER permitted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["POST", "PATCH", "PUT", "DELETE"],
                    "description": "HTTP method for the mutation.",
                },
                "endpoint": {"type": "string", "description": "REST path or 'graphql'."},
                "body": {"type": "object", "description": "JSON request body."},
                "fields": {"type": "object", "description": "Optional -f fields."},
                "graphql_mutation": {"type": "string", "description": "A single GraphQL mutation."},
                "graphql_vars": {
                    "type": "object",
                    "description": "Variables for the GraphQL mutation.",
                },
            },
            "required": ["endpoint"],
        },
    },
]


def get_github_tools() -> list[dict]:
    return GITHUB_TOOLS
