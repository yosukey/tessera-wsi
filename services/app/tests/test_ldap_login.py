"""LDAP login against a schema-bearing directory.

ldap3 validates requested attribute names against the server schema whenever
one is loaded. A user search requesting ``attributes=["dn"]`` with
``get_info=ALL`` fails: ``dn`` is not an attribute type, so against a real
OpenLDAP / AD schema every search raises ``LDAPAttributeError`` and every
login (and the dashboard's connection test) fails. These tests run the real
code paths over ldap3's MOCK_SYNC strategy with the OFFLINE_SLAPD_2_4 schema
forced on, which is what surfaces that failure.
"""
from __future__ import annotations

from types import MappingProxyType

import ldap3
import pytest
from ldap3.core.exceptions import LDAPBindError

from wsi_app import app_auth

BASE_DN = "dc=example,dc=org"
BIND_DN = f"cn=svc,{BASE_DN}"
USER_DN = f"uid=alice,ou=people,{BASE_DN}"


@pytest.fixture
def mock_directory(monkeypatch):
    """Point ldap3.Server / ldap3.Connection at one in-memory directory that
    carries a real slapd schema, whatever get_info the caller asks for."""
    real_server = ldap3.Server
    real_connection = ldap3.Connection
    server = real_server("mock-ldap", get_info=ldap3.OFFLINE_SLAPD_2_4)

    seed = real_connection(server, client_strategy=ldap3.MOCK_SYNC)
    seed.strategy.add_entry(BIND_DN, {"objectClass": "person", "sn": "svc",
                                      "cn": "svc", "userPassword": "svc-pw"})
    seed.strategy.add_entry(USER_DN, {"objectClass": "inetOrgPerson", "sn": "A",
                                      "cn": "alice", "uid": "alice",
                                      "userPassword": "alice-pw"})

    def fake_server(*_a, **_kw):
        return server

    def fake_connection(srv, *a, **kw):
        # MOCK_SYNC skips auto_bind entirely (no real DSA), so emulate what
        # the real sync strategy does with it: bind, and raise on failure.
        auto_bind = kw.pop("auto_bind", False)
        kw["client_strategy"] = ldap3.MOCK_SYNC
        conn = real_connection(srv, *a, **kw)
        if auto_bind in (ldap3.AUTO_BIND_NO_TLS, ldap3.AUTO_BIND_TLS_BEFORE_BIND):
            if not conn.bind():
                raise LDAPBindError("automatic bind not successful")
        return conn

    monkeypatch.setattr(ldap3, "Server", fake_server)
    monkeypatch.setattr(ldap3, "Connection", fake_connection)
    monkeypatch.setattr(
        app_auth,
        "_snapshot",
        app_auth._AuthSnapshot(
            mode="ldap",
            ldap=MappingProxyType({
                "server_uri": "ldap://mock-ldap",
                "bind_dn": BIND_DN,
                "bind_password": "svc-pw",
                "user_base_dn": BASE_DN,
                "user_filter": "(uid={username})",
                "use_starttls": False,
            }),
        ),
    )
    return server


def test_verify_ldap_accepts_correct_password(mock_directory):
    assert app_auth.verify_ldap("alice", "alice-pw") is True


def test_verify_ldap_rejects_wrong_password_and_unknown_user(mock_directory):
    assert app_auth.verify_ldap("alice", "wrong") is False
    assert app_auth.verify_ldap("nobody", "alice-pw") is False


def test_connection_test_reaches_ok(mock_directory):
    res = app_auth.test_ldap_settings("alice", "alice-pw")
    assert res["ok"] is True, res
    assert res["stage"] == "ok"


def test_connection_test_without_user_passes_search(mock_directory):
    res = app_auth.test_ldap_settings()
    assert res["ok"] is True, res


# ---------------------------------------------------------------------------
# TLS to the directory verifies the server certificate
# ---------------------------------------------------------------------------
def _self_signed_pem(tmp_path, host="localhost", with_ip=True):
    import datetime as _dt
    import ipaddress as _ip

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName(
            [x509.DNSName(host)]
            + ([x509.IPAddress(_ip.ip_address("127.0.0.1"))] if with_ip else [])
        ), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "ldap-cert.pem"
    key_path = tmp_path / "ldap-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    return str(cert_path), str(key_path)


@pytest.fixture
def ldaps_listener(tmp_path, request):
    """A TLS listener on 127.0.0.1 presenting a self-signed certificate.

    It only completes the handshake (no LDAP), which is all an ldaps://
    Connection.open() needs to either reject or accept the certificate."""
    import socket
    import ssl
    import threading

    cert, key = _self_signed_pem(tmp_path, with_ip=getattr(request, "param", True))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(4)
    stop = threading.Event()

    def serve():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = sock.accept()
            except OSError:
                continue
            try:
                with ctx.wrap_socket(c, server_side=True) as tc:
                    tc.settimeout(1)
                    try:
                        tc.recv(1)
                    except OSError:
                        pass
            except (OSError, ssl.SSLError):
                pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    yield sock.getsockname()[1], cert
    stop.set()
    t.join(timeout=2)
    sock.close()


def test_ldap_server_requires_certificate_validation():
    import ssl

    server = app_auth._ldap_server("ldaps://ldap.example.org")
    assert server.tls is not None
    assert server.tls.validate == ssl.CERT_REQUIRED
    # StartTLS on an ldap:// URI uses the same Tls object (ldap3 only makes
    # its own CERT_NONE Tls() when server.tls is unset).
    plain = app_auth._ldap_server("ldap://ldap.example.org")
    assert plain.tls is not None and plain.tls.validate == ssl.CERT_REQUIRED


def test_ldaps_rejects_untrusted_certificate(ldaps_listener):
    from ldap3.core.exceptions import LDAPSocketOpenError

    port, _cert = ldaps_listener
    conn = ldap3.Connection(app_auth._ldap_server(f"ldaps://127.0.0.1:{port}"))
    with pytest.raises(LDAPSocketOpenError):
        conn.open()


def test_ldaps_accepts_certificate_from_configured_ca(ldaps_listener, monkeypatch):
    import dataclasses

    port, cert = ldaps_listener
    monkeypatch.setattr(
        app_auth, "_settings",
        dataclasses.replace(app_auth._settings, ldap_ca_certs_file=cert),
    )
    conn = ldap3.Connection(app_auth._ldap_server(f"ldaps://127.0.0.1:{port}"))
    conn.open()
    try:
        assert not conn.closed
    finally:
        conn.unbind()


@pytest.fixture
def py312_hostname_matcher(monkeypatch):
    """Python >= 3.12 has no ssl.match_hostname, so ldap3 uses its backport,
    which ignores IP subjectAltNames. Force that matcher on any interpreter."""
    import ldap3.core.tls as _ldap_tls
    from ldap3.utils import tls_backport as _backport

    monkeypatch.setattr(_ldap_tls, "match_hostname", _backport.match_hostname)
    monkeypatch.setattr(_ldap_tls, "CertificateError", _backport.CertificateError)


def _trusting(monkeypatch, cert):
    import dataclasses

    monkeypatch.setattr(
        app_auth, "_settings",
        dataclasses.replace(app_auth._settings, ldap_ca_certs_file=cert),
    )


def test_ldaps_ip_host_matches_ip_san_with_backport_matcher(
    ldaps_listener, monkeypatch, py312_hostname_matcher
):
    """ldaps://<IP> with a certificate carrying that IP must connect even
    where ldap3's own check can't read IP SANs."""
    port, cert = ldaps_listener
    _trusting(monkeypatch, cert)
    conn = ldap3.Connection(app_auth._ldap_server(f"ldaps://127.0.0.1:{port}"))
    conn.open()
    try:
        assert not conn.closed
    finally:
        conn.unbind()


@pytest.mark.parametrize("ldaps_listener", [False], indirect=True)
def test_ldaps_ip_host_without_ip_san_is_refused(ldaps_listener, monkeypatch, py312_hostname_matcher):
    """The IP path is a real check, not a bypass: a trusted certificate that
    names only 'localhost' does not authenticate 127.0.0.1."""
    from ldap3.core.exceptions import LDAPException

    port, cert = ldaps_listener
    _trusting(monkeypatch, cert)
    conn = ldap3.Connection(app_auth._ldap_server(f"ldaps://127.0.0.1:{port}"))
    with pytest.raises(LDAPException):
        conn.open()
    # ...while the DNS name it does carry still verifies through ldap3.
    ok = ldap3.Connection(app_auth._ldap_server(f"ldaps://localhost:{port}"))
    ok.open()
    try:
        assert not ok.closed
    finally:
        ok.unbind()


def test_verify_ldap_fails_closed_on_missing_ca_file(mock_directory, monkeypatch, tmp_path):
    import dataclasses

    # A configured-but-missing CA bundle must not silently fall back to an
    # unverified (or system-store) connection: the Tls build raises first.
    monkeypatch.setattr(
        app_auth, "_settings",
        dataclasses.replace(app_auth._settings,
                            ldap_ca_certs_file=str(tmp_path / "absent.pem")),
    )
    assert app_auth.verify_ldap("alice", "alice-pw") is False
    res = app_auth.test_ldap_settings()
    assert res["ok"] is False and res["stage"] == "connect"


def test_connection_test_reports_unknown_user_as_not_found(mock_directory):
    """ldap3 returns False for a search that succeeded but matched nothing; that
    is "no such user", not an unreachable base DN."""
    res = app_auth.test_ldap_settings("nobody", "whatever")
    assert res["ok"] is False
    assert res["stage"] == "not_found", res


def test_starttls_is_skipped_for_an_ldaps_uri(mock_directory, monkeypatch):
    """ldap3 refuses STARTTLS on a connection that is already TLS, which would
    fail every login when both an ldaps:// URI and "Use StartTLS" are set."""
    monkeypatch.setattr(ldap3.Server("mock-ldap"), "ssl", True)  # the shared mock server
    patched_connection = ldap3.Connection
    seen = []

    def spy(srv, *a, **kw):
        seen.append(kw.get("auto_bind"))
        return patched_connection(srv, *a, **kw)

    monkeypatch.setattr(ldap3, "Connection", spy)
    cfg = dict(app_auth._snapshot.ldap, use_starttls=True)
    assert app_auth.verify_ldap("alice", "alice-pw", cfg=cfg) is True
    assert seen and ldap3.AUTO_BIND_TLS_BEFORE_BIND not in seen
