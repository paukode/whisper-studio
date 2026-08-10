"""Workspace-mode prompt — injected when a code workspace is connected."""

# The two branches of the ``code_output_rule`` prompt section. Exactly one is
# emitted per request, chosen on whether a workspace is connected (see
# ``server.prompts._build_code_output_rule``). They are mutually exclusive: the
# generic rule asks for complete fenced code in the reply because there is
# nowhere to write it, and the workspace rule asks for ws_* edits instead.
CODE_OUTPUT_RULE_GENERIC = (
    "\n\nCRITICAL CODE OUTPUT RULE: When generating or modifying HTML, CSS, JavaScript, Python, or any code, "
    "you MUST wrap the COMPLETE code in a fenced code block using triple backticks with the language tag "
    "(e.g. ```html ... ```). Never output raw HTML or code outside of a code fence. "
    "When modifying an existing app, always output the FULL updated code in a single ```html code block. "
    "Do not output partial snippets or raw HTML mixed with explanation text."
)

CODE_OUTPUT_RULE_WORKSPACE = (
    "\n\nCODE OUTPUT RULE: A workspace is connected, so modify code with the ws_* tools rather than printing it. "
    "Do NOT output full files in code blocks. Use code blocks only for short snippets when explaining something."
)


def workspace_prompt(ws_path: str, session_id: str = "default") -> str:
    """Product context plus the write-approval contract.

    Deliberately short. What the ws_* tools do, when to read a whole file, and
    that parent directories are created for you all live in the tool
    descriptions, which load with the tools instead of on every turn. What
    survives here is the part no tool description can express on its own: the
    turn is PAUSED mid-flight by the harness, and the model has to recognise the
    resume markers and the one-write-per-turn pacing that follows from them.
    """
    from server.workspace.paths import get_scratch_dir

    scratch_dir = get_scratch_dir(session_id)
    return (
        f"\n\nCODE WORKSPACE: A code workspace is connected at '{ws_path}'. "
        "Edit it with the ws_* tools rather than asking the user where to save."
        "\n\nWRITE APPROVAL: Each write, create, edit, and delete pauses the turn. The user sees "
        "your proposed change and decides, then the turn resumes with one of:\n"
        "  • '[User approved]' plus the real filesystem result: continue to the next change.\n"
        "  • '[User denied]': respect it and ask what they want instead.\n"
        "  • '[User approved but write failed] …': the disk write failed, so try a different "
        "approach (smaller ws_edit_file edits, a different path, or ask). Repeating the identical "
        "call will fail identically.\n"
        "Because of that pause, propose only ONE write-type call per turn. Reads and searches have "
        "no such limit and are worth batching. Session pre-approval only removes the human click, "
        "so keep the same one-change-per-turn pacing either way."
        f"\n\nSCRATCH FILES: Use '{scratch_dir}' — not the connected workspace root — for anything "
        "intermediate or throwaway: temp scripts, probe files, format-conversion intermediates. It's "
        "there via ws_run_command like any other path. Always delete what you create there, and "
        "anything throwaway you create in the workspace itself, once you're done with it."
        "\n\nCLEANUP: Before you consider any task finished, check whether it left behind a "
        "temporary or throwaway file — a test file, an intermediate conversion file, a "
        "capability-probe file, anything you created just to get the work done rather than as "
        "part of the deliverable — and delete it. This applies generally, to every task, not just "
        "ones a specific skill calls out."
    )
