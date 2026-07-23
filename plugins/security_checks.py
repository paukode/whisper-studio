"""
Security Checks Plugin — scans file writes for dangerous code patterns.

Intercepts ws_write_file, ws_edit_file, and ws_create_file tools.
Blocks writes that contain risky patterns and reports findings to the user.
"""

import re

__version__ = "1.0.0"
__description__ = "Scans file writes for dangerous code patterns and blocks them with warnings"

# Tools that write file content
WRITE_TOOLS = {"ws_write_file", "ws_edit_file", "ws_create_file"}

# Each pattern: (compiled regex, short description, severity)
# Severity: "high" = almost always dangerous, "medium" = context-dependent
_H = "high"
_M = "medium"

SECURITY_PATTERNS = [
    (re.compile(r"\beval\s*\("), "eval function — arbitrary code execution risk", _H),
    (re.compile(r"\bexec\s*\("), "exec function — arbitrary code execution risk", _H),
    (re.compile(r"\b__import__\s*\("), "Dynamic import — code injection risk", _H),
    (re.compile(r"\bos\.system\s*\("), "os.system call — shell injection risk", _H),
    (
        re.compile(r"subprocess\.\w+\([^)]*shell\s*=\s*True"),
        "subprocess with shell=True — injection risk",
        _M,
    ),
    (re.compile(r"pickle\.loads?\s*\("), "pickle deserialization — code execution risk", _H),
    (re.compile(r"yaml\.load\s*\([^)]*\)(?!.*Loader)"), "yaml.load without safe Loader", _M),
    (
        re.compile(
            r"(?i)(password|secret|api_key|api_secret|token|private_key)"
            r'\s*=\s*["\x27][A-Za-z0-9+/=_\-]{12,}'
        ),
        "Possible hardcoded secret or credential",
        _H,
    ),
    (re.compile(r"(?i)SELECT\s+.+FROM\s+.+%s"), "SQL string interpolation — injection risk", _M),
    (re.compile(r"(?i)SELECT\s+.+FROM\s+.+\.format\s*\("), "SQL .format injection risk", _M),
    (
        re.compile(r"\.innerHTML\s*[\+]?="),
        "innerHTML assignment — XSS risk, prefer textContent or DOM APIs",
        _M,
    ),
]


def _extract_content(tool_input: dict) -> str:
    """Extract writable content from tool input, checking common field names."""
    for key in ("content", "new_content", "new_string", "text"):
        val = tool_input.get(key)
        if val:
            return val
    return ""


def _scan_content(content: str) -> list[dict]:
    """Scan content for security patterns. Returns list of finding dicts."""
    findings = []
    for pattern, description, severity in SECURITY_PATTERNS:
        matches = pattern.findall(content)
        if matches:
            findings.append(
                {
                    "description": description,
                    "severity": severity,
                    "count": len(matches),
                }
            )
    return findings


async def check_security(tool_name: str, tool_input: dict) -> dict | None:
    """PreToolUse hook — block writes with dangerous patterns."""
    if tool_name not in WRITE_TOOLS:
        return None

    content = _extract_content(tool_input)
    if not content:
        return None

    findings = _scan_content(content)
    if not findings:
        return None

    path = tool_input.get("path", tool_input.get("file_path", "unknown"))
    high_count = sum(1 for f in findings if f["severity"] == _H)
    medium_count = len(findings) - high_count

    summary_parts = []
    if high_count:
        summary_parts.append(f"{high_count} high-severity")
    if medium_count:
        summary_parts.append(f"{medium_count} medium-severity")

    reason = (
        f"Blocked write to '{path}': {', '.join(summary_parts)} "
        f"security issue{'s' if len(findings) > 1 else ''} detected. "
        f"Rewrite the code to avoid these patterns, or ask the user "
        f"to disable the security-checks plugin."
    )

    return {
        "reason": reason,
        "findings": findings,
        "path": path,
        "tool_name": tool_name,
    }


def register(app, executor_registry):
    """Plugin entry point — register the security check hook."""
    from server.infrastructure.plugin_hooks import register_pre_tool_hook

    register_pre_tool_hook(check_security)
