"""Bedrock tool-definition schemas for the 14 live-preview tools.

Split into read-only (no approval) and approval-gated groups — see
server/executors/preview.py for the executor registrations that back this
split, and server/approval/bootstrap.py for the ApprovalSpecs.
"""

_SESSION_NAME = {
    "type": "string",
    "description": "The preview session's name (from preview_start / preview_list).",
}

READ_ONLY_TOOLS: list[dict] = [
    {
        "name": "preview_list",
        "description": "List all running preview sessions (dev server + browser) with their status.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "preview_logs",
        "description": "Read the dev server's stdout/stderr for a preview session.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_name": _SESSION_NAME,
                "stream": {
                    "type": "string",
                    "enum": ["stdout", "stderr", "both"],
                    "description": "Which stream to read. Defaults to both.",
                },
                "tail_bytes": {
                    "type": "integer",
                    "description": "How many trailing bytes to return. Defaults to 8192.",
                },
            },
            "required": ["session_name"],
        },
    },
    {
        "name": "preview_screenshot",
        "description": "Capture a screenshot of the preview session's current page. Returns an actual image you can see, downscaled and compressed. Prefer preview_console_logs/preview_network/preview_snapshot for precise checks — use this for visual judgment.",
        "input_schema": {
            "type": "object",
            "properties": {"session_name": _SESSION_NAME},
            "required": ["session_name"],
        },
    },
    {
        "name": "preview_console_logs",
        "description": "Read the browser console output (console.log/warn/error) for a preview session.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_name": _SESSION_NAME,
                "level": {
                    "type": "string",
                    "description": "Filter to one console level (e.g. 'error', 'warning', 'log').",
                },
                "lines": {
                    "type": "integer",
                    "description": "Max trailing entries to return. Defaults to 100.",
                },
            },
            "required": ["session_name"],
        },
    },
    {
        "name": "preview_network",
        "description": "Read network requests made by the preview session's page (method, url, status).",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_name": _SESSION_NAME,
                "filter": {
                    "type": "string",
                    "enum": ["all", "failed"],
                    "description": "Show all requests, or only failed/4xx/5xx ones.",
                },
                "lines": {
                    "type": "integer",
                    "description": "Max trailing entries to return. Defaults to 100.",
                },
            },
            "required": ["session_name"],
        },
    },
    {
        "name": "preview_snapshot",
        "description": "Get the accessibility tree of the current page as text — roles, names, and structure. Usually more reliable than a screenshot for confirming layout/content.",
        "input_schema": {
            "type": "object",
            "properties": {"session_name": _SESSION_NAME},
            "required": ["session_name"],
        },
    },
    {
        "name": "preview_inspect",
        "description": "Inspect a single element by CSS selector: computed styles, bounding box, text content. Best tool for verifying exact colors, sizes, and spacing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_name": _SESSION_NAME,
                "selector": {
                    "type": "string",
                    "description": "CSS selector for the element to inspect.",
                },
                "styles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CSS properties to return. Defaults to a common set.",
                },
            },
            "required": ["session_name", "selector"],
        },
    },
]

APPROVAL_GATED_TOOLS: list[dict] = [
    {
        "name": "preview_start",
        "description": "Start a dev server for a preview session, by name (resolved against .whisper/launch.json) or with an ad-hoc command. Does not load a page yet — call preview_navigate next.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_name": _SESSION_NAME,
                "runtimeExecutable": {
                    "type": "string",
                    "description": "Command to run, e.g. 'npm'. Omit to resolve session_name against .whisper/launch.json instead.",
                },
                "runtimeArgs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Arguments for runtimeExecutable, e.g. ['run', 'dev'].",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory. Defaults to the connected workspace.",
                },
                "port": {
                    "type": "integer",
                    "description": "Port the server listens on, for informational display.",
                },
                "url": {
                    "type": "string",
                    "description": "Base URL to associate with this session, for informational display.",
                },
            },
            "required": ["session_name"],
        },
    },
    {
        "name": "preview_stop",
        "description": "Stop a preview session's dev server and close its browser.",
        "input_schema": {
            "type": "object",
            "properties": {"session_name": _SESSION_NAME},
            "required": ["session_name"],
        },
    },
    {
        "name": "preview_navigate",
        "description": "Navigate the preview session's browser to a URL. Only http:// and https:// are allowed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_name": _SESSION_NAME,
                "url": {"type": "string", "description": "URL to navigate to."},
            },
            "required": ["session_name", "url"],
        },
    },
    {
        "name": "preview_click",
        "description": "Click an element on the preview session's current page, by CSS selector.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_name": _SESSION_NAME,
                "selector": {
                    "type": "string",
                    "description": "CSS selector for the element to click.",
                },
                "doubleClick": {
                    "type": "boolean",
                    "description": "Perform a double-click instead of a single click.",
                },
            },
            "required": ["session_name", "selector"],
        },
    },
    {
        "name": "preview_fill",
        "description": "Fill an input/textarea on the preview session's current page, by CSS selector.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_name": _SESSION_NAME,
                "selector": {
                    "type": "string",
                    "description": "CSS selector for the input element.",
                },
                "value": {"type": "string", "description": "Value to fill."},
            },
            "required": ["session_name", "selector", "value"],
        },
    },
    {
        "name": "preview_eval",
        "description": "Execute JavaScript in the preview session's page context and return the result. Use for debugging/inspection only — prefer preview_click/preview_fill for interactions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_name": _SESSION_NAME,
                "expression": {
                    "type": "string",
                    "description": "JavaScript expression to evaluate.",
                },
            },
            "required": ["session_name", "expression"],
        },
    },
    {
        "name": "preview_resize",
        "description": "Resize the preview session's browser viewport, for responsive/dark-mode testing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_name": _SESSION_NAME,
                "preset": {
                    "type": "string",
                    "enum": ["mobile", "tablet", "desktop"],
                    "description": "Device preset. Overrides width/height if provided.",
                },
                "width": {"type": "integer"},
                "height": {"type": "integer"},
                "colorScheme": {
                    "type": "string",
                    "enum": ["light", "dark"],
                    "description": "Emulate prefers-color-scheme.",
                },
            },
            "required": ["session_name"],
        },
    },
]

PREVIEW_TOOLS: list[dict] = READ_ONLY_TOOLS + APPROVAL_GATED_TOOLS
PREVIEW_TOOL_NAMES: set[str] = {t["name"] for t in PREVIEW_TOOLS}
