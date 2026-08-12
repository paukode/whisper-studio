"""Keep the original bytes of an uploaded document, not just its extracted text.

Extraction is lossy by design (a large spreadsheet becomes a header plus a
20-row sample), which is readable but not computable: asked for a total, a
model summing the sample returns a confident wrong number. The new column
records where the upload's bytes were kept so run_python can open the real
file. Migration 011's CREATE now includes the column directly, so this is a
no-op on fresh databases.
"""

import sqlite3

VERSION = 14
DESCRIPTION = "attachments: add source_path for the upload's original bytes"


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(attachments)")}
    if not cols:
        return  # table not created yet; 011 creates the new shape
    if "source_path" not in cols:
        conn.execute("ALTER TABLE attachments ADD COLUMN source_path TEXT NOT NULL DEFAULT ''")
