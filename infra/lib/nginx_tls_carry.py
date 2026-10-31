#!/usr/bin/env python3
"""Carry a certbot-patched site file's TLS settings over to the operator's own
snippet, so that install.sh / update.sh can replace the site file without
taking HTTPS away.

install.sh reinstalls /etc/nginx/sites-available/wsi from the repository on
every run. `certbot --nginx` patches that file (a `listen 443 ssl` and the
certificate paths into the server block, a redirect block beside it), and an
operator adds the HSTS header by hand, so replacing it as-is would silently
revert all of it: nginx would reload without port 443, and a browser that had
seen HSTS would refuse plain HTTP until certbot was run again. The stock server
block therefore includes /etc/nginx/snippets/wsi-tls.conf, which install.sh
creates empty and never overwrites. This reads the OLD site file (the one about
to be replaced) and writes what belongs in that snippet:

* from the server block certbot added `listen ... 443` to: its `listen`,
  `ssl_*` and `include /etc/letsencrypt/...` lines (all marked
  `# managed by Certbot`), an uncommented `Strict-Transport-Security` header,
  and the block's `server_name` (the domain; `_` is left out);
* from certbot's redirect block (`if ($host = example.com) { return 301 ... }`):
  the same redirect for that host, but only for plain HTTP. Certbot's lives in
  a server block of its own that listens on port 80 alone; here it sits in the
  one block that listens on 443 too, where an `if ($host = ...)` alone would
  send HTTPS requests to themselves. The scheme the CLIENT used is
  `$wsi_xfp` (wsi.conf: the listener's scheme, or the one Cloudflare's edge
  reported), so the test is on `"$wsi_xfp:$host"`.

Usage:  nginx_tls_carry.py SITE_FILE
        nginx_tls_carry.py --merge SNIPPET_FILE < CARRIED_TEXT

The first prints the snippet's text on stdout. Exit status:
  0  nothing to carry (no TLS in the file), or the text above was printed;
  3  the file has TLS this tool cannot attribute to certbot -- an `ssl_*`
     directive or a TLS listener anywhere but in the one certbot block it
     carries (hand-written TLS, a second domain, http-level settings): the
     caller must not replace the file silently; the lines are named on stderr
     (not 2, which is what the interpreter itself exits with for a script it
     cannot open);
  1  the file could not be read.

The second prints SNIPPET_FILE's text with the carried lines added to it: the
operator's own lines stay (an HSTS header, `ssl_verify_client`, a certificate
pair of their own, a QUIC listener ...), and only a line the carried text
replaces -- the same `listen` address, the same single-valued `ssl_*` directive,
an earlier certbot certificate pair -- is dropped, then listed in a comment at
the end of the result. Bytes that are not UTF-8 pass through unchanged.

nginx's own reading rules are followed where they matter here: a quote opens a
string, and `#` a comment, only at the start of a word; `${name}` is a variable,
not a block.

Standard library only; reads its inputs and writes nothing.
"""
from __future__ import annotations

import re
import sys
from typing import Dict, List, Optional, Tuple

MARK = "# managed by Certbot"

_LISTEN_443 = re.compile(r"^listen\b[^;]*\b443\b")
_CARRIED = re.compile(r"^(listen|ssl_[a-z0-9_]+|include\s+/etc/letsencrypt/)\b")
_SERVER_NAME = re.compile(r"^server_name\s+([^;]+);")
_HSTS = re.compile(r"^add_header\s+Strict-Transport-Security\b[^;]*;", re.IGNORECASE)
_REDIRECT_IF = re.compile(r"^if\s*\(\s*\$host\s*=\s*([A-Za-z0-9.*-]+)\s*\)\s*\{")
# An active line that makes a server block serve TLS or shapes it: any `ssl_*`
# directive (a certificate, but also `ssl_verify_client`, which is an access
# gate, or `ssl_protocols`), or a `listen` on 443 / with `ssl` (Ubuntu's own
# `listen 443 ssl; include snippets/snakeoil.conf;` has no ssl_certificate line
# at all). Certbot marks the ones it writes; one that is not marked is somebody
# else's, and silently dropping it is not on.
#
# An `include` of a file whose name says ssl / tls / snakeoil
# (`include snippets/ssl-params.conf;`) pulls such settings in the same way; the
# repository's own `wsi-*` snippets are not that.
_TLS_EVIDENCE = re.compile(
    r"^(ssl_[a-z0-9_]+\b|listen\b[^;]*(\b443\b|\bssl\b)"
    r"|include\s+(?!(?:/etc/nginx/)?snippets/wsi-)\S*(ssl|tls|snakeoil)\S*)")


def _token_start(s: str, i: int) -> bool:
    """Whether ``s[i]`` begins a token: nginx opens a quote, or a comment, only
    there (``it's`` and ``a#b`` are plain words)."""
    return i == 0 or s[i - 1].isspace() or s[i - 1] in ";{}"


def _variable_end(s: str, i: int) -> int:
    """``s[i:]`` starts with ``${``: the index just after its ``}`` (a variable,
    not a block), or ``i + 1`` if there is none."""
    j = s.find("}", i + 2)
    return j + 1 if j != -1 else i + 1


def _split_comment(raw: str) -> Tuple[str, str]:
    """``(code, comment)`` of one line. A comment starts at a ``#`` that begins
    a token outside quotes and not escaped by a backslash, as nginx reads it."""
    quote = ""
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\":
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'" and _token_start(raw, i):
            quote = ch
        elif ch == "$" and raw.startswith("${", i):
            i = _variable_end(raw, i)
            continue
        elif ch == "#" and _token_start(raw, i):
            return raw[:i], raw[i:]
        i += 1
    return raw, ""


def _code(raw: str) -> str:
    return _split_comment(raw)[0].strip()


def _statements(code: str) -> List[str]:
    """``code`` cut after each ``;`` ``{`` ``}`` that is outside quotes and is
    not part of a ``${variable}``."""
    pieces: List[str] = []
    cur: List[str] = []
    quote = ""
    i = 0
    while i < len(code):
        ch = code[i]
        if ch == "\\" and i + 1 < len(code):
            cur.append(code[i:i + 2])
            i += 2
            continue
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'" and _token_start(code, i):
            cur.append(ch)
            quote = ch
        elif ch == "$" and code.startswith("${", i):
            j = _variable_end(code, i)
            cur.append(code[i:j])
            i = j
            continue
        else:
            cur.append(ch)
            if ch in ";{}":
                pieces.append("".join(cur))
                cur = []
        i += 1
    pieces.append("".join(cur))
    return [p for p in pieces if p.strip()]


def _expand(text: str) -> List[str]:
    """The lines of ``text``, with a line that holds several statements or a
    brace and a statement (``server { listen 8443 ssl; ssl_certificate x; }``)
    split into one line per statement, outside quotes. A line with a single
    statement or brace is kept as it is -- carried lines are written verbatim --
    and a trailing comment goes with every piece it was split into."""
    out: List[str] = []
    for raw in text.splitlines():
        code, comment = _split_comment(raw)
        pieces = _statements(code)
        if len(pieces) <= 1:
            out.append(raw)
            continue
        for p in pieces:
            out.append(p.strip() + ((" " + comment) if comment else ""))
    return out


def _server_blocks(text: str) -> List[List[str]]:
    """The lines of each top-level ``server { ... }`` block, braces tracked.

    A site file is included at http level, so its server blocks are top level.
    Only the lines at the block's own depth matter to the callers, so nested
    ``location`` / ``if`` contents are kept in the list but flagged by depth:
    each entry is (depth, stripped line) packed as "depth\\tline". The ``{``
    may be on the line after ``server``."""
    blocks: List[List[str]] = []
    current: Optional[List[str]] = None
    depth = 0
    pending = False  # a line that is just `server`: its `{` follows
    for raw in _expand(text):
        line = raw.strip()
        code = _code(line)
        if current is None:
            if pending:
                if not code:
                    continue
                pending = False
                if code == "{":
                    current, depth = [], 1
                    continue
            if re.match(r"^server\s*\{$", code):
                current, depth = [], 1
            elif code == "server":
                pending = True
            continue
        current.append(f"{depth}\t{line}")
        # (_expand leaves at most one brace per line, at its end or alone)
        depth += (1 if code.endswith("{") else 0) - (1 if code.startswith("}") else 0)
        if depth <= 0:
            blocks.append(current)
            current = None
    if current is not None:
        blocks.append(current)
    return blocks


def _own_lines(block: List[str]) -> List[str]:
    return [entry.split("\t", 1)[1] for entry in block if entry.startswith("1\t")]


def _all_lines(block: List[str]) -> List[str]:
    return [entry.split("\t", 1)[1] for entry in block]


def analyse(text: str) -> Tuple[str, List[str]]:
    """``(snippet text, leftover)``: the text to put in wsi-tls.conf ("" if the
    file carries no certbot TLS), and the active TLS lines of the file that the
    snippet does not hold -- an ``ssl_*`` directive or a TLS listener that
    certbot did not write, in certbot's block or in any other server block, at
    http level, or a second certbot block. Dropping those would switch TLS off
    (or an access gate such as ``ssl_verify_client``) without a word, so the
    caller must not replace the file while there are any."""
    carried: List[str] = []
    names: List[str] = []
    redirects: List[str] = []
    found_tls_block = False

    for block in _server_blocks(text):
        own = _own_lines(block)
        if not found_tls_block and any(MARK in l and _LISTEN_443.match(_code(l)) for l in own):
            found_tls_block = True
            for l in own:
                code = _code(l)
                if MARK in l and _CARRIED.match(code):
                    carried.append(l)
                elif _HSTS.match(code):
                    carried.append(code)
                else:
                    m = _SERVER_NAME.match(code)
                    if m:
                        names.extend(n for n in m.group(1).split() if n != "_")
        # certbot's redirect: `if ($host = example.com) { return 301 ... } # managed by Certbot`
        lines = _all_lines(block)
        for i, l in enumerate(lines):
            m = _REDIRECT_IF.match(_code(l))
            if not m:
                continue
            window = " ".join(lines[i:i + 4])
            if "return 301 https://" in window and MARK in window:
                redirects.append(m.group(1))

    # Every active TLS line of the file, in order, less the ones carried.
    credit: Dict[str, int] = {}
    for l in carried:
        code = _code(l)
        if _TLS_EVIDENCE.match(code):
            credit[code] = credit.get(code, 0) + 1
    leftover: List[str] = []
    for l in _expand(text):
        code = _code(l)
        if not _TLS_EVIDENCE.match(code):
            continue
        if credit.get(code, 0) > 0:
            credit[code] -= 1
        else:
            leftover.append(code)

    if not (carried or names or redirects):
        return "", leftover

    out = [
        "# TLS settings for the wsi site, carried over by install.sh from the site file",
        "# certbot had patched. This file is yours: install.sh and update.sh create it",
        "# when it is missing and never overwrite it, so HTTPS survives updates. See",
        "# the TLS section of QUICKSTART_FOR_UBUNTU.md.",
    ]
    seen = set()
    for n in names:
        if n not in seen:
            seen.add(n)
    if seen:
        out.append("server_name " + " ".join(sorted(seen)) + ";")
    out.extend(dict.fromkeys(carried))
    if redirects:
        out.append('set $wsi_tls_redirect "$wsi_xfp:$host";')
    for host in dict.fromkeys(redirects):
        out.append(f'if ($wsi_tls_redirect = "http:{host}") {{ return 301 https://$host$request_uri; }}')
    return "\n".join(out) + "\n", leftover


def extract(text: str) -> Tuple[str, bool]:
    """``(snippet text, unattributed)``: see :func:`analyse`."""
    body, leftover = analyse(text)
    return body, bool(leftover)


def _norm(code: str) -> str:
    """A statement as one comparable string: no comment, one space between
    words, none before the ``;``."""
    return re.sub(r"\s+;", ";", " ".join(_code(code).split()))


def _listen_address(arg: str) -> str:
    """The address of a ``listen`` as nginx compares them: ``443``, ``*:443``
    and ``0.0.0.0:443`` are one listener ("a duplicate listen 0.0.0.0:443")."""
    arg = arg.strip("\"'")
    if arg.isdigit():
        return "0.0.0.0:" + arg
    if arg.startswith("*:"):
        return "0.0.0.0:" + arg[2:]
    return arg


def _key(norm: str) -> Optional[Tuple[str, ...]]:
    """What a single-statement line sets, for the lines a carry replaces: a
    ``listen`` address (a QUIC listener is another one: UDP), an ``ssl_*``
    directive, the HSTS header. ``None`` for anything else (kept as it is)."""
    if not norm.endswith(";") or len(_statements(norm)) != 1:
        return None
    words = norm[:-1].split()
    if len(words) >= 2 and words[0] == "listen":
        key = ("listen", _listen_address(words[1]))
        return key + ("quic",) if "quic" in words[2:] else key
    m = re.match(r"^(ssl_[a-z0-9_]+)\b", norm)
    if m:
        return (m.group(1),)
    if _HSTS.match(norm):
        return ("hsts",)
    return None


# nginx takes several of these in one server block (an ECDSA and an RSA pair).
_MULTI_VALUED = ("ssl_certificate", "ssl_certificate_key")


def _replaceable(raw: str, norm: str) -> bool:
    """Whether a line the operator has may be dropped for a carried line that
    sets the same thing. A certificate pair is replaced only if it is certbot's
    (an older path of its own: marked, or under /etc/letsencrypt/); the
    operator's own pair stays beside it."""
    key = _key(norm)
    if key is not None and key[0] in _MULTI_VALUED:
        return MARK in raw or "/etc/letsencrypt/" in norm
    return True


_ADDED = "# Added by install.sh from the site file certbot patched:"
_REPLACED = "# These lines of this file were replaced by the ones above:"


def _units(text: str) -> List[Tuple[str, List[str]]]:
    """``(line, statements)`` for each line of ``text``: the statements are the
    line itself, or -- for a line of several statements with no braces
    (``listen 443 ssl; listen 8443 ssl;``) -- one per statement."""
    units = []
    for line in text.splitlines():
        code = _code(line)
        pieces = _expand(line) if code and "{" not in code and "}" not in code else [line]
        units.append((line, pieces if len(pieces) > 1 else [line]))
    return units


def merge(existing: str, carried: str) -> str:
    """``existing`` (the operator's wsi-tls.conf) with the lines of ``carried``
    added. Everything the operator wrote stays, except a line that sets what a
    carried line sets -- the same ``listen`` address, the same ``ssl_*``
    directive (certbot has just written those) -- which is dropped and listed in
    a comment at the end; a certificate pair is replaced only if it is itself
    certbot's, and a QUIC listener is not the TCP one. A carried line already
    there (same words, any comment) is not added again; an HSTS header the
    operator has is not replaced by the carried one. The result is ``existing``
    itself when nothing changes."""
    units = _units(existing)
    present = {_norm(p) for _, ps in units for p in ps if _code(p)}
    present_keys = {_key(n) for n in present}
    present_keys.discard(None)
    c_lines = [l.strip() for l in carried.splitlines() if _code(l)]
    carried_norms = {_norm(l) for l in c_lines}

    add: List[str] = []
    replaced = set()
    added = set()
    for l in c_lines:
        n = _norm(l)
        if n in present or n in added:
            continue
        k = _key(n)
        if k == ("hsts",) and k in present_keys:
            continue
        if k is not None:
            replaced.add(k)
        added.add(n)
        add.append(l)

    def droppable(piece: str) -> bool:
        n = _norm(piece)
        return bool(n) and n not in carried_norms and _key(n) in replaced and _replaceable(piece, n)

    out: List[str] = []
    dropped: List[str] = []
    for line, pieces in units:
        if len(pieces) == 1:
            if droppable(line):
                dropped.append(line.strip())
            else:
                out.append(line)
            continue
        kept = [p for p in pieces if not droppable(p)]
        if len(kept) == len(pieces):
            out.append(line)
        else:
            out.extend(kept)
            dropped.extend(p.strip() for p in pieces if droppable(p))
    if not add and not dropped:
        return existing
    # The notes of an earlier merge are stale by now: drop them (the copy of the
    # file beside it has the whole history) and write the current ones.
    fresh: List[str] = []
    skipping = False
    for line in out:
        if line == _REPLACED:
            skipping = True
            continue
        if skipping and line.startswith("#   "):
            continue
        skipping = False
        if line != _ADDED:
            fresh.append(line)
    while fresh and not fresh[-1].strip():
        fresh.pop()
    fresh.append("")
    if add:
        fresh.append(_ADDED)
        fresh.extend(add)
    if dropped:
        fresh.append(_REPLACED)
        fresh.extend("#   " + d for d in dropped)
    return "\n".join(fresh) + "\n"


def _eprint(text: str) -> None:
    sys.stderr.buffer.write((text + "\n").encode("utf-8", "replace"))


def _read(path: str) -> str:
    # (surrogateescape: bytes that are not UTF-8 come back out as they went in)
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as fh:
        return fh.read()


def main(argv: List[str]) -> int:
    if len(argv) == 3 and argv[1] == "--merge":
        try:
            existing = _read(argv[2])
        except OSError as exc:
            _eprint(f"error: cannot read {argv[2]}: {exc}")
            return 1
        carried = sys.stdin.buffer.read().decode("utf-8", "surrogateescape")
        sys.stdout.buffer.write(merge(existing, carried).encode("utf-8", "surrogateescape"))
        return 0
    if len(argv) != 2:
        _eprint(__doc__ or "")
        return 1
    try:
        text = _read(argv[1])
    except OSError as exc:
        _eprint(f"error: cannot read {argv[1]}: {exc}")
        return 1
    body, leftover = analyse(text)
    if leftover:
        _eprint("TLS settings in this file that are not certbot's, or not in its one server block:")
        for l in leftover[:8]:
            _eprint("    " + l)
        if len(leftover) > 8:
            _eprint(f"    ... and {len(leftover) - 8} more")
        return 3
    sys.stdout.buffer.write(body.encode("utf-8", "surrogateescape"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
