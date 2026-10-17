"""Per-project instructions, kept under ``~/.whisper/rules/<project-slug>/``.

The workspace itself carries no instructions file for this app any more. A
project's ``WHISPER.md`` (its overview and conventions) and its ``rules.md``
(rules that apply only there) live next to each other in the user's own
folder, so nothing is committed into the repo and the assistant can edit them
without an approval (every write under ``~/.whisper`` is silent). The folder
is named with the memory store's slug, ``<basename>-<8 char hash>``, so a
project's instructions, rules and memories share a name.

``AGENTS.md`` (the ecosystem-standard instructions filename other coding tools
maintain) is still honored live, read-only, from the workspace root: a repo
that keeps one works here without an import. The project ``WHISPER.md`` wins
on conflicts, and an ``AGENTS.md`` whose text is already inside it (the
post-import state) is not loaded twice.

Every file is capped, so one long file cannot tax every turn of the session.
"""

from __future__ import annotations

import logging
import os

from server.prompts.rules import (
    PROFILE_FILENAME,
    project_profile_path,
    project_rules_dir,
    project_slug,
    read_capped,
    rules_files,
)

log = logging.getLogger("whisper-studio")

FILENAME = PROFILE_FILENAME
COMPAT_FILENAME = "AGENTS.md"


def get_whisper_md_context(ws_path: str | None) -> str:
    """The project instructions block for one request, or '' with no workspace."""
    if not ws_path:
        return ""

    slug = project_slug(ws_path)
    parts: list[str] = []

    profile = read_capped(project_profile_path(ws_path), f"rules/{slug}/{FILENAME}")
    if profile:
        parts.append(f"[rules/{slug}/{FILENAME}, project instructions]\n{profile}")

    for path in rules_files(project_rules_dir(ws_path)):
        name = os.path.basename(path)
        text = read_capped(path, f"rules/{slug}/{name}")
        if text:
            parts.append(f"[rules/{slug}/{name}, project rules]\n{text}")

    compat = read_capped(os.path.join(ws_path, COMPAT_FILENAME), COMPAT_FILENAME)
    # Post-import state: the importer copied AGENTS.md into the project
    # WHISPER.md verbatim, so loading both would bill the same text twice.
    if compat and compat in profile:
        compat = ""
    if compat:
        if profile:
            parts.append(
                f"[{COMPAT_FILENAME} in the workspace, project instructions maintained for "
                f"other coding tools; where this conflicts with {FILENAME} above, "
                f"{FILENAME} wins]\n{compat}"
            )
        else:
            parts.append(f"[{COMPAT_FILENAME} in the workspace, project instructions]\n{compat}")

    if not parts:
        return ""
    return "\n\n" + "\n\n".join(parts)
