"""WSI_PUBLIC_BASE_URL is an origin, read the same way everywhere.

Tessera is served at the root path only. The frontend uses just the origin
of this value; were the app to keep a path verbatim, a value such as
``https://host/wsi`` would give live-sync share links and an OAuth
redirect_uri under ``/wsi/...`` that lead nowhere, beside static share links
without it. The app reduces the value to its origin, as install.sh and
the Makefile do with ``wsi_url_origin`` (infra/lib/wsi-common.sh); the last
test runs both on the same inputs so the two patterns cannot drift apart.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from wsi_app.config import public_base_url_from_env

REPO = Path(__file__).resolve().parents[3]
WSI_COMMON = REPO / "infra" / "lib" / "wsi-common.sh"

# (raw value, expected origin, expected warning?)
CASES = [
    ("https://wsi.example.org", "https://wsi.example.org", False),
    ("https://wsi.example.org/", "https://wsi.example.org", False),
    ("https://wsi.example.org//", "https://wsi.example.org", True),
    ("  https://wsi.example.org  ", "https://wsi.example.org", False),
    ("http://10.0.0.5:8080", "http://10.0.0.5:8080", False),
    ("https://[2001:db8::1]:8443", "https://[2001:db8::1]:8443", False),
    ("https://wsi.example.org/wsi", "https://wsi.example.org", True),
    ("https://wsi.example.org:8443/a/b?x=1#f", "https://wsi.example.org:8443", True),
    ("https://wsi.example.org?x=1", "https://wsi.example.org", True),
    ("ftp://wsi.example.org", "", True),
    ("wsi.example.org", "", True),
    ("https://user@wsi.example.org", "", True),
    ("https://user:pw@wsi.example.org/", "", True),
    ("https://", "", True),
    ("https://wsi.example.org:port", "", True),
    ("https://wsi.example.org:65535", "https://wsi.example.org:65535", False),
    ("https://wsi.example.org:65536", "", True),
]


def test_unset_and_blank_are_empty(monkeypatch, caplog):
    monkeypatch.delenv("WSI_PUBLIC_BASE_URL", raising=False)
    assert public_base_url_from_env() == ""
    monkeypatch.setenv("WSI_PUBLIC_BASE_URL", "   ")
    with caplog.at_level(logging.WARNING, logger="wsi_app.config"):
        assert public_base_url_from_env() == ""
    assert not caplog.records


@pytest.mark.parametrize(("raw", "origin", "warns"), CASES)
def test_reduced_to_origin(monkeypatch, caplog, raw, origin, warns):
    monkeypatch.setenv("WSI_PUBLIC_BASE_URL", raw)
    with caplog.at_level(logging.WARNING, logger="wsi_app.config"):
        assert public_base_url_from_env() == origin
    warned = [r for r in caplog.records if "WSI_PUBLIC_BASE_URL" in r.getMessage()]
    assert bool(warned) is warns


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_shell_helper_agrees():
    """wsi_url_origin prints the same origin, or fails where the app ignores
    the value, for every case above."""
    for raw, origin, _ in CASES:
        proc = subprocess.run(
            ["bash", "-c", '. "$1" && wsi_url_origin "$2"', "_", str(WSI_COMMON), raw],
            capture_output=True, text=True, check=False,
        )
        assert proc.stdout.strip() == origin, raw
        assert (proc.returncode == 0) is bool(origin), raw
