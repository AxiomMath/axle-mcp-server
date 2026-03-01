from __future__ import annotations

from axle_mcp_server.server import field_to_json_schema


def test_text() -> None:
    assert field_to_json_schema({"name": "x", "type": "text"})["type"] == "string"


def test_textarea() -> None:
    assert field_to_json_schema({"name": "x", "type": "textarea"})["type"] == "string"


def test_textarea_list() -> None:
    schema = field_to_json_schema({"name": "x", "type": "textarea_list"})
    assert schema["type"] == "array"
    assert schema["items"] == {"type": "string"}


def test_number() -> None:
    assert field_to_json_schema({"name": "x", "type": "number"})["type"] == "number"


def test_checkbox() -> None:
    assert field_to_json_schema({"name": "x", "type": "checkbox"})["type"] == "boolean"


def test_list_string() -> None:
    schema = field_to_json_schema({"name": "x", "type": "list"})
    assert schema["type"] == "array"
    assert schema["items"] == {"type": "string"}


def test_list_int() -> None:
    schema = field_to_json_schema({"name": "x", "type": "list", "cli_list_type": "int"})
    assert schema["items"] == {"type": "integer"}


def test_dict() -> None:
    assert field_to_json_schema({"name": "x", "type": "dict"})["type"] == "object"


def test_description_and_default() -> None:
    schema = field_to_json_schema(
        {"name": "x", "type": "text", "description": "A field", "default": "foo"}
    )
    assert schema["description"] == "A field"
    assert schema["default"] == "foo"


def test_unknown_type_defaults_to_string() -> None:
    assert field_to_json_schema({"name": "x", "type": "unknown_widget"})["type"] == "string"
