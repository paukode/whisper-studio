"""Answers stay in the chat unless the user asks for a file.

Real sessions: "summarize" over a meeting came back as a saved .txt, then as a
.docx in Documents, and "write me briefly the questions" as a .md in the
workspace. The prompt now says what the user asks for goes in the reply and a
file is written only when they ask for one, and every tool that saves a file
outside the workspace points the rest back to the reply.
"""

import re

# A writer ties the file to the user's request: "a file the user asked for",
# "only when the user asks for a file".
_ON_REQUEST = re.compile(r"file the user asked for|only when the user asks for a file")


# ── the prompt and the tools ───────────────────────────────────────────────


def test_the_prompt_keeps_answers_in_the_chat():
    from server.prompts.base import BASE

    assert "Answer in the chat" in BASE
    assert "Write a file only when the user asks for one" in BASE


def test_every_tool_that_saves_outside_the_workspace_waits_to_be_asked():
    # The rule itself lives once, in the prompt (the context report flags a
    # tool description that restates it); each writer makes a file only on
    # request and says that without one the content belongs in the reply.
    from server.chat.tool_pool import assemble_full_catalog

    tools = {t["name"]: t for t in assemble_full_catalog(ws_connected=False)}
    writers = [
        "save_file",
        "ws_create_file",
        "create_docx",
        "create_pptx",
        "create_xlsx",
        "create_pdf",
    ]
    for name in writers:
        text = tools[name]["description"]
        assert _ON_REQUEST.search(text) and "belongs in your reply" in text, name
    # No folder is suggested in place of the one the user named.
    assert "Downloads via" not in tools["save_file"]["description"]
