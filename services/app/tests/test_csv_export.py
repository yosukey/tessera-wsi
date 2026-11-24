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
    # A ';'-separated part is a cell of its own in a ';'-separator locale
    # when csv.writer leaves the value unquoted.
    ("Mozilla/5.0;=HYPERLINK(http://x)", "Mozilla/5.0;'=HYPERLINK(http://x)"),
    ("a;=1+1", "a;'=1+1"),
    ("/a; =1;b;+2", "/a; =1;b;'+2"),
    ("=a;=b", "'=a;'=b"),
    ("a;\t=1", "a;'\t=1"),
    # A value csv.writer quotes is one cell in every locale: only its start.
    ("=a,b;=c", "'=a,b;=c"),
])
def test_formula_like_parts_are_quoted(value, expected):
    assert csv_safe(value) == expected


@pytest.mark.parametrize("value", [
    "", "plain", "10.0.0.99", "a;b;c", "Mozilla/5.0 (X11; Linux x86_64)",
    "  leading space", "trailing =", "a=b", ";", "x;", 'say "hi"',
    # Crawler User-Agents: ';'-separated, unquoted, and a part starting with
    # " +http" — must not get a quote spliced into them.
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
    "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Googlebot/2.1;"
    " +http://www.google.com/bot.html) Chrome/120.0.0.0 Safari/537.36",
    # Quoted by csv.writer (comma / double quote / newline): one cell.
    "x,y;=1", 'say "hi";=1', "line\n;-1",
])
def test_benign_values_pass_unchanged(value):
    assert csv_safe(value) == value


@pytest.mark.parametrize("value", [None, 0, -5, 1.5, True])
def test_non_strings_pass_through(value):
    assert csv_safe(value) is value


@pytest.mark.parametrize("value", [
    "=1", " =1", "a;=b;-c", "\n@x", "plain", "=a;=b", " =a;+b", "=a,b;=c",
])
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

    for value in ("x;=1", " =1", "a;b", "=a;=b", "x,y;=1"):
        assert perf_export._defang_parts(value) == csv_safe(value)


def test_written_crawler_user_agent_is_unchanged_and_quoted_values_stay_whole():
    ua = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
    body = "".join(_iter_csv(["ua", "x"], [[ua, "a,b;=c"]]))
    lines = body.lstrip("\ufeff").split("\r\n")
    assert lines[1] == ua + ',"a,b;=c"'
