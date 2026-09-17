from __future__ import annotations

import json
import logging

from nineveh.observability import JsonFormatter, configure_logging


def _record(**kwargs) -> logging.LogRecord:
    defaults = {
        "name": "nineveh.test",
        "level": logging.INFO,
        "pathname": __file__,
        "lineno": 1,
        "msg": "hello %s",
        "args": ("world",),
        "exc_info": None,
    }
    return logging.LogRecord(**{**defaults, **kwargs})


def test_records_are_emitted_as_one_json_object_per_line():
    event = json.loads(JsonFormatter().format(_record()))
    assert event["level"] == "INFO"
    assert event["logger"] == "nineveh.test"
    assert event["message"] == "hello world"
    assert event["timestamp"].endswith("+00:00")
    assert "exception" not in event


def test_exceptions_are_included_when_present():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        formatted = JsonFormatter().format(_record(exc_info=sys.exc_info()))
    event = json.loads(formatted)
    assert "ValueError: boom" in event["exception"]


def test_non_ascii_messages_survive_intact():
    event = json.loads(JsonFormatter().format(_record(msg="Nineveh — 🦁", args=())))
    assert event["message"] == "Nineveh — 🦁"


def test_configure_logging_installs_a_single_json_handler():
    configure_logging("warning")
    root = logging.getLogger()
    try:
        assert root.level == logging.WARNING
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
    finally:
        logging.basicConfig(force=True)
