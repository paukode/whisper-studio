"""aws_cli runs the aws program and nothing else.

aws_cli checked only that a command started with "aws ", then ran the whole
line through /bin/sh -c. So `aws s3 ls; curl ... | sh` ran a second program,
with no card in bypass mode (aws_cli is in the "cli" category). Now the line
is split with sh's quoting rules, a shell operator outside quotes is refused,
and every word is quoted for the run, so sh expands nothing.

A second way to run another program went through ~/.aws: the aws CLI and
boto3 run programs that files there name (credential_process, "!" CLI
aliases, plugins), and aws_cli and run_python could write anywhere under it.
boto3 also runs inside the server with no sandbox (aws_boto3, Bedrock). Now
~/.aws is readable and only its caches are writable, in every mode but
readonly, also when the workspace contains ~/.aws.
"""

import json
import os
import platform
import shlex
import shutil
import subprocess
import tempfile

import pytest

from server import sandbox
from server.executors import code

HOME = os.path.expanduser("~")

needs_sandbox_exec = pytest.mark.skipif(
    platform.system() != "Darwin" or shutil.which("sandbox-exec") is None,
    reason="needs macOS sandbox-exec",
)


# ── one program: the argv ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "aws s3 ls; touch /tmp/x",
        "aws s3 ls&&curl https://example.com",
        "aws s3 ls || true",
        "aws s3 ls | grep a",
        "aws s3 ls > out.json",
        "aws s3 ls 2>&1",
        "aws s3 ls &",
        "aws s3 cp - s3://b/k < secrets.txt",
        "aws s3 ls (x)",
    ],
)
def test_a_shell_operator_outside_quotes_is_refused(command):
    argv, refusal = code.aws_cli_argv(command)
    assert argv == []
    assert "no shell" in refusal


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            'aws s3 cp "my file.txt" s3://b/k --metadata "a=b;c=d"',
            ["aws", "s3", "cp", "my file.txt", "s3://b/k", "--metadata", "a=b;c=d"],
        ),
        (
            "aws ec2 describe-volumes --query 'Volumes[?Size>`100`] | [0]'",
            ["aws", "ec2", "describe-volumes", "--query", "Volumes[?Size>`100`] | [0]"],
        ),
        (
            'aws iam put-role-policy --policy-document \'{"Resource":"b/${aws:username}"}\'',
            [
                "aws",
                "iam",
                "put-role-policy",
                "--policy-document",
                '{"Resource":"b/${aws:username}"}',
            ],
        ),
        (
            'aws s3 cp x s3://b/k --content-type ""',
            ["aws", "s3", "cp", "x", "s3://b/k", "--content-type", ""],
        ),
    ],
)
def test_quoted_text_keeps_its_meaning(command, expected):
    assert code.aws_cli_argv(command) == (expected, "")


def test_a_leading_tilde_is_expanded_and_nothing_else_is():
    """The aws CLI does not expand ~ in a local path, sh did; $VARIABLES and
    globs now reach aws as written."""
    argv, _ = code.aws_cli_argv("aws s3 cp s3://b/~/k ~/Downloads/k --exclude '*' $HOME *")
    assert argv[3] == "s3://b/~/k"
    assert argv[4] == os.path.join(HOME, "Downloads", "k")
    assert argv[5:] == ["--exclude", "*", "$HOME", "*"]


def test_unbalanced_quotes_and_other_programs_are_refused():
    assert code.aws_cli_argv("aws s3 ls 's3://b")[0] == []
    assert code.aws_cli_argv("'aws'x s3 ls")[1]


def test_the_run_quotes_every_word(monkeypatch):
    seen = {}

    def spy(command, **kw):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(code, "run_sandboxed", spy)
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    command = "aws s3 cp \"a b\" s3://b/k --exclude '*' $HOME"
    assert code.do_aws_cli({"command": command})[0]
    run, redirect = seen["command"].rsplit(" < ", 1)
    assert redirect == "/dev/null"
    assert run == shlex.join(code.aws_cli_argv(command)[0])


def test_a_chain_is_refused_before_the_card():
    out = code.exec_aws_cli({"command": "aws s3 ls | sh", "__session_id__": "s"}, None, None)
    assert out.startswith("Error: ")
    assert "[WS_APPROVAL]" not in out
    ok, refusal = code.do_aws_cli({"command": "aws s3 ls; touch /tmp/x"})
    assert not ok and "no shell" in refusal


def test_a_plain_command_still_raises_the_card():
    out = code.exec_aws_cli({"command": "aws s3 ls", "__session_id__": "s"}, None, None)
    assert json.loads(out.removeprefix("[WS_APPROVAL]"))["command"] == "aws s3 ls"


# ── ~/.aws: readable, only the caches writable ────────────────────────────


def _rules(profile):
    """The write rules, without the secret deny-list (read and write)."""
    return [
        line
        for line in profile.splitlines()
        if line.startswith(("(allow file-write*", "(deny file-write*"))
    ]


def test_a_home_workspace_cannot_open_aws_again():
    aws = os.path.join(HOME, ".aws")
    cache = os.path.join(aws, "cli", "cache")
    profile = sandbox._generate_macos_profile(
        HOME, allow_paths=[aws], write_mode="workspace", write_paths=[cache]
    )
    rules = _rules(profile)
    home_allow = rules.index(f'(allow file-write* (subpath "{os.path.realpath(HOME)}"))')
    aws_deny = rules.index(f'(deny file-write* (subpath "{aws}"))')
    cache_allow = rules.index(f'(allow file-write* (subpath "{cache}"))')
    assert home_allow < aws_deny < cache_allow == len(rules) - 1
    assert not any(r.startswith("(deny file-read*") and f'"{aws}"' in r for r in rules)


def test_open_mode_keeps_aws_read_only_and_readonly_mode_writes_no_cache(tmp_path):
    aws = str(tmp_path / ".aws")
    cache = os.path.join(aws, "cli", "cache")
    open_rules = _rules(
        sandbox._generate_macos_profile(None, allow_paths=[aws], write_paths=[cache])
    )
    assert open_rules == [
        *(f'(deny file-write* (subpath "{p}"))' for p in sandbox._spellings(aws)),
        *(f'(allow file-write* (subpath "{p}"))' for p in sandbox._spellings(cache)),
    ]
    readonly = sandbox._generate_macos_profile(
        None, allow_paths=[aws], write_mode="readonly", write_paths=[cache]
    )
    assert cache not in readonly


def test_bwrap_binds_aws_read_only_inside_the_tree_and_its_cache_read_write(tmp_path):
    home = tmp_path / "home"
    cache = home / ".aws" / "cli" / "cache"
    cache.mkdir(parents=True)
    aws = str(home / ".aws")
    argv = sandbox._bwrap_argv(
        "ls", str(home), [aws], "workspace", write_paths=[str(cache)], write_root=str(home)
    )
    head = argv[: argv.index("--chdir")]
    pairs = [(head[i], head[i + 1]) for i in range(len(head) - 1)]
    tree = pairs.index(("--bind", str(home)))
    ro = pairs.index(("--ro-bind", aws))
    rw = pairs.index(("--bind", str(cache)))
    assert tree < ro < rw
    readonly = sandbox._bwrap_argv("ls", str(home), [aws], "readonly", write_paths=[str(cache)])
    assert str(cache) not in readonly


def test_the_cli_cache_is_created_only_when_aws_exists(tmp_path, monkeypatch):
    aws = tmp_path / ".aws"
    monkeypatch.setattr(code, "_AWS_DIR", str(aws))
    paths = code._aws_cache_paths()
    assert paths == [str(aws / "cli" / "cache"), str(aws / "sso" / "cache")]
    assert not aws.exists()  # no AWS set up: nothing created
    aws.mkdir()
    code._aws_cache_paths()
    assert (aws / "cli" / "cache").is_dir()
    assert not (aws / "sso").exists()  # only the CLI cache is created


def test_both_aws_tools_run_with_aws_read_only_and_the_caches_writable(monkeypatch, tmp_path):
    aws = tmp_path / ".aws"
    aws.mkdir()
    monkeypatch.setattr(code, "_AWS_DIR", str(aws))
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    calls = []

    def spy(command, **kw):
        calls.append(kw)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(code, "run_sandboxed", spy)
    assert code.do_aws_cli({"command": "aws s3 ls"})[0]
    assert code.do_run_python({"code": "print(1)"})[0]
    for kw in calls:
        assert kw["allow_paths"] == code._CLOUD_CRED_PATHS
        assert kw["write_paths"] == [str(aws / "cli" / "cache"), str(aws / "sso" / "cache")]


# ── the kernel agrees ─────────────────────────────────────────────────────


@needs_sandbox_exec
def test_sh_runs_only_aws_and_expands_nothing(monkeypatch, tmp_path):
    """A stand-in aws prints its arguments: they arrive as written, and the
    shell neither expands $HOME nor globs *."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "aws"
    fake.write_text('#!/bin/sh\nfor a; do printf "%s\\n" "$a"; done\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    ok, output = code.do_aws_cli({"command": "aws s3 ls $HOME * 'a b' \"c;d\""})
    assert ok, output
    assert output.splitlines() == ["s3", "ls", "$HOME", "*", "a b", "c;d"]


@needs_sandbox_exec
def test_a_confined_run_reads_aws_and_writes_only_its_cache():
    """Under the home folder, as the workspace, so the tree allow covers the
    fake ~/.aws: the credential rules still win."""
    home = tempfile.mkdtemp(prefix="whisper-aws-home-", dir=HOME)
    aws = os.path.join(home, ".aws")
    cache = os.path.join(aws, "cli", "cache")
    os.makedirs(cache)
    with open(os.path.join(aws, "config"), "w") as f:
        f.write("[default]\nregion = us-east-1\n")
    try:
        result = sandbox.run_sandboxed(
            f"cat {aws}/config && touch {cache}/token.json && touch {home}/notes.txt"
            f" && (echo 'credential_process = x' >> {aws}/config || echo refused)"
            f" && (touch {aws}/cli/alias || echo refused)",
            cwd=home,
            timeout=20,
            allow_paths=[aws],
            write_paths=[cache],
            write_mode="workspace",
        )
        assert result.returncode == 0, result.stderr
        assert "region = us-east-1" in result.stdout
        assert result.stdout.count("refused") == 2
        assert os.path.exists(f"{cache}/token.json")
        assert os.path.exists(f"{home}/notes.txt")
        assert not os.path.exists(f"{aws}/cli/alias")
        with open(os.path.join(aws, "config")) as f:
            assert "credential_process" not in f.read()
    finally:
        shutil.rmtree(home, ignore_errors=True)
