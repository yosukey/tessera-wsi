"""ANALYTICS_HASH_SALT validation: a weak (short) salt is treated as unset."""
from __future__ import annotations

import importlib
import logging

import pytest

from wsi_app import analytics_hash as _hash


def test_short_salt_is_rejected_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="wsi_app.analytics_hash"):
        assert _hash._validated_salt("x") is None
        assert _hash._validated_salt("a" * 31) is None
    assert "ANALYTICS_HASH_SALT is only 1 characters" in caplog.text


def test_unset_salt_is_none_without_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="wsi_app.analytics_hash"):
        assert _hash._validated_salt(None) is None
        assert _hash._validated_salt("   ") is None
    assert caplog.text == ""


def test_long_enough_salt_is_accepted():
    salt = "a" * 32
    assert _hash._validated_salt(f"  {salt}  ") == salt


@pytest.fixture
def reload_hash(monkeypatch):
    """Re-import analytics_hash under a given environment; restore afterwards."""
    def _reload(salt, insecure):
        monkeypatch.setenv("ANALYTICS_HASH_SALT", salt)
        monkeypatch.setenv("WSI_ALLOW_INSECURE_DEFAULTS", "1" if insecure else "0")
        return importlib.reload(_hash)

    yield _reload
    monkeypatch.undo()
    importlib.reload(_hash)


def test_one_char_salt_is_not_available(reload_hash):
    mod = reload_hash("x", insecure=False)
    assert mod.salt_available() is False
    with pytest.raises(RuntimeError):
        mod.compute_user_hash("a@example.com")


def test_one_char_salt_falls_back_to_ephemeral_in_dev(reload_hash):
    """Dev setups (WSI_ALLOW_INSECURE_DEFAULTS=1) fall back to an ephemeral salt."""
    mod = reload_hash("x", insecure=True)
    assert mod.salt_available() is True
    assert len(mod.compute_user_hash("a@example.com")) == 64


def test_proper_salt_is_available_and_used(reload_hash):
    mod = reload_hash("b" * 64, insecure=False)
    assert mod.salt_available() is True
    assert len(mod.compute_user_hash("a@example.com")) == 64
