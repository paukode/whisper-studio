"""Download worker: ``python -m server.models_manager.worker <key>``.

Runs ONE model download to completion in its own process by calling the same
ensure_* function the app uses lazily (see catalog.py), so the on-disk result
is identical — including GLiNER's backbone-tokenizer pre-cache. Being a
separate process is the whole point: the manager cancels a download by
terminating it, which no thread-based approach can do against a blocking
huggingface_hub call.

Spawned with the server's environment (WHISPER_HOME et al.), cwd at the repo
root, and output redirected to a per-key log the manager tails on failure.

On ANY failure the worker prints a full traceback (for the log) and then a
single ``DOWNLOAD FAILED: <short reason>`` line as its LAST act, before exiting
non-zero. The manager reads that last line as the error it shows the user, so a
dead download becomes an actionable message instead of a stuck percent.
"""

from __future__ import annotations

import errno
import importlib
import sys
import traceback


def _short_reason(exc: BaseException) -> str:
    """Map a download failure to a one-line reason a user can act on.

    Categorises the common huggingface_hub / OS failures (repo or file missing,
    gated repo, auth, network, disk full) and falls back to the exception type
    and message. Best-effort: the hf exception imports are optional, so a broken
    or absent huggingface_hub never turns a real failure into a crash here.
    """
    # Disk full is an OSError regardless of where it came from.
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return "not enough disk space to store the model"

    try:
        from huggingface_hub.utils import (
            EntryNotFoundError,
            GatedRepoError,
            HfHubHTTPError,
            LocalEntryNotFoundError,
            RepositoryNotFoundError,
        )
    except Exception:  # pragma: no cover - hf missing/renamed; use the fallback
        RepositoryNotFoundError = EntryNotFoundError = GatedRepoError = ()  # type: ignore
        HfHubHTTPError = LocalEntryNotFoundError = ()  # type: ignore

    if RepositoryNotFoundError and isinstance(exc, RepositoryNotFoundError):
        return "repository not found on Hugging Face (check repo_id, or that it is public)"
    if GatedRepoError and isinstance(exc, GatedRepoError):
        return "this repository is gated; accept its license and set an HF token"
    if EntryNotFoundError and isinstance(exc, EntryNotFoundError):
        return "file not found in the repository (check the filename)"
    if LocalEntryNotFoundError and isinstance(exc, LocalEntryNotFoundError):
        return "could not reach Hugging Face and no local copy is cached (network error?)"
    if HfHubHTTPError and isinstance(exc, HfHubHTTPError):
        return f"Hugging Face request failed: {exc}"

    # Network-ish failures from the underlying requests stack.
    name = type(exc).__name__
    if name in {"ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout"}:
        return "network error reaching Hugging Face"

    reason = str(exc).strip().splitlines()[0] if str(exc).strip() else name
    return f"{name}: {reason}" if str(exc).strip() else name


def main(key: str) -> int:
    from server.models_manager.catalog import get_entry

    entry = get_entry(key)
    if entry is None:
        # No traceback to add — this is the whole story.
        print(f"DOWNLOAD FAILED: unknown model key {key!r}", file=sys.stderr)
        return 2
    try:
        module = importlib.import_module(entry.ensure_module)
        ensure = getattr(module, entry.ensure_func)
        if entry.ensure_arg is not None:
            ensure(entry.ensure_arg)
        else:
            ensure()
    except BaseException as exc:  # noqa: BLE001 - report EVERYTHING, then exit non-zero
        # Full traceback first (debug context in the log), reason last (the line
        # the manager surfaces). KeyboardInterrupt/SystemExit included on purpose:
        # a cancel terminates the process, but any other abrupt end must still
        # leave a reason rather than a silent stuck download.
        traceback.print_exc()
        print(f"DOWNLOAD FAILED: {_short_reason(exc)}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m server.models_manager.worker <key>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
