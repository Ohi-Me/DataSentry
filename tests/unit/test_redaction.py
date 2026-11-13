"""The redaction filter.

docs/07 says no raw warehouse value is ever logged. This is the test that
makes that a rule rather than an intention -- and the reason the filter lives
in one place instead of being everyone's responsibility.
"""

from __future__ import annotations

import json
import logging

from datasentry_core.logging import FORBIDDEN_KEYS, JsonFormatter, RedactionFilter, redact


def test_value_bearing_keys_are_stripped():
    payload = {
        "column": "revenue_usd",
        "sample": ["alice@example.com", "bob@example.com"],
        "values": [1, 2, 3],
        "row": {"customer": "ACME"},
        "score": 12.4,
    }
    cleaned = redact(payload)
    assert cleaned["column"] == "revenue_usd"      # metadata is fine
    assert cleaned["score"] == 12.4                # statistics are fine
    for key in ("sample", "values", "row"):
        assert cleaned[key] == "[redacted]"


def test_redaction_reaches_into_nested_structures():
    cleaned = redact({"outer": {"inner": [{"values": ["secret"]}]}})
    assert cleaned["outer"]["inner"][0]["values"] == "[redacted]"


def test_credentials_are_stripped_from_free_text():
    for secret in ("sk-abcdefghijklmnopqrstuv", "ghp_abcdefghijklmnopqrstuvwxyz01",
                   "AKIAIOSFODNN7EXAMPLE"):
        assert secret not in redact("connect using " + secret)


def test_the_forbidden_key_list_covers_the_obvious_ones():
    for key in ("value", "values", "sample", "raw", "row", "password", "token", "secret"):
        assert key in FORBIDDEN_KEYS


def test_the_filter_is_applied_by_the_logging_pipeline():
    """Not just the function -- the filter, wired into a record."""
    record = logging.LogRecord("t", logging.INFO, __file__, 1,
                               "key sk-abcdefghijklmnopqrst", None, None)
    record.context = {"sample": ["raw customer value"], "rows": 42}
    RedactionFilter().filter(record)

    document = json.loads(JsonFormatter().format(record))
    assert "sk-abcdefghijklmnopqrst" not in document["msg"]
    assert document["context"]["sample"] == "[redacted]"
    assert document["context"]["rows"] == "[redacted]"
