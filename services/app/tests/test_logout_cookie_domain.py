"""Logout must clear the domain-scoped session cookie too.

When WSI_SESSION_COOKIE_DOMAIN is configured, maybe_carry_cf_session mints a
domain-scoped wsi_session cookie. Browsers key cookies by (name, domain, path),
so a host-only deletion leaves the domain-scoped one alive until its TTL —
"Log out" would silently not log the CF-carried session out.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from wsi_app import app_auth as core_app_auth


def _set_cookie_headers(response) -> list[str]:
    headers = response.headers
    if hasattr(headers, "get_list"):
        return headers.get_list("set-cookie")
    return [v for k, v in headers.multi_items() if k.lower() == "set-cookie"]


def test_logout_without_domain_emits_single_host_only_deletion(client: TestClient, monkeypatch):
    monkeypatch.setattr(core_app_auth, "SESSION_COOKIE_DOMAIN", "")
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    cookies = _set_cookie_headers(r)
    assert all("domain=" not in c.lower() for c in cookies)


def test_logout_with_domain_also_clears_domain_scoped_cookie(client: TestClient, monkeypatch):
    monkeypatch.setattr(core_app_auth, "SESSION_COOKIE_DOMAIN", ".example.com")
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    cookies = _set_cookie_headers(r)
    # One host-only deletion + one domain-scoped deletion.
    assert any("domain=.example.com" in c.lower() for c in cookies), cookies
    assert any("domain=" not in c.lower() for c in cookies), cookies
