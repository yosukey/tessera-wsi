#!/usr/bin/env python3
"""Report the backend's direct dependencies that have a newer release.

``make lock-upgrade`` (and the monthly lock-refresh workflow) only moves pins
*within* the ranges in requirements.txt / requirements-dev.txt, and every
range there carries a deliberate upper bound, so a release past that bound
(the next major, or the next calendar year for tifffile / imagecodecs) never
shows up by itself. This lists, for each package named in the range files,
the installed (locked) version, the newest release on PyPI, and whether the
current range admits it:

- ``in range``  - the next lock refresh picks it up; nothing to decide.
- ``capped``    - outside the range: test the new release, then move the
                  bounds deliberately and ``make lock``.

Run it from the dev venv (``make outdated`` does), which holds the locked
versions for this interpreter and ``packaging`` (a test-tool dependency the
runtime-only venv of ``make venv`` lacks). Read-only: it queries PyPI through
``pip list --outdated`` and changes nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

try:
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.specifiers import SpecifierSet
    from packaging.utils import canonicalize_name
    from packaging.version import Version
except ImportError:
    sys.exit("outdated.py needs 'packaging', which comes with the dev "
             "dependencies: run it from the venv `make venv-dev` creates.")

APP_DIR = Path(__file__).resolve().parent.parent
RANGE_FILES = ("requirements.txt", "requirements-dev.txt")


def direct_ranges() -> dict[str, SpecifierSet]:
    """Map each package named in the range files to its combined range."""
    ranges: dict[str, SpecifierSet] = {}
    for name in RANGE_FILES:
        for line in (APP_DIR / name).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            try:
                req = Requirement(line)
            except InvalidRequirement:
                continue
            key = canonicalize_name(req.name)
            # A package listed in both files must satisfy both ranges.
            ranges[key] = ranges.get(key, SpecifierSet()) & req.specifier
    return ranges


def outdated() -> dict[str, tuple[str, str]]:
    """Installed packages with a newer release: name -> (installed, latest)."""
    out = subprocess.run(
        [sys.executable, "-m", "pip", "list", "--outdated", "--format=json",
         "--disable-pip-version-check"],
        check=True, capture_output=True, text=True,
    ).stdout
    return {
        canonicalize_name(p["name"]): (p["version"], p["latest_version"])
        for p in json.loads(out)
    }


def main() -> int:
    ranges = direct_ranges()
    rows = []
    for name, (installed, latest) in sorted(outdated().items()):
        spec = ranges.get(name)
        if spec is None:
            continue  # transitive: follows its parent's pins
        status = "in range" if spec.contains(Version(latest)) else "capped"
        rows.append((name, installed, latest, str(spec) or "(any)", status))

    if not rows:
        print("Every direct backend dependency is at its newest release.")
        return 0
    header = ("package", "locked", "latest", "range", "status")
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
    capped = sum(r[4] == "capped" for r in rows)
    if capped:
        print(f"\n{capped} capped: newer than requirements*.txt allows. "
              "Test the release, move the bounds, then `make lock`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
