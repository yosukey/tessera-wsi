#!/usr/bin/env python3
"""Set one ``KEY=value`` in an env file (``/etc/wsi/wsi.env``), atomically.

The env-file rewrite shared by infra/set-workers.sh and
infra/set-cache-size.sh (run it with ``python3 -I`` like they do)::

    envfile_set.py ENV_FILE KEY VALUE --tool NAME --heading TEXT
                   [--comment-out OTHER_KEY]

- The first assignment of KEY — a live one, or a commented-out example like
  the template's ``#DZI_CACHE_SIZE=8`` — becomes ``KEY=VALUE``; any further
  ones are dropped, so exactly one live line is left.
- A live assignment of OTHER_KEY is commented out as
  ``#OTHER_KEY=(commented out by NAME)``; set-workers.sh uses this so only one
  of UVICORN_WORKERS / UVICORN_WORKERS_PERCENT is in force.
- With no assignment of KEY at all, ``# TEXT`` and ``KEY=VALUE`` are
  appended.
- The new content goes to a temp file in the same directory, which takes the
  original's mode and is renamed over it, so an interruption never leaves a
  half-written file. An env file that is a symlink (``wsi.env`` kept in a
  config-managed directory) is followed: the file it points to is rewritten
  and the link stays a link, instead of being replaced by a regular file.

VALUE is written verbatim: the callers validate it first.
"""

from __future__ import annotations

import argparse
import os
import re
import tempfile


def matches_key(stripped: str, key: str, tool: str) -> bool:
    """Whether a line (leading blanks removed) is an assignment of ``key``.

    Only a real assignment counts: a bare ``KEY=value`` line, or the commented
    form ``#KEY=value`` / ``# KEY=value`` whose remainder is a single token. A
    documentation line that merely starts with the assignment ("# KEY=16 on a
    4-core host still ...") has a multi-word remainder and is left alone;
    otherwise it would be rewritten into a live setting.
    """
    commented = stripped.startswith("#")
    candidate = stripped.lstrip("#").lstrip() if commented else stripped
    m = re.match(rf"{re.escape(key)}[ \t]*=(.*)$", candidate)
    if m is None:
        return False
    # The marker this tool itself leaves for a key it commented out has a
    # multi-word remainder; recognise it by its exact text so switching back
    # reuses/removes it instead of stacking another one (and another heading)
    # on every round trip.
    if commented and m.group(1).strip() == f"(commented out by {tool})":
        return True
    return not commented or len(m.group(1).split()) <= 1


def rewrite(lines: list[str], key: str, value: str, tool: str, heading: str,
            comment_out: str | None) -> list[str]:
    out: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.lstrip()
        if matches_key(stripped, key, tool):
            if not replaced:
                out.append(f"{key}={value}\n")
                replaced = True
            continue
        if (comment_out and not stripped.startswith("#")
                and matches_key(stripped, comment_out, tool)):
            out.append(f"#{comment_out}=(commented out by {tool})\n")
            continue
        out.append(line)
    if not replaced:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append(f"\n# {heading}\n")
        out.append(f"{key}={value}\n")
    return out


def write_atomic(env_file: str, lines: list[str], keep_owner: bool = True) -> None:
    """Replace ``env_file`` with ``lines`` through a same-directory temp file
    that has the original's mode and, as far as the caller may give it away
    (root), its owner and group: the file is made by whoever runs this, and a
    rewrite must not hand a ``root:wsi`` file, or a symlink's target owned by
    someone else, to root alone.

    A symlinked ``env_file`` is resolved first, so the rename lands on the file
    it points to and the link survives; the temp file is created next to that
    file, which also keeps the rename on one filesystem."""
    target = os.path.realpath(env_file)
    fd, tmp = tempfile.mkstemp(prefix="wsi.env.", dir=os.path.dirname(target))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as t:
            t.writelines(lines)
        st = os.stat(target)
        os.chmod(tmp, st.st_mode & 0o777)
        if keep_owner:
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except PermissionError:
                pass  # not root: the file stays the caller's own
        os.replace(tmp, target)
    except BaseException:
        os.unlink(tmp)
        raise


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("env_file")
    ap.add_argument("key")
    ap.add_argument("value")
    ap.add_argument("--tool", required=True,
                    help="name of the calling script, used in the markers")
    ap.add_argument("--heading", required=True,
                    help="comment written above an appended assignment")
    ap.add_argument("--comment-out", metavar="OTHER_KEY",
                    help="comment out a live assignment of this key")
    args = ap.parse_args()

    with open(args.env_file, encoding="utf-8") as f:
        lines = f.readlines()
    out = rewrite(lines, args.key, args.value, args.tool, args.heading,
                  args.comment_out)
    write_atomic(args.env_file, out)


if __name__ == "__main__":
    main()
