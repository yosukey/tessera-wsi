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
    # After a line break the rest starts a new row in a ';' import, so the
    # part after it is checked as well.
    ("\r=1", "'\r'=1"),
    # A leading LF, and whitespace a trimming import would drop first.
    ("\n=1", "'\n'=1"),
    ("x\n=1+2\ny", "x\n'=1+2\ny"),
    ("x\r\n=1", "x\r\n'=1"),
    ("x\r=1", "x\r'=1"),
    ("a;b\n=1+2\nc", "a;b\n'=1+2\nc"),
    ("x\n =1+2", "x\n' =1+2"),
    (" =HYPERLINK(\"http://x\")", "' =HYPERLINK(\"http://x\")"),
    ("  \n @x", "'  \n' @x"),
    # A ';'-separated part is a cell of its own in a ';'-separator locale.
    ("Mozilla/5.0;=HYPERLINK(http://x)", "Mozilla/5.0;'=HYPERLINK(http://x)"),
    ("a;=1+1", "a;'=1+1"),
    ("/a; =1;b;+2", "/a;' =1;b;'+2"),
    ("=a;=b", "'=a;'=b"),
    ("a;\t=1", "a;'\t=1"),
    # Quoting does not keep a value whole in a ';' locale (the commas
    # between columns are not separators there), so its parts count too.
    ("=a,b;=c", "'=a,b;'=c"),
    ("x,y;=1+2;", "x,y;'=1+2;"),
    ('x"y;=1+5;', 'x"y;\'=1+5;'),
    ("line\n;-1", "line\n;'-1"),
    # Crawler User-Agents get a cosmetic quote: their " +http" part is a
    # formula once a trimming import drops the space.
    ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
     "Mozilla/5.0 (compatible; Googlebot/2.1;' +http://www.google.com/bot.html)"),
])
def test_formula_like_parts_are_quoted(value, expected):
    assert csv_safe(value) == expected


@pytest.mark.parametrize("value", [
    "", "plain", "10.0.0.99", "a;b;c", "Mozilla/5.0 (X11; Linux x86_64)",
    "  leading space", "trailing =", "a=b", ";", "x;", 'say "hi"',
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "x,y;z", 'say "hi";ok',
])
def test_benign_values_pass_unchanged(value):
    assert csv_safe(value) == value


@pytest.mark.parametrize("value", [None, 0, -5, 1.5, True])
def test_non_strings_pass_through(value):
    assert csv_safe(value) is value


@pytest.mark.parametrize("value", [
    "=1", " =1", "a;=b;-c", "\n@x", "plain", "=a;=b", " =a;+b", "=a,b;=c",
    "x,y;=1+2;", "a; +b",
    "x\n=1", "\r\n=1", "a\r\n;\n=b",
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


def test_no_semicolon_cell_starts_with_a_formula_character():
    """Read the export the way a ';'-separator spreadsheet does: split each
    line on ';' only (commas and double quotes are plain characters there),
    and require that no resulting cell starts with a formula character, even
    after the leading whitespace a trimming import drops. A quoted field in a
    later column is the case quoting alone does not protect."""
    import re

    hostile = [
        "x,y;=1+2;", 'x"y;=1+5;', "a,b;=HYPERLINK(CHAR(104),3);",
        "UA;=cmd|'/c calc'!A0", " =1", "p; +2", "q;\t-3", "line\n;@4",
        "x\n=1+2\ny", "m\r\n=HYPERLINK(CHAR(104))", "r\r-1", "s\n =2",
    ]
    body = "".join(_iter_csv(["lead", "v", "tail"], [["lead", v, "tail"] for v in hostile]))
    # Such an import also ends the row at every line break, quoted or not.
    for line in re.split(r"\r\n|\r|\n", body.lstrip("\ufeff"))[1:]:
        for cell in line.split(";"):
            assert not re.match(r'[\s"]*[=+\-@]', cell.replace('"', "")), (line, cell)
