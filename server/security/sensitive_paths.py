"""Single source of truth for sensitive-path denylists.

Two consumers with different shapes:
  - server/sandbox.py needs expanded literal filesystem paths for the
    OS sandbox profile (sandbox-exec / bwrap) — the real boundary.
  - server/security/command_validator.py needs regexes matched against
    command text — defense-in-depth on paths that read naturally in
    commands.

The shared core lives in HOME_PATHS / ABSOLUTE_PATHS so the two layers
cannot drift on it. Deliberate asymmetries stay per-consumer:
  - SANDBOX_ONLY_HOME_PATHS (browser profiles, keychains, shell
    histories, password managers) are too noisy as command-text regexes
    but cheap for the kernel to deny.
  - /etc/passwd is validator-only: sandbox-denying it breaks NSS (every
    user/group lookup reads it), but commands that mention it explicitly
    are still suspicious.
"""

import os
import re

# Home-relative paths denied by BOTH the OS sandbox and the command
# validator. No leading ~/ — builders add the prefix they need.
HOME_PATHS = [
    ".ssh",  # SSH keys
    ".aws",  # AWS credentials
    ".gnupg",  # GPG
    ".docker",  # Docker auth
    ".kube",  # Kubernetes
    ".config/gcloud",  # Google Cloud
    ".azure",  # Azure
    ".npmrc",  # npm tokens
    ".pypirc",  # PyPI tokens
    ".git-credentials",  # Git credentials
    ".config/git/credentials",
    ".netrc",
]

# Denied only by the OS sandbox (see module docstring).
SANDBOX_ONLY_HOME_PATHS = [
    # Browser profiles & cookies (session tokens)
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/Chromium",
    "Library/Application Support/Firefox",
    "Library/Application Support/BraveSoftware",
    "Library/Containers/com.apple.Safari",
    "Library/Cookies",
    ".mozilla",
    ".config/google-chrome",
    ".config/chromium",
    ".config/BraveSoftware",
    # macOS Keychains
    "Library/Keychains",
    # Shell history (frequently contains tokens/passwords)
    ".bash_history",
    ".zsh_history",
    ".local/share/fish/fish_history",
    # Password managers
    ".password-store",
    ".config/op",
    ".1password",
    # Other CLI credential stores
    ".config/gh/hosts.yml",
    ".cargo/credentials",
    ".cargo/credentials.toml",
    ".gem/credentials",
    ".terraform.d/credentials.tfrc.json",
    ".config/doctl/config.yaml",
    ".netlify",
    ".config/heroku",
]

# Absolute system paths denied by both layers.
ABSOLUTE_PATHS = [
    "/etc/shadow",
    "/etc/sudoers",
    "/etc/ssl/private",
]

# Checked only by the command validator (see module docstring).
VALIDATOR_ONLY_ABSOLUTE_PATHS = [
    "/etc/passwd",
]

# Validator-only raw regexes: catch relative references the path-derived
# patterns miss (e.g. ``cat .ssh/id_rsa`` from inside $HOME).
VALIDATOR_EXTRA_REGEXES = [
    r"\.ssh/id_",
    r"\.ssh/authorized_keys",
    r"\.ssh/known_hosts",
]


def expanded_sandbox_paths() -> list[str]:
    """Literal filesystem paths for the OS sandbox deny profile."""
    return [
        os.path.expanduser(f"~/{p}") for p in HOME_PATHS + SANDBOX_ONLY_HOME_PATHS
    ] + ABSOLUTE_PATHS


def validator_path_patterns() -> list[re.Pattern]:
    """Compiled regexes for command-text matching.

    Each home path matches both ``~/<p>`` and any absolute/relative
    ``/<p>`` reference; trailing ``\\b`` (or ``/``) keeps lookalikes such
    as ``~/.awsome`` from tripping the ``.aws`` rule.
    """
    patterns = [re.compile(rf"~/{re.escape(p)}\b|/{re.escape(p)}(?:/|\b)") for p in HOME_PATHS]
    patterns += [
        re.compile(re.escape(p) + r"\b") for p in ABSOLUTE_PATHS + VALIDATOR_ONLY_ABSOLUTE_PATHS
    ]
    patterns += [re.compile(p) for p in VALIDATOR_EXTRA_REGEXES]
    return patterns
