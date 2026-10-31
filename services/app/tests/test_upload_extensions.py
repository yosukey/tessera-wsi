"""The default upload allowlist holds single-file WSI formats only.

An upload carries exactly one file, so a multi-file format (MIRAX .mrxs,
Hamamatsu .vms / .vmu: an index file plus companion data files beside it)
could never be stored whole — the multi-GB body would be accepted and the
slide then refused as unreadable. The backend default, the upload form's accept
list and the shipped environment file must agree on that.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from wsi_app.admin_helpers import validated_extension
from wsi_app.config import DEFAULT_WSI_ALLOWED_EXTENSIONS, wsi_allowed_extensions_from_env

REPO = Path(__file__).resolve().parents[3]
MULTI_FILE = (".mrxs", ".vms", ".vmu")
DEFAULT = DEFAULT_WSI_ALLOWED_EXTENSIONS.split(",")
# A line naming the multi-file formats, as a hand-edited /etc/wsi/wsi.env
# may carry (install.sh never regenerates it).
HAND_EDITED_ENV_LINE = ".svs,.ndpi,.tif,.tiff,.mrxs,.vms,.vmu,.scn,.bif,.szi"


def test_default_lists_no_multi_file_format():
    assert not set(MULTI_FILE) & set(DEFAULT)
    assert {".svs", ".ndpi", ".tif", ".tiff", ".szi"} <= set(DEFAULT)


def test_upload_form_accept_list_matches_the_default():
    src = (REPO / "frontend" / "src" / "lib" / "admin" / "validation.ts").read_text(encoding="utf-8")
    m = re.search(r"ACCEPTED_WSI_EXTENSIONS = \[([^\]]*)\]", src)
    assert m
    assert re.findall(r"'([^']+)'", m.group(1)) == DEFAULT


def test_env_example_matches_the_default():
    src = (REPO / "infra" / "wsi.env.example").read_text(encoding="utf-8")
    m = re.search(r"^WSI_ALLOWED_EXTENSIONS=(.*)$", src, re.MULTILINE)
    assert m
    assert m.group(1).strip() == DEFAULT_WSI_ALLOWED_EXTENSIONS


@pytest.fixture
def default_allowlist():
    """Settings is a frozen dataclass read at import (the environment may
    override the default): pin the field to the default, restore afterwards."""
    from wsi_app.config import settings

    saved = settings.wsi_allowed_extensions
    object.__setattr__(settings, "wsi_allowed_extensions", tuple(DEFAULT))
    yield
    object.__setattr__(settings, "wsi_allowed_extensions", saved)


@pytest.mark.parametrize("name", ["slide.mrxs", "slide.vms", "Slide.VMU"])
def test_multi_file_formats_are_refused(name, default_allowlist):
    with pytest.raises(HTTPException) as exc:
        validated_extension(name)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("name", ["slide.svs", "slide.ome.tiff", "slide.szi"])
def test_single_file_formats_are_accepted(name, default_allowlist):
    assert validated_extension(name) == "." + name.rsplit(".", 1)[1]


def test_multi_file_formats_left_in_an_upgraded_env_are_dropped(monkeypatch, caplog):
    monkeypatch.setenv("WSI_ALLOWED_EXTENSIONS", HAND_EDITED_ENV_LINE.replace(".vmu", ".VMU"))
    with caplog.at_level("WARNING", logger="wsi_app.config"):
        allowed = wsi_allowed_extensions_from_env()
    assert list(allowed) == DEFAULT
    warnings = [r for r in caplog.records if r.name == "wsi_app.config"]
    assert len(warnings) == 1
    assert ".mrxs, .vms, .vmu" in warnings[0].getMessage()
    assert "not supported" in warnings[0].getMessage()


def test_env_without_multi_file_formats_is_taken_as_is(monkeypatch, caplog):
    monkeypatch.setenv("WSI_ALLOWED_EXTENSIONS", " .SVS, .tif ,,.szi")
    with caplog.at_level("WARNING", logger="wsi_app.config"):
        assert wsi_allowed_extensions_from_env() == (".svs", ".tif", ".szi")
    assert not [r for r in caplog.records if r.name == "wsi_app.config"]


def test_upgraded_env_refuses_mrxs_and_does_not_list_it(monkeypatch):
    from wsi_app.config import settings

    monkeypatch.setenv("WSI_ALLOWED_EXTENSIONS", HAND_EDITED_ENV_LINE)
    saved = settings.wsi_allowed_extensions
    object.__setattr__(settings, "wsi_allowed_extensions", wsi_allowed_extensions_from_env())
    try:
        with pytest.raises(HTTPException) as exc:
            validated_extension("slide.mrxs")
        assert exc.value.status_code == 400
        assert exc.value.detail["code"] == "adminDash.apiErr.wsiExtensionNotAllowed"
        assert not any(ext in exc.value.detail["params"]["allowed"] for ext in MULTI_FILE)
    finally:
        object.__setattr__(settings, "wsi_allowed_extensions", saved)
