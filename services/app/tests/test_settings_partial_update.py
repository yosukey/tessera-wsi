"""Regression tests for network-settings PATCH partial updates.

The PATCH /api/admin/network/settings handler must honour PATCH semantics:
omitting a field leaves its stored value untouched. The IP-filter fields,
ip_rules and ga_measurement_id must survive a partial update (e.g. one that
only changes analytics_mode) rather than resetting the IP allowlist and the GA
id. These tests pin the preserved-on-omit behaviour.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import delete

from wsi_app import app_auth as _app_auth
from wsi_app import network as _net
from wsi_app.admin_helpers import get_or_create_singleton
from wsi_app.db import SessionLocal
from wsi_app.models import AuthSettings, AuthUser, NetworkSettings


@pytest.fixture(autouse=True)
def _reset_network_settings():
    """Reset the NetworkSettings singleton after each test.

    This suite has no per-test DB wipe, so enabling the IP filter here would
    leak into later tests (a deny-default filter would 403 the loopback test
    client). Restore safe defaults and refresh the in-process network cache.
    """
    yield
    db = SessionLocal()
    try:
        ns = get_or_create_singleton(db, NetworkSettings)
        ns.ip_filter_enabled = False
        ns.ip_filter_default = "allow"
        ns.ip_rules = "[]"
        ns.ga_measurement_id = ""
        db.commit()
        _net.refresh(db)
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _reset_auth_settings():
    """Reset the AuthSettings singleton (and the live snapshot) after each test.

    The auth tests below write login-screen overrides and set
    lan_direct_noauth_mode; without this reset those values would leak into
    other suites via the shared session DB and the in-process app_auth snapshot.
    """
    yield
    db = SessionLocal()
    try:
        s = get_or_create_singleton(db, AuthSettings)
        s.login_message = ""
        s.login_button_label = ""
        s.lan_direct_noauth_mode = "off"
        s.session_ttl_seconds = 86400
        # The auth-mode readiness tests below flip `mode` and the LDAP/Google
        # provider config and create AuthUser rows; reset all of it (and the
        # live snapshot) so nothing leaks into other suites via the shared DB.
        s.mode = "none"
        s.ldap_server_uri = ""
        s.ldap_user_base_dn = ""
        s.google_client_id = ""
        s.google_client_secret = ""
        db.execute(delete(AuthUser))
        db.commit()
        _app_auth.refresh(db)
    finally:
        db.close()


def _seed_network_settings(**kwargs) -> None:
    db = SessionLocal()
    try:
        ns = get_or_create_singleton(db, NetworkSettings)
        for k, v in kwargs.items():
            setattr(ns, k, v)
        db.commit()
    finally:
        db.close()


def _read_network_settings() -> NetworkSettings:
    db = SessionLocal()
    try:
        return get_or_create_singleton(db, NetworkSettings)
    finally:
        db.close()


def test_partial_patch_preserves_ip_rules_and_ga(client, admin_headers):
    rules = [{"ip": "192.168.1.0/24", "action": "allow", "note": "lan"}]
    _seed_network_settings(
        ip_filter_enabled=True,
        ip_filter_default="deny",
        ip_rules=json.dumps(rules),
        ga_measurement_id="G-ABCDEFGHIJ",
    )

    # Submit ONLY analytics_mode — every IP/GA field omitted.
    r = client.patch(
        "/api/admin/network/settings",
        data={"analytics_mode": "plain"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text

    ns = _read_network_settings()
    # The IP allowlist, filter toggle/default and GA id must all survive.
    assert bool(ns.ip_filter_enabled) is True
    assert ns.ip_filter_default == "deny"
    assert json.loads(ns.ip_rules) == rules
    assert ns.ga_measurement_id == "G-ABCDEFGHIJ"
    # The one submitted field still applied.
    assert ns.analytics_mode == "plain"


def test_explicit_empty_ip_rules_still_clears(client, admin_headers):
    # Sending ip_rules explicitly (as an empty array) must still clear them —
    # only the OMITTED case leaves them alone, not an explicit reset.
    _seed_network_settings(
        ip_rules=json.dumps([{"ip": "10.0.0.1", "action": "deny"}]),
    )
    r = client.patch(
        "/api/admin/network/settings",
        data={"ip_rules": "[]"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert json.loads(_read_network_settings().ip_rules) == []


# ---------------------------------------------------------------------------
# Auth-settings: login-screen overrides + direct-LAN level round-trip
# ---------------------------------------------------------------------------
def test_auth_login_overrides_and_lan_direct_roundtrip(client, admin_headers):
    r = client.patch(
        "/api/admin/auth/settings",
        data={
            "login_message": "サインインしてください。",
            "login_button_label": "ログイン",
            "lan_direct_noauth_mode": "lan",
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["login_message"] == "サインインしてください。"
    assert body["login_button_label"] == "ログイン"
    assert body["lan_direct_noauth_mode"] == "lan"

    # GET reflects the persisted values.
    g = client.get("/api/admin/auth/settings", headers=admin_headers)
    assert g.status_code == 200
    assert g.json()["login_message"] == "サインインしてください。"
    assert g.json()["lan_direct_noauth_mode"] == "lan"


def test_auth_lan_direct_rejects_unknown_level(client, admin_headers):
    # Only the three documented levels are accepted; anything else is a 422 so
    # a malformed value can never reach the gate as a silent "on".
    r = client.patch(
        "/api/admin/auth/settings",
        data={"lan_direct_noauth_mode": "yes"},
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text


def test_auth_login_overrides_clear_to_default(client, admin_headers):
    # Clearing the login-screen overrides back to the per-mode default
    # requires the explicit clear_login_* flags. FastAPI collapses an empty
    # form value into the Form() default, so an empty "login_message=" is
    # indistinguishable from an omitted field; a bare empty string cannot
    # clear (that path is "leave alone" to protect partial PATCHes —
    # see test_auth_partial_patch_preserves_login_overrides). The dashboard
    # sends the clear flag when the operator empties and saves the field.
    client.patch(
        "/api/admin/auth/settings",
        data={"login_message": "keep me", "login_button_label": "Enter"},
        headers=admin_headers,
    )
    r = client.patch(
        "/api/admin/auth/settings",
        data={"clear_login_message": "1", "clear_login_button_label": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["login_message"] == ""
    assert r.json()["login_button_label"] == ""


def test_auth_partial_patch_preserves_login_overrides(client, admin_headers):
    # A partial PATCH that omits the login strings (e.g. one
    # that only changes the session TTL) must NOT wipe the public login screen
    # back to its default; the omitted fields must keep their stored values.
    client.patch(
        "/api/admin/auth/settings",
        data={"login_message": "Welcome", "login_button_label": "Sign in"},
        headers=admin_headers,
    )
    r = client.patch(
        "/api/admin/auth/settings",
        data={"session_ttl_seconds": "3600"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["login_message"] == "Welcome"
    assert r.json()["login_button_label"] == "Sign in"
    assert r.json()["session_ttl_seconds"] == 3600


def test_clear_flag_clears_viewer_free_text(client, admin_headers):
    # An empty form value coerces to the Form(None) default, so a bare
    # empty string can't clear a stored free-text field — the companion
    # clear_<field> flag is what actually resets it.
    client.patch(
        "/api/admin/viewer/settings",
        data={"copyright_msg": "© 2026 Example"},
        headers=admin_headers,
    )
    # Empty value alone leaves the stored value untouched (FastAPI quirk).
    r = client.patch(
        "/api/admin/viewer/settings",
        data={"copyright_msg": ""},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    g = client.get("/api/admin/viewer/settings", headers=admin_headers)
    assert g.json()["copyright_msg"] == "© 2026 Example"
    # The explicit clear flag resets it to "".
    r = client.patch(
        "/api/admin/viewer/settings",
        data={"clear_copyright_msg": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    g = client.get("/api/admin/viewer/settings", headers=admin_headers)
    assert g.json()["copyright_msg"] == ""


def test_clear_flag_clears_ga_measurement_id(client, admin_headers):
    # "ga_measurement_id=" coerces to "not sent", so without a clear flag a
    # GA ID once set could never be removed and gtag.js would keep loading
    # on every public page. clear_ga_measurement_id=1 removes it and
    # the public settings stop advertising it.
    _seed_network_settings(ga_measurement_id="G-ABCDEFGHIJ")
    r = client.patch(
        "/api/admin/network/settings",
        data={"ga_measurement_id": ""},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert _read_network_settings().ga_measurement_id == "G-ABCDEFGHIJ"

    r = client.patch(
        "/api/admin/network/settings",
        data={"clear_ga_measurement_id": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["ga_measurement_id"] == ""
    assert _read_network_settings().ga_measurement_id == ""
    pub = client.get("/api/viewer/settings")
    assert pub.status_code == 200
    assert not pub.json().get("ga_measurement_id")


def test_clear_flag_clears_google_allowed_domains(client, admin_headers):
    # Auth settings: same clear-flag contract for the LDAP/Google
    # config fields.
    client.patch(
        "/api/admin/auth/settings",
        data={"google_allowed_domains": "example.com"},
        headers=admin_headers,
    )
    r = client.patch(
        "/api/admin/auth/settings",
        data={"clear_google_allowed_domains": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["google_allowed_domains"] == ""


def test_auth_settings_flags_accept_every_boolean_spelling(client, admin_headers):
    # The auth PATCH parses its flags like the viewer / network PATCHes:
    # "on" (an HTML checkbox's value) counts, as do "1" / "true" / "yes".
    for flag in ("on", "yes", "true", "1"):
        client.patch(
            "/api/admin/auth/settings",
            data={"google_allowed_domains": "example.com"},
            headers=admin_headers,
        )
        r = client.patch(
            "/api/admin/auth/settings",
            data={"clear_google_allowed_domains": flag},
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["google_allowed_domains"] == "", flag
    for raw, expected in (("on", True), ("off", False), ("true", True), ("0", False)):
        r = client.patch(
            "/api/admin/auth/settings",
            data={"ldap_use_starttls": raw},
            headers=admin_headers,
        )
        assert r.json()["ldap_use_starttls"] is expected, raw
    # Omitted leaves it alone.
    r = client.patch(
        "/api/admin/auth/settings", data={"login_button_label": "Go"}, headers=admin_headers,
    )
    assert r.json()["ldap_use_starttls"] is False


def test_ip_filter_default_rejects_invalid_value(client, admin_headers):
    # An out-of-range ip_filter_default must 422 (fail loud) rather than be
    # silently coerced to "allow" (which would fail open, dropping a deny
    # default on a typo). Matches analytics_mode's enum handling.
    r = client.patch(
        "/api/admin/network/settings",
        data={"ip_filter_default": "deby"},
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text
    # A valid value still applies.
    r = client.patch(
        "/api/admin/network/settings",
        data={"ip_filter_default": "deny"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert _read_network_settings().ip_filter_default == "deny"


def test_auth_partial_patch_preserves_lan_direct_level(client, admin_headers):
    # lan_direct_noauth_mode keeps partial-update semantics: a PATCH that omits
    # it (e.g. only changing the session TTL) must not reset the security level.
    client.patch(
        "/api/admin/auth/settings",
        data={"lan_direct_noauth_mode": "ip"},
        headers=admin_headers,
    )
    r = client.patch(
        "/api/admin/auth/settings",
        data={"session_ttl_seconds": "3600"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["lan_direct_noauth_mode"] == "ip"
    assert r.json()["session_ttl_seconds"] == 3600


# ---------------------------------------------------------------------------
# IP-filter self-lockout guard
# ---------------------------------------------------------------------------
def test_ip_filter_self_lockout_refused(client, admin_headers):
    # Enabling the filter with a deny default and a rule set that does NOT cover
    # the requesting admin's own resolved IP would 403 the very next request at
    # the outer middleware (recovery would be loopback-only). The handler must
    # refuse it with a 422 rather than persist the lockout.
    r = client.patch(
        "/api/admin/network/settings",
        data={
            "ip_filter_enabled": "1",
            "ip_filter_default": "deny",
            "ip_rules": json.dumps([{"ip": "10.9.9.9", "action": "allow"}]),
        },
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text
    # A translatable error code naming the IP the guard evaluated (the
    # TestClient peer, as /api/admin/network/my-ip would report it).
    my_ip = client.get("/api/admin/network/my-ip", headers=admin_headers).json()["ip"]
    assert r.json()["detail"] == {
        "code": "adminDash.apiErr.ipFilterSelfLockout",
        "params": {"ip": my_ip},
    }
    # Nothing was persisted: the filter stays off.
    ns = _read_network_settings()
    assert bool(ns.ip_filter_enabled) is False


def test_ip_filter_self_lockout_ipv6_cidr(admin_headers):
    # The guard matches IPv6 CIDRs too (the dashboard's pre-save prediction
    # mirrors it): a deny rule for the admin's /32 is refused, naming the IP;
    # an allow rule for the admin's address above it is the way out.
    from fastapi.testclient import TestClient
    from wsi_app.main import app

    # Loopback peer = the trusted nginx hop, so X-Real-IP is the client IP.
    via_nginx = TestClient(app, client=("127.0.0.1", 12345))
    headers = {**admin_headers, "X-Real-IP": "2001:db8::5"}
    deny_net = {"ip": "2001:db8::/32", "action": "deny"}
    r = via_nginx.patch(
        "/api/admin/network/settings",
        data={"ip_filter_enabled": "1", "ip_filter_default": "allow",
              "ip_rules": json.dumps([deny_net])},
        headers=headers,
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {
        "code": "adminDash.apiErr.ipFilterSelfLockout",
        "params": {"ip": "2001:db8::5"},
    }
    assert bool(_read_network_settings().ip_filter_enabled) is False

    r = via_nginx.patch(
        "/api/admin/network/settings",
        data={"ip_filter_enabled": "1", "ip_filter_default": "allow",
              "ip_rules": json.dumps([{"ip": "2001:db8::5", "action": "allow"}, deny_net])},
        headers=headers,
    )
    assert r.status_code == 200, r.text


def test_ip_filter_self_lockout_override_saves(client, admin_headers):
    # The operator who knows they'll reconnect from an allowed IP can force the
    # save with override_ip_lockout=1.
    r = client.patch(
        "/api/admin/network/settings",
        data={
            "ip_filter_enabled": "1",
            "ip_filter_default": "deny",
            "ip_rules": json.dumps([{"ip": "10.9.9.9", "action": "allow"}]),
            "override_ip_lockout": "1",
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    ns = _read_network_settings()
    assert bool(ns.ip_filter_enabled) is True
    assert ns.ip_filter_default == "deny"


def test_ip_filter_allow_default_not_a_lockout(client, admin_headers):
    # Enabling the filter with an allow default (deny only listed rules) does
    # not lock the admin out — the guard must not fire.
    r = client.patch(
        "/api/admin/network/settings",
        data={
            "ip_filter_enabled": "1",
            "ip_filter_default": "allow",
            "ip_rules": json.dumps([{"ip": "10.9.9.9", "action": "deny"}]),
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert bool(_read_network_settings().ip_filter_enabled) is True


def test_ip_filter_guard_ignores_unrelated_partial_patch(client, admin_headers):
    # A pre-existing deny filter whose rules don't cover the admin must NOT
    # cause a 422 on a partial PATCH that leaves the IP-filter fields untouched
    # (here: only analytics_mode). The guard is scoped to requests that
    # actually change an IP-filter field.
    _seed_network_settings(
        ip_filter_enabled=True,
        ip_filter_default="deny",
        ip_rules=json.dumps([{"ip": "10.9.9.9", "action": "allow"}]),
    )
    r = client.patch(
        "/api/admin/network/settings",
        data={"analytics_mode": "plain"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# auth-mode readiness warning
# ---------------------------------------------------------------------------
def _clear_auth_users():
    db = SessionLocal()
    try:
        db.execute(delete(AuthUser))
        db.commit()
    finally:
        db.close()


def test_auth_mode_basic_without_users_is_refused(client, admin_headers):
    # Under 'basic' the gate challenges every page load, /admin included, and
    # only an AuthUser can answer it, so a switch with zero AuthUser rows would
    # lock the operator out of any dashboard tab not already open: refused,
    # with nothing changed.
    _clear_auth_users()
    r = client.patch("/api/admin/auth/settings", data={"mode": "none"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    before = "none"
    r = client.patch(
        "/api/admin/auth/settings",
        data={"mode": "basic", "login_message": "not saved"},
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "adminDash.apiErr.basicNeedsUser"
    after = client.get("/api/admin/auth/settings", headers=admin_headers).json()
    assert after["mode"] == before
    assert after["login_message"] != "not saved"


def test_last_basic_user_cannot_be_deleted_while_basic(client, admin_headers):
    _clear_auth_users()
    r = client.patch("/api/admin/auth/settings", data={"mode": "none"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    ids = []
    for name in ("first-user", "second-user"):
        u = client.post(
            "/api/admin/auth/users",
            data={"username": name, "password": "longenoughpw"},
            headers=admin_headers,
        )
        assert u.status_code == 200, u.text
        ids.append(u.json()["id"])
    r = client.patch("/api/admin/auth/settings", data={"mode": "basic"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    # Not the last one: fine.
    r = client.delete(f"/api/admin/auth/users/{ids[0]}", headers=admin_headers)
    assert r.status_code == 200, r.text
    # The last one while basic: refused, and the user is kept.
    r = client.delete(f"/api/admin/auth/users/{ids[1]}", headers=admin_headers)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "adminDash.apiErr.basicLastUser"
    names = [u["username"] for u in client.get("/api/admin/auth/users", headers=admin_headers).json()]
    assert names == ["second-user"]
    # Under another mode it can go.
    r = client.patch("/api/admin/auth/settings", data={"mode": "none"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    r = client.delete(f"/api/admin/auth/users/{ids[1]}", headers=admin_headers)
    assert r.status_code == 200, r.text


@pytest.fixture
def _other_worker_before_flush():
    """Run ``action`` (in its own session, as another worker would) once,
    between a handler's pre-checks and its first flush."""
    from sqlalchemy import event

    pending = []

    def _hook(session, flush_context, instances):
        while pending:
            other = SessionLocal()
            try:
                pending.pop()(other)
                other.commit()
            finally:
                other.close()

    event.listen(SessionLocal, "before_flush", _hook)
    try:
        yield pending.append
    finally:
        event.remove(SessionLocal, "before_flush", _hook)


def _add_auth_users(client, admin_headers, *names):
    ids = []
    for name in names:
        u = client.post(
            "/api/admin/auth/users",
            data={"username": name, "password": "longenoughpw"},
            headers=admin_headers,
        )
        assert u.status_code == 200, u.text
        ids.append(u.json()["id"])
    return ids


def test_concurrent_deletes_cannot_empty_basic(client, admin_headers, _other_worker_before_flush):
    # Two workers each delete one of the last two users: both pass the
    # pre-check (each sees the other user); the one that flushes second must
    # re-count under the write lock and refuse.
    _clear_auth_users()
    ids = _add_auth_users(client, admin_headers, "race-a", "race-b")
    r = client.patch("/api/admin/auth/settings", data={"mode": "basic"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    _other_worker_before_flush(lambda db: db.execute(delete(AuthUser).where(AuthUser.id == ids[1])))
    r = client.delete(f"/api/admin/auth/users/{ids[0]}", headers=admin_headers)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "adminDash.apiErr.basicLastUser"
    names = [u["username"] for u in client.get("/api/admin/auth/users", headers=admin_headers).json()]
    assert names == ["race-a"]
    r = client.patch("/api/admin/auth/settings", data={"mode": "none"}, headers=admin_headers)
    assert r.status_code == 200, r.text


def test_switch_to_basic_racing_last_user_delete_is_refused(client, admin_headers, _other_worker_before_flush):
    # The switch's pre-check sees a user; another worker deletes it (its own
    # check saw mode none) before the switch commits: the switch must refuse.
    _clear_auth_users()
    r = client.patch("/api/admin/auth/settings", data={"mode": "none"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    _add_auth_users(client, admin_headers, "race-only")
    _other_worker_before_flush(lambda db: db.execute(delete(AuthUser)))
    r = client.patch(
        "/api/admin/auth/settings",
        data={"mode": "basic", "login_message": "not saved"},
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "adminDash.apiErr.basicNeedsUser"
    after = client.get("/api/admin/auth/settings", headers=admin_headers).json()
    assert after["mode"] == "none"
    assert after["login_message"] != "not saved"
    assert _app_auth.current_mode() == "none"


def test_auth_mode_basic_already_without_users_still_warns(client, admin_headers):
    # A database already in basic with no users (e.g. restored from a backup)
    # is not refused on save — that would block fixing it — but warned about.
    _clear_auth_users()
    db = SessionLocal()
    try:
        get_or_create_singleton(db, AuthSettings).mode = "basic"
        db.commit()
    finally:
        db.close()
    r = client.patch(
        "/api/admin/auth/settings",
        data={"mode": "basic", "login_message": "hello"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["warning"]  # non-empty


def test_auth_mode_basic_with_user_no_warning(client, admin_headers):
    # A basic-mode switch with at least one login user configured is ready.
    u = client.post(
        "/api/admin/auth/users",
        data={"username": "loginuser", "password": "longenoughpw"},
        headers=admin_headers,
    )
    assert u.status_code == 200, u.text
    r = client.patch(
        "/api/admin/auth/settings",
        data={"mode": "basic"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["warning"] == ""


def test_auth_mode_google_oauth_missing_config_is_refused(client, admin_headers):
    # The dashboard sign-in sits behind the gate, so a mode that admits
    # nobody would strand staff too: the switch is refused, nothing changes.
    r = client.patch(
        "/api/admin/auth/settings",
        data={
            "mode": "google_oauth",
            "clear_google_client_id": "1",
            "clear_google_client_secret": "1",
        },
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "adminDash.apiErr.googleNeedsConfig"
    assert _app_auth.current_mode() == "none"


def test_auth_mode_ldap_missing_config_is_refused(client, admin_headers):
    r = client.patch(
        "/api/admin/auth/settings",
        data={"mode": "ldap", "clear_ldap_server_uri": "1", "clear_ldap_user_base_dn": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "adminDash.apiErr.ldapNeedsConfig"
    assert _app_auth.current_mode() == "none"


def test_auth_mode_google_oauth_already_incomplete_still_warns(client, admin_headers):
    # A database already in the mode with incomplete settings (e.g. restored)
    # must stay editable, so a save there is only warned about.
    db = SessionLocal()
    try:
        row = get_or_create_singleton(db, AuthSettings)
        row.mode = "google_oauth"
        row.google_client_id = ""
        db.commit()
    finally:
        db.close()
    r = client.patch(
        "/api/admin/auth/settings",
        data={"mode": "google_oauth", "login_message": "hello"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert "staff included" in r.json()["warning"]


def test_auth_mode_google_oauth_with_config_no_warning(client, admin_headers):
    r = client.patch(
        "/api/admin/auth/settings",
        data={
            "mode": "google_oauth",
            "google_client_id": "cid.apps.googleusercontent.com",
            "google_client_secret": "s3kr3t-value",
            "google_allowed_domains": "example.org",
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["warning"] == ""


def test_auth_mode_google_oauth_without_allowed_domains_is_refused(client, admin_headers):
    # An empty allow-list is fail-closed at sign-in (google_email_allowed
    # rejects everyone), so it is treated like a missing client ID.
    r = client.patch(
        "/api/admin/auth/settings",
        data={
            "mode": "google_oauth",
            "google_client_id": "cid.apps.googleusercontent.com",
            "google_client_secret": "s3kr3t-value",
            "clear_google_allowed_domains": "1",
            "clear_google_allowed_emails": "1",
        },
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "adminDash.apiErr.googleNeedsConfig"


def test_google_allowed_domains_over_limit_is_refused_not_truncated(client, admin_headers):
    # Cutting the joined list at the column width would store the head of
    # the last domain as a domain of its own ("other.edu" out of
    # "other.edu.example.org"), admitting a domain nobody named.
    prefix = ",".join(f"org{i:02d}.example.org" for i in range(27)) + ",xxxxxxxxxxxx.org"
    assert len(prefix) == 502
    before = client.get("/api/admin/auth/settings", headers=admin_headers).json()
    r = client.patch(
        "/api/admin/auth/settings",
        data={"google_allowed_domains": prefix + ",other.edu.example.org"},
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text
    after = client.get("/api/admin/auth/settings", headers=admin_headers).json()
    assert after["google_allowed_domains"] == before["google_allowed_domains"]

    r = client.patch(
        "/api/admin/auth/settings",
        data={"google_allowed_domains": "a" * 254},
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text


def test_auth_mode_google_oauth_with_only_allowed_emails_is_ready(client, admin_headers):
    # An individual address alone is enough to let someone sign in.
    r = client.patch(
        "/api/admin/auth/settings",
        data={
            "mode": "google_oauth",
            "google_client_id": "cid.apps.googleusercontent.com",
            "google_client_secret": "s3kr3t-value",
            "clear_google_allowed_domains": "1",
            "google_allowed_emails": "friend@gmail.com",
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["warning"] == ""


def test_google_allowed_emails_normalized_and_cleared(client, admin_headers):
    r = client.patch(
        "/api/admin/auth/settings",
        data={"google_allowed_emails": " Alice@Gmail.com , ,bob@partner.example.org,alice@gmail.com"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["google_allowed_emails"] == "alice@gmail.com,bob@partner.example.org"
    # A partial PATCH that omits the field leaves it alone.
    r = client.patch(
        "/api/admin/auth/settings",
        data={"session_ttl_seconds": "3600"},
        headers=admin_headers,
    )
    assert r.json()["google_allowed_emails"] == "alice@gmail.com,bob@partner.example.org"
    r = client.patch(
        "/api/admin/auth/settings",
        data={"clear_google_allowed_emails": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["google_allowed_emails"] == ""


@pytest.mark.parametrize(
    "bad", ["gmail.com", "@gmail.com", "alice@", "a@b@c.org", "a b@gmail.com"]
)
def test_google_allowed_emails_rejects_non_addresses(client, admin_headers, bad):
    r = client.patch(
        "/api/admin/auth/settings",
        data={"google_allowed_emails": f"ok@example.org,{bad}"},
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text


def test_google_allowed_emails_over_limit_is_refused_not_truncated(client, admin_headers):
    r = client.patch(
        "/api/admin/auth/settings",
        data={"google_allowed_emails": ",".join(f"user{i:04d}@example.org" for i in range(200))},
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text
    r = client.patch(
        "/api/admin/auth/settings",
        data={"google_allowed_emails": "a" * 250 + "@x.org"},
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text


def test_auth_partial_patch_without_mode_has_empty_warning(client, admin_headers):
    # A partial PATCH that doesn't select a mode must not nag about
    # pre-existing config: warning is empty when `mode` is omitted.
    r = client.patch(
        "/api/admin/auth/settings",
        data={"session_ttl_seconds": "3600"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["warning"] == ""


def test_invalid_ip_rule_is_422_not_500(client, admin_headers):
    """A validator-raised ValueError (e.g. an out-of-range octet) must come
    back as a readable 422, not a 500 from serialising the exception."""
    r = client.patch(
        "/api/admin/network/settings",
        data={"ip_rules": json.dumps([{"ip": "999.1.1.1", "action": "allow"}])},
        headers=admin_headers,
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, list) and "999.1.1.1" in detail[0]["msg"]


def test_single_host_cidr_dedupes_with_bare_address(client, admin_headers):
    """"10.0.0.1" and "10.0.0.1/32" (likewise "::1" and "::1/128") are the same
    rule: only the first survives, so a later deny is not silently shadowed by
    an earlier allow for the same host."""
    rules = [
        {"ip": "10.0.0.1", "action": "allow"},
        {"ip": "10.0.0.1/32", "action": "deny"},
        {"ip": "2001:db8::5/128", "action": "allow"},
        {"ip": "2001:db8::5", "action": "deny"},
        {"ip": "10.0.1.0/24", "action": "allow"},
    ]
    r = client.patch(
        "/api/admin/network/settings",
        data={"ip_rules": json.dumps(rules)},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    stored = json.loads(_read_network_settings().ip_rules)
    assert [(x["ip"], x["action"]) for x in stored] == [
        ("10.0.0.1", "allow"),
        ("2001:db8::5", "allow"),
        ("10.0.1.0/24", "allow"),
    ]


def test_auth_mode_ldap_breaking_a_working_config_is_refused(client, admin_headers):
    # Already in ldap with a working config: a save (no `mode` field) that
    # clears the server URI would strand everyone, staff included.
    db = SessionLocal()
    try:
        row = get_or_create_singleton(db, AuthSettings)
        row.mode = "ldap"
        row.ldap_server_uri = "ldaps://ldap.example.org"
        row.ldap_user_base_dn = "dc=example,dc=org"
        db.commit()
        _app_auth.refresh(db)
    finally:
        db.close()
    r = client.patch(
        "/api/admin/auth/settings", data={"clear_ldap_server_uri": "1"}, headers=admin_headers,
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "adminDash.apiErr.ldapNeedsConfig"
    db = SessionLocal()
    try:
        assert get_or_create_singleton(db, AuthSettings).ldap_server_uri == "ldaps://ldap.example.org"
    finally:
        db.close()
