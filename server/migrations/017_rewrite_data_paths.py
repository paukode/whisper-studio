"""Rewrite absolute data paths recorded before user data moved to ~/.whisper.

Two columns persist absolute paths into the data directory: attachments'
``source_path`` (the uploaded original, re-read into prompts) and background
tasks' ``output_path`` (re-read for tails). The relocation to ~/.whisper moves
the files themselves at boot (paths.relocate_legacy_home, which runs before
this migration); this rewrites the recorded prefixes so the rows keep
resolving with no compatibility symlinks left in Application Support.

A dev checkout is a no-op (the old and new prefixes are the same directory),
and re-running is a no-op (the old prefix no longer matches anything).
"""

import os
import sqlite3

VERSION = 17
DESCRIPTION = "rewrite attachment/task paths for the ~/.whisper move"


def migrate(conn: sqlite3.Connection) -> None:
    from server.infrastructure.paths import app_home, data_root

    old_prefix = os.path.join(app_home(), "data") + os.sep
    new_prefix = data_root() + os.sep
    if old_prefix == new_prefix:
        return
    for table, column in (("attachments", "source_path"), ("tasks", "output_path")):
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            continue
        conn.execute(
            f"UPDATE {table} SET {column} = REPLACE({column}, ?, ?) WHERE {column} LIKE ? || '%'",
            (old_prefix, new_prefix, old_prefix),
        )
