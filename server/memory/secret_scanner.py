"""Secret scanner — high-confidence regex rules to detect secrets in memory content.

Derived from gitleaks (MIT license). Only includes patterns with near-zero
false-positive rates (distinctive prefixes, checksums, known formats).
"""

import logging
import re

log = logging.getLogger("whisper-studio")

# (rule_id, human_label, regex_pattern)
_RULE_DEFS: list[tuple[str, str, str]] = [
    # Cloud providers
    (
        "aws-access-token",
        "AWS Access Token",
        r"\b((?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16})\b",
    ),
    ("gcp-api-key", "GCP API Key", r"\b(AIza[\w\-]{35})\b"),
    (
        "azure-client-secret",
        "Azure Client Secret",
        r"\b([a-zA-Z0-9~_.\-]{3}8Q~[a-zA-Z0-9~_.\-]{34})\b",
    ),
    ("digitalocean-pat", "DigitalOcean PAT", r"\b(dop_v1_[a-f0-9]{64})\b"),
    # AI APIs
    ("anthropic-api-key", "Anthropic API Key", r"\b(sk\-ant\-[a-zA-Z0-9_\-]{80,})\b"),
    ("openai-api-key", "OpenAI API Key", r"\b(sk\-[a-zA-Z0-9]{20}T3BlbkFJ[a-zA-Z0-9]{20})\b"),
    # Modern OpenAI keys: project / service-account / admin prefixes (kept
    # alongside the legacy rule above so both fire on their respective shapes).
    (
        "openai-api-key-modern",
        "OpenAI API Key",
        r"\b(sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{40,}T3BlbkFJ[A-Za-z0-9_-]{20,}"
        r"|sk-[a-zA-Z0-9]{20}T3BlbkFJ[a-zA-Z0-9]{20})\b",
    ),
    ("huggingface-token", "HuggingFace Token", r"\b(hf_[a-zA-Z]{34})\b"),
    # Version control
    ("github-pat", "GitHub PAT", r"\b(ghp_[0-9a-zA-Z]{36})\b"),
    ("github-fine-grained-pat", "GitHub Fine-Grained PAT", r"\b(github_pat_\w{82})\b"),
    ("github-oauth-token", "GitHub OAuth Token", r"\b(gho_[0-9a-zA-Z]{36})\b"),
    ("github-app-token", "GitHub App Token", r"\b(ghu_[0-9a-zA-Z]{36})\b"),
    ("github-server-token", "GitHub Server Token", r"\b(ghs_[0-9a-zA-Z]{36})\b"),
    ("github-refresh-token", "GitHub Refresh Token", r"\b(ghr_[0-9a-zA-Z]{36})\b"),
    ("bitbucket-app-password", "Bitbucket App Password", r"\b(ATBB[a-zA-Z0-9]{32})\b"),
    # Communication
    ("slack-bot-token", "Slack Bot Token", r"\b(xoxb\-[0-9]{10,13}\-[0-9]{10,13}[a-zA-Z0-9\-]*)\b"),
    (
        "slack-user-token",
        "Slack User Token",
        r"\b(xoxp\-[0-9]{10,13}\-[0-9]{10,13}[a-zA-Z0-9\-]*)\b",
    ),
    ("slack-app-token", "Slack App Token", r"\b(xapp\-\d\-[A-Z0-9]{10,}\-\d{13}\-[a-f0-9]{64})\b"),
    (
        "slack-webhook",
        "Slack Webhook",
        r"(https://hooks\.slack\.com/services/T[a-zA-Z0-9_]{8,}/B[a-zA-Z0-9_]{8,}/[a-zA-Z0-9_]{24,})",
    ),
    (
        "discord-webhook",
        "Discord Webhook",
        r"(https://discord(?:app)?\.com/api/webhooks/\d+/[\w\-]+)",
    ),
    # Dev tooling
    ("npm-token", "NPM Token", r"\b(npm_[a-zA-Z0-9]{36})\b"),
    ("pypi-token", "PyPI Token", r"\b(pypi\-[a-zA-Z0-9]{100,})\b"),
    # Real PyPI tokens are base64url (contain _ and -) after the fixed
    # "pypi-AgEIcHlwaS5vcmc" prefix, which the alphanumeric-only rule above
    # misses. Match the distinctive prefix directly.
    ("pypi-token-modern", "PyPI Token", r"(pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{50,})"),
    ("sendgrid-api-key", "SendGrid API Key", r"\b(SG\.[a-zA-Z0-9_\-]{22}\.[a-zA-Z0-9_\-]{43})\b"),
    ("twilio-api-key", "Twilio API Key", r"\b(SK[a-f0-9]{32})\b"),
    ("databricks-pat", "Databricks PAT", r"\b(dapi[a-f0-9]{32})\b"),
    ("hashicorp-tf-token", "Terraform Token", r"\b([\w\-]+\.atlasv1\.[a-zA-Z0-9_\-]{60,})\b"),
    ("postman-api-key", "Postman API Key", r"\b(PMAK\-[a-f0-9]{24}\-[a-f0-9]{34})\b"),
    # Observability
    ("grafana-token", "Grafana Token", r"\b(glc_[a-zA-Z0-9\+/]{32,}={0,2})\b"),
    ("sentry-auth-token", "Sentry Auth Token", r"\b(sntrys_[a-zA-Z0-9]{56,})\b"),
    # Payment
    ("stripe-secret-key", "Stripe Secret Key", r"\b(sk_live_[a-zA-Z0-9]{24,})\b"),
    ("stripe-restricted-key", "Stripe Restricted Key", r"\b(rk_live_[a-zA-Z0-9]{24,})\b"),
    ("shopify-access-token", "Shopify Access Token", r"\b(shpat_[a-f0-9]{32})\b"),
    # Crypto / private keys
    (
        "private-key-header",
        "Private Key",
        r"(-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----)",
    ),
    (
        "jwt-secret",
        "JWT Token",
        r"\b(eyJ[a-zA-Z0-9_\-]{10,}\.eyJ[a-zA-Z0-9_\-]{10,}\.[a-zA-Z0-9_\-]{10,})\b",
    ),
    # Generic high-entropy (only with known prefixes)
    ("generic-api-key-bearer", "Bearer Token", r"(?i)(?:bearer\s+)([a-zA-Z0-9_\-]{40,})"),
]

# Lazy-compiled patterns
_compiled_rules: list[tuple[str, str, re.Pattern]] | None = None


def _get_rules() -> list[tuple[str, str, re.Pattern]]:
    """Compile regex patterns on first use."""
    global _compiled_rules
    if _compiled_rules is None:
        _compiled_rules = []
        for rule_id, label, pattern in _RULE_DEFS:
            try:
                _compiled_rules.append((rule_id, label, re.compile(pattern)))
            except re.error as e:
                log.warning("Failed to compile secret rule %s: %s", rule_id, e)
    return _compiled_rules


def scan_for_secrets(content: str) -> list[dict]:
    """Scan content for secrets. Returns list of {rule, label, line} dicts.

    Deduplicates by rule_id (one match per fired rule).
    """
    rules = _get_rules()
    findings = []
    seen_rules: set[str] = set()

    for line_num, line in enumerate(content.split("\n"), 1):
        for rule_id, label, pattern in rules:
            if rule_id in seen_rules:
                continue
            if pattern.search(line):
                findings.append({"rule": rule_id, "label": label, "line": line_num})
                seen_rules.add(rule_id)

    return findings


def redact_secrets(content: str) -> str:
    """Replace detected secrets with [REDACTED] markers."""
    rules = _get_rules()
    result = content
    for rule_id, _label, pattern in rules:
        result = pattern.sub(f"[REDACTED:{rule_id}]", result)
    return result


def check_and_block(content: str) -> tuple[bool, str, list[dict]]:
    """Check content for secrets. Returns (is_clean, redacted_content, findings).

    Always runs regardless of feature flags — secret scanning is a safety guard.
    """
    findings = scan_for_secrets(content)
    if not findings:
        return True, content, []
    return False, redact_secrets(content), findings
