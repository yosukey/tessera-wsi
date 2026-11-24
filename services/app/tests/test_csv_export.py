"""csv_export.csv_safe: the spreadsheet formula-injection defang every admin
CSV export runs each cell through."""
from __future__ import annotations

import pytest

from wsi_app.csv_export import _iter_csv, csv_safe


@pytest.mark.parametrize("value, expected", [
    ("=1+1", "'=1+1"),
    ("+1", "'+1"),
    ("-1", "'-1"),
    ("@SUM(A1)", "'@SUM(A1)"),
    ("\t=1", "'\t=1"),
    ("\r=1", "'\r=1"),
    # A leading LF, and whitespace a trimming import would drop first.
    ("\n=1", "'\n=1"),
    (" =HYPERLINK(\"http://x\")", "' =HYPERLINK(\"http://x\")"),
    ("  \n @x", "'  \n @x"),
    # A ';'-separated part is a cell of its own in a ';'-separator locale.
    ("Mozilla/5.0;=HYPERLINK(\"http://x\")", "Mozilla/5.0;'=HYPERLINK(\"http://x\")"),
    ("/a; =1;b;+2", "/a;' =1;b;'+2"),
    ("=a;=b", "'=a;'=b"),
])
def test_formula_like_parts_are_quoted(value, expected):
    assert csv_safe(value) == expected


@pytest.mark.parametrize("value", [
    "", "plain", "10.0.0.99", "a;b;c", "Mozilla/5.0 (X11; Linux x86_64)",
    "  leading space", "trailing =", "a=b", ";", "x;", 'say "hi"',
])
def test_benign_values_pass_unchanged(value):
    assert csv_safe(value) == value


@pytest.mark.parametrize("value", [None, 0, -5, 1.5, True])
def test_non_strings_pass_through(value):
    assert csv_safe(value) is value


@pytest.mark.parametrize("value", ["=1", " =1", "a;=b;-c", "\n@x", "plain"])
def test_idempotent(value):
    once = csv_safe(value)
    assert csv_safe(once) == once


def test_written_rows_are_defanged_per_part():
    body = "".join(_iter_csv(["ua", "path"], [["UA;=cmd|'/c calc'!A0", " =1"]]))
    lines = body.lstrip("﻿").split("\r\n")
    assert lines[1] == "UA;'=cmd|'/c calc'!A0,' =1"


def test_perf_export_defang_is_csv_safe():
    """perf_export's ';'-part defang is the shared one, not a second copy."""
    from wsi_app import perf_export

    for value in ("x;=1", " =1", "a;b", "=a;=b"):
        assert perf_export._defang_parts(value) == csv_safe(value)
