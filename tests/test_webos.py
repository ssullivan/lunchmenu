"""Tests for lunchmenu.webos: build_toast_text's char budget and truncation,
resolve_effective_day's today/rollover logic, and the --toast --message
custom-toast path (prepare_custom_message + cmd_toast). No test here opens a
websocket, sends a toast, or launches a TV browser -- only the pure text/date
logic is exercised."""

from __future__ import annotations

import argparse
import datetime as dt
import io
import sys

import pytest

from lunchmenu import announce as announce_mod
from lunchmenu import menu as menu_mod
from lunchmenu import webos


@pytest.fixture(autouse=True)
def example_school(monkeypatch):
    monkeypatch.setattr(announce_mod, "SCHOOL", "Example Elementary")


def _make_payload(
    day: dt.date, categories: dict[str, list[str]], session: str = "Lunch", notes=None
) -> dict:
    date_str = f"{day.month}/{day.day}/{day.year}"
    recipe_categories = [
        {"CategoryName": name, "Recipes": [{"RecipeName": n} for n in items]}
        for name, items in categories.items()
    ]
    payload: dict = {
        "FamilyMenuSessions": [
            {
                "ServingSession": session,
                "MenuPlans": [
                    {
                        "Days": [
                            {
                                "Date": date_str,
                                "MenuMeals": [{"RecipeCategories": recipe_categories}],
                            }
                        ]
                    }
                ],
            }
        ],
        "AcademicCalendars": [],
    }
    if notes:
        payload["AcademicCalendars"] = [{"Days": [{"Date": date_str, "Note": n} for n in notes]}]
    return payload


# ---------------------------------------------------------------------------
# build_toast_text: char budget
# ---------------------------------------------------------------------------


def test_build_toast_text_within_budget(monkeypatch):
    day = dt.date(2026, 9, 4)
    payload = _make_payload(
        day, {"Main Entree": ["Cheese Pizza"], "Hot Vegetable": ["Green Beans"]}
    )
    monkeypatch.setattr(menu_mod, "get", lambda path, **params: payload)

    text = webos.build_toast_text(day, "today", "lunch", "district", "building")
    assert len(text) <= webos.TOAST_MAX_CHARS
    assert "Example Elementary" in text


def test_build_toast_text_huge_item_list_stays_within_budget(monkeypatch):
    day = dt.date(2026, 9, 4)
    entrees = [f"Very Long Entree Name {i} With Extra Descriptive Words" for i in range(30)]
    sides = [f"Side Dish Item {i} That Is Also Quite Verbose" for i in range(30)]
    payload = _make_payload(day, {"Main Entree": entrees, "Hot Vegetable": sides})
    monkeypatch.setattr(menu_mod, "get", lambda path, **params: payload)

    text = webos.build_toast_text(day, "today", "lunch", "district", "building")
    assert len(text) <= webos.TOAST_MAX_CHARS
    assert "more" in text


def test_build_toast_text_stale_marker_plus_huge_list_stays_within_budget(monkeypatch):
    day = dt.date(2026, 9, 4)
    entrees = [f"Very Long Entree Name {i} With Extra Descriptive Words" for i in range(30)]
    payload = _make_payload(day, {"Main Entree": entrees})
    stale_payload = menu_mod.StaleMenuPayload(payload)
    stale_payload.stale_since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=3)
    monkeypatch.setattr(menu_mod, "fetch_week", lambda *a, **kw: stale_payload)

    text = webos.build_toast_text(day, "today", "lunch", "district", "building")
    assert len(text) <= webos.TOAST_MAX_CHARS
    assert text.startswith(webos.STALE_TOAST_MARKER)


def test_build_toast_text_plus_n_more_when_items_exceed_item_budget(monkeypatch):
    day = dt.date(2026, 9, 4)
    entrees = ["Pizza", "Tacos", "Burger", "Salad", "Soup"]  # 5 short items, cap is 3
    payload = _make_payload(day, {"Main Entree": entrees})
    monkeypatch.setattr(menu_mod, "get", lambda path, **params: payload)

    text = webos.build_toast_text(day, "today", "lunch", "district", "building")
    assert "+2 more" in text
    assert len(text) <= webos.TOAST_MAX_CHARS


def test_build_toast_text_closure_day(monkeypatch):
    day = dt.date(2026, 9, 7)
    payload = _make_payload(day, {}, notes=["Labor Day - Schools Closed"])
    monkeypatch.setattr(menu_mod, "get", lambda path, **params: payload)

    text = webos.build_toast_text(day, "Monday", "lunch", "district", "building")
    assert "Labor Day" in text
    assert len(text) <= webos.TOAST_MAX_CHARS


# ---------------------------------------------------------------------------
# resolve_effective_day
# ---------------------------------------------------------------------------


def test_resolve_effective_day_before_rollover_is_today(freeze_datetime):
    # Thursday 2026-09-03, 9am Eastern (EDT = UTC-4) -> 13:00 UTC.
    freeze_datetime(dt.datetime(2026, 9, 3, 13, 0, tzinfo=dt.UTC))
    day, when = webos.resolve_effective_day("today", "district", "building")
    assert day == dt.date(2026, 9, 3)
    assert when == "today"


def test_resolve_effective_day_after_rollover_skips_weekend_and_closure(
    monkeypatch, freeze_datetime, week_holiday
):
    # Friday 2026-09-04, 3pm Eastern (EDT = UTC-4) -> 19:00 UTC: past ROLLOVER_HOUR.
    freeze_datetime(dt.datetime(2026, 9, 4, 19, 0, tzinfo=dt.UTC))
    monkeypatch.setattr(menu_mod, "get", lambda path, **params: week_holiday)

    day, when = webos.resolve_effective_day("today", "district", "building")
    # Sat 9/5 and Sun 9/6 are weekends; Mon 9/7 is Labor Day (closed, no
    # menu); Tue 9/8 is the first day with a menu in week_holiday.
    assert day == dt.date(2026, 9, 8)
    assert when == "Tuesday"


def test_resolve_effective_day_explicit_date_is_never_second_guessed(freeze_datetime):
    # Even well past the rollover hour, an explicit date argument (not the
    # literal "today") is used exactly as given.
    freeze_datetime(dt.datetime(2026, 9, 4, 23, 0, tzinfo=dt.UTC))
    day, when = webos.resolve_effective_day("2026-09-07", "district", "building")
    assert day == dt.date(2026, 9, 7)
    assert when == "Monday"


# ---------------------------------------------------------------------------
# prepare_custom_message (--toast --message)
# ---------------------------------------------------------------------------


def test_prepare_custom_message_short_text_passes_through_unchanged():
    assert webos.prepare_custom_message("Dinner's ready") == "Dinner's ready"


def test_prepare_custom_message_strips_single_trailing_newline():
    assert webos.prepare_custom_message("hello\n") == "hello"


def test_prepare_custom_message_collapses_interior_newlines():
    # webOS's toast UI isn't documented to render multi-line text and
    # notify() forwards the string as-is -- see the comment on
    # prepare_custom_message.
    assert webos.prepare_custom_message("line one\nline two") == "line one line two"


def test_prepare_custom_message_reads_stdin_for_dash(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("from stdin\n"))
    assert webos.prepare_custom_message("-") == "from stdin"


def test_prepare_custom_message_empty_is_error():
    with pytest.raises(ValueError):
        webos.prepare_custom_message("")


def test_prepare_custom_message_whitespace_only_is_error():
    with pytest.raises(ValueError):
        webos.prepare_custom_message("   \t  ")


def test_prepare_custom_message_over_cap_is_truncated_and_warns(capsys):
    long_message = "x" * (webos.TOAST_MAX_CHARS + 220)
    result = webos.prepare_custom_message(long_message)
    assert len(result) <= webos.TOAST_MAX_CHARS
    assert result.endswith(webos.CUSTOM_TOAST_ELLIPSIS)
    err = capsys.readouterr().err
    assert "truncated" in err


# ---------------------------------------------------------------------------
# cmd_toast --message: no menu fetch, existing --toast plumbing unaffected
# ---------------------------------------------------------------------------


def _refuse_to_fetch(*_args, **_kwargs):
    raise AssertionError("a custom --message toast must never fetch the menu")


def _toast_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        date="today",
        meal="lunch",
        host=None,
        room=None,
        district="district",
        building="building",
        message=None,
        dry_run=True,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_cmd_toast_message_never_fetches_the_menu(monkeypatch, capsys):
    monkeypatch.setattr(menu_mod, "get", _refuse_to_fetch)
    monkeypatch.setattr(menu_mod, "fetch_week", _refuse_to_fetch)

    rc = webos.cmd_toast(_toast_args(message="Dinner's ready"))
    assert rc == 0
    assert "Dinner's ready" in capsys.readouterr().out


def test_cmd_toast_message_dash_reads_stdin_and_still_skips_fetch(monkeypatch, capsys):
    monkeypatch.setattr(menu_mod, "get", _refuse_to_fetch)
    monkeypatch.setattr(menu_mod, "fetch_week", _refuse_to_fetch)
    monkeypatch.setattr(sys, "stdin", io.StringIO("from stdin\n"))

    rc = webos.cmd_toast(_toast_args(message="-"))
    assert rc == 0
    assert "from stdin" in capsys.readouterr().out


def test_cmd_toast_message_over_cap_truncates_and_warns(monkeypatch, capsys):
    monkeypatch.setattr(menu_mod, "get", _refuse_to_fetch)
    monkeypatch.setattr(menu_mod, "fetch_week", _refuse_to_fetch)

    rc = webos.cmd_toast(_toast_args(message="z" * 400))
    assert rc == 0
    captured = capsys.readouterr()
    assert "truncated" in captured.err


def test_cmd_toast_message_empty_is_an_error_not_an_empty_toast(monkeypatch, capsys):
    monkeypatch.setattr(menu_mod, "get", _refuse_to_fetch)
    monkeypatch.setattr(menu_mod, "fetch_week", _refuse_to_fetch)

    rc = webos.cmd_toast(_toast_args(message="   "))
    assert rc == 1
    assert "empty" in capsys.readouterr().err.lower()


def test_cmd_toast_without_message_is_unchanged(monkeypatch, capsys):
    # Same fixture/expectations as test_build_toast_text_within_budget --
    # confirms the no-argument --toast path is untouched by the --message
    # plumbing added around it.
    day = dt.date(2026, 9, 4)
    payload = _make_payload(
        day, {"Main Entree": ["Cheese Pizza"], "Hot Vegetable": ["Green Beans"]}
    )
    monkeypatch.setattr(menu_mod, "get", lambda path, **params: payload)
    monkeypatch.setattr(webos, "resolve_effective_day", lambda *a, **kw: (day, "today"))

    rc = webos.cmd_toast(_toast_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "Example Elementary" in out


def test_message_flag_rejected_outside_toast(monkeypatch, capsys):
    # argparse-level failure, not a silently-ignored flag: --message only
    # means something with --toast.
    monkeypatch.setattr(
        sys, "argv", ["lunchmenu-webos", "--show", "--message", "nope", "--dry-run"]
    )
    with pytest.raises(SystemExit) as exc_info:
        webos.main()
    assert exc_info.value.code == 2
    assert "--message" in capsys.readouterr().err
