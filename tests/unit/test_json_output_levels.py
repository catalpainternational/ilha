"""Unit tests for ilha/utils/json_output.py.

Covers JSONOutput.success/error/notice dict shapes, the level carried by
notice(), the JSON emitted by print_info/print_warning/print_error in JSON
mode (captured from stdout and parsed), and the handle_json_output decorator
for dict/list/bool/None results.
"""

import json

import pytest

from ilha.utils.json_output import JSONOutput, handle_json_output


def _parse_stdout(capsys):
    out = capsys.readouterr().out
    return json.loads(out)


# ---------------------------------------------------------------------------
# success()
# ---------------------------------------------------------------------------

def test_success_shape_minimal():
    result = JSONOutput.success("done")
    assert result["success"] is True
    assert result["message"] == "done"
    assert "timestamp" in result
    assert "data" not in result


def test_success_with_data():
    result = JSONOutput.success("done", {"count": 3})
    assert result["data"] == {"count": 3}


def test_success_empty_data_omitted():
    # Falsy data (empty dict) is not attached.
    result = JSONOutput.success("done", {})
    assert "data" not in result


# ---------------------------------------------------------------------------
# error()
# ---------------------------------------------------------------------------

def test_error_shape_minimal():
    result = JSONOutput.error("boom")
    assert result["success"] is False
    assert result["error"] == "boom"
    assert "timestamp" in result
    assert "error_code" not in result
    assert "details" not in result


def test_error_with_code_and_details():
    result = JSONOutput.error("boom", "E_CODE", {"why": "bad"})
    assert result["error_code"] == "E_CODE"
    assert result["details"] == {"why": "bad"}


def test_error_has_no_message_key():
    # Errors use "error", not "message".
    result = JSONOutput.error("boom")
    assert "message" not in result


# ---------------------------------------------------------------------------
# notice()
# ---------------------------------------------------------------------------

def test_notice_info_level():
    result = JSONOutput.notice("heads up", "info")
    assert result["success"] is True
    assert result["level"] == "info"
    assert result["message"] == "heads up"
    assert "timestamp" in result


def test_notice_warning_level():
    result = JSONOutput.notice("careful", "warning")
    assert result["success"] is True
    assert result["level"] == "warning"


def test_notice_with_data():
    result = JSONOutput.notice("x", "info", {"k": "v"})
    assert result["data"] == {"k": "v"}


def test_notice_carries_success_true():
    # notice deliberately keeps success=True but adds level to disambiguate.
    result = JSONOutput.notice("x", "warning")
    assert result["success"] is True
    assert "level" in result


# ---------------------------------------------------------------------------
# print_info / print_warning in JSON mode (stdout capture)
# ---------------------------------------------------------------------------

def test_print_info_json_emits_info_level(capsys):
    JSONOutput.print_info("hello", json_output=True)
    parsed = _parse_stdout(capsys)
    assert parsed["level"] == "info"
    assert parsed["success"] is True
    assert parsed["message"] == "hello"


def test_print_warning_json_emits_warning_level(capsys):
    JSONOutput.print_warning("watch out", json_output=True)
    parsed = _parse_stdout(capsys)
    assert parsed["level"] == "warning"
    assert parsed["success"] is True
    assert parsed["message"] == "watch out"


def test_print_info_json_includes_data(capsys):
    JSONOutput.print_info("hi", {"a": 1}, json_output=True)
    parsed = _parse_stdout(capsys)
    assert parsed["data"] == {"a": 1}


def test_print_info_non_json_emits_nothing_parseable(capsys):
    # Without json_output the info goes through the rich logger (verbose-gated),
    # so stdout has no JSON payload.
    JSONOutput.print_info("hi", json_output=False)
    out = capsys.readouterr().out
    assert out.strip() == "" or not out.strip().startswith("{")


def test_print_error_json_shape(capsys):
    JSONOutput.print_error("nope", "E1", {"d": 1}, json_output=True)
    parsed = _parse_stdout(capsys)
    assert parsed["success"] is False
    assert parsed["error"] == "nope"
    assert parsed["error_code"] == "E1"
    assert parsed["details"] == {"d": 1}


def test_print_success_json_shape(capsys):
    JSONOutput.print_success("ok", {"n": 2}, json_output=True)
    parsed = _parse_stdout(capsys)
    assert parsed["success"] is True
    assert parsed["message"] == "ok"
    assert parsed["data"] == {"n": 2}


def test_print_json_is_indented(capsys):
    JSONOutput.print_json({"a": 1})
    out = capsys.readouterr().out
    # indent=2 produces newlines within the object.
    assert "\n" in out
    assert json.loads(out) == {"a": 1}


# ---------------------------------------------------------------------------
# handle_json_output decorator
# ---------------------------------------------------------------------------

def test_handle_json_output_dict_result(capsys):
    @handle_json_output
    def fn():
        return {"custom": "payload"}

    ret = fn(json=True)
    parsed = _parse_stdout(capsys)
    assert parsed == {"custom": "payload"}
    assert ret == {"custom": "payload"}


def test_handle_json_output_bool_true(capsys):
    @handle_json_output
    def fn():
        return True

    fn(json=True)
    parsed = _parse_stdout(capsys)
    assert parsed["success"] is True
    assert parsed["message"] == "Operation completed"


def test_handle_json_output_bool_false(capsys):
    @handle_json_output
    def fn():
        return False

    fn(json=True)
    parsed = _parse_stdout(capsys)
    # A False result still yields a success envelope but "Operation failed".
    assert parsed["message"] == "Operation failed"
    assert parsed["success"] is True


def test_handle_json_output_list_result(capsys):
    @handle_json_output
    def fn():
        return [1, 2, 3]

    fn(json=True)
    parsed = _parse_stdout(capsys)
    assert parsed["message"] == "Operation completed"
    assert parsed["data"] == {"result": [1, 2, 3]}


def test_handle_json_output_none_result(capsys):
    @handle_json_output
    def fn():
        return None

    fn(json=True)
    parsed = _parse_stdout(capsys)
    assert parsed["data"] == {"result": None}


def test_handle_json_output_no_json_flag_no_output(capsys):
    @handle_json_output
    def fn():
        return {"x": 1}

    ret = fn()
    out = capsys.readouterr().out
    assert out.strip() == ""
    assert ret == {"x": 1}


def test_handle_json_output_exception_json_mode_prints_error(capsys):
    @handle_json_output
    def fn():
        raise ValueError("kaboom")

    ret = fn(json=True)
    parsed = _parse_stdout(capsys)
    assert parsed["success"] is False
    assert parsed["error"] == "kaboom"
    assert parsed["error_code"] == "command_error"
    assert ret is None


def test_handle_json_output_exception_non_json_reraises():
    @handle_json_output
    def fn():
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        fn()


def test_handle_json_output_pops_json_kwarg(capsys):
    # The wrapped function must not receive the json kwarg.
    received = {}

    @handle_json_output
    def fn(**kwargs):
        received.update(kwargs)
        return {"ok": True}

    fn(json=True, extra="value")
    assert "json" not in received
    assert received == {"extra": "value"}
