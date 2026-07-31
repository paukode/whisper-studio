"""Native tool schemas for the executor-backed core tools.

Pure data — no imports, no side effects (mirrors server/agent_tools/schemas.py).
The implementations live in sibling modules (code.py, web.py, terminal_run.py)
and are dispatched by name through the bare-executor fallthrough in
server/skills.execute_tool.

These tools used to be defined as executor-backed skill files under /skills/,
which made core product capabilities editable and deletable from the Skills
panel while the permission, sandbox, and approval layers referenced their
names in code. Their model-facing schemas now ship here, next to their
executors; /skills/ holds only user-authored prompt skills.
"""

AWS_BOTO3_TOOL = {
    "name": "aws_boto3",
    "description": (
        "Runs one read-only AWS API call via boto3 and returns the JSON response. Only "
        "client methods whose snake_case names start with list, get, describe, head, "
        "query, search, lookup, batch_get, scan, or select are allowed; anything else is "
        "rejected with an explanation. Use for every AWS read, including inventories, "
        "resource status, configuration, CloudWatch data, Bedrock model ids, and caller "
        "identity. Do not use for writes (create, update, delete, put, tag), for methods "
        "outside the allowlist such as check_*, simulate_*, or generate_presigned_url, "
        "or for high-level CLI features like s3 sync; route those to aws_cli, which asks "
        "the user for approval. Runs instantly with no approval prompt. Output has "
        "ResponseMetadata removed and is truncated at 100,000 characters. Region "
        "defaults to us-east-1."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "description": (
                    "AWS service name for boto3.client(), e.g. s3, ec2, bedrock, iam, "
                    "lambda, dynamodb, ecs, rds, cloudwatch, logs, sns, sqs, sts."
                ),
            },
            "method": {
                "type": "string",
                "description": (
                    "Read-only boto3 client method in snake_case. Must start with list, "
                    "get, describe, head, query, search, lookup, batch_get, scan, or "
                    "select (e.g. list_buckets, describe_instances, "
                    "list_foundation_models, get_caller_identity)."
                ),
            },
            "params": {
                "type": "object",
                "description": (
                    "Keyword arguments for the method, as a JSON object with exact "
                    "PascalCase boto3 parameter names. Example: {'Filters': [{'Name': "
                    "'instance-state-name', 'Values': ['running']}]} or {'Bucket': "
                    "'my-bucket', 'MaxKeys': 20}. Use MaxResults / NextToken to paginate."
                ),
            },
            "region": {
                "type": "string",
                "description": "AWS region. Optional; defaults to us-east-1.",
            },
        },
        "required": ["service", "method"],
    },
}

AWS_CLI_TOOL = {
    "name": "aws_cli",
    "description": (
        "Builds and runs AWS CLI commands for write and modify operations (create, "
        "delete, update, put, deploy, tag, terminate) and for operations aws_boto3 "
        "rejects, such as s3 sync or cp, presigned URLs, and check_* or simulate_* "
        "methods. The command runs in a sandbox with the user's AWS credentials and is "
        "shown on an approval card before it executes, so route mutations here rather "
        'than through terminal_run. It must start with "aws ", has a 30s default '
        "timeout, and output is truncated at 10,000 characters. Command substitution "
        "($() or backticks outside single quotes) is blocked; run a read via aws_boto3 "
        "first and substitute literal values, and wrap --query JMESPath filters in "
        "single quotes. When a mutating command is missing a required parameter, set "
        "mode to check_params to return a parameter checklist for the user instead of "
        "running. For ordinary reads (list, describe, get) use aws_boto3 instead; it is "
        "faster and needs no approval."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "Full AWS CLI command starting with 'aws ' (e.g. aws s3api "
                    "create-bucket --bucket my-bucket --region eu-west-1). No $() or "
                    "backtick substitution; single-quote any --query JMESPath. In "
                    "check_params mode this field instead carries the plain-text "
                    "parameter checklist to show the user."
                ),
            },
            "mode": {
                "type": "string",
                "description": (
                    "Closed set: 'execute' or empty requests user approval and runs the "
                    "command; 'check_params' skips approval and execution and returns "
                    "the checklist text in `command` so it can be presented to the user."
                ),
            },
        },
        "required": ["command"],
    },
}

RUN_PYTHON_TOOL = {
    "name": "run_python",
    "description": (
        "Executes a self-contained Python 3 script in an OS-sandboxed subprocess and "
        "returns combined stdout and stderr. Every call is approval-gated; the user "
        "must approve before the code runs. Use for calculations, data analysis, text "
        "processing, parsing, and quick prototyping when no shell is needed. Limits are "
        "a 15 second timeout, no stdin, cwd /tmp, and no state between calls; secret "
        "paths like ~/.ssh are blocked while ~/.aws is allowed, and AWS write "
        "operations are blocked in-process (only read-shaped boto3 calls pass). Results "
        "appear only if printed; bare expressions are not echoed. Do not use for shell "
        "commands (use terminal_run), for files in a connected workspace (use "
        "ws_run_command or ws_read_file), or for read-only AWS queries (prefer "
        "aws_boto3, which needs no approval)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": (
                    "Complete Python 3 script. Print anything that should be returned; "
                    "bare expressions are not echoed. No state persists between calls, "
                    "stdin is unavailable, 15 second limit, network access is allowed."
                ),
            },
        },
        "required": ["code"],
    },
}

TERMINAL_RUN_TOOL = {
    "name": "terminal_run",
    "description": (
        "Runs a one-shot, non-interactive shell command and returns the exit code plus "
        "combined stdout/stderr. Prefer this over asking the user to run something "
        "themselves; asking is friction, this is one approval click. mode=sandbox "
        "(default) uses a hidden ephemeral PTY with a minimal rc-free shell, sandboxed "
        "away from secrets; use it to check, try, or probe. Sandbox mode hides the "
        "output, it does not isolate the filesystem, so writes still land on the real "
        "machine. mode=visible types the command into the user's open terminal so they "
        "can watch, and errors if none is open; use it only when the user asks to see "
        "it run. Every call is approval-gated and validated first; dangerous commands "
        "and interactive ones that wait for stdin (vim, less, top, watch, ssh without "
        "BatchMode=yes, sudo without -n) are refused before running. Default timeout "
        "30s, max 300s; on timeout partial output is returned. Works without a "
        "connected workspace, but prefer ws_run_command for project commands when a "
        "workspace is connected. Not for long-running servers or watch loops; use "
        "preview_start for anything that serves a page."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "The shell command to execute, single line; compose with && ; or "
                    "pipes. Must be non-interactive."
                ),
            },
            "mode": {
                "type": "string",
                "description": (
                    "Either sandbox (default; hidden ephemeral PTY, invisible to the "
                    "user) or visible (types into the user's open terminal and errors "
                    "if none is open)."
                ),
            },
            "timeout": {
                "type": "number",
                "description": (
                    "Seconds before the command is killed. Default 30, max 300. Pass it "
                    "explicitly when the command is expected to be slow, such as a "
                    "large install."
                ),
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Working directory. Defaults to the workspace path if connected, "
                    "else $HOME. Supports ~ expansion; a nonexistent path falls back "
                    "silently."
                ),
            },
        },
        "required": ["command"],
    },
}

WEB_FETCH_TOOL = {
    "name": "web_fetch",
    "description": (
        "Fetches a single public web page over HTTP(S) and returns its text as a "
        "header (URL, HTTP status, title) plus the content converted from HTML to "
        "plain text with heading and list markers, truncated at 30,000 characters. Use "
        "when the user provides a URL or a specific known page must be read. Use "
        "web_search first when the page must be found. Limits are a 15 second timeout "
        "and no JavaScript rendering, so script-heavy pages may return little text; "
        "localhost, private, and cloud-metadata addresses are blocked; https is "
        "assumed when the scheme is missing; HTTP errors are reported in the Status "
        "line instead of failing. Not for local files (use ws_read_file) or "
        "interactive pages."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The full URL to fetch. https:// is assumed if no scheme is given.",
            },
        },
        "required": ["url"],
    },
}

WEB_SEARCH_TOOL = {
    "name": "web_search",
    "description": (
        "Searches the web via the Tavily API and returns up to 5 results, each with "
        "title, URL, and a short content snippet. Use for current events, prices, "
        "versions, docs, and any fact that may postdate training. Results are snippets "
        "only; follow up with web_fetch on the most promising URL when full page "
        "content is needed. Requires a Tavily API key (app Settings or the "
        "TAVILY_API_KEY environment variable); without one the tool explains the "
        "fallback options instead of returning results, so a non-result response means "
        "setup is needed, not that nothing was found. Do not use when the user already "
        "supplied the exact URL (use web_fetch) or for questions answerable from the "
        "conversation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query.",
            },
        },
        "required": ["query"],
    },
}

SUMMARIZE_TRANSCRIPT_TOOL = {
    "name": "summarize_transcript",
    "description": (
        "Summarizes the live or recorded session transcript captured by this app in "
        "one of four fixed styles. Takes no text input; the server supplies the "
        "current transcript automatically and returns it with formatting instructions "
        "for the chosen style, from which the assistant writes the summary. Use when "
        "the user wants quick action items, key points, a short brief, or basic "
        "meeting notes of what was said in the current session. Do not use for notes "
        "or text the user pasted into chat. Not for attached files (use "
        "analyze_document). Returns a no-transcript message when nothing has been "
        "transcribed yet."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "style": {
                "type": "string",
                "description": (
                    "One of action_items (tasks and commitments with owners when "
                    "mentioned), meeting_notes (attendees if identifiable, topics, "
                    "decisions, actions), key_points (bulleted takeaways), or brief "
                    "(2-3 sentences)."
                ),
                "enum": ["action_items", "meeting_notes", "key_points", "brief"],
            },
        },
        "required": ["style"],
    },
}

ANALYZE_DOCUMENT_TOOL = {
    "name": "analyze_document",
    "description": (
        "Retrieves the content of a file the user attached to the chat so the "
        "assistant can answer a question from it. Returns the whole document capped "
        "at 150,000 characters, or one section in full when section is set to a "
        "number from the attachment's outline. Use when an attached document's inline "
        "preview was truncated (the message shows an outline plus a first slice) or a "
        "specific section is needed in full. Do not use for small attachments already "
        "fully visible in the conversation, for images (already visible, analyze "
        "directly), for workspace files (use ws_read_file), or for the live session "
        "transcript (use summarize_transcript). Results over 50KB are truncated by "
        "the tool-result budget to a short head plus a cache-file reference, so "
        "prefer section fetches for large documents. An unmatched filename returns "
        "the list of available attachments."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": (
                    "Exact filename of the attachment as shown in the conversation; "
                    "matching is exact and case-sensitive, and a mismatch returns the "
                    "available filenames."
                ),
            },
            "question": {
                "type": "string",
                "description": (
                    "The analysis question or task. It is echoed back with the "
                    "content and does not affect what is retrieved."
                ),
            },
            "section": {
                "type": "string",
                "description": (
                    'Section number from the attachment outline, as a string like "3". '
                    "Returns that section in full. Only works for documents with "
                    "headings."
                ),
            },
        },
        "required": ["filename", "question"],
    },
}

# The always-on native pool contribution, added unconditionally by
# server/chat/tool_pool.assemble_full_catalog.
CORE_EXECUTOR_TOOLS = [
    ANALYZE_DOCUMENT_TOOL,
    AWS_BOTO3_TOOL,
    AWS_CLI_TOOL,
    RUN_PYTHON_TOOL,
    SUMMARIZE_TRANSCRIPT_TOOL,
    TERMINAL_RUN_TOOL,
    WEB_FETCH_TOOL,
    WEB_SEARCH_TOOL,
]
