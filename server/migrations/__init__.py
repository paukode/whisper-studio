"""Database migration system for Whisper Studio.

Numbered migration files in server/migrations/ are applied in order on startup.
Each migration runs exactly once, tracked via the schema_version table.

Migration files must define:
    VERSION: int  — unique, monotonically increasing version number
    DESCRIPTION: str — human-readable description
    def migrate(conn: sqlite3.Connection) -> None
"""
