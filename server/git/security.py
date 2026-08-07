"""Secret file detection for git operations.

Used by the git executor to warn before staging or committing files that
look like credentials.
"""

import fnmatch
import os

SECRET_PATTERNS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "credentials.json",
    "service-account*.json",
    "*secret*",
    "*password*",
    "*.keystore",
    "id_rsa",
    "id_ed25519",
    "*.pub",
]


def contains_secret_files(file_list: list[str]) -> list[str]:
    """Check a list of file paths for potential secret files.

    Returns list of matched secret file paths (empty if none found).
    """
    secrets = []
    for filepath in file_list:
        basename = os.path.basename(filepath)
        for pattern in SECRET_PATTERNS:
            if fnmatch.fnmatch(basename, pattern):
                secrets.append(filepath)
                break
    return secrets
