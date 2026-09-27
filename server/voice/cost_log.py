"""Nova Sonic's own usage in the cost log.

Sonic's usageEvent carries the stream's running totals (details.total: input
and output, speech and text tokens), and every renewal opens a new stream
whose totals start again at zero. A meter per stream records what the stream
used since its last record, at each completion end and once more when the
stream closes, so every token lands in the log exactly once.

Speech and text tokens are billed at different rates, so they are two model
keys: ``<model id>:speech`` and ``<model id>:text``, each priced in
pricing.example.json at the AWS Price List rates. The delegated assistant's
own rounds are chat engine rounds with source ``voice`` and are logged by the
engine.
"""

from __future__ import annotations

import logging

log = logging.getLogger("whisper-studio")

# The protocol parser's usage fields (server.voice.protocol.OutputParser).
_FIELDS = ("input_speech", "input_text", "output_speech", "output_text")


def model_keys(model_id: str) -> dict[str, str]:
    return {kind: f"{model_id}:{kind}" for kind in ("speech", "text")}


class SonicUsageMeter:
    """One Sonic stream's usage: ``observe`` every usage event, ``flush`` to
    log what the stream used since the previous flush."""

    def __init__(self, *, model_id: str, session_id: str) -> None:
        self._keys = model_keys(model_id)
        self._session_id = session_id
        self._latest = dict.fromkeys(_FIELDS, 0)
        self._logged = dict.fromkeys(_FIELDS, 0)

    def observe(self, ev: dict) -> None:
        """A parsed usage event: the stream's totals so far."""
        for field in _FIELDS:
            try:
                value = int(ev.get(field) or 0)
            except (TypeError, ValueError):
                continue
            # Totals only grow; an out-of-order event never takes one back.
            self._latest[field] = max(self._latest[field], value)

    def flush(self) -> None:
        """Log the tokens used since the last flush: one row per key that
        used any. Never raises: a cost write never costs the conversation."""
        delta = {f: self._latest[f] - self._logged[f] for f in _FIELDS}
        if not any(v > 0 for v in delta.values()):
            return
        self._logged = dict(self._latest)
        try:
            from server.costs.calls import record_counts
            from server.costs.capture import counts

            for kind, key in self._keys.items():
                inp, out = delta[f"input_{kind}"], delta[f"output_{kind}"]
                if inp > 0 or out > 0:
                    record_counts(
                        counts(inp, out), model_key=key, source="voice", session_id=self._session_id
                    )
        except Exception as e:  # noqa: BLE001 - logged loudly, never raised
            log.error("Cost log: could not record Nova Sonic usage: %s", e)
