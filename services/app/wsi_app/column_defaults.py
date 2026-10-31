"""Render an ORM column's constant default as a SQL literal.

Shared by the boot-time reconciler (``main._column_add_clause``, which needs a
``DEFAULT`` for ``ALTER TABLE ADD COLUMN``) and ``tools/normalize_live_db.py``
(which back-fills NULL cells with the column's own default), so the two can
never disagree on what a column's default *is*. Dependency-free on purpose:
the normaliser imports it without loading any app configuration.
"""
from __future__ import annotations

import re
from typing import Optional

_NUMERIC_LITERAL_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?\Z")
# SQL constants that must reach the DDL unquoted to keep their meaning.
_SQL_CONSTANT_KEYWORDS = frozenset(
    {"NULL", "TRUE", "FALSE", "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP"}
)


def as_sql_literal(text: str) -> str:
    """Render a ``server_default`` fragment as a DDL-safe literal.

    Emitting a bare word like ``instructor`` (Users.role's server_default)
    verbatim would only work because SQLite quietly reinterprets an
    unresolvable identifier in a DEFAULT clause as a string, and a
    server_default containing a space or an apostrophe emitted the same way
    would be a syntax error at ALTER TABLE time — i.e. every worker failing
    to boot. So quote anything not positively recognised as a numeric
    literal, an already-quoted string, or a SQL constant keyword. (A SQL
    numeric literal, not whatever Python's ``float()`` accepts: ``nan``,
    ``inf`` and ``1_0`` are strings to SQLite.)
    """
    if text.upper() in _SQL_CONSTANT_KEYWORDS:
        return text
    if len(text) >= 2 and text.startswith("'") and text.endswith("'"):
        return text
    if _NUMERIC_LITERAL_RE.match(text):
        return text
    return "'" + text.replace("'", "''") + "'"


def column_default_literal(col) -> Optional[str]:
    """SQL literal of ``col``'s constant default, or None if it has none.

    A ``server_default`` that is a function call (``now()``) or empty is not a
    constant; a Python-side scalar ``default`` (e.g. ``default=""``) still
    counts. Callables (``_mint_public_id``) and SQL expressions do not.
    """
    sd = col.server_default
    if sd is not None:
        text = getattr(sd.arg, "text", None)
        if text is None:
            text = str(sd.arg)
        text = text.strip()
        if text and "(" not in text:
            return as_sql_literal(text)
        # Empty string or function call (now(), CURRENT_TIMESTAMP() variants,
        # etc.) — SQLite won't accept these as DEFAULT in ALTER TABLE. Fall
        # through to col.default so a Python-side scalar (e.g. default="")
        # still produces a properly quoted literal.
    if col.default is not None and getattr(col.default, "is_scalar", False):
        val = col.default.arg
        if isinstance(val, bool):
            return "1" if val else "0"
        if isinstance(val, (int, float)):
            return str(val)
        if isinstance(val, str):
            return "'" + val.replace("'", "''") + "'"
    return None
