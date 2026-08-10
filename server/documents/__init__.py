"""Document-creation tools: create_docx, create_pptx, create_xlsx, create_pdf.

    tools      — tool schemas surfaced to the LLM (DOCUMENT_TOOLS)
    executors  — @register_executor implementations

Each tool writes into the connected workspace exactly like ws_create_file:
missing workspace -> the same [WS_WORKSPACE_PROMPT] sentinel, folder picker,
turn resumes. Unlike ws_create_file these write the resulting bytes straight
to disk (no [WS_APPROVAL] pause) since the model never sees or edits the
generated binary content directly.

server/main.py imports `server.documents.executors` at boot (in
_EXECUTOR_MODULES) so the @register_executor decorators fire; that import is
this package's only required side effect.
"""

from .tools import DOCUMENT_TOOLS  # noqa: F401
