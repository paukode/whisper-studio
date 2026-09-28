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
    "sonnet": (_e(3.0, 15.0, 0.3, 3.75),),
    "sonnet5": (_e(3.0, 15.0, 0.3, 3.75), _e(2.0, 10.0, 0.2, 4.0)),
    "opus5.5": (_e(4.0, 20.0, 0.2, 8.0),),
    "opus5.0": (_e(5.0, 25.0, 0.5, 6.25), _e(5.0, 25.0, 0.5, 10.0)),
    "opus4.6": (_e(5.0, 25.0, 0.5, 6.25),),
    "opus4.7": (_e(5.0, 25.0, 0.5, 6.25), _e(5.0, 25.0, 0.5, 10.0)),
    "opus4.8": (_e(5.0, 25.0, 0.5, 6.25), _e(5.0, 25.0, 0.5, 10.0)),
    "fable5.1": (_e(10.0, 50.0, 1.0, 12.5), _e(10.0, 50.0, 0.25, 20.0)),
    "fable5.0": (_e(10.0, 50.0, 1.0, 12.5), _e(10.0, 50.0, 1.0, 20.0)),
    "gpt6-astra": (
        _e(10.0, 50.0, 1.0, 12.5, True),
        _e(11.0, 55.0, 1.1, 13.75, True, (_GPT_BREAK, 22.0, 82.5, 2.2, 27.5)),
    ),
    "gpt6-sol": (_e(2.2, 11.0, 0.22, 2.75, True, (_GPT_BREAK, 4.4, 16.5, 0.44, 5.5)),),
    "gpt6-luna": (_e(0.11, 0.55, 0.011, 0.1375, True, (_GPT_BREAK, 0.22, 0.825, 0.022, 0.275)),),
    "gpt5.6-sol": (
        _e(5.5, 33.0, 0.55, 6.88, True),
        _e(4.4, 22.0, 0.44, 5.5, True, (_GPT_BREAK, 8.8, 33.0, 0.88, 11.0)),
    ),
    "gpt5.6-terra": (
        _e(2.75, 16.5, 0.28, 3.44, True),
        _e(2.2, 13.2, 0.22, 2.75, True, (_GPT_BREAK, 4.4, 19.8, 0.44, 5.5)),
    ),
    "gpt5.6-luna": (
        _e(1.1, 6.6, 0.11, 1.38, True),
        _e(0.22, 1.32, 0.022, 0.275, True, (_GPT_BREAK, 0.44, 1.98, 0.044, 0.55)),
    ),
    "gpt5.5": (
        _e(5.5, 33.0, 0.55, 0.0, True),
        _e(5.5, 33.0, 0.55, 5.5, True, (_GPT_BREAK, 11.0, 49.5, 1.1, 11.0)),
    ),
    "gpt5.4": (
        _e(2.75, 16.5, 0.275, 0.0, True),
        _e(2.75, 16.5, 0.275, 2.75, True, (_GPT_BREAK, 5.5, 24.75, 0.55, 5.5)),
    ),
    "amazon.nova-2-sonic-v1:0:speech": (_e(3.0, 12.0, 0.0, 0.0),),
    "amazon.nova-2-sonic-v1:0:text": (_e(0.33, 2.75, 0.0, 0.0),),
    "cohere.embed-v4:0": (_e(0.12, 0.0, 0.0, 0.0),),
}


def is_shipped(key: str, entry: dict) -> bool:
    """True when ``entry`` (normalized like tracker._coerce_pricing_entry)
    equals a rate some release shipped for ``key``."""
    return entry in SHIPPED_RATES.get(key, ())
