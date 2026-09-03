"""Tests for lunchmenu.announce: _speakable, _join, spoken_text."""

from __future__ import annotations

import datetime as dt

import pytest

from lunchmenu import announce
from lunchmenu import menu as menu_mod


@pytest.fixture(autouse=True)
def example_school(monkeypatch):
    """announce.SCHOOL is a module-level constant resolved from config at
    import time, and this test process imports the module once with an
    empty (autouse-isolated) config -- so set it directly per test rather
    than trying to make a config reload reach an already-bound global."""
    monkeypatch.setattr(announce, "SCHOOL", "Example Elementary")


# ---------------------------------------------------------------------------
# _speakable
# ---------------------------------------------------------------------------


def test_speakable_reverses_inventory_first_name():
    assert announce._speakable("Lettuce, Romaine") == "Romaine Lettuce"


def test_speakable_drops_packaging_noun():
    assert announce._speakable("Raisins, Seedless, Box") == "Seedless Raisins"


def test_speakable_expands_wg():
    assert announce._speakable("Bread WG") == "Bread whole grain"


def test_speakable_expands_ampersand():
    assert announce._speakable("Mac & Cheese") == "Mac and Cheese"


def test_speakable_single_clause_name_unchanged():
    assert announce._speakable("Apple Slices") == "Apple Slices"


# ---------------------------------------------------------------------------
# _join
# ---------------------------------------------------------------------------


def test_join_single_item_unchanged():
    assert announce._join(["Apple Slices"]) == "Apple Slices"


def test_join_multiple_items_oxford_or():
    assert announce._join(["Apple", "Banana", "Carrot"]) == "Apple, Banana, or Carrot"


def test_join_resolves_speakable_per_item():
    assert announce._join(["Lettuce, Romaine", "Apple"]) == "Romaine Lettuce, or Apple"


# ---------------------------------------------------------------------------
# spoken_text
# ---------------------------------------------------------------------------


def test_spoken_text_includes_school_name(monkeypatch, week_regular):
    monkeypatch.setattr(menu_mod, "get", lambda path, **params: week_regular)
    day = dt.date(2026, 9, 4)  # Friday, present in the fixture
    text = announce.spoken_text(day, "lunch", "district", "building")
    assert "Example Elementary" in text


def test_spoken_text_no_school_on_closure_day(monkeypatch, week_holiday):
    monkeypatch.setattr(menu_mod, "get", lambda path, **params: week_holiday)
    monkeypatch.setattr(menu_mod, "today", lambda: dt.date(2026, 9, 7))
    day = dt.date(2026, 9, 7)  # Labor Day
    text = announce.spoken_text(day, "lunch", "district", "building")
    assert "No school today" in text
    assert "Labor Day" in text


def test_spoken_text_carries_stale_caveat(monkeypatch, week_regular):
    stale_payload = menu_mod.StaleMenuPayload(week_regular)
    stale_payload.stale_since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=6)
    monkeypatch.setattr(menu_mod, "fetch_week", lambda *a, **kw: stale_payload)

    day = dt.date(2026, 9, 4)
    text = announce.spoken_text(day, "lunch", "district", "building")
    assert text.startswith(announce.STALE_CAVEAT)


def test_spoken_text_live_payload_has_no_stale_caveat(monkeypatch, week_regular):
    monkeypatch.setattr(menu_mod, "get", lambda path, **params: week_regular)
    day = dt.date(2026, 9, 4)
    text = announce.spoken_text(day, "lunch", "district", "building")
    assert not text.startswith(announce.STALE_CAVEAT)
