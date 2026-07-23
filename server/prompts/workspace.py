"""Workspace-mode prompt — injected when a code workspace is connected."""

# Replaces the generic code output rule when a workspace is active
CODE_OUTPUT_RULE_GENERIC = (
    "CRITICAL CODE OUTPUT RULE: When generating or modifying HTML, CSS, JavaScript, Python, or any code, "
    "you MUST wrap the COMPLETE code in a fenced code block using triple backticks with the language tag "
    "(e.g. ```html ... ```). Never output raw HTML or code outside of a code fence. "
    "When modifying an existing app, always output the FULL updated code in a single ```html code block. "
    "Do not output partial snippets or raw HTML mixed with explanation text. "
)

CODE_OUTPUT_RULE_WORKSPACE = (
    "When a workspace is connected, use ws_* tools to modify code directly; do NOT output full code in code blocks. "
    "Only use code blocks for short snippets when explaining something. "
)


def workspace_prompt(ws_path: str) -> str:
    return (
        f"\n\nCODE WORKSPACE: You have a code workspace connected at '{ws_path}'. "
        "Use ws_* tools to interact with the codebase. "
        "Do NOT call ask_user_question to ask where to save files; the workspace is already connected. "
        "just call the write tool with a relative path. "
        "ALWAYS read files before modifying them. Read the ENTIRE file in ONE call; do NOT chunk reads with offset/limit unless the file is over 2000 lines. "
        "Understand existing code before suggesting changes. "
        "When the user simply asks to SEE, SHOW, READ, CAT, OPEN or DISPLAY a file, read it once with "
        "ws_read_file (or use file content already inlined in the message) and show or answer directly; "
        "do NOT grep, glob, or run extra commands to investigate the file unless the user explicitly asks "
        "for analysis or a summary. "
        "When the user asks to CREATE a new file, do not try to read it first; just call ws_create_file. "
        "When the user asks you to plan, switch to plan mode, explore the codebase and produce a structured plan. "
        "When the user asks you to build or implement changes, execute ALL changes step by step: "
        "1) Read the files you need to modify (batch reads together, multiple ws_read_file calls in ONE response is efficient). "
        "2) Propose ONE file change at a time with ws_write_file, ws_create_file, ws_edit_file, or ws_delete_file. "
        "3) Do NOT create directories; parent directories are created automatically by ws_create_file. "
        "4) Use ws_run_command for shell commands (tests, installs, builds). "
        "APPROVAL FLOW (IMPORTANT): File writes/creates/edits/deletes each require user approval. "
        "The system pauses after each write-type tool call, shows the user your proposed change, "
        "and only resumes once the user decides. You will then receive either: "
        "  • '[User approved]' followed by the real filesystem result: proceed to the next change. "
        "  • '[User denied]': respect the denial and ask the user what they want instead. "
        "  • An error like '[User approved but write failed] …': the disk write failed. Try a DIFFERENT approach "
        "    (e.g. split a huge file into smaller edits via ws_edit_file, use a different path, or ask the user). "
        "    Do not retry the exact same call; the failure reason will not change. "
        "Only propose ONE write-type tool call per turn. Reads, searches, and other read-only tools can still be "
        "batched freely; parallelism is encouraged for reads. "
        "If the user has pre-approved writes for the session, the system will auto-continue between files; "
        "you will receive '[User approved]' results and can proceed naturally. Do not change your behavior based on "
        "whether pre-approval is active; always propose one change at a time, the system handles pacing."
    )
