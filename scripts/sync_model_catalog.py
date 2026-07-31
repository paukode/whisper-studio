"""Add-only sync of new template models into an existing user config.

setup.sh runs this on every invocation (all modes, with or without --new),
so a model added to config.example.json reaches machines whose gitignored
config.json predates it — and the registry-driven download loop in the same
setup run then pulls its weights. Without this, an existing config.json is
honored untouched and template-only models silently never appear (the
config's ``chat_models`` replaces the defaults wholesale by design).

Semantics, deliberately narrow:
  - ADD-ONLY: a template key missing from the user file is appended with the
    template's full entry. Keys the user already has are never modified or
    removed, so local edits (ctx overrides, relabels) survive.
  - A key removed from the user file but still present in the template comes
    back on the next run. To drop a model permanently, remove it from BOTH
    files.
  - Corrupt or missing files change nothing and exit 0 — setup must not die
    on a config problem the app itself would surface later.
  - A timestamped .bak is written only when a file actually changes.

Covers chat_models in config.json and the top-level model keys of
pricing.json (a new cloud model needs both to be priced correctly). Stdlib
only, so setup.sh can run it before the venv exists.
"""

import json
import os
import sys
import time


def _load(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_with_backup(path: str, data: dict) -> None:
    backup = f"{path}.bak.{time.strftime('%Y%m%d%H%M%S')}"
    try:
        os.replace(path, backup)
    except OSError:
        backup = ""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    if backup:
        print(f"  (previous file kept as {os.path.basename(backup)})")


def sync_chat_models(config_path: str, example_path: str) -> list[str]:
    """Append template-only chat_models entries to the user config.

    Returns the added keys; [] when nothing changed (including every error
    path — a broken file must never block setup).
    """
    cfg = _load(config_path)
    tpl = _load(example_path)
    if cfg is None or tpl is None:
        return []
    tpl_models = tpl.get("chat_models")
    cfg_models = cfg.get("chat_models")
    # A config without its own catalog falls back to the template at runtime
    # (load_config seeds from config.example.json), so there is nothing to sync.
    if not isinstance(tpl_models, dict) or not isinstance(cfg_models, dict) or not cfg_models:
        return []
    added = [k for k in tpl_models if k not in cfg_models]
    if not added:
        return []
    for k in added:
        cfg_models[k] = tpl_models[k]
    _write_with_backup(config_path, cfg)
    return added


def sync_pricing(pricing_path: str, example_path: str) -> list[str]:
    """Append template-only top-level pricing entries to the user pricing file."""
    cur = _load(pricing_path)
    tpl = _load(example_path)
    if cur is None or tpl is None or not cur:
        return []
    added = [k for k in tpl if k not in cur]
    if not added:
        return []
    for k in added:
        cur[k] = tpl[k]
    _write_with_backup(pricing_path, cur)
    return added


def main(root: str) -> int:
    models = sync_chat_models(
        os.path.join(root, "config.json"), os.path.join(root, "config.example.json")
    )
    if models:
        print(f"Synced {len(models)} new model(s) from config.example.json: {', '.join(models)}")
    prices = sync_pricing(
        os.path.join(root, "pricing.json"), os.path.join(root, "pricing.example.json")
    )
    if prices:
        print(f"Synced {len(prices)} new pricing entr(y/ies): {', '.join(prices)}")
    if not models and not prices:
        print("Model catalog already in sync with the templates.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
