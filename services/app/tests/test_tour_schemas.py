"""Validation tests for the tour pin/shape schemas (PinItem, Shape).

Labels are single-line annotation captions capped at the Step.label column
width; ids back String(64) columns. Both reject overlong / control-char input
at the schema boundary so an oversized value 422s instead of being silently
truncated (a debugging trap) or bloating the SQLite row.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from wsi_app.schemas import (
    LAYER_PIN_BASE, PinItem, Shape, _MAX_LAYER_Z,
)


def test_pin_label_over_255_is_rejected():
    PinItem(id="p1", x=0.0, y=0.0, label="a" * 255)  # boundary ok
    with pytest.raises(ValidationError):
        PinItem(id="p1", x=0.0, y=0.0, label="a" * 256)


def test_shape_label_over_255_is_rejected():
    Shape(id="s1", x=0.0, y=0.0, w=1.0, h=1.0, label="a" * 255)  # boundary ok
    with pytest.raises(ValidationError):
        Shape(id="s1", x=0.0, y=0.0, w=1.0, h=1.0, label="a" * 256)


def test_pin_and_shape_labels_reject_control_chars():
    with pytest.raises(ValidationError):
        PinItem(id="p1", x=0.0, y=0.0, label="line1\nline2")
    with pytest.raises(ValidationError):
        Shape(id="s1", x=0.0, y=0.0, w=1.0, h=1.0, label="a\x00b")


def test_pin_and_shape_ids_reject_overlong_values():
    # pin_id / shape_id are String(64) columns; an oversized id must 422 at the
    # schema boundary rather than bloat the SQLite row.
    with pytest.raises(ValidationError):
        PinItem(id="p" * 65, x=0.0, y=0.0)
    with pytest.raises(ValidationError):
        Shape(id="s" * 65, x=0.0, y=0.0, w=1.0, h=1.0)
    # A 64-char id is accepted.
    assert PinItem(id="p" * 64, x=0.0, y=0.0).id == "p" * 64
    assert Shape(id="s" * 64, x=0.0, y=0.0, w=1.0, h=1.0).id == "s" * 64


def test_z_defaults_put_pins_above_shapes():
    # With no explicit z, a pin defaults into the pin band and a shape to 0, so
    # pins sit above shapes — the desired default stacking.
    assert PinItem(id="p1", x=0.0, y=0.0).z == LAYER_PIN_BASE
    assert Shape(id="s1", x=0.0, y=0.0, w=1.0, h=1.0).z == 0
    assert PinItem(id="p1", x=0.0, y=0.0).z > Shape(id="s1", x=0.0, y=0.0, w=1.0, h=1.0).z


def test_z_accepts_explicit_values_and_rejects_out_of_range():
    assert PinItem(id="p1", x=0.0, y=0.0, z=-5).z == -5
    assert Shape(id="s1", x=0.0, y=0.0, w=1.0, h=1.0, z=42).z == 42
    # Boundary values are accepted; anything beyond the cap 422s.
    assert PinItem(id="p1", x=0.0, y=0.0, z=_MAX_LAYER_Z).z == _MAX_LAYER_Z
    with pytest.raises(ValidationError):
        PinItem(id="p1", x=0.0, y=0.0, z=_MAX_LAYER_Z + 1)
    with pytest.raises(ValidationError):
        Shape(id="s1", x=0.0, y=0.0, w=1.0, h=1.0, z=-_MAX_LAYER_Z - 1)


def test_pin_shape_is_a_closed_set():
    # Mirrors the frontend PinShape union ('pin' | 'circle' | 'arrow'), like
    # Shape.kind; the model default is "circle" (the StepPin column default).
    for shape in ("pin", "circle", "arrow"):
        assert PinItem(id="p1", x=0.0, y=0.0, shape=shape).shape == shape
    assert PinItem(id="p1", x=0.0, y=0.0).shape == "circle"
    # "" has always meant "the default" to the writers (`p.shape or "circle"`).
    assert PinItem(id="p1", x=0.0, y=0.0, shape="").shape == "circle"
    for bad in ("star", "Circle", "pin ", "x" * 17):
        with pytest.raises(ValidationError):
            PinItem(id="p1", x=0.0, y=0.0, shape=bad)
