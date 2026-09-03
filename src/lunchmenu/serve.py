#!/usr/bin/env python3
"""Serve the school menu as an HTML page for a TV browser.

Stdlib only. Reuses menu.py for every fetch/parse/timezone call -- this file
only turns the result into HTML and caches the upstream response.

Designed to be read from a couch on a 65" screen: large type, a near-black
background (this runs on OLED TVs -- a full-white field is the thing to
avoid), high contrast, no dependency on hover or a pointer. Degrades to a
smaller but still comfortable layout on a phone via one media query. Plain
CSS, no CDN, no framework, and nothing that assumes a browser newer than the
2020 LG CX's (no :has(), no container queries, no CSS nesting).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import html
import http.server
import sys
import threading
import time
import urllib.parse

from . import config
from . import menu as menu_mod

BIND_HOST = config.get("web", "bind_host")
# 8080 was already bound by an unrelated pre-existing service on this host
# (a "nuclio" dashboard, listening 0.0.0.0:8080) on the original deployment
# -- 8090 is the neutral default here. This is web.port in config -- one
# key, shared with webos.py's SERVE_PORT, so the two can no longer drift
# out of sync by hand.
PORT = config.get("web", "port")

CACHE_TTL_SECONDS = 30 * 60
SESSION_ORDER = ("Breakfast", "Lunch", "Snack")

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def cached_fetch_week(day: dt.date) -> dict:
    """fetch_week, cached ~30min per Mon-Sun week so a TV left on this page
    doesn't hammer the vendor API.

    allow_stale=True: this must never turn into a 500 or a blank page just
    because the upstream menu server is down -- see menu.fetch_week's
    docstring and do_GET's caveat rendering below.
    """
    monday = day - dt.timedelta(days=day.weekday())
    key = monday.isoformat()
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    payload = menu_mod.fetch_week(
        day, menu_mod.DEFAULT_DISTRICT, menu_mod.DEFAULT_BUILDING, allow_stale=True
    )
    with _cache_lock:
        _cache[key] = (now, payload)
    return payload


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- A TV left parked on this page never issues a new request on its own,
     so without this it just keeps showing whatever day it loaded --
     including yesterday's menu, forever. This reload is what makes
     "today" actually mean today again after midnight. 900s = 15min. -->
<meta http-equiv="refresh" content="900">
<title>__PAGE_TITLE__</title>
<style>
  :root {{
    --bg: #0a0a0b;
    --bg-card: #18181b;
    --fg: #efeee8;
    --fg-dim: #9d9d97;
    --accent: #ffb454;
    --accent-dim: #6b4e26;
    --err: #e07a6e;
    --err-bg: #2a1616;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--fg);
  }}
  body {{
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 28px;
    line-height: 1.45;
    padding: 3vw 6vw 6vw;
  }}
  .wrap {{ max-width: 1100px; margin: 0 auto; }}
  .nav {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1.3em;
  }}
  .nav a {{
    color: var(--accent);
    text-decoration: none;
    font-size: 0.7em;
    padding: 0.4em 0.9em;
    border: 2px solid var(--accent-dim);
    border-radius: 0.5em;
    white-space: nowrap;
  }}
  h1 {{ font-size: 1.6em; margin: 0 0 0.1em; }}
  .subtitle {{ color: var(--fg-dim); font-size: 0.85em; margin-bottom: 1em; }}
  .note {{
    background: var(--bg-card);
    border-left: 8px solid var(--accent);
    padding: 0.9em 1.1em;
    margin-bottom: 1.2em;
    font-size: 1.15em;
  }}
  .error {{
    background: var(--err-bg);
    border-left: 8px solid var(--err);
    color: var(--fg);
    padding: 1em 1.2em;
    font-size: 1.05em;
    margin-bottom: 1.2em;
  }}
  .error .empty {{ display: block; margin-top: 0.4em; }}
  .meal {{
    background: var(--bg-card);
    border-radius: 0.7em;
    padding: 1.3em 1.6em;
    margin-bottom: 1.2em;
  }}
  .meal h2 {{ margin: 0 0 0.6em; font-size: 1.3em; color: var(--accent); }}
  .cat {{ margin-bottom: 0.7em; }}
  .cat:last-child {{ margin-bottom: 0; }}
  .cat-name {{
    color: var(--fg-dim);
    font-size: 0.65em;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }}
  .cat-items {{ margin: 0.2em 0 0; padding: 0; list-style: none; }}
  .cat-items li {{ padding: 0.08em 0; }}
  .empty {{ color: var(--fg-dim); font-style: italic; }}
  @media (max-width: 700px) {{
    body {{ font-size: 21px; padding: 5vw 5vw 8vw; }}
    .nav a {{ font-size: 0.75em; padding: 0.35em 0.7em; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <div class="nav">
    <a href="/?date={prev}">&larr; Prev</a>
    <a href="/">Today</a>
    <a href="/?date={next}">Next &rarr;</a>
  </div>
  <h1>{heading}</h1>
  <div class="subtitle">__PAGE_SUBTITLE__</div>
{body}
</div>
</body>
</html>
"""

SCHOOL = config.get("school", "name")
# __PAGE_TITLE__/__PAGE_SUBTITLE__ are plain-text placeholders (not {}-style)
# so they can be filled in once, here, without colliding with PAGE's other
# {prev}/{next}/{heading}/{body} placeholders, which stay unresolved until
# render_page() calls PAGE.format() per request.
PAGE = PAGE.replace(
    "__PAGE_TITLE__", html.escape(f"{SCHOOL} Lunch Menu" if SCHOOL else "Lunch Menu")
)
PAGE = PAGE.replace(
    "__PAGE_SUBTITLE__", html.escape(f"{SCHOOL} — lunch menu" if SCHOOL else "Lunch menu")
)


def _format_stale_age(age: dt.timedelta) -> str:
    """Human-readable, coarse on purpose -- the point is "is this from
    today or from last week", not a precise duration.

    Phrased as "...ago" (not "...old") because every call site reads it as
    "showing a cached copy from {this}." -- "from ... old" doesn't parse.
    """
    hours = age.total_seconds() / 3600
    if hours < 1:
        return "less than an hour ago"
    if hours < 48:
        n = round(hours)
        return f"about {n} hour{'s' if n != 1 else ''} ago"
    n = round(hours / 24)
    return f"about {n} day{'s' if n != 1 else ''} ago"


def render_page(
    day: dt.date,
    *,
    payload: dict | None = None,
    fetch_error: str | None = None,
    bad_date: str | None = None,
    stale_age: dt.timedelta | None = None,
) -> str:
    prev_day = (day - dt.timedelta(days=1)).isoformat()
    next_day = (day + dt.timedelta(days=1)).isoformat()
    heading = html.escape(day.strftime("%A, %B %-d, %Y"))

    parts: list[str] = []

    if bad_date is not None:
        parts.append(
            f'<div class="error">Couldn\'t understand the date '
            f"&ldquo;{html.escape(bad_date)}&rdquo;."
            f'<span class="empty">Showing today instead.</span></div>'
        )

    if stale_age is not None:
        parts.append(
            f'<div class="error">The menu server is unreachable right now &mdash; '
            f"showing a cached copy from {html.escape(_format_stale_age(stale_age))}."
            f'<span class="empty">This page retries automatically on the '
            f"next visit.</span></div>"
        )

    if fetch_error is not None:
        parts.append(
            f'<div class="error">Menu unavailable right now: '
            f"{html.escape(fetch_error)}"
            f'<span class="empty">This page retries automatically on the '
            f"next visit.</span></div>"
        )
    elif payload is not None:
        notes = menu_mod.day_notes(payload, day)
        menu = menu_mod.day_menu(payload, day)

        for note in notes:
            parts.append(f'<div class="note">{html.escape(note)} &mdash; no school</div>')

        if not menu:
            if not notes:
                parts.append('<div class="empty">(no menu published for this day)</div>')
        else:
            ordered_sessions = [s for s in SESSION_ORDER if s in menu]
            ordered_sessions += [s for s in menu if s not in SESSION_ORDER]
            for session in ordered_sessions:
                categories = menu[session]
                cats_html = []
                for cat, items in categories.items():
                    items_html = "".join(f"<li>{html.escape(i)}</li>" for i in items)
                    cats_html.append(
                        f'<div class="cat"><div class="cat-name">{html.escape(cat)}</div>'
                        f'<ul class="cat-items">{items_html}</ul></div>'
                    )
                parts.append(
                    f'<div class="meal"><h2>{html.escape(session)}</h2>{"".join(cats_html)}</div>'
                )

    return PAGE.format(prev=prev_day, next=next_day, heading=heading, body="\n".join(parts))


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "LunchMenuWeb/1.0"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/", ""):
            self.send_error(404, "Not found")
            return

        qs = urllib.parse.parse_qs(parsed.query)
        date_str = qs.get("date", [None])[0]

        bad_date = None
        if date_str:
            try:
                day = menu_mod.parse_day(date_str)
            except SystemExit:
                day = menu_mod.today()
                bad_date = date_str
        else:
            day = menu_mod.today()

        try:
            payload = cached_fetch_week(day)
        except Exception as exc:  # never 500 on an upstream failure
            body = render_page(day, fetch_error=str(exc), bad_date=bad_date)
            self._respond(200, body)
            return

        status = 400 if bad_date else 200
        stale = menu_mod.stale_age(payload)
        body = render_page(day, payload=payload, bad_date=bad_date, stale_age=stale)
        self._respond(status, body)

    def _respond(self, status: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:  # quieter, timestamped
        sys.stderr.write(f"serve: {self.address_string()} {fmt % args}\n")


def main() -> int:
    server = http.server.ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    print(f"serving on http://{BIND_HOST}:{PORT}  (Ctrl-C to stop)")
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
