"""Tests for lunchmenu.serve: render_page's HTML output, plus a regression
test pinning webos.py's deliberate bare-URL behavior for --show (see
webos.py's cmd_show docstring comment) -- the page's own 15-minute
<meta refresh> only rolls "today" forward correctly if the TV was pointed at
the bare "/" and not a URL pinned to an explicit ?date=, so a future change
must not "simplify" that away."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from lunchmenu import serve, webos


def _payload_with_recipe(day: dt.date, name: str) -> dict:
    date_str = f"{day.month}/{day.day}/{day.year}"
    return {
        "FamilyMenuSessions": [
            {
                "ServingSession": "Lunch",
                "MenuPlans": [
                    {
                        "Days": [
                            {
                                "Date": date_str,
                                "MenuMeals": [
                                    {
                                        "RecipeCategories": [
                                            {
                                                "CategoryName": "Main Entree",
                                                "Recipes": [{"RecipeName": name}],
                                            }
                                        ]
                                    }
                                ],
                            }
                        ]
                    }
                ],
            }
        ],
        "AcademicCalendars": [],
    }


def test_render_page_escapes_recipe_name(week_regular):
    day = dt.date(2026, 9, 10)  # any date not already in week_regular
    payload = _payload_with_recipe(day, "<script>alert(1)</script>")
    html_out = serve.render_page(day, payload=payload)
    assert "<script>alert(1)</script>" not in html_out
    assert "&lt;script&gt;" in html_out


def test_render_page_bad_date_shows_notice_instead_of_raising():
    today = dt.date(2026, 9, 3)
    html_out = serve.render_page(
        today, payload={"FamilyMenuSessions": [], "AcademicCalendars": []}, bad_date="not-a-date"
    )
    assert "Couldn't understand the date" in html_out
    assert "not-a-date" in html_out
    assert "Showing today instead" in html_out


def test_render_page_closure_day_renders_note(week_holiday):
    labor_day = dt.date(2026, 9, 7)
    html_out = serve.render_page(labor_day, payload=week_holiday)
    assert "Labor Day" in html_out
    assert "no school" in html_out


def test_render_page_stale_caveat_renders(week_regular):
    day = dt.date(2026, 9, 4)
    html_out = serve.render_page(day, payload=week_regular, stale_age=dt.timedelta(hours=5))
    assert "menu server is unreachable" in html_out
    assert "5 hours ago" in html_out


def test_render_page_fetch_error_does_not_raise():
    day = dt.date(2026, 9, 3)
    html_out = serve.render_page(day, fetch_error="connection refused")
    assert "Menu unavailable" in html_out
    assert "connection refused" in html_out


# ---------------------------------------------------------------------------
# Bare-URL regression: webos.py --show must launch the BARE "/" for the
# literal-today case, and only append "?date=" for a genuinely different day.
# ---------------------------------------------------------------------------


def _show_args(date: str) -> SimpleNamespace:
    return SimpleNamespace(
        date=date,
        meal="lunch",
        host=None,
        room=None,
        district="district",
        building="building",
        dry_run=True,
        wake=False,
        wake_timeout=60.0,
    )


def test_show_today_uses_bare_url(monkeypatch, capsys, freeze_datetime):
    # Well before the 2pm rollover, so "today" resolves to the frozen date.
    freeze_datetime(dt.datetime(2026, 9, 3, 13, 0, tzinfo=dt.UTC))
    exit_code = webos.cmd_show(_show_args("today"))
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "would launch browser at" in out
    url = out.split("would launch browser at ")[1].split(" on:")[0]
    assert "?date=" not in url
    assert url.endswith("/")


def test_show_explicit_other_date_pins_query_string(capsys, freeze_datetime):
    freeze_datetime(dt.datetime(2026, 9, 3, 13, 0, tzinfo=dt.UTC))
    exit_code = webos.cmd_show(_show_args("2026-09-10"))
    assert exit_code == 0
    out = capsys.readouterr().out
    url = out.split("would launch browser at ")[1].split(" on:")[0]
    assert "?date=2026-09-10" in url
