"""CF Access JWKS client caching (exercises the real ``_jwks_client``;
test_cf_access.py stubs it out for the verification tests)."""
from __future__ import annotations

from wsi_app import cf_access as _cf_access


def test_forced_jwks_refresh_is_rate_limited(monkeypatch):
    """An unknown ``kid`` forces a JWKS refresh, but a flood of tokens with
    made-up kids must not turn into one HTTPS fetch per request."""
    created = []

    class _CountingClient:
        def __init__(self, uri, cache_keys=False):
            created.append(uri)

    monkeypatch.setattr(_cf_access.jwt, "PyJWKClient", _CountingClient)
    monkeypatch.setattr(_cf_access, "_TEAM_DOMAIN", "rl-team.cloudflareaccess.com")
    monkeypatch.setattr(
        _cf_access, "_jwks_cache",
        {"client": None, "fetched_at": 0.0, "team_domain": "", "refreshed_mono": None},
    )
    now = [1000.0]
    monkeypatch.setattr(_cf_access.time, "monotonic", lambda: now[0])

    first = _cf_access._jwks_client()
    assert len(created) == 1
    for _ in range(20):
        assert _cf_access._jwks_client(force_refresh=True) is first
    assert len(created) == 1

    now[0] += _cf_access._JWKS_FORCE_REFRESH_MIN_INTERVAL + 1
    refreshed = _cf_access._jwks_client(force_refresh=True)
    assert refreshed is not first
    assert len(created) == 2
    # ...and the floor applies again from that refresh.
    assert _cf_access._jwks_client(force_refresh=True) is refreshed
    assert len(created) == 2
