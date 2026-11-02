"""git_add_commit blocks on real secret CONTENT, never on a file name.

Every fake secret here is built at runtime by string concatenation, so the
repository never holds a secret-shaped literal (and this file commits cleanly
through the same guard it tests).
"""

import random
import string
import subprocess

import pytest

from server.git import executor as gx
from server.git import security


def _run(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _rand(alphabet, n, seed):
    rng = random.Random(seed)
    return "".join(rng.choice(alphabet) for _ in range(n))


def _fake_aws_key():
    return "AK" + "IA" + _rand("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567", 16, 1)


def _fake_private_key():
    body = "\n".join(_rand(string.ascii_letters + string.digits + "+/", 64, i) for i in range(4))
    dashes = "-" * 5
    return f"{dashes}BEGIN RSA PRIVATE KEY{dashes}\n{body}\n{dashes}END RSA PRIVATE KEY{dashes}\n"


def _fake_password():
    return _rand(string.ascii_letters + string.digits, 24, 7)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _run(tmp_path, "init", "-q", "-b", "main")
    _run(tmp_path, "config", "user.email", "t@example.com")
    _run(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one\n")
    _run(tmp_path, "add", "a.txt")
    _run(tmp_path, "commit", "-q", "-m", "first")
    monkeypatch.setattr(gx, "get_workspace_path", lambda: str(tmp_path))
    return tmp_path


def _commits(repo):
    return _run(repo, "log", "--oneline").stdout.count("\n")


def _staged(repo):
    return _run(repo, "diff", "--cached", "--name-only").stdout


# --- File names never block ---


def test_files_named_secret_or_password_commit(repo):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_secret_reads.py").write_text("def test_x():\n    assert True\n")
    (repo / "password_policy.md").write_text("Use a long password.\n")
    (repo / "id_ed25519.pub").write_text("ssh-ed25519 public part only\n")

    ok, out = gx.do_git_add_commit(
        {"message": "m", "files": ["tests/test_secret_reads.py", "password_policy.md"]}
    )
    assert ok, out
    ok, out = gx.do_git_add_commit({"message": "m2", "all": True})
    assert ok, out
    assert "id_ed25519.pub" in _run(repo, "show", "--name-only", "HEAD").stdout
    assert _commits(repo) == 3


def test_an_env_file_with_placeholders_commits(repo):
    (repo / ".env").write_text("API_KEY=your-api-key-here\nPASSWORD=changeme\nDEBUG=1\n")

    ok, out = gx.do_git_add_commit({"message": "m", "all": True})

    assert ok, out
    assert ".env" in _run(repo, "show", "--name-only", "HEAD").stdout


def test_the_real_test_file_commits(repo):
    """The file that sat blocked for 23 hours commits through the guard."""
    from pathlib import Path

    source = Path(__file__).with_name("test_secret_reads.py").read_text()
    (repo / "test_secret_reads.py").write_text(source)

    ok, out = gx.do_git_add_commit({"message": "m", "files": ["test_secret_reads.py"]})

    assert ok, out


# --- Real secret content blocks, with file and line ---


def test_a_staged_aws_key_is_refused_with_file_and_line(repo):
    key = _fake_aws_key()
    (repo / "config.py").write_text("REGION = 'us-east-1'\n\nAWS_ID = '" + key + "'\n")

    ok, out = gx.do_git_add_commit({"message": "m", "files": ["config.py"]})

    assert not ok
    assert "config.py:3" in out
    assert "AWS" in out
    assert key not in out
    assert _staged(repo) == ""
    assert _commits(repo) == 1


def test_a_private_key_block_is_refused_with_all(repo):
    (repo / "a.txt").write_text("two\n")
    (repo / "deploy_note.txt").write_text("note\n" + _fake_private_key())

    ok, out = gx.do_git_add_commit({"message": "m", "all": True})

    assert not ok
    assert "deploy_note.txt:2" in out
    assert "Private key" in out
    assert _staged(repo) == ""
    assert _commits(repo) == 1


def test_an_env_file_with_a_real_value_is_refused(repo):
    value = _fake_password()
    (repo / ".env").write_text("DEBUG=1\nDB_PASSWORD=" + value + "\n")

    ok, out = gx.do_git_add_commit({"message": "m", "all": True})

    assert not ok
    assert ".env:2" in out
    assert value not in out


def test_an_ignored_env_file_does_not_block_all(repo):
    (repo / ".gitignore").write_text(".env\n")
    (repo / ".env").write_text("DB_PASSWORD=" + _fake_password() + "\n")

    ok, out = gx.do_git_add_commit({"message": "m", "all": True})

    assert ok, out
    assert ".env" not in _run(repo, "show", "--name-only", "HEAD").stdout


def test_a_refusal_keeps_what_the_user_already_staged(repo):
    (repo / "a.txt").write_text("two\n")
    _run(repo, "add", "a.txt")
    (repo / "creds.py").write_text("token = '" + _fake_aws_key() + "'\n")

    ok, _out = gx.do_git_add_commit({"message": "m", "files": ["creds.py"]})

    assert not ok
    assert _staged(repo).split() == ["a.txt"]


def test_a_secret_already_staged_is_refused_without_files(repo):
    (repo / "creds.py").write_text("x = 1\nkey = '" + _fake_aws_key() + "'\n")
    _run(repo, "add", "creds.py")

    ok, out = gx.do_git_add_commit({"message": "m"})

    assert not ok
    assert "creds.py:2" in out
    assert _commits(repo) == 1


def test_new_files_are_committed_with_all(repo):
    (repo / "b.txt").write_text("new\n")

    ok, out = gx.do_git_add_commit({"message": "m", "all": True})

    assert ok, out
    assert "b.txt" in _run(repo, "show", "--name-only", "HEAD").stdout


def test_a_commit_in_a_repo_with_no_commits_is_scanned(tmp_path, monkeypatch):
    _run(tmp_path, "init", "-q", "-b", "main")
    _run(tmp_path, "config", "user.email", "t@example.com")
    _run(tmp_path, "config", "user.name", "t")
    monkeypatch.setattr(gx, "get_workspace_path", lambda: str(tmp_path))
    (tmp_path / "k.txt").write_text(_fake_aws_key() + "\n")

    ok, out = gx.do_git_add_commit({"message": "m", "all": True})

    assert not ok
    assert "k.txt:1" in out


# --- The scanner on its own ---


def _scan(text):
    return security.scan_added_lines("f", list(enumerate(text.split("\n"), 1)))


@pytest.mark.parametrize(
    "text",
    [
        "AK" + "IAIOSFODNN7" + "EXAMPLE",
        "-" * 5 + "BEGIN RSA PRIVATE KEY" + "-" * 5,
        "password = os.environ['DB_PASSWORD']",
        "token = settings.api_token_value",
        "api_key = 'replace-with-your-key'",
        "secret = 'my_test_secret_value'",
        "PASSWORD=${DB_PASSWORD}",
        "url = 'postgres://user:changeme@localhost/db'",
        "Authorization: Bearer <token>",
        "def test_secret_file_is_refused(password_store):",
        "_TOKEN = 'pypi-AgEIcHlwaS5vcmc' + ('A9b8C7d6' * 6)",
    ],
)
def test_placeholders_names_and_code_never_match(text):
    assert _scan(text) == []


def test_generic_assignments_and_url_passwords_match():
    value = _fake_password()
    assert _scan("client_secret = '" + value + "'")[0].label == "Password or secret assignment"
    assert _scan("export API_TOKEN=" + value)[0].label == "Password or secret assignment"
    assert _scan("dsn = 'postgres://app:" + value + "@db/x'")[0].label == "Password in a URL"


def test_the_refusal_names_file_line_and_kind_never_the_value():
    key = _fake_aws_key()
    diff = (
        "diff --git a/s.py b/s.py\n--- a/s.py\n+++ b/s.py\n"
        "@@ -10,0 +11,2 @@\n+x = 1\n+k = '" + key + "'\n"
    )
    findings = security.scan_diff(diff)
    assert [(f.path, f.line) for f in findings] == [("s.py", 12)]
    text = security.format_refusal(findings)
    assert "s.py:12" in text
    assert key not in text
