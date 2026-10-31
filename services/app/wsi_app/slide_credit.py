"""Per-slide image credit: who holds the rights to a WSI and under what terms.

Distinct from ``ViewerSettings.copyright_msg`` (the site operator's own
notice) and from ``SlideAttribute`` (the filterable categories): a slide's
credit names the rights holder of *that image*, so the viewer shows it
whenever the slide is on screen, independent of the metadata-panel toggle.

Three fields on ``Slide``:

* ``credit_text`` — free text shown verbatim, e.g. "© 2024 Dept. of Pathology".
* ``license_id``  — one of ``LICENSE_IDS``; the frontend maps it to the
  license's display name and canonical URL, so no admin-entered URL is
  needed for the common licenses.
* ``source_url``  — optional link to the original source; http(s) only, so a
  stored value can never become a ``javascript:`` link in the viewer.
"""
from __future__ import annotations

import ipaddress
import re
import unicodedata
from typing import Dict, Optional
from urllib.parse import urlsplit

from fastapi import HTTPException

from .admin_helpers import CTRL_CHAR_RE, api_error

# Mirror the String(...) widths on the matching Slide columns.
CREDIT_TEXT_MAX_LEN = 512      # Slide.credit_text
SOURCE_URL_MAX_LEN = 1024      # Slide.source_url

# Accepted license identifiers. "" = not specified. SPDX ids where one exists;
# "PDM" is the Creative Commons Public Domain Mark, "ARR" is "All rights
# reserved", and "custom" means the terms are spelled out in credit_text /
# at source_url. Keep in sync with frontend/src/lib/slideCredit.ts.
LICENSE_IDS = frozenset({
    "",
    "CC-BY-4.0",
    "CC-BY-SA-4.0",
    "CC-BY-NC-4.0",
    "CC-BY-NC-SA-4.0",
    "CC-BY-ND-4.0",
    "CC-BY-NC-ND-4.0",
    "CC0-1.0",
    "PDM",
    "ARR",
    "custom",
})


def clean_credit_text(raw: Optional[str]) -> str:
    """Validate and normalise ``credit_text``; raises a coded 422 if invalid."""
    value = (raw or "").strip()
    if len(value) > CREDIT_TEXT_MAX_LEN:
        raise api_error(
            422, "adminDash.validationMaxLen",
            field="credit_text", max=CREDIT_TEXT_MAX_LEN, len=len(value),
        )
    if CTRL_CHAR_RE.search(value):
        raise api_error(422, "adminDash.validationCtrlChar", field="credit_text")
    return value


def clean_license_id(raw: Optional[str]) -> str:
    """Validate ``license_id`` against ``LICENSE_IDS``; raises a coded 422."""
    value = (raw or "").strip()
    if value not in LICENSE_IDS:
        raise api_error(422, "adminDash.apiErr.invalidLicenseId")
    return value


# host[:port] of an http(s) URL without userinfo: a bracketed IPv6 literal or
# a name, then an optional port of ASCII digits (urlsplit's ``port`` accepts
# "+80" and non-ASCII digits on older Pythons; browsers refuse both).
_NETLOC_RE = re.compile(r"(\[([^\]]*)\]|[^\[\]:]*)(?::([0-9]*))?")

# The WHATWG URL standard's "forbidden domain code points" that survive the
# whole-URL check below (controls and whitespace are refused before): a
# browser refuses to parse a URL whose host contains one, so the viewer could
# never link it.
_FORBIDDEN_HOST_CHARS = frozenset(" #%/:<>?@[\\]^|")


_RADIX_DIGITS = {8: "01234567", 10: "0123456789", 16: "0123456789abcdefABCDEF"}


def _ipv4_number(part: str) -> Optional[int]:
    """WHATWG "IPv4 number parser": decimal, 0x-hex or 0-prefixed octal."""
    if not part:
        return None
    radix = 10
    if part[:2] in ("0x", "0X"):
        part, radix = part[2:], 16
    elif len(part) > 1 and part[0] == "0":
        part, radix = part[1:], 8
    if not part:
        return 0
    if any(c not in _RADIX_DIGITS[radix] for c in part):
        return None
    return int(part, radix)


def _browser_accepts_name(host: str) -> bool:
    """Whether a browser would parse ``host`` (not an IPv6 literal).

    A browser maps the name (UTS 46, which for these checks is NFKC: full-width
    digits and dots become ASCII ones) and then refuses forbidden code points;
    a name whose last label is a number is an IPv4 address and must be a valid
    one ("https://1.2.3.4.5/" does not parse). The IDNA validity of non-ASCII
    and "xn--" labels is left to the browser — the one known difference, which
    only means the viewer shows such a link as plain text.
    """
    name = unicodedata.normalize("NFKC", host).replace("\u3002", ".")
    if not name or any(c in _FORBIDDEN_HOST_CHARS for c in name):
        return False
    labels = name.split(".")
    if len(labels) > 1 and labels[-1] == "":
        labels.pop()
    last = labels[-1]
    if not (last.isascii() and last.isdigit()) and _ipv4_number(last) is None:
        return True
    numbers = [_ipv4_number(label) for label in labels]
    if len(numbers) > 4 or None in numbers:
        return False
    if any(n > 255 for n in numbers[:-1]):
        return False
    return numbers[-1] < 256 ** (5 - len(numbers))


def clean_source_url(raw: Optional[str]) -> str:
    """Validate ``source_url``: empty, or an absolute http(s) URL with a host.

    The viewer only links a URL that ``safeSourceUrl`` in
    frontend/src/lib/slideCredit.ts accepts, which also requires the browser
    to parse it; the rules here refuse what a browser would not parse, so a
    stored link is always shown as one. ``slideCredit.sourceUrlCases.json``
    there holds the cases both sides are tested against.
    """
    value = (raw or "").strip()
    if not value:
        return ""
    if len(value) > SOURCE_URL_MAX_LEN:
        raise api_error(
            422, "adminDash.validationMaxLen",
            field="source_url", max=SOURCE_URL_MAX_LEN, len=len(value),
        )
    invalid = api_error(422, "adminDash.apiErr.invalidSourceUrl")
    # Whitespace and invisible characters (controls, zero-width / bidi format
    # characters) are never intended in a URL and would let a value read
    # differently in the admin form than where the link actually goes.
    # A backslash is read as "/" by browsers but not by urlsplit, so the host
    # checked here would not be the host the link opens.
    if "\\" in value or any(c.isspace() or unicodedata.category(c).startswith("C") for c in value):
        raise invalid
    try:
        parts = urlsplit(value)
        host = parts.hostname
        # A non-numeric or out-of-range (> 65535) port raises ValueError.
        parts.port
    except ValueError:
        raise invalid
    if parts.scheme.lower() not in ("http", "https") or not host:
        raise invalid
    # "https://trusted.example@elsewhere.test/" goes to elsewhere.test; userinfo
    # has no use in a public source link, so refuse it rather than store it.
    if "@" in parts.netloc:
        raise invalid
    m = _NETLOC_RE.fullmatch(parts.netloc)
    if not m:
        raise invalid
    if m.group(2) is not None:
        # IPv6 literal. Browsers refuse a zone id ("%25eth0") and IPvFuture.
        if "%" in m.group(2):
            raise invalid
        try:
            ipaddress.IPv6Address(m.group(2))
        except ValueError:
            raise invalid
    elif not _browser_accepts_name(host):
        raise invalid
    return value


def clean_credit(raw: object) -> Dict[str, str]:
    """Validate a ``{credit_text, license_id, source_url}`` object as a whole.

    A missing key means "empty": the object replaces all three fields, so a
    client clears one by omitting it (or sending ""). Raises a coded 422 on
    any invalid field, or when ``raw`` is not an object of strings.
    """
    if not isinstance(raw, dict):
        raise api_error(422, "adminDash.apiErr.invalidCredit")
    out: Dict[str, str] = {}
    for key, clean in (
        ("credit_text", clean_credit_text),
        ("license_id", clean_license_id),
        ("source_url", clean_source_url),
    ):
        value = raw.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise api_error(422, "adminDash.apiErr.invalidCredit")
        out[key] = clean(value)
    return out


def bundle_credit(sd: dict) -> Dict[str, str]:
    """The credit fields of an imported bundle's slide entry, field by field.

    Lenient where the PATCH endpoint is strict: an import must not fail as a
    whole over one malformed field, so an invalid value (hand-edited bundle,
    or a license id this build does not know) imports as "" and the rest of
    the credit survives.
    """
    out: Dict[str, str] = {}
    for key, clean in (
        ("credit_text", clean_credit_text),
        ("license_id", clean_license_id),
        ("source_url", clean_source_url),
    ):
        value = sd.get(key)
        try:
            out[key] = clean(value) if isinstance(value, str) else ""
        except HTTPException:
            out[key] = ""
    return out
