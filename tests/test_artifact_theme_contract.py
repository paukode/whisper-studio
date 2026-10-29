"""The theme variables the model is told about are the ones the card adds.

The create_artifact tool tells the model to style an HTML page with --ws-*
CSS variables; src/utils/artifactTheme.ts is what adds them to the page. A
name in one list and not the other is a page that silently renders in the
browser's default colors, so the two lists must match exactly.
"""

import re
from pathlib import Path

from server.ask_user import CREATE_ARTIFACT_TOOL
from server.prompt_tools.create_program import BODY

ROOT = Path(__file__).resolve().parent.parent
_VAR_RE = re.compile(r"--ws-[a-z0-9-]*[a-z0-9]")


def _told_to_model() -> set[str]:
    return set(
        _VAR_RE.findall(CREATE_ARTIFACT_TOOL["input_schema"]["properties"]["html"]["description"])
    )


def _added_by_card() -> set[str]:
    source = (ROOT / "src/utils/artifactTheme.ts").read_text()
    table = source[source.index("export const ARTIFACT_VARS") :]
    table = table[: table.index("];")]
    return set(re.findall(r"\['(--ws-[a-z0-9-]+)'", table))


def test_the_tool_and_the_card_name_the_same_variables():
    told, added = _told_to_model(), _added_by_card()
    assert added, "no variables parsed from artifactTheme.ts"
    assert told == added, f"only in the tool: {told - added}; only in the card: {added - told}"


def test_a_single_page_takes_its_colors_and_fonts_from_the_theme():
    # The single-page rules point at the theme. The old house style (own
    # fonts, a dark page) survives only for folders, where no card adds it.
    body = " ".join(BODY.split())
    single = body[body.index("## Step 2A") : body.index("## Step 2B")]
    modular = body[body.index("## Step 2B") :]
    assert "--ws-" in single
    assert "Google Fonts" not in single
    assert "Dark background" not in single
    assert "No card adds the theme to a folder" in modular
