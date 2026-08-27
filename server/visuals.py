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
import re

# Model-authored XML: defused parser blocks entity-expansion payloads that
# stdlib expat would happily inflate.
import defusedxml.ElementTree as ET

# A chart spec is inlined into the session's chat_history blob, which is
# rewritten on every message append, so a huge spec taxes every later turn.
# The cap is a backstop against a runaway data dump, not the working limit:
# the model has to WRITE every inlined row, which prices real charts down to a
# few hundred rows long before this bites. Aggregate first, then chart.
MAX_SPEC_CHARS = 1_000_000
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


# --- geometry lint -----------------------------------------------------------
# Small models draw structurally fine diagrams with sloppy coordinates:
# content past the viewBox bottom (clipped by the card), text running off the
# canvas, boxes drawn on top of each other. The lint catches only those
# high-confidence cases and hands the model ONE repair round; a second miss
# renders anyway, so a model that can't fix its geometry never loops.

# Content inside these never draws in canvas coordinates.
_LINT_SKIP_TAGS = {"defs", "marker", "symbol", "clipPath", "mask", "pattern"}

_TRANSLATE_RE = re.compile(r"^\s*translate\(\s*(-?[0-9.]+)(?:[\s,]+(-?[0-9.]+))?\s*\)\s*$")

# Average glyph width (conservative, below the ~8px/char the contract budgets)
# so a bounce means the text truly leaves the canvas, not that the estimate
# was pessimistic. A false positive costs a whole diagram rewrite.
_CHAR_W_14 = 7.0
_CHAR_W_12 = 6.0
_EDGE_TOL = 2.0

# Sessions that were already handed a geometry error and owe us a resubmit.
_PENDING_GEOMETRY_REPAIR: set[str] = set()


def _attr_floats(el, names: tuple[str, ...], defaults: tuple[float, ...]) -> list[float] | None:
    out = []
    for name, default in zip(names, defaults, strict=True):
        raw = el.get(name)
        if raw is None:
            out.append(default)
            continue
        try:
            out.append(float(raw))
        except ValueError:
            return None  # units/percentages: extent unknown, skip the element
    return out


def _points_extent(el) -> tuple[float, float, float, float] | None:
    nums = []
    for token in re.findall(r"-?[0-9.]+", el.get("points", "")):
        try:
            nums.append(float(token))
        except ValueError:
            return None
    if len(nums) < 4:
        return None
    xs, ys = nums[0::2], nums[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def _text_extent(el) -> tuple[float, float, float, float] | None:
    pos = _attr_floats(el, ("x", "y"), (0.0, 0.0))
    if pos is None:
        return None
    x, y = pos
    content = "".join(el.itertext()).strip()
    if not content:
        return None
    classes = (el.get("class") or "").split()
    char_w = _CHAR_W_12 if "ts" in classes else _CHAR_W_14
    est = len(content) * char_w
    anchor = el.get("text-anchor", "start")
    if anchor == "middle":
        x0, x1 = x - est / 2, x + est / 2
    elif anchor == "end":
        x0, x1 = x - est, x
    else:
        x0, x1 = x, x + est
    # Baseline at y: ~11px of ascender above, ~4px of descender below at 14px.
    return x0, y - 11.0, x1, y + 4.0


def _label(el, extent) -> str:
    content = "".join(el.itertext()).strip()
    if content:
        return f'text "{content[:28]}"'
    tag = el.tag.rsplit("}", 1)[-1]
    return f"<{tag}> at ({extent[0]:.0f},{extent[1]:.0f})"


def _lint_svg_geometry(svg: str) -> str | None:
    """Best-effort coordinate check. Returns a short repair prompt or None.

    Never raises and never blocks rendering on uncertainty: anything it cannot
    measure (paths, rotated groups, unit-suffixed values, unparseable XML) is
    skipped, because DOMPurify downstream is more forgiving than ElementTree
    and a wrong bounce costs a full model round.
    """
    try:
        root = ET.fromstring(svg)
    except Exception:  # ParseError, or defusedxml refusing an entity payload
        return None
    parts = (root.get("viewBox") or "").replace(",", " ").split()
    if len(parts) != 4:
        return None
    try:
        min_x, min_y, vb_w, vb_h = (float(p) for p in parts)
    except ValueError:
        return None
    if vb_w <= 0 or vb_h <= 0:
        return None
    x_max, y_max = min_x + vb_w, min_y + vb_h

    issues: list[str] = []
    boxes: list[tuple[float, float, float, float, str]] = []

    def out_of_bounds(el, extent) -> None:
        x0, y0, x1, y1 = extent
        if (
            x1 > x_max + _EDGE_TOL
            or x0 < min_x - _EDGE_TOL
            or y1 > y_max + _EDGE_TOL
            or y0 < min_y - _EDGE_TOL
        ):
            issues.append(
                f"{_label(el, extent)} spans x {x0:.0f}..{x1:.0f}, y {y0:.0f}..{y1:.0f}, "
                f"outside the viewBox ({min_x:.0f} {min_y:.0f} {vb_w:.0f} {vb_h:.0f})"
            )

    def walk(parent, dx: float, dy: float) -> None:
        for el in parent:
            tag = el.tag.rsplit("}", 1)[-1]
            if tag in _LINT_SKIP_TAGS:
                continue
            edx, edy = dx, dy
            transform = el.get("transform")
            if transform:
                m = _TRANSLATE_RE.match(transform)
                if not m:
                    continue  # rotated/scaled subtree: extents unknown
                edx += float(m.group(1))
                edy += float(m.group(2) or 0.0)

            extent = None
            if tag == "rect":
                vals = _attr_floats(el, ("x", "y", "width", "height"), (0.0, 0.0, 0.0, 0.0))
                if vals is not None:
                    x, y, w, h = vals
                    extent = (x + edx, y + edy, x + w + edx, y + h + edy)
                    if w > 0 and h > 0:
                        boxes.append((*extent, _label(el, extent)))
            elif tag in ("circle", "ellipse"):
                names = ("cx", "cy", "r") if tag == "circle" else ("cx", "cy", "rx", "ry")
                vals = _attr_floats(el, names, (0.0,) * len(names))
                if vals is not None:
                    cx, cy = vals[0] + edx, vals[1] + edy
                    rx, ry = (vals[2], vals[2]) if tag == "circle" else (vals[2], vals[3])
                    extent = (cx - rx, cy - ry, cx + rx, cy + ry)
            elif tag == "line":
                vals = _attr_floats(el, ("x1", "y1", "x2", "y2"), (0.0, 0.0, 0.0, 0.0))
                if vals is not None:
                    x1, y1, x2, y2 = vals
                    extent = (
                        min(x1, x2) + edx,
                        min(y1, y2) + edy,
                        max(x1, x2) + edx,
                        max(y1, y2) + edy,
                    )
            elif tag in ("polyline", "polygon"):
                pts = _points_extent(el)
                if pts is not None:
                    extent = (pts[0] + edx, pts[1] + edy, pts[2] + edx, pts[3] + edy)
            elif tag == "text":
                ext = _text_extent(el)
                if ext is not None:
                    extent = (ext[0] + edx, ext[1] + edy, ext[2] + edx, ext[3] + edy)

            if extent is not None:
                out_of_bounds(el, extent)
            walk(el, edx, edy)

    walk(root, 0.0, 0.0)

    # Boxes that partially overlap read as a drawing mistake; full containment
    # (a group border around inner boxes) is a normal diagram idiom.
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            ax0, ay0, ax1, ay1, alabel = boxes[i]
            bx0, by0, bx1, by1, blabel = boxes[j]
            ix = min(ax1, bx1) - max(ax0, bx0)
            iy = min(ay1, by1) - max(ay0, by0)
            if ix <= 0 or iy <= 0:
                continue
            a_in_b = (
                ax0 >= bx0 - _EDGE_TOL
                and ay0 >= by0 - _EDGE_TOL
                and ax1 <= bx1 + _EDGE_TOL
                and ay1 <= by1 + _EDGE_TOL
            )
            b_in_a = (
                bx0 >= ax0 - _EDGE_TOL
                and by0 >= ay0 - _EDGE_TOL
                and bx1 <= ax1 + _EDGE_TOL
                and by1 <= ay1 + _EDGE_TOL
            )
            if a_in_b or b_in_a:
                continue
            smaller = min((ax1 - ax0) * (ay1 - ay0), (bx1 - bx0) * (by1 - by0))
            if smaller > 0 and (ix * iy) / smaller > 0.3:
                issues.append(f"boxes {alabel} and {blabel} overlap")

    if not issues:
        return None
    listed = "\n".join(f"- {line}" for line in issues[:4])
    if len(issues) > 4:
        listed += f"\n- plus {len(issues) - 4} more"
    return (
        "Error: the SVG has coordinate problems that would render badly:\n"
        f"{listed}\n"
        "Fix the coordinates and call create_visual once more with the full "
        "corrected SVG: grow the viewBox height if content needs room, keep "
        "everything inside the canvas with ~20px padding, and separate "
        "overlapping boxes."
    )


def check_svg_geometry(svg: str, session_id: str) -> str | None:
    """Geometry lint with a one-bounce budget.

    First miss returns the repair prompt; the resubmission renders whether or
    not it is fixed, so the extra cost is capped at one model round per
    diagram and a weak model can never retry-loop.
    """
    error = _lint_svg_geometry(svg)
    if error is None:
        _PENDING_GEOMETRY_REPAIR.discard(session_id)
        return None
    if session_id in _PENDING_GEOMETRY_REPAIR:
        _PENDING_GEOMETRY_REPAIR.discard(session_id)
        return None
    if len(_PENDING_GEOMETRY_REPAIR) > 256:  # backstop for abandoned sessions
        _PENDING_GEOMETRY_REPAIR.clear()
    _PENDING_GEOMETRY_REPAIR.add(session_id)
    return error


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
