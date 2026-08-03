"""Download worker: ``python -m server.models_manager.worker <key>``.

Runs ONE model download to completion in its own process by calling the same
ensure_* function the app uses lazily (see catalog.py), so the on-disk result
is identical — including GLiNER's backbone-tokenizer pre-cache. Being a
separate process is the whole point: the manager cancels a download by
terminating it, which no thread-based approach can do against a blocking
huggingface_hub call.

Spawned with the server's environment (WHISPER_HOME et al.), cwd at the repo
root, and output redirected to a per-key log the manager tails on failure.
"""

from __future__ import annotations

import importlib
import sys


def main(key: str) -> int:
    from server.models_manager.catalog import get_entry

    entry = get_entry(key)
    if entry is None:
        print(f"unknown model key: {key}", file=sys.stderr)
        return 2
    module = importlib.import_module(entry.ensure_module)
    ensure = getattr(module, entry.ensure_func)
    if entry.ensure_arg is not None:
        ensure(entry.ensure_arg)
    else:
        ensure()
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m server.models_manager.worker <key>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
