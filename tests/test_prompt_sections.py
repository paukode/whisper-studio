"""System-prompt section invariants.

These guard the two failure modes that context engineering hits silently: the
prompt telling the model two contradictory things, and the prompt telling it
about a tool it cannot call. Both were live before this suite existed, and
neither shows up as a test failure or an exception anywhere else — the model
just behaves worse.
"""

import re

import pytest

from server.git.prompts import build_git_instructions_prompt
from server.prompts import build_system_prompt, build_system_prompt_split
from server.prompts.workspace import (
    CODE_OUTPUT_RULE_GENERIC,
    CODE_OUTPUT_RULE_WORKSPACE,
    workspace_prompt,
)

# ── The code-output rule is a branch, never both branches ─────────────────────


@pytest.mark.parametrize("ws_path", [None, "/tmp/ws"])
def test_exactly_one_code_output_rule(ws_path):
    """The generic rule ("print the complete file in a fence") and the workspace
    rule ("edit via ws_* tools, don't print files") are contradictory. Shipping
    both leaves the model to guess, so exactly one must be present.

    Regression: the rules were swapped with ``str.replace`` against a copy of the
    text inlined in BASE. The two copies drifted by one character (", do" vs
    ". Do"), the replace silently became a no-op, and every workspace session
    carried the generic rule while ALSO being told to use ws_* tools.
    """
    prompt = build_system_prompt(ws_path=ws_path, session_id="s")
    has_generic = CODE_OUTPUT_RULE_GENERIC.strip() in prompt
    has_workspace = CODE_OUTPUT_RULE_WORKSPACE.strip() in prompt

    assert has_generic != has_workspace, (
        f"expected exactly one code-output rule, got generic={has_generic} "
        f"workspace={has_workspace}"
    )
    assert has_workspace if ws_path else has_generic


def test_code_output_rule_text_is_not_duplicated_in_the_prompt():
    """Each rule appears once. A second copy anywhere means the text was inlined
    somewhere as well as registered, which is how the drift started."""
    prompt = build_system_prompt(ws_path="/tmp/ws", session_id="s")
    assert prompt.count(CODE_OUTPUT_RULE_WORKSPACE.strip()) == 1
    assert "always output the FULL updated code" not in prompt


# ── Static git instructions are cached; live git status is not ────────────────


def test_git_instructions_are_static_and_status_is_dynamic(tmp_path, monkeypatch):
    """~1.5K tokens of fixed git workflow text must sit in the cached prefix,
    while the branch/dirty/recent-commits block stays in the per-request tail.

    Regression: both were concatenated by one DYNAMIC-layer section, so the fixed
    text was re-billed as uncached input on every round of every turn.
    """
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    monkeypatch.setattr("server.git.core.find_git_root", lambda _p: str(repo))
    # Patch where the name is bound (server.prompts imported it at module load),
    # not where it is defined.
    monkeypatch.setattr(
        "server.prompts.build_git_status_prompt",
        lambda _p: "# Git Status\nCurrent branch: feat/x\nStatus: (clean)\n",
    )

    static, dynamic = build_system_prompt_split(ws_path=str(repo), session_id="s")

    assert "# Git Commit Instructions" in static
    assert "# Git Commit Instructions" not in dynamic
    assert "Current branch: feat/x" in dynamic
    assert "Current branch: feat/x" not in static


def test_git_sections_appear_and_disappear_together(monkeypatch):
    """Both sections share one gate, so a non-repo workspace gets neither."""
    monkeypatch.setattr("server.git.core.find_git_root", lambda _p: None)
    static, dynamic = build_system_prompt_split(ws_path="/tmp/not-a-repo", session_id="s")
    whole = static + dynamic
    assert "# Git Commit Instructions" not in whole
    assert "# Git Status" not in whole


# ── The prompt may only name tools that exist ────────────────────────────────


def _catalog_tool_names(monkeypatch, repo: str) -> set[str]:
    """Every tool name the model could be offered with a git workspace open."""
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: repo)
    monkeypatch.setattr("server.chat.tool_pool.get_workspace_path", lambda: repo)
    monkeypatch.setattr("server.workspace.tools.get_workspace_path", lambda: repo)

    from server.chat.tool_pool import assemble_full_catalog
    from server.skills import init_skills

    init_skills()
    return {t["name"] for t in assemble_full_catalog(ws_connected=True)}


def test_prompt_prose_only_references_real_tools(tmp_path, monkeypatch):
    """Every ``git_*`` / ``ws_*`` name the prompt instructs the model to call must
    be a real tool in the catalog.

    Regression: the git instructions spent ~400 tokens per turn telling the model
    to "Call the enter_worktree TOOL", which has an ApprovalSpec and a permissions
    entry but no tool schema and no route. The model could not call it, so the
    branch-swap protection it described never applied.
    """
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    available = _catalog_tool_names(monkeypatch, str(repo))

    prose = build_git_instructions_prompt() + workspace_prompt(str(repo))
    referenced = set(re.findall(r"\b(?:git|ws)_[a-z_]+\b", prose))

    missing = sorted(referenced - available)
    assert not missing, f"prompt references tools that are not in the catalog: {missing}"


def test_worktree_warning_lives_on_the_tools_that_need_it(tmp_path, monkeypatch):
    """The branch-swap warning belongs in the descriptions of the tools that
    rewrite the working tree, so it loads with them instead of on every turn.
    These tools are deferred (not in CORE_TOOLS), so the cost is zero until used.
    """
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: str(repo))
    monkeypatch.setattr("server.workspace.tools.get_workspace_path", lambda: str(repo))

    from server.git.tools import GIT_TOOLS

    by_name = {t["name"]: t["description"] for t in GIT_TOOLS}
    for name in ("git_checkout", "git_merge", "git_stash"):
        assert "chat session" in by_name[name], f"{name} lost its working-tree warning"

    from server.chat.tool_partition import core_names

    assert not ({"git_checkout", "git_merge", "git_stash"} & core_names())
