"""Per-slide image credit (credit_text / license_id / source_url).

Contract:
  * PATCH /api/admin/slides/{id} takes the credit as one ``credit_json``
    object that replaces all three fields; omitting it leaves them alone;
  * invalid values (unknown license, non-http(s) or host-less URL, control
    characters, over-length text, malformed JSON) are a coded 422 that leaves
    the stored credit untouched;
  * the credit is public — echoed by the public list and detail endpoints,
    since the viewer must show it wherever the image is shown;
  * slide export carries it and import restores it, leniently: one invalid
    field imports as "" without failing the slide or dropping the others.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi import HTTPException

from wsi_app import slide_transfer as _st
from wsi_app.models import AuditLog, Slide
from wsi_app.slide_credit import SOURCE_URL_MAX_LEN, clean_source_url

from .test_slide_transfer import _patch_wsi_readers, _stage_one_wsi

# Shared with the frontend's safeSourceUrl test (slideCredit.test.ts), so the
# server stores exactly the source URLs the viewer links.
_URL_CASES = json.loads(
    (Path(__file__).resolve().parents[3] / "frontend" / "src" / "lib" / "slideCredit.sourceUrlCases.json")
    .read_text(encoding="utf-8")
)

CREDIT = {
    "credit_text": "© 2024 Dept. of Pathology",
    "license_id": "CC-BY-4.0",
    "source_url": "https://example.org/slides/1",
}


def _patch_credit(client, headers, slide_id, credit):
    body = credit if isinstance(credit, str) else json.dumps(credit)
    return client.patch(
        f"/api/admin/slides/{slide_id}",
        data={"credit_json": body},
        headers=headers,
    )


def _stored(db_session, slide_id) -> dict:
    db_session.expire_all()
    s = db_session.get(Slide, slide_id)
    return {"credit_text": s.credit_text, "license_id": s.license_id, "source_url": s.source_url}


def test_patch_sets_credit_and_admin_detail_echoes_it(client, admin_headers, slide, db_session):
    r = _patch_credit(client, admin_headers, slide.id, CREDIT)
    assert r.status_code == 200, r.text
    assert _stored(db_session, slide.id) == CREDIT

    d = client.get(f"/api/admin/slides/{slide.id}", headers=admin_headers).json()
    assert {k: d[k] for k in CREDIT} == CREDIT


def test_patch_strips_surrounding_whitespace(client, admin_headers, slide, db_session):
    r = _patch_credit(client, admin_headers, slide.id, {
        "credit_text": "  Courtesy of X  ", "license_id": " CC0-1.0 ", "source_url": " http://x.test/a ",
    })
    assert r.status_code == 200, r.text
    assert _stored(db_session, slide.id) == {
        "credit_text": "Courtesy of X", "license_id": "CC0-1.0", "source_url": "http://x.test/a",
    }


def test_patch_without_credit_json_leaves_credit_alone(client, admin_headers, slide, db_session):
    assert _patch_credit(client, admin_headers, slide.id, CREDIT).status_code == 200
    r = client.patch(
        f"/api/admin/slides/{slide.id}", data={"title": "Renamed"}, headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert _stored(db_session, slide.id) == CREDIT


def test_credit_json_replaces_all_three_fields(client, admin_headers, slide, db_session):
    assert _patch_credit(client, admin_headers, slide.id, CREDIT).status_code == 200
    # A key left out clears that field; null counts as empty too.
    r = _patch_credit(client, admin_headers, slide.id, {"credit_text": "New holder", "source_url": None})
    assert r.status_code == 200, r.text
    assert _stored(db_session, slide.id) == {"credit_text": "New holder", "license_id": "", "source_url": ""}

    assert _patch_credit(client, admin_headers, slide.id, {}).status_code == 200
    assert _stored(db_session, slide.id) == {"credit_text": "", "license_id": "", "source_url": ""}


@pytest.mark.parametrize("credit,code", [
    ({"license_id": "CC-BY-5.0"}, "adminDash.apiErr.invalidLicenseId"),
    ({"license_id": "cc-by-4.0"}, "adminDash.apiErr.invalidLicenseId"),
    ({"source_url": "javascript:alert(1)"}, "adminDash.apiErr.invalidSourceUrl"),
    ({"source_url": "JAVASCRIPT://example.org/%0Aalert(1)"}, "adminDash.apiErr.invalidSourceUrl"),
    ({"source_url": "data:text/html,<script>alert(1)</script>"}, "adminDash.apiErr.invalidSourceUrl"),
    ({"source_url": "ftp://example.org/x"}, "adminDash.apiErr.invalidSourceUrl"),
    ({"source_url": "https://"}, "adminDash.apiErr.invalidSourceUrl"),
    ({"source_url": "example.org/x"}, "adminDash.apiErr.invalidSourceUrl"),
    ({"source_url": "https://example.org/a b"}, "adminDash.apiErr.invalidSourceUrl"),
    ({"source_url": "https://[::1"}, "adminDash.apiErr.invalidSourceUrl"),
    # No host, userinfo (link goes elsewhere than it reads), browser-only
    # backslash host, invisible characters.
    ({"source_url": "https://:80"}, "adminDash.apiErr.invalidSourceUrl"),
    ({"source_url": "https://@"}, "adminDash.apiErr.invalidSourceUrl"),
    ({"source_url": "https://user:pw@example.org/"}, "adminDash.apiErr.invalidSourceUrl"),
    ({"source_url": "https://trusted.example@elsewhere.test/"}, "adminDash.apiErr.invalidSourceUrl"),
    ({"source_url": "https://\\elsewhere.test/"}, "adminDash.apiErr.invalidSourceUrl"),
    ({"source_url": "https://exa\u200bmple.org/"}, "adminDash.apiErr.invalidSourceUrl"),
    ({"source_url": "https://example.org/\ufeff"}, "adminDash.apiErr.invalidSourceUrl"),
    ({"source_url": "https://example.org/\u202egnp.exe"}, "adminDash.apiErr.invalidSourceUrl"),
    ({"source_url": "https://example.org/" + "a" * 1100}, "adminDash.validationMaxLen"),
    ({"credit_text": "line1\nline2"}, "adminDash.validationCtrlChar"),
    ({"credit_text": "x" * 513}, "adminDash.validationMaxLen"),
    ({"credit_text": 5}, "adminDash.apiErr.invalidCredit"),
    (["CC-BY-4.0"], "adminDash.apiErr.invalidCredit"),
    ("not json", "adminDash.apiErr.invalidCredit"),
    # json.loads raises RecursionError here, not ValueError.
    ("[" * 100000, "adminDash.apiErr.invalidCredit"),
])
def test_invalid_credit_is_rejected_and_keeps_stored_value(
    client, admin_headers, slide, db_session, credit, code
):
    assert _patch_credit(client, admin_headers, slide.id, CREDIT).status_code == 200
    r = _patch_credit(client, admin_headers, slide.id, credit)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == code
    assert _stored(db_session, slide.id) == CREDIT


@pytest.mark.parametrize("url", [
    "https://例え.jp/パス",
    "https://example.org/a%20b?q=1#frag",
    "http://192.0.2.10:8080/x",
    "https://[2001:db8::1]/slides",
])
def test_ordinary_source_urls_are_accepted(client, admin_headers, slide, db_session, url):
    r = _patch_credit(client, admin_headers, slide.id, {"source_url": url})
    assert r.status_code == 200, r.text
    assert _stored(db_session, slide.id)["source_url"] == url


@pytest.mark.parametrize("url", _URL_CASES["accept"])
def test_shared_source_url_cases_accepted(url):
    assert clean_source_url(url) == url


@pytest.mark.parametrize("url", _URL_CASES["refuse"])
def test_shared_source_url_cases_refused(url):
    """Includes what a naive check would store but a browser cannot parse (so
    the viewer would show no link): a bad port, a forbidden host character, an
    invalid IPv4 address; and "https:///host", which a browser reads as
    "https://host"."""
    with pytest.raises(HTTPException) as exc:
        clean_source_url(url)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "adminDash.apiErr.invalidSourceUrl"


@pytest.mark.parametrize("url", _URL_CASES["leftToTheBrowser"])
def test_idna_validity_is_left_to_the_browser(url):
    assert clean_source_url(url) == url


def test_source_url_length_is_counted_in_characters():
    base = "https://example.org/"
    # 1024 characters, but 2028 UTF-16 units: the frontend counts the same way.
    astral = base + "🔬" * (SOURCE_URL_MAX_LEN - len(base))
    assert clean_source_url(astral) == astral
    with pytest.raises(HTTPException) as exc:
        clean_source_url(astral + "a")
    assert exc.value.detail == {
        "code": "adminDash.validationMaxLen",
        "params": {"field": "source_url", "max": SOURCE_URL_MAX_LEN, "len": SOURCE_URL_MAX_LEN + 1},
    }


def test_invalid_credit_does_not_apply_the_rest_of_the_patch(client, admin_headers, slide, db_session):
    r = client.patch(
        f"/api/admin/slides/{slide.id}",
        data={"title": "Should not stick", "credit_json": json.dumps({"license_id": "nope"})},
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text
    db_session.expire_all()
    assert db_session.get(Slide, slide.id).title == "Test Slide"


def test_instructor_cannot_set_credit(client, instructor_headers, slide):
    assert _patch_credit(client, instructor_headers, slide.id, CREDIT).status_code == 403


def test_credit_change_is_audited(client, admin_headers, slide, db_session):
    assert _patch_credit(client, admin_headers, slide.id, CREDIT).status_code == 200
    db_session.expire_all()
    row = (
        db_session.query(AuditLog)
        .filter_by(action="slide.update_metadata", target=str(slide.id))
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None
    assert json.loads(row.details)["credit"] == CREDIT


def test_public_list_and_detail_echo_credit(client, admin_headers, slide, db_session, attach_wsi):
    attach_wsi(slide)
    assert _patch_credit(client, admin_headers, slide.id, CREDIT).status_code == 200
    db_session.expire_all()
    s = db_session.get(Slide, slide.id)
    s.is_published = True
    db_session.commit()

    d = client.get(f"/api/slides/{s.public_id}").json()
    assert {k: d[k] for k in CREDIT} == CREDIT

    items = client.get("/api/slides", params={"per_page": 5000}).json()["items"]
    item = next(i for i in items if i["public_id"] == s.public_id)
    assert {k: item[k] for k in CREDIT} == CREDIT


def test_slide_without_credit_serialises_empty_fields(client, admin_headers, slide):
    d = client.get(f"/api/admin/slides/{slide.id}", headers=admin_headers).json()
    assert {k: d[k] for k in CREDIT} == {"credit_text": "", "license_id": "", "source_url": ""}


def test_export_serialises_credit(slide, db_session):
    slide.credit_text = CREDIT["credit_text"]
    slide.license_id = CREDIT["license_id"]
    slide.source_url = CREDIT["source_url"]
    db_session.commit()
    sd = _st._serialize_slide(slide)
    assert {k: sd[k] for k in CREDIT} == CREDIT


def _import(db_session, monkeypatch, pid, extra):
    _patch_wsi_readers(monkeypatch)
    stage_dir = _stage_one_wsi(pid, f"{pid}.svs")
    sd = {
        "public_id": pid, "wsi_original_basename": f"{pid}.svs",
        "title": "T", "catalog_id": "C", "tours": [], **extra,
    }
    try:
        res = _st._import_one_slide(db_session, stage_dir, sd, {}, "copy")
        return _stored(db_session, res["slide_id"])
    finally:
        db_session.rollback()
        shutil.rmtree(stage_dir, ignore_errors=True)


def test_import_restores_credit(db_session, monkeypatch):
    assert _import(db_session, monkeypatch, "credpid", CREDIT) == CREDIT


def test_import_of_a_bundle_without_credit_leaves_it_empty(db_session, monkeypatch):
    assert _import(db_session, monkeypatch, "nocredpid", {}) == {
        "credit_text": "", "license_id": "", "source_url": "",
    }


def test_import_drops_only_the_invalid_credit_field(db_session, monkeypatch):
    got = _import(db_session, monkeypatch, "badcredpid", {
        "credit_text": "Kept holder",
        "license_id": "CC-BY-99",
        "source_url": "javascript:alert(1)",
    })
    assert got == {"credit_text": "Kept holder", "license_id": "", "source_url": ""}

    got = _import(db_session, monkeypatch, "badcredpid2", {
        "credit_text": ["not", "a", "string"],
        "license_id": "CC0-1.0",
        "source_url": "https://example.org",
    })
    assert got == {"credit_text": "", "license_id": "CC0-1.0", "source_url": "https://example.org"}


def test_dropped_credit_fields_are_reported(db_session, monkeypatch):
    """A credit field the import drops (a license id this build does not
    know, a non-http(s) URL, a non-string) is named in the preflight and in
    the result's warnings rather than vanishing silently; an absent or blank
    field is not a drop."""
    assert _st._bundle_credit({"credit_text": "  ", "license_id": None})[1] == []
    assert _st._bundle_credit({"credit_text": ["x"]})[1] == ["credit_text"]

    _patch_wsi_readers(monkeypatch)
    stage_dir = _stage_one_wsi("dropcred", "dropcred.svs")
    sd = {
        "public_id": "dropcred", "wsi_original_basename": "dropcred.svs",
        "title": "T", "catalog_id": "C", "tours": [],
        "credit_text": "Kept holder", "license_id": "CC-BY-99",
        "source_url": "javascript:alert(1)",
    }
    try:
        res = _st._import_one_slide(db_session, stage_dir, sd, {}, "copy")
        assert res["credit_skipped"] == ["license_id", "source_url"]
        warns = [w for w in _st._import_warnings(db_session, [res]) if w["code"] == "importCreditSkipped"]
        assert [w["params"] for w in warns] == [{"field": "license_id"}, {"field": "source_url"}]
        assert all(w["public_id"] == "dropcred" for w in warns)
    finally:
        db_session.rollback()
        shutil.rmtree(stage_dir, ignore_errors=True)
