"""Sandboxed runner for `office_script` — model-written python-docx /
python-pptx / openpyxl code that produces one Office file.

Why this exists: create_docx/pptx/xlsx take a fixed schema (HTML, or
title+bullets, or rows of values), and a fixed schema can only express what
someone anticipated. Table cell shading, merged cells, shapes, images,
conditional number formats, and editing an EXISTING document are all outside
it — and the docx path is lossier still, since macOS textutil silently drops
cell shading on its way through HTML. Rather than grow the schemas one
formatting knob at a time, this hands the model the actual libraries.

The contract with the script is deliberately tiny: read INPUT_PATH (an
existing document, or None when creating from scratch), write OUTPUT_PATH.
Everything between is the model's business.

Trust model — the script is NOT trusted, and this module is not what
contains it:
- It runs only AFTER the user approves the code on an approval card, the
  same gate `run_python` uses. Nothing here executes at tool-call time.
- It runs under server/sandbox.py's OS sandbox (sandbox-exec / bwrap), which
  is the real boundary, with the standard sensitive-path deny-list.
- It writes into a throwaway work directory, never to the destination. The
  finished bytes come back here and the APPROVAL executor puts them in
  place, so a script cannot pick its own output location.
"""

import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

from server.documents.inspect import SUPPORTED_FORMATS, format_of
from server.sandbox import run_sandboxed

log = logging.getLogger("whisper-studio")

# Wall-clock budget. Building a 200-slide deck or restyling a 10k-row sheet
# with openpyxl is seconds of work; this is generous enough for that and
# short enough that a runaway loop still ends on its own. The user has
# already approved the code by the time it runs.
OFFICE_SCRIPT_TIMEOUT_S = 120

# The script's stdout is a debugging aid for the model (print() what you
# changed), not a data channel — capped like the other command executors.
MAX_STDOUT_CHARS = 20_000

# Ceiling on the produced document. Well past any real deliverable; guards
# the base64 approval payload and the staging area against a script that
# writes in a loop.
MAX_OUTPUT_BYTES = 50_000_000

# Bound the code itself, mirroring create_docx's html_content limit.
MAX_CODE_BYTES = 200_000

# Prepended to the model's code. Only job is to name the two paths the
# runner controls, so the script never hardcodes a location and never has to
# guess the input's extension.
_PREAMBLE = '''\
"""Injected by whisper-studio. INPUT_PATH / OUTPUT_PATH are set for you."""
import os as _os

INPUT_PATH = _os.environ.get("WS_OFFICE_INPUT") or None
OUTPUT_PATH = _os.environ["WS_OFFICE_OUTPUT"]
'''


def resolve_source_path(source_path: str) -> tuple[bool, str]:
    """Resolve `source_path` (the document to edit) to an absolute path.

    Mirrors resolve_write_destination's rules from the read side: an
    absolute/~ path is taken as given, a bare relative path resolves against
    a connected workspace, and with no workspace a relative path is an error
    rather than a guess. Returns (ok, path_or_error_message)."""
    from server.security.sensitive_paths import is_sensitive_path
    from server.workspace.state import _ws_validate_path, get_workspace_path

    raw = os.path.expanduser(str(source_path).strip())
    if not raw:
        return False, "Error: source_path is empty."
    if os.path.isabs(raw):
        full = os.path.realpath(raw)
    else:
        ws = get_workspace_path()
        if not ws:
            return False, (
                "Error: no workspace is connected, so source_path must be an absolute "
                "path (~ is allowed), e.g. '~/Documents/report.docx'."
            )
        full = os.path.join(ws, raw)
        if not _ws_validate_path(full, ws):
            return False, f"Error: source_path escapes the workspace: {source_path}"
        full = os.path.realpath(full)
    if is_sensitive_path(full):
        return False, f"Error: refusing to read a sensitive path: {source_path}"
    if not os.path.isfile(full):
        return False, f"Error: no such file: {source_path}"
    if not format_of(full):
        return False, (
            f"Error: source_path must be a .docx/.xlsx/.pptx file, got '{source_path}'. "
            "The pre-2007 .doc/.xls/.ppt formats cannot be opened by these libraries."
        )
    return True, full


def validate_script_request(code: str, out_format: str, source_path: str = "") -> str:
    """Everything checkable BEFORE the user is shown an approval card, so a
    malformed request is corrected by the model instead of by a click.
    Returns "" when the request is runnable, else the error to return."""
    if not code or not code.strip():
        return "Error: code is required and must be non-empty."
    encoded_len = len(code.encode("utf-8", errors="ignore"))
    if encoded_len > MAX_CODE_BYTES:
        return f"Error: code is too large ({encoded_len} bytes) — keep it under {MAX_CODE_BYTES}."
    if out_format not in SUPPORTED_FORMATS:
        return (
            f"Error: path must end in .docx, .xlsx, or .pptx (got '.{out_format}'). "
            "Use create_pdf for PDFs."
        )
    if source_path:
        ok, resolved = resolve_source_path(source_path)
        if not ok:
            return resolved
    return ""


def run_office_script(
    code: str,
    out_format: str,
    source_path: str = "",
) -> tuple[bool, str, bytes]:
    """Run approved code in a throwaway work dir under the OS sandbox.

    Returns (ok, message, data). On success `data` is the produced document
    and `message` is the script's stdout (or ""); on failure `data` is empty
    and `message` explains what went wrong, phrased for the model to fix and
    retry."""
    work_dir = tempfile.mkdtemp(prefix="whisper_office_")
    try:
        env = {"WS_OFFICE_OUTPUT": os.path.join(work_dir, f"output.{out_format}")}
        if source_path:
            ok, resolved = resolve_source_path(source_path)
            if not ok:
                return False, resolved, b""
            # Copy in rather than pointing the script at the original: an
            # edit script that opens and re-saves in place would otherwise
            # mutate the user's file before anyone approved anything.
            staged_input = os.path.join(work_dir, f"input.{format_of(resolved)}")
            try:
                shutil.copyfile(resolved, staged_input)
            except OSError as e:
                return False, f"Error: could not read {source_path}: {e}", b""
            env["WS_OFFICE_INPUT"] = staged_input

        script_path = os.path.join(work_dir, "script.py")
        with open(script_path, "w") as f:
            f.write(_PREAMBLE + "\n" + code)

        # sys.executable, not "python3": the Office libraries live in the
        # app's venv, and the system interpreter has none of them.
        try:
            result = run_sandboxed(
                f"{shlex.quote(sys.executable)} {shlex.quote(script_path)} < /dev/null",
                cwd=work_dir,
                timeout=OFFICE_SCRIPT_TIMEOUT_S,
                env_extra=env,
            )
        except subprocess.TimeoutExpired:
            return False, f"Error: the script timed out ({OFFICE_SCRIPT_TIMEOUT_S}s limit).", b""
        except Exception as e:
            return False, f"Error running the script: {e}", b""

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if result.returncode != 0:
            detail = stderr or stdout or "(no output)"
            if len(detail) > MAX_STDOUT_CHARS:
                detail = detail[:MAX_STDOUT_CHARS] + "\n... (truncated)"
            return False, f"The script failed (exit {result.returncode}):\n{detail}", b""

        out_path = env["WS_OFFICE_OUTPUT"]
        if not os.path.isfile(out_path):
            return (
                False,
                (
                    "The script ran without error but wrote nothing to OUTPUT_PATH. "
                    "Save the document to OUTPUT_PATH as the last step "
                    "(e.g. doc.save(OUTPUT_PATH))."
                ),
                b"",
            )
        size = os.path.getsize(out_path)
        if size == 0:
            return False, "The script produced an empty file at OUTPUT_PATH.", b""
        if size > MAX_OUTPUT_BYTES:
            return (
                False,
                (
                    f"The script produced a {size} byte file, over the "
                    f"{MAX_OUTPUT_BYTES} byte limit."
                ),
                b"",
            )
        with open(out_path, "rb") as f:
            data = f.read()

        message = stdout
        if stderr:
            # A successful run can still warn (deprecations, library notices);
            # keep them, since they often explain a surprising result.
            message = f"{message}\n{stderr}".strip()
        if len(message) > MAX_STDOUT_CHARS:
            message = message[:MAX_STDOUT_CHARS] + "\n... (truncated)"
        return True, message, data
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
