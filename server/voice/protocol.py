"""Amazon Nova Sonic bidirectional-stream protocol: pure event builders and an
output parser. No I/O, no SDK import, so every shape here is unit-testable.

Input events (client -> model) are JSON strings sent one per stream chunk, in a
fixed order: sessionStart, promptStart, then ``contentStart / <content> /
contentEnd`` triplets for the system prompt, seeded history, live audio, typed
text and tool results; promptEnd and sessionEnd close the stream. Every event
after promptStart carries the same ``promptName``; every content block carries
its own ``contentName``.

Output events (model -> client) arrive as JSON too. ``OutputParser`` turns them
into a small neutral vocabulary the session layer consumes:

    user_transcript   what the model heard the user say (final only)
    assistant_text    the model's own words, stage "speculative" (about to be
                      spoken) or "final" (what was actually spoken)
    audio             one base64-decoded PCM16 chunk of assistant speech
    tool_use          the model wants a tool run (id, name, parsed input)
    interrupted       the user barged in; drop any queued playback
    turn_end          the model finished a response
    usage             cumulative speech/text token counts
    error             an in-band error the stream reported
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MODEL_ID = "amazon.nova-2-sonic-v1:0"
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000
DEFAULT_VOICE_ID = "tiffany"
DEFAULT_ENDPOINTING = "MEDIUM"
ENDPOINTING_LEVELS = ("LOW", "MEDIUM", "HIGH")

# Nova 2 Sonic voices. Tiffany and Matthew are polyglot (speak every supported
# language); the rest are tied to one locale.
VOICES: tuple[dict[str, str], ...] = (
    {"id": "tiffany", "label": "Tiffany", "locale": "en-US", "polyglot": "yes"},
    {"id": "matthew", "label": "Matthew", "locale": "en-US", "polyglot": "yes"},
    {"id": "amy", "label": "Amy", "locale": "en-GB", "polyglot": "no"},
    {"id": "olivia", "label": "Olivia", "locale": "en-AU", "polyglot": "no"},
    {"id": "kiara", "label": "Kiara", "locale": "en-IN", "polyglot": "no"},
    {"id": "arjun", "label": "Arjun", "locale": "en-IN", "polyglot": "no"},
    {"id": "ambre", "label": "Ambre", "locale": "fr-FR", "polyglot": "no"},
    {"id": "florian", "label": "Florian", "locale": "fr-FR", "polyglot": "no"},
    {"id": "beatrice", "label": "Beatrice", "locale": "it-IT", "polyglot": "no"},
    {"id": "lorenzo", "label": "Lorenzo", "locale": "it-IT", "polyglot": "no"},
    {"id": "tina", "label": "Tina", "locale": "de-DE", "polyglot": "no"},
    {"id": "lennart", "label": "Lennart", "locale": "de-DE", "polyglot": "no"},
    {"id": "lupe", "label": "Lupe", "locale": "es-US", "polyglot": "no"},
    {"id": "carlos", "label": "Carlos", "locale": "es-US", "polyglot": "no"},
    {"id": "carolina", "label": "Carolina", "locale": "pt-BR", "polyglot": "no"},
    {"id": "leo", "label": "Leo", "locale": "pt-BR", "polyglot": "no"},
)
VOICE_IDS = frozenset(v["id"] for v in VOICES)


def new_id() -> str:
    return uuid.uuid4().hex


def _event(body: dict[str, Any]) -> str:
    return json.dumps({"event": body}, separators=(",", ":"))


# ── Input events ────────────────────────────────────────────────────────────


def session_start(
    *,
    max_tokens: int = 1024,
    top_p: float = 0.9,
    temperature: float = 0.7,
    endpointing: str = DEFAULT_ENDPOINTING,
) -> str:
    level = (
        endpointing.upper() if endpointing.upper() in ENDPOINTING_LEVELS else DEFAULT_ENDPOINTING
    )
    return _event(
        {
            "sessionStart": {
                "inferenceConfiguration": {
                    "maxTokens": max_tokens,
                    "topP": top_p,
                    "temperature": temperature,
                },
                "turnDetectionConfiguration": {"endpointingSensitivity": level},
            }
        }
    )


def to_tool_spec(tool: dict[str, Any]) -> dict[str, Any]:
    """Convert the app's Anthropic-shaped tool ``{name, description,
    input_schema}`` into Sonic's ``toolSpec`` (schema travels as a JSON string)."""
    schema = tool.get("input_schema") or {"type": "object", "properties": {}}
    return {
        "toolSpec": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "inputSchema": {"json": json.dumps(schema, separators=(",", ":"))},
        }
    }


def prompt_start(
    prompt_name: str,
    *,
    voice_id: str = DEFAULT_VOICE_ID,
    output_sample_rate: int = OUTPUT_SAMPLE_RATE,
    tools: list[dict[str, Any]] | None = None,
) -> str:
    body: dict[str, Any] = {
        "promptName": prompt_name,
        "textOutputConfiguration": {"mediaType": "text/plain"},
        "audioOutputConfiguration": {
            "mediaType": "audio/lpcm",
            "sampleRateHertz": output_sample_rate,
            "sampleSizeBits": 16,
            "channelCount": 1,
            "voiceId": voice_id if voice_id in VOICE_IDS else DEFAULT_VOICE_ID,
            "encoding": "base64",
            "audioType": "SPEECH",
        },
    }
    if tools:
        body["toolUseOutputConfiguration"] = {"mediaType": "application/json"}
        body["toolConfiguration"] = {
            "tools": [to_tool_spec(t) for t in tools],
            "toolChoice": {"auto": {}},
        }
    return _event({"promptStart": body})


def text_content(
    prompt_name: str,
    role: str,
    text: str,
    *,
    interactive: bool = False,
    content_name: str | None = None,
) -> list[str]:
    """The three events of one text block. ``role`` is SYSTEM, USER or
    ASSISTANT. ``interactive=True`` is the cross-modal typed-turn form; seeded
    history and the system prompt use ``False``."""
    name = content_name or new_id()
    return [
        _event(
            {
                "contentStart": {
                    "promptName": prompt_name,
                    "contentName": name,
                    "type": "TEXT",
                    "interactive": interactive,
                    "role": role,
                    "textInputConfiguration": {"mediaType": "text/plain"},
                }
            }
        ),
        _event({"textInput": {"promptName": prompt_name, "contentName": name, "content": text}}),
        _event({"contentEnd": {"promptName": prompt_name, "contentName": name}}),
    ]


def audio_content_start(
    prompt_name: str, content_name: str, *, sample_rate: int = INPUT_SAMPLE_RATE
) -> str:
    return _event(
        {
            "contentStart": {
                "promptName": prompt_name,
                "contentName": content_name,
                "type": "AUDIO",
                "interactive": True,
                "role": "USER",
                "audioInputConfiguration": {
                    "mediaType": "audio/lpcm",
                    "sampleRateHertz": sample_rate,
                    "sampleSizeBits": 16,
                    "channelCount": 1,
                    "audioType": "SPEECH",
                    "encoding": "base64",
                },
            }
        }
    )


def audio_input(prompt_name: str, content_name: str, pcm16: bytes) -> str:
    return _event(
        {
            "audioInput": {
                "promptName": prompt_name,
                "contentName": content_name,
                "content": base64.b64encode(pcm16).decode("ascii"),
            }
        }
    )


def content_end(prompt_name: str, content_name: str) -> str:
    return _event({"contentEnd": {"promptName": prompt_name, "contentName": content_name}})


def tool_result(prompt_name: str, tool_use_id: str, result: Any) -> list[str]:
    """The three events answering one ``toolUse``. Sonic parses ``content`` as a
    JSON object (observed live: a plain-text string fails the whole stream with
    "Tool Response parsing error"), so a string is wrapped as ``{"result": ...}``
    and a dict is dumped as is. Sonic stalls forever on an unanswered tool call,
    so callers must send this even for failures."""
    name = new_id()
    if isinstance(result, dict):
        content = json.dumps(result)
    else:
        content = json.dumps({"result": result if isinstance(result, str) else str(result)})
    return [
        _event(
            {
                "contentStart": {
                    "promptName": prompt_name,
                    "contentName": name,
                    "interactive": False,
                    "type": "TOOL",
                    "role": "TOOL",
                    "toolResultInputConfiguration": {
                        "toolUseId": tool_use_id,
                        "type": "TEXT",
                        "textInputConfiguration": {"mediaType": "text/plain"},
                    },
                }
            }
        ),
        _event(
            {
                "toolResult": {
                    "promptName": prompt_name,
                    "contentName": name,
                    "content": content,
                }
            }
        ),
        _event({"contentEnd": {"promptName": prompt_name, "contentName": name}}),
    ]


def prompt_end(prompt_name: str) -> str:
    return _event({"promptEnd": {"promptName": prompt_name}})


def session_end() -> str:
    return _event({"sessionEnd": {}})


def history_events(
    prompt_name: str, history: list[dict[str, Any]], *, limit: int = 40
) -> list[str]:
    """Seed prior conversation turns (``{"role": "user"|"assistant",
    "content": str}``) as non-interactive text blocks. Sonic accepts history
    once, after the system prompt and before audio starts; other roles and
    empty rows are skipped, and only the last ``limit`` rows are sent."""
    events: list[str] = []
    rows = [
        r
        for r in history
        if r.get("role") in ("user", "assistant") and str(r.get("content") or "").strip()
    ][-limit:]
    for row in rows:
        role = "USER" if row["role"] == "user" else "ASSISTANT"
        events.extend(text_content(prompt_name, role, str(row["content"]).strip()))
    return events


# ── Output parsing ──────────────────────────────────────────────────────────


@dataclass
class _Block:
    role: str
    type: str
    stage: str  # "speculative" | "final" | ""


@dataclass
class OutputParser:
    """Stateful parser: remembers each content block's role/type/stage from
    its ``contentStart`` so the flat ``textOutput`` / ``audioOutput`` events
    that follow can be labeled. One instance per stream."""

    _blocks: dict[str, _Block] = field(default_factory=dict)

    def parse(self, raw: bytes | str) -> list[dict[str, Any]]:
        """Parse one raw stream chunk into zero or more neutral events."""
        try:
            data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (ValueError, UnicodeDecodeError):
            return [{"kind": "error", "message": "unparseable event from model"}]
        event = data.get("event") if isinstance(data, dict) else None
        if not isinstance(event, dict):
            return []
        out: list[dict[str, Any]] = []
        if "contentStart" in event:
            cs = event["contentStart"]
            stage = ""
            extra = cs.get("additionalModelFields")
            if isinstance(extra, str) and extra:
                try:
                    fields = json.loads(extra)
                except ValueError:
                    fields = None
                if isinstance(fields, dict):
                    stage = str(fields.get("generationStage", "")).lower()
            self._blocks[str(cs.get("contentId"))] = _Block(
                role=str(cs.get("role", "")), type=str(cs.get("type", "")), stage=stage
            )
        elif "textOutput" in event:
            to = event["textOutput"]
            block = self._blocks.get(str(to.get("contentId")))
            text = str(to.get("content", ""))
            if block and block.role == "USER":
                out.append({"kind": "user_transcript", "text": text})
            elif block and block.role == "ASSISTANT":
                out.append(
                    {
                        "kind": "assistant_text",
                        "text": text,
                        "stage": block.stage or "speculative",
                        "content_id": str(to.get("contentId")),
                    }
                )
        elif "audioOutput" in event:
            ao = event["audioOutput"]
            try:
                pcm = base64.b64decode(ao.get("content", ""))
            except (ValueError, TypeError):
                pcm = b""
            if pcm:
                out.append({"kind": "audio", "pcm": pcm, "content_id": str(ao.get("contentId"))})
        elif "toolUse" in event:
            tu = event["toolUse"]
            raw_input = tu.get("content", "{}")
            try:
                parsed = json.loads(raw_input) if isinstance(raw_input, str) else raw_input
            except ValueError:
                parsed = {"_raw": raw_input}
            out.append(
                {
                    "kind": "tool_use",
                    "tool_use_id": str(tu.get("toolUseId", "")),
                    "name": str(tu.get("toolName", "")),
                    "input": parsed if isinstance(parsed, dict) else {"value": parsed},
                }
            )
        elif "contentEnd" in event:
            ce = event["contentEnd"]
            block = self._blocks.pop(str(ce.get("contentId")), None)
            stop = str(ce.get("stopReason", ""))
            if stop == "INTERRUPTED":
                out.append({"kind": "interrupted"})
            if block and block.role == "ASSISTANT" and block.type == "TEXT":
                out.append(
                    {
                        "kind": "assistant_text_end",
                        "stage": block.stage or "speculative",
                        "stop_reason": stop,
                        "content_id": str(ce.get("contentId")),
                    }
                )
        elif "completionEnd" in event:
            out.append(
                {
                    "kind": "turn_end",
                    "stop_reason": str(event["completionEnd"].get("stopReason", "")),
                }
            )
        elif "completionStart" in event:
            out.append({"kind": "turn_start"})
        elif "usageEvent" in event:
            ue = event["usageEvent"]
            total = (ue.get("details") or {}).get("total") or {}
            inp = total.get("input") or {}
            outp = total.get("output") or {}
            out.append(
                {
                    "kind": "usage",
                    "input_speech": int(inp.get("speechTokens", 0) or 0),
                    "input_text": int(inp.get("textTokens", 0) or 0),
                    "output_speech": int(outp.get("speechTokens", 0) or 0),
                    "output_text": int(outp.get("textTokens", 0) or 0),
                    "total_tokens": int(ue.get("totalTokens", 0) or 0),
                }
            )
        return out
