"""create_visual / create_chart: inline diagrams and charts in chat.

Both are side-effect-only routes. The contract worth pinning:

  - They emit a `viz_artifact` side effect and NEVER a [WS_APPROVAL] payload.
    Rendering a picture is not an approval-gated action, and a prompt on every
    diagram would make the feature unusable.
  - A bad input comes back as an error string rather than a broken card, which
    is what gives the model a self-repair round.
  - The host owns presentation, so a spec cannot pin its own background,
    size, or config and fight the theme.
"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from server.tool_router import route_tool
from server.visuals import MAX_SVG_CHARS, validate_chart_spec, validate_svg

GOOD_SVG = '<svg width="100%" viewBox="0 0 680 120"><title>t</title><rect class="box"/></svg>'
GOOD_SPEC = {
    "mark": "bar",
    "data": {"values": [{"a": "x", "b": 1}]},
    "encoding": {
        "x": {"field": "a", "type": "nominal"},
        "y": {"field": "b", "type": "quantitative"},
    },
}


def _route(name, tool_input, session_id="s1"):
    async def go():
        with ThreadPoolExecutor(max_workers=1) as pool:
            return await route_tool(
                name,
                tool_input,
                loop=asyncio.get_running_loop(),
                executor=pool,
                transcript="",
                attachments=None,
                session_id=session_id,
                model_id="m",
                tool_use_id="tu_1",
            )

    return asyncio.run(go())


# ---------------------------------------------------------------- validation


def test_svg_must_be_a_single_complete_element():
    assert validate_svg("") is not None
    assert validate_svg("just some text") is not None
    assert validate_svg('<svg viewBox="0 0 680 10">') is not None, "unclosed"
    assert validate_svg('<svg width="100%">no viewbox</svg>') is not None
    assert validate_svg(GOOD_SVG) is None


def test_oversized_svg_is_rejected():
    assert validate_svg('<svg viewBox="0 0 680 1">' + "x" * MAX_SVG_CHARS + "</svg>")


def test_spec_accepts_json_string_or_object():
    for candidate in (GOOD_SPEC, json.dumps(GOOD_SPEC)):
        spec, error = validate_chart_spec(candidate)
        assert error is None
        assert spec["mark"] == "bar"


@pytest.mark.parametrize(
    "spec, needle",
    [
        ("{not json", "valid JSON"),
        ([], "spec object"),
        ({"data": {"values": []}}, 'no "mark"'),
        ({"mark": "bar"}, 'no "data"'),
        ({"mark": "bar", "data": {"url": "https://evil.test/x.csv"}}, "blocked"),
    ],
)
def test_bad_specs_come_back_as_model_readable_errors(spec, needle):
    parsed, error = validate_chart_spec(spec)
    assert parsed is None
    assert needle in error


def test_presentation_keys_are_stripped_so_the_theme_wins():
    spec, error = validate_chart_spec(
        {**GOOD_SPEC, "background": "#fff", "width": 900, "height": 400, "config": {"axis": {}}}
    )
    assert error is None
    for key in ("background", "width", "height", "config"):
        assert key not in spec


def test_multi_view_specs_need_no_top_level_mark():
    spec, error = validate_chart_spec(
        {"data": {"values": [{"a": 1}]}, "vconcat": [{"mark": "line"}]}
    )
    assert error is None and spec is not None


# ------------------------------------------------------------- geometry lint
# The lint is deliberately conservative: it bounces only what would visibly
# render broken (content past the viewBox, boxes drawn on each other), it
# skips anything it cannot measure, and it hands out ONE repair round — the
# resubmission renders whether or not it is fixed.


def _svg(body, h=200):
    return f'<svg width="100%" viewBox="0 0 680 {h}"><title>t</title>{body}</svg>'


def test_content_past_the_viewbox_bottom_bounces_once_then_renders():
    from server.visuals import check_svg_geometry

    bad = _svg('<rect class="box" x="20" y="180" width="200" height="60"/>')

    output, effects = _route("create_visual", {"title": "F", "svg": bad}, session_id="geo-1")
    assert effects == []
    assert output.startswith("Error:") and "viewBox" in output

    # Second attempt renders even if still broken: cap is one repair round.
    output, effects = _route("create_visual", {"title": "F", "svg": bad}, session_id="geo-1")
    (effect,) = effects
    assert effect["viz_artifact"]["kind"] == "svg"

    # The budget resets after the render, so a later diagram gets its own round.
    assert check_svg_geometry(bad, "geo-1") is not None


def test_text_running_past_the_canvas_edge_is_flagged():
    from server.visuals import check_svg_geometry

    bad = _svg('<text class="t" x="600" y="40">a label of thirty characters!!</text>')
    error = check_svg_geometry(bad, "geo-2")
    assert error is not None and "text" in error


def test_partially_overlapping_boxes_are_flagged_but_containment_is_not():
    from server.visuals import check_svg_geometry

    overlapping = _svg(
        '<rect class="c-1" x="100" y="100" width="200" height="80"/>'
        '<rect class="c-2" x="150" y="120" width="200" height="80"/>'
    )
    assert "overlap" in check_svg_geometry(overlapping, "geo-3")

    # A group border around inner boxes is a normal diagram idiom.
    contained = _svg(
        '<rect class="box" x="20" y="20" width="600" height="150"/>'
        '<rect class="c-1" x="40" y="40" width="100" height="40"/>'
    )
    assert check_svg_geometry(contained, "geo-4") is None


def test_translate_is_accounted_and_unmeasurable_subtrees_are_skipped():
    from server.visuals import check_svg_geometry

    translated = _svg(
        '<g transform="translate(0, 150)">'
        '<rect class="box" x="20" y="100" width="100" height="100"/></g>'
    )
    assert check_svg_geometry(translated, "geo-5") is not None

    rotated = _svg(
        '<g transform="rotate(45)"><rect class="box" x="20" y="500" width="100" height="100"/></g>'
    )
    assert check_svg_geometry(rotated, "geo-6") is None


def test_entity_payloads_and_broken_xml_are_skipped_not_crashed():
    from server.visuals import check_svg_geometry

    entity = (
        '<!DOCTYPE svg [<!ENTITY a "aaaa">]>'
        '<svg viewBox="0 0 680 100"><text x="0" y="50">&a;</text></svg>'
    )
    assert check_svg_geometry(entity, "geo-7") is None
    assert check_svg_geometry('<svg viewBox="0 0 680 100"><rect</svg>', "geo-8") is None


# -------------------------------------------------------------------- routing


def test_create_visual_emits_a_card_without_asking_for_approval():
    output, effects = _route("create_visual", {"title": "Flow", "svg": GOOD_SVG})
    assert "[WS_APPROVAL]" not in output
    (effect,) = effects
    assert effect["viz_artifact"]["kind"] == "svg"
    assert effect["viz_artifact"]["title"] == "Flow"
    assert effect["viz_artifact"]["source"] == GOOD_SVG
    assert effect["viz_artifact"]["tool_use_id"] == "tu_1"


def test_create_chart_emits_a_serialised_spec():
    output, effects = _route("create_chart", {"title": "Sales", "spec": GOOD_SPEC})
    assert "[WS_APPROVAL]" not in output
    (effect,) = effects
    assert effect["viz_artifact"]["kind"] == "chart"
    assert json.loads(effect["viz_artifact"]["source"])["mark"] == "bar"


def test_invalid_input_returns_an_error_and_renders_no_card():
    output, effects = _route("create_visual", {"title": "Flow", "svg": "nope"})
    assert effects == []
    assert output.startswith("Error:")

    output, effects = _route("create_chart", {"title": "Sales", "spec": {"mark": "bar"}})
    assert effects == []
    assert output.startswith("Error:")


def test_tools_are_advertised_and_never_deferred():
    from server.chat.tool_partition import CORE_TOOLS
    from server.visuals import VISUAL_TOOLS

    names = {t["name"] for t in VISUAL_TOOLS}
    assert names == {"create_visual", "create_chart"}
    assert names <= CORE_TOOLS
