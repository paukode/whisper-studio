"""Inline visuals for chat: hand-authored SVG diagrams and Vega-Lite charts.

Two emitters, one card. Both are pure side-effect tools routed in
tool_router.py (like create_artifact) rather than registered executors: they
compute nothing, they just hand a payload to the UI. Neither is
approval-gated — nothing is written, nothing is executed on the host, and the
chart spec is evaluated inside a sandboxed iframe.

`create_visual` renders model-authored SVG inline in the message, so it costs
one model turn and no runtime. `create_chart` renders a Vega-Lite spec in the
chart host page, which carries a vendored offline Vega runtime.
"""

import json

# A chart spec is inlined into the session's chat_history blob, which is
# rewritten on every message append. Cap it so a runaway data dump cannot make
# every later turn in the session pay to re-serialise it.
MAX_SPEC_CHARS = 400_000
MAX_SVG_CHARS = 200_000

_MULTI_VIEW_KEYS = ("layer", "facet", "hconcat", "vconcat", "concat", "repeat", "spec")


CREATE_VISUAL_TOOL = {
    "name": "create_visual",
    "description": (
        "Render a hand-authored SVG diagram inline in the chat, with buttons to "
        "download it as SVG or PNG and copy it to the clipboard. Use whenever an "
        "explanation would land better as a picture: architecture and flow "
        "diagrams, comparisons, timelines, state machines, sequence and pipeline "
        "diagrams, or when the user asks to 'visualise', 'draw', 'diagram' or "
        "'show me' a concept. Prefer this over ASCII art and over describing a "
        "diagram in prose. For charts of actual data use create_chart instead; "
        "for an interactive HTML app use create_program."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short title shown above the diagram (sentence case).",
            },
            "svg": {
                "type": "string",
                "description": (
                    "A complete <svg> element following this contract, which keeps "
                    "diagrams legible in all of the app's light and dark themes:\n"
                    '- Root: <svg width="100%" viewBox="0 0 680 H" role="img"> with a '
                    "<title> child. Width is always 680 user units so text renders "
                    "1:1 with pixels; set H to the content height plus 20.\n"
                    "- Never set a fill, stroke, or font-size attribute yourself. Use "
                    "these classes only, which read from the active theme:\n"
                    "  rects/circles: class='box' (neutral) or 'c-1'..'c-6' (six "
                    "categorical colours; group by category, 2-3 per diagram)\n"
                    "  text: class='t' (14px), 'th' (14px medium, for titles), "
                    "'ts' (12px, for subtitles and edge labels)\n"
                    "  connectors: class='arr' on <line>/<path>, with "
                    "marker-end='url(#arrow)'\n"
                    "- Include once: <defs><marker id='arrow' viewBox='0 0 10 10' "
                    "refX='8' refY='5' markerWidth='6' markerHeight='6' "
                    "orient='auto-start-reverse'><path d='M2 1L8 5L2 9' fill='none' "
                    "stroke='context-stroke' stroke-width='1.5' "
                    "stroke-linecap='round'/></marker></defs>\n"
                    "- Keep x within 20..660. SVG text does not wrap: budget ~8px per "
                    "character at 14px, ~6.6px at 12px, and check every label fits "
                    "its box. Titles are sentence case, subtitles five words or "
                    "fewer. No emoji, no scripts, no external images."
                ),
            },
            "description": {
                "type": "string",
                "description": "One-line caption shown under the title.",
            },
        },
        "required": ["title", "svg"],
    },
}


CREATE_CHART_TOOL = {
    "name": "create_chart",
    "description": (
        "Render a chart of real data inline in the chat, with buttons to download "
        "it as SVG or PNG and copy it to the clipboard. Takes a Vega-Lite v6 "
        "spec. Use for any request to chart, plot, graph or visualise data, "
        "including data read from an attached spreadsheet or CSV and figures "
        "computed with run_python. Colours, fonts, grid and legend styling are "
        "applied automatically from the app theme, so specify encodings, not "
        "appearance. For a concept diagram with no underlying data use "
        "create_visual instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short title shown above the chart (sentence case).",
            },
            "spec": {
                "type": "object",
                "description": (
                    "A Vega-Lite v6 spec. Data must be inline as "
                    '{"data": {"values": [...]}} — remote URLs are blocked. Omit '
                    '"width", "height", "background", "config" and any colour '
                    "range: the host fits the chart to the card and themes it. Give "
                    "every field an explicit type (quantitative, nominal, ordinal, "
                    "temporal) and a readable axis/legend title. Aggregate large "
                    "inputs before charting: keep it under a few hundred rows."
                ),
            },
            "description": {
                "type": "string",
                "description": "One-line caption shown under the title.",
            },
        },
        "required": ["title", "spec"],
    },
}


VISUAL_TOOLS = [CREATE_VISUAL_TOOL, CREATE_CHART_TOOL]


def validate_svg(svg: str) -> str | None:
    """Return an error message for the model, or None when the SVG is usable.

    Deliberately shallow: DOMPurify in the renderer is the security boundary,
    so this only catches the mistakes worth spending a retry on.
    """
    if not svg or not svg.strip():
        return "Error: `svg` was empty."
    if len(svg) > MAX_SVG_CHARS:
        return (
            f"Error: the SVG is {len(svg):,} characters, over the "
            f"{MAX_SVG_CHARS:,} limit. Simplify the diagram."
        )
    stripped = svg.strip()
    if not stripped.startswith("<svg") or "</svg>" not in stripped:
        return "Error: `svg` must be a single complete <svg>...</svg> element."
    if "viewBox" not in stripped:
        return 'Error: the <svg> needs a viewBox="0 0 680 H" attribute.'
    return None


def validate_chart_spec(spec) -> tuple[dict | None, str | None]:
    """Return (spec, None) when the spec is renderable, else (None, error).

    The error text goes back to the model as the tool result, which gives a
    self-repair round with no extra machinery: it sees exactly what was wrong
    and calls create_chart again.
    """
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except json.JSONDecodeError as e:
            return None, f"Error: `spec` is not valid JSON ({e})."
    if not isinstance(spec, dict):
        return None, "Error: `spec` must be a Vega-Lite spec object."

    encoded = json.dumps(spec)
    if len(encoded) > MAX_SPEC_CHARS:
        return None, (
            f"Error: the spec is {len(encoded):,} characters, over the "
            f"{MAX_SPEC_CHARS:,} limit. Aggregate the data before charting it."
        )

    has_view = "mark" in spec or any(k in spec for k in _MULTI_VIEW_KEYS)
    if not has_view:
        return None, (
            'Error: the spec has no "mark" (or layer/facet/concat/repeat), so '
            "there is nothing to draw."
        )

    data = spec.get("data")
    if isinstance(data, dict) and "url" in data:
        return None, (
            "Error: remote data URLs are blocked in chat charts. Inline the rows "
            'as {"data": {"values": [...]}}.'
        )
    if not any(k in spec for k in ("data", "datasets")) and "layer" not in spec:
        return None, 'Error: the spec has no "data". Inline the rows as {"values": [...]}.'

    # The host owns presentation; dropping these avoids a spec fighting the
    # theme (a hardcoded white background is unreadable in dark mode).
    for key in ("background", "config", "width", "height"):
        spec.pop(key, None)
    return spec, None
