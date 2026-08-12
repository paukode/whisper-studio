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


def _route(name, tool_input):
    async def go():
        with ThreadPoolExecutor(max_workers=1) as pool:
            return await route_tool(
                name,
                tool_input,
                loop=asyncio.get_running_loop(),
                executor=pool,
                transcript="",
                attachments=None,
                session_id="s1",
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
        ({"data": {"values": []}}, "no \"mark\""),
        ({"mark": "bar"}, "no \"data\""),
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
