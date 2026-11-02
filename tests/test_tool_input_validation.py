"""Tool input validation rejects ambiguity without changing model arguments."""

import copy
import json
import socket

import pytest

from server.tool_inputs import (
    ToolCatalog,
    ToolInputError,
    parse_tool_arguments,
    schemas_for_tools,
    validate_call_ids,
    validate_tool_input,
)


@pytest.mark.parametrize(
    "raw",
    [None, "", " ", "{", '{"path":', "null", "[]", '"path"', "1", "true"],
)
def test_only_explicit_complete_json_objects_are_parsed(raw):
    with pytest.raises(ToolInputError):
        parse_tool_arguments(raw, "probe")


def test_an_explicit_empty_object_is_not_a_missing_argument_payload():
    assert parse_tool_arguments("{}", "probe") == {}


@pytest.mark.parametrize(
    "raw",
    [
        '{"path":"a","path":"b"}',
        '{"options":{"n":1,"n":2}}',
        '{"n":NaN}',
        '{"n":Infinity}',
        '{"n":-Infinity}',
    ],
)
def test_ambiguous_or_non_json_arguments_are_rejected(raw):
    with pytest.raises(ToolInputError):
        parse_tool_arguments(raw, "probe")


def test_valid_nested_arguments_are_preserved():
    expected = {"path": "notes.txt", "options": [None, True, 3, {"enabled": False}]}
    assert parse_tool_arguments(json.dumps(expected), "probe") == expected


def test_validation_does_not_insert_defaults_or_coerce_values():
    schema = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 5},
            "enabled": {"type": "boolean", "default": True},
        },
    }
    original = {}
    validate_tool_input("probe", original, {"probe": schema})
    assert original == {}
    for value in ({"limit": "5"}, {"enabled": "true"}):
        before = copy.deepcopy(value)
        with pytest.raises(ToolInputError):
            validate_tool_input("probe", value, {"probe": schema})
        assert value == before


def test_missing_required_input_is_not_replaced_with_a_default():
    schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "default": "."}},
        "required": ["path"],
    }
    with pytest.raises(ToolInputError):
        validate_tool_input("probe", {}, {"probe": schema})


def test_zero_required_field_tool_accepts_an_explicit_empty_object():
    validate_tool_input("probe", {}, {"probe": {"type": "object", "properties": {}}})


@pytest.mark.parametrize("value", [None, [], "text", 3, False])
def test_executor_input_must_be_an_object(value):
    with pytest.raises(ToolInputError):
        validate_tool_input("probe", value, {"probe": {"type": "object"}})


@pytest.mark.parametrize(
    "value",
    [
        {"items": (1, 2)},
        {1: "not a JSON object key"},
        {"nested": {False: "not a JSON object key"}},
        {"nested": [{"items": (1, 2)}]},
        {"number": float("nan")},
        {"number": float("inf")},
        {"items": {1, 2}},
        {"bytes": b"not JSON"},
    ],
)
def test_hook_inputs_cannot_rely_on_json_serialization_coercion(value):
    with pytest.raises(ToolInputError):
        validate_tool_input("probe", value, {"probe": {"type": "object"}})


@pytest.mark.parametrize(
    "property_schema",
    [
        {"type": "integer"},
        {"type": "string", "enum": ["allowed"]},
        {"type": "string", "pattern": "^allowed$"},
        {"type": "string", "maxLength": 3},
        {"const": "allowed"},
    ],
)
def test_validation_errors_do_not_disclose_argument_values(property_schema):
    sentinel = "PRIVATE_ARGUMENT_SENTINEL"
    schema = {"type": "object", "properties": {"value": property_schema}}
    with pytest.raises(ToolInputError) as caught:
        validate_tool_input("probe", {"value": sentinel}, {"probe": schema})
    assert sentinel not in str(caught.value)
    assert "probe" in str(caught.value)


def test_local_schema_references_work_without_network(monkeypatch):
    def network_is_forbidden(*args, **kwargs):
        raise AssertionError("schema validation must not access the network")

    monkeypatch.setattr(socket, "create_connection", network_is_forbidden)
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "$defs": {"name": {"type": "string", "minLength": 1}},
        "properties": {"name": {"$ref": "#/$defs/name"}},
        "required": ["name"],
    }
    validate_tool_input("probe", {"name": "demo"}, {"probe": schema})
    with pytest.raises(ToolInputError):
        validate_tool_input("probe", {"name": ""}, {"probe": schema})


def test_external_schema_references_fail_without_retrieval(monkeypatch):
    requests = []

    def network_is_forbidden(*args, **kwargs):
        requests.append(args)
        raise AssertionError("schema validation attempted a network connection")

    monkeypatch.setattr(socket, "create_connection", network_is_forbidden)
    with pytest.raises(ToolInputError):
        validate_tool_input("probe", {}, {"probe": {"$ref": "https://schema.invalid/private.json"}})
    assert requests == []


@pytest.mark.parametrize(
    "schemas",
    [
        {},
        {"probe": None},
        {"probe": {"type": "not-a-json-schema-type"}},
        {"probe": {"$schema": "https://schema.invalid/unknown-dialect"}},
        {"probe": False},
    ],
)
def test_unknown_tools_and_unusable_schemas_never_skip_validation(schemas):
    with pytest.raises(ToolInputError):
        validate_tool_input("probe", {}, schemas)


def test_schema_snapshot_preserves_advertised_bytes_and_freezes_definitions():
    offered = [{"name": "visible", "input_schema": {"type": "object"}}]
    available = [
        {"name": "visible", "input_schema": False},
        {"name": "deferred", "input_schema": {"type": "object", "required": ["path"]}},
    ]
    catalog = ToolCatalog(offered, available=available)
    before = json.dumps(catalog, sort_keys=True)
    frozen = schemas_for_tools(catalog)
    assert before == json.dumps(offered, sort_keys=True)
    assert frozen["visible"] == offered[0]["input_schema"]
    assert "deferred" in frozen
    available[1]["input_schema"]["required"].append("changed")
    offered[0]["input_schema"]["properties"] = {"changed": {"type": "boolean"}}
    assert frozen["deferred"]["required"] == ["path"]
    assert frozen["visible"] == {"type": "object"}


@pytest.mark.parametrize(
    "calls",
    [
        [None],
        [{"name": "probe", "input": {}}],
        [{"id": "", "name": "probe", "input": {}}],
        [{"id": "call", "name": "", "input": {}}],
        [{"id": "call", "name": "probe"}, {"id": "call", "name": "other"}],
    ],
)
def test_missing_or_duplicate_call_identities_are_rejected(calls):
    with pytest.raises(ToolInputError):
        validate_call_ids(calls)


def test_distinct_calls_of_the_same_tool_are_valid():
    validate_call_ids(
        [
            {"id": "first", "name": "probe", "input": {}},
            {"id": "second", "name": "probe", "input": {}},
        ]
    )
