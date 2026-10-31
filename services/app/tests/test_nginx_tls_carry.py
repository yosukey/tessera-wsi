"""infra/lib/nginx_tls_carry.py: what install.sh keeps of a certbot-patched site.

install.sh reinstalls the nginx site file on every run, so the TLS settings
certbot wrote into it (and the HSTS line an operator uncommented) would go
with each update. The helper reads the old file and returns what belongs in the
operator-owned snippet the stock server block includes.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
HELPER = REPO / "infra" / "lib" / "nginx_tls_carry.py"

_spec = importlib.util.spec_from_file_location("nginx_tls_carry", HELPER)
carry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(carry)

# What `certbot --nginx -d example.com` leaves of the stock site (abridged).
CERTBOT_SITE = """\
upstream wsi_app { server 127.0.0.1:8000; keepalive 32; }

server {
    server_name example.com;
    client_max_body_size 2m;
    include /etc/nginx/snippets/wsi-access-log.conf;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options "DENY" always;
    include /etc/nginx/snippets/wsi-locations.conf;

    listen [::]:443 ssl ipv6only=on; # managed by Certbot
    listen 443 ssl; # managed by Certbot
    ssl_certificate /etc/letsencrypt/live/example.com/fullchain.pem; # managed by Certbot
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem; # managed by Certbot
    include /etc/letsencrypt/options-ssl-nginx.conf; # managed by Certbot
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem; # managed by Certbot
}

server {
    if ($host = example.com) {
        return 301 https://$host$request_uri;
    } # managed by Certbot

    listen 80 ;
    listen [::]:80 ;
    server_name example.com;
    return 404; # managed by Certbot
}
"""

STOCK_SITE = """\
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    # add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options "DENY" always;
    location / { proxy_pass http://wsi_app; }
}
"""


def test_a_stock_site_has_nothing_to_carry():
    assert carry.extract(STOCK_SITE) == ("", False)


def test_a_certbot_patched_site_yields_its_tls_settings():
    body, unattributed = carry.extract(CERTBOT_SITE)
    assert not unattributed
    lines = [l for l in body.splitlines() if not l.startswith("#")]
    assert "server_name example.com;" in lines
    assert "listen 443 ssl; # managed by Certbot" in lines
    assert "listen [::]:443 ssl ipv6only=on; # managed by Certbot" in lines
    assert "ssl_certificate /etc/letsencrypt/live/example.com/fullchain.pem; # managed by Certbot" in lines
    assert "ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem; # managed by Certbot" in lines
    assert "include /etc/letsencrypt/options-ssl-nginx.conf; # managed by Certbot" in lines
    assert "ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem; # managed by Certbot" in lines
    # the HSTS line the operator uncommented, and certbot's http -> https redirect
    assert 'add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;' in lines
    # Only for plain HTTP: the stock block listens on 443 too, where certbot's
    # bare `if ($host = ...)` redirect would send HTTPS requests to themselves.
    assert 'set $wsi_tls_redirect "$wsi_xfp:$host";' in lines
    assert 'if ($wsi_tls_redirect = "http:example.com") { return 301 https://$host$request_uri; }' in lines
    assert "if ($host = example.com) { return 301 https://$host$request_uri; }" not in lines


def test_what_the_stock_server_block_already_has_is_not_carried():
    body, _ = carry.extract(CERTBOT_SITE)
    for stray in ("client_max_body_size", "wsi-access-log", "X-Frame-Options", "wsi-locations", "upstream",
                  "listen 80", "return 404"):
        assert stray not in body


def test_a_commented_out_hsts_line_is_not_carried():
    body, _ = carry.extract(CERTBOT_SITE.replace(
        '    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;',
        '    # add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;'))
    assert "Strict-Transport-Security" not in body
    assert "listen 443 ssl" in body


def test_the_wildcard_server_name_is_left_out():
    body, _ = carry.extract(CERTBOT_SITE.replace("server_name example.com;\n    client_max_body_size",
                                                 "server_name _;\n    client_max_body_size"))
    assert not any(l.startswith("server_name") for l in body.splitlines())


def test_hand_written_tls_is_reported_not_carried():
    hand = STOCK_SITE.replace("    server_name _;", "    listen 443 ssl;\n    server_name _;\n"
                              "    ssl_certificate /etc/ssl/mine.pem;\n    ssl_certificate_key /etc/ssl/mine.key;")
    assert carry.extract(hand) == ("", True)


def test_a_commented_ssl_certificate_is_not_tls():
    assert carry.extract(STOCK_SITE.replace("    server_name _;",
                                            "    server_name _;\n    # ssl_certificate /old/path.pem;")) == ("", False)


def test_a_second_certbot_domain_block_is_not_mixed_in():
    two = CERTBOT_SITE + CERTBOT_SITE.replace("example.com", "other.org")
    body, _ = carry.extract(two)
    # One TLS block only (the first): the second domain's certificate and
    # listeners are not folded into the first one's.
    assert body.count("listen 443 ssl;") == 1
    assert "/live/other.org/" not in body
    assert "/live/example.com/" in body


@pytest.mark.parametrize("rc,site,expected_out", [
    (0, STOCK_SITE, ""),
    (0, CERTBOT_SITE, None),
])
def test_cli_prints_the_snippet_and_exits_0(tmp_path, rc, site, expected_out):
    f = tmp_path / "wsi"
    f.write_text(site)
    r = subprocess.run([sys.executable, "-I", str(HELPER), str(f)], capture_output=True, text=True)
    assert r.returncode == rc, r.stderr
    if expected_out is not None:
        assert r.stdout == expected_out
    else:
        assert "listen 443 ssl" in r.stdout


def test_cli_exits_3_for_tls_it_cannot_attribute(tmp_path):
    f = tmp_path / "wsi"
    f.write_text(STOCK_SITE.replace("    server_name _;", "    server_name _;\n    ssl_certificate /etc/ssl/mine.pem;"))
    r = subprocess.run([sys.executable, "-I", str(HELPER), str(f)], capture_output=True, text=True)
    assert r.returncode == 3 and r.stdout == ""


def test_cli_exits_1_for_an_unreadable_file(tmp_path):
    r = subprocess.run([sys.executable, "-I", str(HELPER), str(tmp_path / "absent")], capture_output=True, text=True)
    assert r.returncode == 1


# --- TLS the tool cannot carry is never dropped silently -----------------------
SECOND_DOMAIN = CERTBOT_SITE + '''
server {
    server_name b.example.org;
    listen 443 ssl; # managed by Certbot
    ssl_certificate /etc/letsencrypt/live/b.example.org/fullchain.pem; # managed by Certbot
    ssl_certificate_key /etc/letsencrypt/live/b.example.org/privkey.pem; # managed by Certbot
}
'''

HAND_WRITTEN_SECOND = CERTBOT_SITE + '''
server {
    listen 8443 ssl;
    server_name other.example.org;
    ssl_certificate /etc/ssl/other.pem;
    ssl_certificate_key /etc/ssl/other.key;
}
'''

SNAKEOIL_ONLY = '''
server {
    listen 80 default_server;
    listen 443 ssl default_server;
    include snippets/snakeoil.conf;
    server_name _;
}
'''

EXTRA_LISTENER_IN_CERTBOT_BLOCK = CERTBOT_SITE.replace(
    "listen 443 ssl; # managed by Certbot",
    "listen 443 ssl; # managed by Certbot\n    listen 8443 ssl;", 1)


@pytest.mark.parametrize("site", [SECOND_DOMAIN, HAND_WRITTEN_SECOND, EXTRA_LISTENER_IN_CERTBOT_BLOCK],
                         ids=["a second certbot domain", "a hand-written second block", "a listener certbot did not write"])
def test_tls_beside_the_carried_block_makes_the_site_unattributable(site):
    body, unattributed = carry.extract(site)
    assert unattributed, "the second block's certificate would be dropped with no word"


def test_ubuntus_snakeoil_include_counts_as_tls():
    # `listen 443 ssl; include snippets/snakeoil.conf;` has no ssl_certificate line.
    body, unattributed = carry.extract(SNAKEOIL_ONLY)
    assert unattributed and body == ""


def test_plain_http_sites_are_still_nothing_to_carry():
    body, unattributed = carry.extract("server {\n    listen 80 default_server;\n    server_name _;\n}\n")
    assert (body, unattributed) == ("", False)


# --- directives the tool does not recognise are never dropped silently --------------
CLIENT_CERT_IN_CERTBOT_BLOCK = CERTBOT_SITE.replace(
    "listen 443 ssl; # managed by Certbot",
    "listen 443 ssl; # managed by Certbot\n    ssl_verify_client on;\n    ssl_client_certificate /etc/ssl/ca.pem;", 1)

ONE_LINE_TLS_BLOCK = CERTBOT_SITE + "\nserver { listen 8443 ssl; ssl_certificate /etc/ssl/b.pem; ssl_certificate_key /etc/ssl/b.key; }\n"

UNMARKED_PROTOCOLS = CERTBOT_SITE.replace(
    "listen 443 ssl; # managed by Certbot",
    "listen 443 ssl; # managed by Certbot\n    ssl_protocols TLSv1.2 TLSv1.3;", 1)


@pytest.mark.parametrize("site", [CLIENT_CERT_IN_CERTBOT_BLOCK, ONE_LINE_TLS_BLOCK, UNMARKED_PROTOCOLS],
                         ids=["a client-certificate gate", "a one-line server block", "an unmarked ssl_ directive"])
def test_tls_the_tool_does_not_carry_is_flagged(site):
    _body, unattributed = carry.extract(site)
    assert unattributed


def test_a_one_line_block_does_not_swallow_the_blocks_after_it():
    site = "server { listen 80; server_name a; }\n" + CERTBOT_SITE
    body, unattributed = carry.extract(site)
    assert not unattributed and "listen 443 ssl; # managed by Certbot" in body


def test_semicolons_inside_quotes_do_not_split_a_statement():
    expanded = carry._expand('add_header Content-Security-Policy "default-src \'self\'; img-src data:" always;')
    assert expanded == ['add_header Content-Security-Policy "default-src \'self\'; img-src data:" always;']


def test_the_stock_site_file_is_nothing_to_carry():
    stock = (Path(__file__).resolve().parents[3] / "infra" / "nginx" / "wsi.conf").read_text()
    assert carry.extract(stock) == ("", False)


# --- TLS a naive block scan would miss --------------------------------------------------
HTTP_LEVEL_SSL = "ssl_verify_client on;\nssl_client_certificate /etc/ssl/ca.pem;\n" + CERTBOT_SITE

BRACE_ON_NEXT_LINE = CERTBOT_SITE + """
server
{
    listen 8443 ssl;
    ssl_certificate /etc/ssl/b.pem;
    ssl_certificate_key /etc/ssl/b.key;
}
"""

BRACE_ON_NEXT_LINE_CERTBOT = CERTBOT_SITE.replace("\nserver {\n    server_name example.com;", "\nserver\n{\n    server_name example.com;", 1)


@pytest.mark.parametrize("site", [HTTP_LEVEL_SSL, BRACE_ON_NEXT_LINE, "map $a $b { default 1; }\n" + HTTP_LEVEL_SSL],
                         ids=["ssl_ directives outside any server block", "a server whose { is on the next line",
                              "...after another block"])
def test_tls_the_block_scan_could_miss_is_flagged_even_when_something_is_carried(site):
    body, unattributed = carry.extract(site)
    assert body and unattributed, "certbot's block is carried, so the rest would be dropped silently"


def test_a_certbot_block_with_its_brace_on_the_next_line_is_still_carried():
    body, unattributed = carry.extract(BRACE_ON_NEXT_LINE_CERTBOT)
    assert not unattributed
    assert "listen 443 ssl; # managed by Certbot" in body
    assert "server_name example.com;" in body


def test_a_hash_inside_a_quoted_string_or_after_a_backslash_is_not_a_comment():
    assert carry._split_comment('add_header X "a # b"; # real') == ('add_header X "a # b"; ', "# real")
    assert carry._split_comment(r'add_header X "a \" # b"; # real') == (r'add_header X "a \" # b"; ', "# real")
    assert carry._split_comment("server_name a#b;") == ("server_name a#b;", "")
    # ...so a quote closed by an escape does not hide the statements after it
    expanded = carry._expand(r'return 200 "a \" ; b"; ssl_certificate /x;')
    assert expanded == [r'return 200 "a \" ; b";', "ssl_certificate /x;"]


def test_single_statement_lines_are_carried_exactly_as_written():
    body, _ = carry.extract(CERTBOT_SITE.replace(
        "listen 443 ssl; # managed by Certbot", "listen   443   ssl ;   # managed by Certbot", 1))
    assert "listen   443   ssl ;   # managed by Certbot" in body.splitlines()


def test_cli_names_the_lines_it_cannot_attribute(tmp_path):
    f = tmp_path / "wsi"
    f.write_text(HTTP_LEVEL_SSL)
    r = subprocess.run([sys.executable, "-I", str(HELPER), str(f)], capture_output=True, text=True)
    assert r.returncode == 3 and r.stdout == ""
    assert "ssl_verify_client on;" in r.stderr and "ssl_client_certificate /etc/ssl/ca.pem;" in r.stderr
    assert "ssl_certificate /etc/letsencrypt" not in r.stderr, "certbot's own lines are not the problem"


# --- merging into a snippet the operator has edited -----------------------------------
CARRIED = carry.extract(CERTBOT_SITE)[0]
STOCK_TLS_SNIPPET = (Path(__file__).resolve().parents[3] / "infra" / "nginx" / "wsi-tls.conf").read_text()


def _active(text):
    return [l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]


def test_merge_keeps_every_line_the_operator_wrote_that_certbot_does_not_replace():
    mine = ("# my notes\nadd_header Strict-Transport-Security \"max-age=63072000; includeSubDomains; preload\" always;\n"
            "ssl_verify_client on;\nssl_client_certificate /etc/ssl/ca.pem;\nssl_protocols TLSv1.3;\n")
    merged = carry.merge(mine, CARRIED)
    active = _active(merged)
    for line in _active(mine):
        assert line in active, line
    assert merged.startswith("# my notes\n")
    # ...and the listener and certificates certbot wrote are there
    assert "listen 443 ssl; # managed by Certbot" in merged
    assert "ssl_certificate /etc/letsencrypt/live/example.com/fullchain.pem; # managed by Certbot" in merged
    # the operator's HSTS (their max-age) wins over the one carried from the site
    assert sum("Strict-Transport-Security" in l for l in active) == 1
    assert "63072000" in merged


def test_merge_replaces_only_what_certbot_has_just_written():
    mine = ("listen 443 ssl http2;\n"
            "ssl_certificate /etc/letsencrypt/live/old.example.org/fullchain.pem; # managed by Certbot\n"
            "ssl_certificate_key /etc/letsencrypt/live/old.example.org/privkey.pem; # managed by Certbot\n"
            "ssl_verify_client on;\nlisten 8443 ssl;\n")
    merged = carry.merge(mine, CARRIED)
    active = _active(merged)
    assert "listen 443 ssl http2;" not in active, "the same TCP address certbot listens on"
    assert not any("old.example.org" in l for l in active), "certbot's own earlier pair is replaced"
    assert "ssl_verify_client on;" in active, "the access gate stays"
    assert "listen 8443 ssl;" in active, "a listener certbot did not write stays"
    assert sum(l.startswith("ssl_certificate ") for l in active) == 1
    assert sum(l.startswith("listen 443 ") for l in active) == 1
    # what went is listed, not just gone
    assert "#   listen 443 ssl http2;" in merged
    assert "#   ssl_certificate /etc/letsencrypt/live/old.example.org/fullchain.pem; # managed by Certbot" in merged


def test_merge_keeps_the_operators_own_certificate_pair_and_a_quic_listener():
    # nginx takes an ECDSA and an RSA pair in one block, and a UDP (QUIC) listener
    # on 443 beside the TCP one: neither is replaced by certbot's lines.
    mine = ("listen 443 quic reuseport;\nhttp3 on;\n"
            "ssl_certificate /etc/ssl/ecdsa.pem;\nssl_certificate_key /etc/ssl/ecdsa.key;\n")
    merged = carry.merge(mine, CARRIED)
    active = _active(merged)
    for line in _active(mine):
        assert line in active, line
    assert "listen 443 ssl; # managed by Certbot" in active
    assert sum(l.startswith("ssl_certificate ") for l in active) == 2
    assert "#   " not in merged, "nothing was replaced"


@pytest.mark.parametrize("mine", ["listen 0.0.0.0:443 ssl;\n", "listen *:443 ssl;\n", 'listen "443" ssl;\n',
                                  "listen 443 ssl; listen 8443 ssl;\n", "listen 8443 ssl;listen 443 ssl http2;\n"],
                         ids=["0.0.0.0:443", "*:443", "quoted port", "two statements on a line", "two, no space"])
def test_merge_does_not_leave_a_second_listener_on_the_same_address(mine):
    merged = carry.merge(mine, CARRIED)
    listens = [l for l in _active(merged) if l.startswith("listen ")]
    on_443 = [l for l in listens if "[::]" not in l and re.search(r"(^|[ :\"*])443\b", l)]
    assert len(on_443) == 1, listens
    if "8443" in mine:
        assert any("8443" in l for l in listens), "the operator's other port stays"


def test_merge_does_not_pile_up_notes():
    first = carry.merge("listen 443 ssl http2;\nssl_verify_client on;\n", CARRIED)
    again = carry.merge(first, CARRIED.replace("listen 443 ssl;", "listen 443 ssl default_server;"))
    assert again.count("# Added by install.sh") == 1 and again.count("# These lines of this file were replaced") == 1
    assert "#   listen 443 ssl; # managed by Certbot" in again
    assert "#   listen 443 ssl http2;" not in again, "the earlier note is gone with its merge"
    assert "ssl_verify_client on;" in _active(again)


def test_merge_leaves_bytes_that_are_not_utf8_alone(tmp_path):
    f = tmp_path / "wsi-tls.conf"
    f.write_bytes(b"# caf\xe9\nssl_verify_client on;\n")
    r = subprocess.run([sys.executable, "-I", str(HELPER), "--merge", str(f)], input=CARRIED.encode(), capture_output=True)
    assert r.returncode == 0
    assert r.stdout.startswith(b"# caf\xe9\nssl_verify_client on;\n")
    # ...and a merge with nothing to add gives the file back byte for byte
    r = subprocess.run([sys.executable, "-I", str(HELPER), "--merge", str(f)], input=b"", capture_output=True)
    assert r.stdout == f.read_bytes()


def test_merge_is_idempotent_and_does_not_double_a_line_that_differs_only_in_its_comment():
    once = carry.merge("ssl_verify_client on;\n", CARRIED)
    assert carry.merge(once, CARRIED) == once
    plain = "listen 443 ssl;\nssl_certificate /etc/letsencrypt/live/example.com/fullchain.pem;\n"
    merged = carry.merge(plain, CARRIED)
    assert merged.count("listen 443 ssl;") == 1


def test_merge_with_nothing_new_returns_the_snippet_unchanged():
    assert carry.merge(CARRIED, CARRIED) == CARRIED
    assert carry.merge("# only comments\n", "") == "# only comments\n"


def test_cli_merge_reads_the_carried_text_from_stdin(tmp_path):
    f = tmp_path / "wsi-tls.conf"
    f.write_text("ssl_verify_client on;\n")
    r = subprocess.run([sys.executable, "-I", str(HELPER), "--merge", str(f)], input=CARRIED,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "ssl_verify_client on;" in r.stdout and "listen 443 ssl; # managed by Certbot" in r.stdout
    r = subprocess.run([sys.executable, "-I", str(HELPER), "--merge", str(tmp_path / "absent")], input=CARRIED,
                       capture_output=True, text=True)
    assert r.returncode == 1


# --- the scanner reads quotes and variables the way nginx does -------------------------
def test_a_dollar_brace_variable_is_not_a_block():
    site = CERTBOT_SITE.replace("    server_name example.com;\n",
                                "    server_name example.com;\n    add_header X ${hostname} always;\n", 1)
    body, unattributed = carry.extract(site)
    assert not unattributed
    assert "Strict-Transport-Security" in body, "the lines after the variable are still read"
    assert carry._statements("add_header X ${hostname} always; listen 1;") == ["add_header X ${hostname} always;", " listen 1;"]


def test_a_quote_in_the_middle_of_a_word_does_not_open_a_string():
    site = CERTBOT_SITE.replace("    server_name example.com;\n",
                                "    server_name example.com;\n    add_header X-Note it's; listen 8443 ssl;\n", 1)
    _body, unattributed = carry.extract(site)
    assert unattributed, "the 8443 TLS listener is not dropped silently"
    assert carry._split_comment("a it's # c") == ("a it's ", "# c")
    assert carry._split_comment('add_header X "it\'s # not"; # c') == ('add_header X "it\'s # not"; ', "# c")


@pytest.mark.parametrize("line", ["include snippets/ssl-params.conf;", "include /etc/nginx/snippets/tls-modern.conf;",
                                  "include snippets/snakeoil.conf;"])
def test_an_include_that_pulls_in_tls_settings_is_flagged(line):
    site = CERTBOT_SITE.replace("    server_name example.com;\n", f"    server_name example.com;\n    {line}\n", 1)
    _body, unattributed = carry.extract(site)
    assert unattributed


def test_the_repositorys_own_includes_are_not_tls_evidence():
    assert carry.extract(STOCK_SITE + "include /etc/nginx/snippets/wsi-tls.conf;\n") == ("", False)
    stock = (Path(__file__).resolve().parents[3] / "infra" / "nginx" / "wsi.conf").read_text()
    assert "wsi-tls.conf" in stock and carry.extract(stock) == ("", False)


def test_every_historical_stock_site_file_is_nothing_to_carry():
    """The file install.sh replaces may be any release's own: none of them has TLS."""
    revs = subprocess.run(["git", "log", "--format=%H", "--", "infra/nginx/wsi.conf", "infra/nginx/wsi-tls.conf"],
                          cwd=REPO, capture_output=True, text=True)
    if revs.returncode != 0 or not revs.stdout.split():
        pytest.skip("no git history here")
    for rev in revs.stdout.split():
        shown = subprocess.run(["git", "show", f"{rev}:infra/nginx/wsi.conf"], cwd=REPO, capture_output=True, text=True)
        if shown.returncode != 0:
            continue
        assert carry.extract(shown.stdout) == ("", False), rev
