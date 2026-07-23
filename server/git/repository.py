"""
Repository detection — parse git remotes, detect GitHub repos.

Handles SSH, HTTPS, SSH URL, and git protocol formats.
Validates hostnames to reject SSH aliases.
"""

import re
from dataclasses import dataclass

from server.git.core import get_remote_url


@dataclass
class ParsedRepository:
    host: str  # "github.com"
    owner: str  # "paukode"
    name: str  # "whisper"


# --- URL parsing patterns ---

# SSH: git@github.com:owner/repo.git
_SSH_PATTERN = re.compile(r"^[\w.-]+@([\w.-]+):([^/\s]+)/([^/\s]+?)(?:\.git)?$")

# HTTPS: https://github.com/owner/repo.git
_HTTPS_PATTERN = re.compile(r"^https?://([\w.-]+)/([^/\s]+)/([^/\s]+?)(?:\.git)?$")

# SSH URL: ssh://git@github.com/owner/repo
_SSH_URL_PATTERN = re.compile(r"^ssh://[\w.-]+@([\w.-]+)/([^/\s]+)/([^/\s]+?)(?:\.git)?$")

# Git protocol: git://github.com/owner/repo
_GIT_PATTERN = re.compile(r"^git://([\w.-]+)/([^/\s]+)/([^/\s]+?)(?:\.git)?$")


def parse_git_remote(url: str) -> ParsedRepository | None:
    """Parse any git remote URL format into host/owner/name.

    Supports SSH, HTTPS, SSH URL, and git protocol formats.
    Returns None if URL cannot be parsed or hostname looks invalid.
    """
    trimmed = url.strip()
    if not trimmed:
        return None

    # Try each pattern in order
    for pattern in (_SSH_PATTERN, _HTTPS_PATTERN, _SSH_URL_PATTERN, _GIT_PATTERN):
        match = pattern.match(trimmed)
        if match:
            host = match.group(1)
            owner = match.group(2)
            name = match.group(3)
            if not looks_like_real_hostname(host):
                return None
            return ParsedRepository(host=host, owner=owner, name=name)

    return None


def detect_current_repository(path: str) -> str | None:
    """Get 'owner/repo' string for github.com repos only.

    Returns None for non-github.com hosts or if remote URL can't be parsed.
    """
    result = detect_current_repository_with_host(path)
    if result and result.host == "github.com":
        return f"{result.owner}/{result.name}"
    return None


def detect_current_repository_with_host(path: str) -> ParsedRepository | None:
    """Get parsed repository info for any host (including GHE).

    Returns None if remote URL can't be determined or parsed.
    """
    remote_url = get_remote_url(path)
    if not remote_url:
        return None
    return parse_git_remote(remote_url)


def looks_like_real_hostname(host: str) -> bool:
    """Validate that a hostname looks real (has a TLD with alphabetic chars).

    Rejects SSH aliases like 'github.com-work' or bare hostnames
    without dots that aren't localhost.
    """
    if not host:
        return False

    # localhost is always valid
    if host == "localhost":
        return True

    # Must contain at least one dot
    if "." not in host:
        return False

    # Last segment (TLD) must be purely alphabetic
    parts = host.split(".")
    tld = parts[-1]
    if not tld.isalpha():
        return False

    return True
