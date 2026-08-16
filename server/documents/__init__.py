"""Document tools: create_docx/pptx/xlsx/pdf, office_script, inspect_document.

    tools      — tool schemas surfaced to the LLM (DOCUMENT_TOOLS)
    executors  — @register_executor implementations
    standards  — the house layout the create_* tools apply
    inspect    — structural read-out of an existing Office file
    script     — sandboxed runner for office_script's model-written code
    approval   — the office_script approval (run the code, place the file)

The create_* tools take a fixed schema and build the file at tool-call time;
office_script hands the model python-docx/python-pptx/openpyxl directly, for
the formatting those schemas cannot express and for editing files that
already exist. Both end at a human: every one of them returns a
[WS_APPROVAL] payload and nothing reaches the destination until the user
clicks Yes. Destination resolution is shared with ws_create_file, so a
missing workspace behaves identically across all of them.

server/main.py imports `server.documents.executors` at boot (in
_EXECUTOR_MODULES) so the @register_executor decorators fire; that import is
this package's only required side effect.
"""

from .tools import DOCUMENT_TOOLS  # noqa: F401
