"""Every rate entry a tagged release of pricing.example.json carried, per
key, plus the current template's.

Packaged installs used to seed ~/.whisper/pricing.json with a full copy of the
template, and pricing.json overrides the template per key. A seeded copy
therefore froze every rate at its install date and shadowed each later
correction. The loader (tracker._load_pricing) ignores an override entry that
equals one of these shipped entries: it is a stale copy, not a user decision,
so the current default applies. An entry that differs from all of them is a
genuine override and wins.

Only released rates belong here: a value that never shipped in a tag cannot
sit in a seeded copy, so listing it would only discard a deliberate override
that happens to equal it. When a rate in pricing.example.json changes, replace
the current entry if no release carried it yet, otherwise append the new one
(tests/test_pricing_overrides.py builds the expected set from the tags).
Only models still in the template are tracked: a model that leaves the
catalog leaves this history too.
"""

from __future__ import annotations


def _e(
    i: float, o: float, r: float, w: float, cached_in_input: bool = False, long_context=None
) -> dict:
    entry = {"input": i, "output": o, "cache_read": r, "cache_write": w}
    if cached_in_input:
        entry["cached_in_input"] = True
    if long_context:
        above, li, lo, lr, lw = long_context
        entry["long_context"] = {
            "above": above,
            "input": li,
            "output": lo,
            "cache_read": lr,
            "cache_write": lw,
        }
    return entry


# The GPT cards' long-context break: a prompt over 272K input tokens.
_GPT_BREAK = 272_000


SHIPPED_RATES: dict[str, tuple[dict, ...]] = {
    "haiku": (_e(1.0, 5.0, 0.1, 1.25), _e(1.0, 5.0, 0.1, 2.0)),
    "sonnet5.5": (_e(2.0, 10.0, 0.2, 4.0),),
    "opus5.5": (_e(4.0, 20.0, 0.2, 8.0),),
    "fable5.1": (_e(10.0, 50.0, 1.0, 12.5), _e(10.0, 50.0, 0.25, 20.0)),
    "gpt6.1-sol": (_e(2.2, 11.0, 0.11, 2.75, True, (_GPT_BREAK, 4.4, 16.5, 0.22, 5.5)),),
    "gpt6-astra": (
        _e(10.0, 50.0, 1.0, 12.5, True),
        _e(11.0, 55.0, 1.1, 13.75, True, (_GPT_BREAK, 22.0, 82.5, 2.2, 27.5)),
    ),
    "gpt6-luna": (_e(0.11, 0.55, 0.011, 0.1375, True, (_GPT_BREAK, 0.22, 0.825, 0.022, 0.275)),),
    "amazon.nova-2-sonic-v1:0:speech": (_e(3.0, 12.0, 0.0, 0.0),),
    "amazon.nova-2-sonic-v1:0:text": (_e(0.33, 2.75, 0.0, 0.0),),
    "cohere.embed-v4:0": (_e(0.12, 0.0, 0.0, 0.0),),
    "global.cohere.embed-v4:0": (_e(0.12, 0.0, 0.0, 0.0),),
}


def is_shipped(key: str, entry: dict) -> bool:
    """True when ``entry`` (normalized like tracker._coerce_pricing_entry)
    equals a rate some release shipped for ``key``."""
    return entry in SHIPPED_RATES.get(key, ())
