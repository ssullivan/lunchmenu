"""Tests for lunchmenu.menu: parse_day, day_menu/day_notes, retry logic, and
the disk cache's allow_stale fallback. No test here touches the network --
menu.get is always monkeypatched (see conftest.fake_get) or urlopen never
gets called in the first place because get() itself is replaced."""

from __future__ import annotations

import datetime as dt
import urllib.error

import pytest

from lunchmenu import menu

FROZEN_TODAY = dt.date(2026, 9, 3)  # a Thursday


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    monkeypatch.setattr(menu, "today", lambda: FROZEN_TODAY)


# ---------------------------------------------------------------------------
# parse_day
# ---------------------------------------------------------------------------


def test_parse_day_today():
    assert menu.parse_day("today") == FROZEN_TODAY
    assert menu.parse_day("Today") == FROZEN_TODAY


def test_parse_day_tomorrow():
    assert menu.parse_day("tomorrow") == FROZEN_TODAY + dt.timedelta(days=1)


def test_parse_day_yesterday():
    assert menu.parse_day("yesterday") == FROZEN_TODAY - dt.timedelta(days=1)


def test_parse_day_iso():
    assert menu.parse_day("2026-12-25") == dt.date(2026, 12, 25)


def test_parse_day_m_d_yyyy():
    assert menu.parse_day("12/25/2026") == dt.date(2026, 12, 25)


def test_parse_day_bare_m_d_defaults_to_current_year():
    assert menu.parse_day("3/15") == dt.date(FROZEN_TODAY.year, 3, 15)


def test_parse_day_unparseable_exits():
    with pytest.raises(SystemExit):
        menu.parse_day("not a date")


# ---------------------------------------------------------------------------
# day_menu / day_notes
# ---------------------------------------------------------------------------


def test_day_menu_collapses_payload(week_regular):
    friday = dt.date(2026, 9, 4)
    collapsed = menu.day_menu(week_regular, friday)
    assert set(collapsed) == {"Breakfast", "Lunch", "Snack"}
    lunch = collapsed["Lunch"]
    assert "Main Entree" in lunch
    assert all(isinstance(items, list) and items for items in lunch.values())


def test_day_menu_empty_for_a_date_not_in_the_payload(week_regular):
    assert menu.day_menu(week_regular, dt.date(2099, 1, 1)) == {}


def test_day_notes_closure_from_holiday_fixture(week_holiday):
    labor_day = dt.date(2026, 9, 7)
    assert menu.day_notes(week_holiday, labor_day) == ["Labor Day - Schools Closed"]
    assert menu.day_menu(week_holiday, labor_day) == {}


def test_day_notes_empty_on_a_normal_day(week_regular):
    assert menu.day_notes(week_regular, dt.date(2026, 9, 4)) == []


# ---------------------------------------------------------------------------
# get()'s bounded retry
# ---------------------------------------------------------------------------


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://example.invalid", code, "err", {}, None)


def test_get_retries_transient_errors_with_backoff(monkeypatch):
    calls = []
    sleeps = []
    monkeypatch.setattr(menu.time, "sleep", lambda s: sleeps.append(s))

    attempts = {"n": 0}

    def fake_urlopen(req, timeout=None):
        attempts["n"] += 1
        calls.append(attempts["n"])
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(menu.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.URLError):
        menu.get("FamilyMenu")

    assert attempts["n"] == menu.RETRY_ATTEMPTS == 3
    # backoff between attempts only (2 sleeps for 3 attempts), doubling each time.
    assert sleeps == [
        menu.RETRY_BACKOFF_SECONDS * (2**0),
        menu.RETRY_BACKOFF_SECONDS * (2**1),
    ]


def test_get_retries_5xx_and_429(monkeypatch):
    monkeypatch.setattr(menu.time, "sleep", lambda s: None)
    attempts = {"n": 0}

    def fake_urlopen(req, timeout=None):
        attempts["n"] += 1
        raise _http_error(503)

    monkeypatch.setattr(menu.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        menu.get("FamilyMenu")
    assert attempts["n"] == 3


def test_get_does_not_retry_non_429_4xx(monkeypatch):
    sleeps = []
    monkeypatch.setattr(menu.time, "sleep", lambda s: sleeps.append(s))
    attempts = {"n": 0}

    def fake_urlopen(req, timeout=None):
        attempts["n"] += 1
        raise _http_error(404)

    monkeypatch.setattr(menu.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        menu.get("FamilyMenu")

    assert attempts["n"] == 1  # exactly one attempt -- a real 404 won't fix itself
    assert sleeps == []  # and no time wasted sleeping before giving up


# ---------------------------------------------------------------------------
# Disk cache round-trip + allow_stale fallback
# ---------------------------------------------------------------------------


def test_fetch_week_writes_and_reads_back_the_disk_cache(monkeypatch, week_regular):
    monkeypatch.setattr(menu, "get", lambda path, **params: week_regular)

    monday = FROZEN_TODAY - dt.timedelta(days=FROZEN_TODAY.weekday())
    result = menu.fetch_week(FROZEN_TODAY, "district", "building")
    assert result == week_regular
    assert menu.stale_age(result) is None  # a live fetch is never "stale"

    cache_file = menu._cache_path(monday)
    assert cache_file.exists()

    # Now the live fetch fails transiently; allow_stale=True must fall back
    # to exactly what was just cached.
    def failing_get(path, **params):
        raise urllib.error.URLError("network is down")

    monkeypatch.setattr(menu, "get", failing_get)
    stale_result = menu.fetch_week(FROZEN_TODAY, "district", "building", allow_stale=True)
    assert stale_result == week_regular
    age = menu.stale_age(stale_result)
    assert age is not None
    assert age >= dt.timedelta(0)


def test_fetch_week_without_allow_stale_raises_on_transient_failure(monkeypatch, week_regular):
    monkeypatch.setattr(menu, "get", lambda path, **params: week_regular)
    menu.fetch_week(FROZEN_TODAY, "district", "building")  # warm the cache

    def failing_get(path, **params):
        raise urllib.error.URLError("network is down")

    monkeypatch.setattr(menu, "get", failing_get)
    with pytest.raises(urllib.error.URLError):
        menu.fetch_week(FROZEN_TODAY, "district", "building", allow_stale=False)


def test_fetch_week_allow_stale_with_no_cache_still_raises(monkeypatch):
    def failing_get(path, **params):
        raise urllib.error.URLError("network is down")

    monkeypatch.setattr(menu, "get", failing_get)
    with pytest.raises(urllib.error.URLError):
        menu.fetch_week(FROZEN_TODAY, "district", "building", allow_stale=True)


def test_fetch_week_allow_stale_does_not_mask_a_real_4xx(monkeypatch, week_regular):
    """A real 4xx (bad request, unknown building id, ...) is never treated as
    a reason to fall back to a stale cache -- only transient failures are."""
    monkeypatch.setattr(menu, "get", lambda path, **params: week_regular)
    menu.fetch_week(FROZEN_TODAY, "district", "building")  # warm the cache

    def failing_get(path, **params):
        raise _http_error(400)

    monkeypatch.setattr(menu, "get", failing_get)
    with pytest.raises(urllib.error.HTTPError):
        menu.fetch_week(FROZEN_TODAY, "district", "building", allow_stale=True)


def test_ordinary_dict_is_never_stale():
    assert menu.stale_age({"FamilyMenuSessions": []}) is None
