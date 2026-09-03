#!/usr/bin/env python3
"""Print the school menu for a given day from LINQ Connect.

Which school this fetches is entirely config-driven -- see config.py and
config.example.toml -- there is no default school baked into this file.
Point `school.identifier` at the district slug from your school's public
menu URL, e.g. https://linqconnect.com/public/menu/<slug>?buildingId=<guid>,
then run `--list-buildings` to find the district/building GUIDs.

The public site is an Angular SPA over an unauthenticated JSON API:
  GET /api/FamilyMenuIdentifier?identifier=<slug>      -> district id + building list
  GET /api/FamilyMenu?districtId=&buildingId=&startDate=&endDate=
Dates in the API are M-D-YYYY going in and M/D/YYYY coming back.
CloudFront 403s a default curl/urllib User-Agent, so one is set below.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo
from pathlib import Path

from . import config

API = "https://api.linqconnect.com/api"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# get()'s bounded retry: 3 attempts total, ~1s then ~2s between them. Only
# for failures that are plausibly transient (see _is_transient_error) --
# a real 4xx (bad request, 404, ...) is not going to succeed on attempt 2,
# so it is raised immediately instead of wasting ~3s retrying it.
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.0

# Disk cache (fetch_week's allow_stale fallback) -- see _cache_path().
CACHE_SUBDIR = "cache"
CACHE_MAX_AGE_DAYS = 60


def _is_transient_error(exc: BaseException) -> bool:
    """Whether `exc` (raised by urllib during get()) is worth retrying / worth
    falling back to a stale cache for -- a connection problem, a timeout, or
    a 5xx/429 from the server. A 4xx other than 429 is a real error (bad
    request, unknown building id, ...) and must never be treated as if the
    server were merely unreachable."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, (urllib.error.URLError, TimeoutError))


DEFAULT_IDENTIFIER = config.get("school", "identifier")
DEFAULT_DISTRICT = config.get("school", "district_id")
DEFAULT_BUILDING = config.get("school", "building_id")

# The host's system clock runs UTC; the school's timezone (school.timezone
# in config, default America/New_York) is pinned so "today" doesn't roll
# over 4-5 hours early -- don't simplify this back to dt.date.today().
SCHOOL_TZ = zoneinfo.ZoneInfo(config.get("school", "timezone"))


def today() -> dt.date:
    return dt.datetime.now(SCHOOL_TZ).date()


def get(path: str, **params: str) -> dict:
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    # CloudFront 403s a default urllib/curl User-Agent -- these two headers
    # are load-bearing, don't touch them.
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Referer": "https://linqconnect.com/"}
    )
    last_exc: BaseException | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except Exception as exc:
            if not _is_transient_error(exc):
                raise  # a real 4xx, malformed JSON, etc. -- fail immediately
            last_exc = exc
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))
    assert last_exc is not None  # RETRY_ATTEMPTS >= 1 guarantees this is set
    raise last_exc


def parse_day(text: str) -> dt.date:
    base = today()
    key = text.strip().lower()
    if key in ("today", "t"):
        return base
    if key == "tomorrow":
        return base + dt.timedelta(days=1)
    if key == "yesterday":
        return base - dt.timedelta(days=1)
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d", "%b %d", "%B %d"):
        try:
            parsed = dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        return parsed.replace(year=base.year) if parsed.year == 1900 else parsed
    raise SystemExit(f"Cannot parse date: {text!r}")


def api_date(day: dt.date) -> str:
    return f"{day.month}-{day.day}-{day.year}"


# --------------------------------------------------------------------------
# Disk cache for fetch_week's allow_stale fallback -- keyed by the week's
# Monday, under the state dir (never in the repo). Every *successful* fetch
# writes here, regardless of allow_stale, so the cache is warm by the time an
# outage actually happens instead of being empty on its first use.
# --------------------------------------------------------------------------


def _cache_dir() -> Path:
    return config.state_dir() / CACHE_SUBDIR


def _cache_path(monday: dt.date) -> Path:
    return _cache_dir() / f"week-{monday.isoformat()}.json"


def _write_cache(monday: dt.date, payload: dict) -> None:
    """Atomic write (temp file + os.replace) so a crash mid-write can never
    leave a corrupt cache file behind for a later stale-fallback read to
    choke on. Best-effort: a cache write failure (e.g. a read-only state
    dir) must not take down an otherwise-successful fetch."""
    cache_dir = _cache_dir()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "cached_at": dt.datetime.now(dt.UTC).isoformat(),
            "payload": payload,
        }
        fd, tmp_name = tempfile.mkstemp(dir=cache_dir, prefix=".week-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(record, fh)
            os.replace(tmp_name, _cache_path(monday))
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        _prune_cache(cache_dir)
    except OSError as exc:
        print(f"menu: could not write cache for week of {monday} ({exc})", file=sys.stderr)


def _prune_cache(cache_dir: Path) -> None:
    """Opportunistic cleanup so the cache doesn't grow forever -- run once
    per successful write rather than on a schedule of its own."""
    cutoff = today() - dt.timedelta(days=CACHE_MAX_AGE_DAYS)
    for f in cache_dir.glob("week-*.json"):
        try:
            monday = dt.date.fromisoformat(f.name.removeprefix("week-").removesuffix(".json"))
        except ValueError:
            continue
        if monday < cutoff:
            with contextlib.suppress(OSError):
                f.unlink()


def _read_cache(monday: dt.date) -> tuple[dict, dt.datetime] | None:
    """(payload, cached_at) for the given week, or None if there is no
    usable cache entry -- missing, unreadable, or corrupt are all treated
    the same: no stale fallback is available."""
    try:
        record = json.loads(_cache_path(monday).read_text())
        return record["payload"], dt.datetime.fromisoformat(record["cached_at"])
    except (FileNotFoundError, OSError, json.JSONDecodeError, KeyError, ValueError):
        return None


class StaleMenuPayload(dict):
    """A `fetch_week(allow_stale=True)` result that came from the disk cache
    because the live fetch failed, not from the network. It IS a plain dict
    (subscripting, .get(), day_menu()/day_notes() all work exactly as on a
    live payload) with one extra attribute, `stale_since`, carrying when the
    cached snapshot was written. A live fetch returns an ordinary dict (no
    such attribute) -- use `stale_age()` below rather than checking this
    class directly, since it handles both cases via getattr.
    """

    stale_since: dt.datetime | None = None


def stale_age(payload: dict) -> dt.timedelta | None:
    """How old `payload` is if it came from fetch_week's stale-cache
    fallback, else None (including for any ordinary dict). Safe to call on
    any fetch_week result."""
    since = getattr(payload, "stale_since", None)
    if since is None:
        return None
    return dt.datetime.now(dt.UTC) - since


def fetch_week(day: dt.date, district: str, building: str, *, allow_stale: bool = False) -> dict:
    """Fetch the Mon-Sun week containing `day`.

    A week, not a single day: the AcademicCalendars note (e.g. "Labor Day") only
    comes back when the range spans more than the closed day itself.

    On success, the payload is cached to disk (see _write_cache) regardless
    of `allow_stale`, so the cache is warm for the next outage. get() already
    retries transient failures a few times before giving up; if that retry
    is exhausted (or the failure is a real 4xx, which get() never retries)
    and `allow_stale` is True, this falls back to the most recent cached
    payload for the same week instead of raising -- the caller can tell via
    `stale_age()` on the result. `allow_stale=False` (the default) behaves
    exactly as before: it raises, and always returns a plain dict.
    """
    monday = day - dt.timedelta(days=day.weekday())
    try:
        payload = get(
            "FamilyMenu",
            districtId=district,
            buildingId=building,
            startDate=api_date(monday),
            endDate=api_date(monday + dt.timedelta(days=6)),
        )
    except Exception as exc:
        if allow_stale and _is_transient_error(exc):
            cached = _read_cache(monday)
            if cached is not None:
                cached_payload, cached_at = cached
                result = StaleMenuPayload(cached_payload)
                result.stale_since = cached_at
                return result
        raise
    _write_cache(monday, payload)
    return payload


def day_menu(payload: dict, day: dt.date) -> dict:
    """Collapse the API payload to {session: {category: [recipe, ...]}} for one day."""
    wanted = f"{day.month}/{day.day}/{day.year}"
    out: dict[str, dict[str, list[str]]] = {}
    for session in payload.get("FamilyMenuSessions", []):
        categories: dict[str, list[str]] = {}
        for plan in session.get("MenuPlans", []):
            for entry in plan.get("Days", []):
                if entry.get("Date") != wanted:
                    continue
                for meal in entry.get("MenuMeals", []):
                    for cat in meal.get("RecipeCategories", []):
                        names = [r["RecipeName"] for r in cat.get("Recipes", [])]
                        categories.setdefault(cat["CategoryName"], []).extend(names)
        if categories:
            out[session["ServingSession"]] = categories
    return out


def day_notes(payload: dict, day: dt.date) -> list[str]:
    wanted = f"{day.month}/{day.day}/{day.year}"
    return [
        entry["Note"]
        for cal in payload.get("AcademicCalendars", [])
        for entry in cal.get("Days", [])
        if entry.get("Date") == wanted and entry.get("Note")
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "date",
        nargs="?",
        default="today",
        help="today | tomorrow | YYYY-MM-DD | M/D/YYYY (default: today)",
    )
    ap.add_argument(
        "-m", "--meal", default="lunch", help="lunch, breakfast, snack, or all (default: lunch)"
    )
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    ap.add_argument("--raw", action="store_true", help="dump the raw API payload")
    ap.add_argument("--district", default=DEFAULT_DISTRICT)
    ap.add_argument("--building", default=DEFAULT_BUILDING)
    ap.add_argument(
        "--list-buildings",
        action="store_true",
        help="list the district's schools and their building ids",
    )
    ap.add_argument(
        "--identifier", default=DEFAULT_IDENTIFIER, help="district slug from the public menu URL"
    )
    args = ap.parse_args()

    if args.list_buildings:
        ident = get("FamilyMenuIdentifier", identifier=args.identifier)
        print(f"{ident['DistrictName']}  (districtId {ident['DistrictId']})")
        for b in ident["Buildings"]:
            print(f"  {b['BuildingId']}  {b['Name']}")
        return 0

    day = parse_day(args.date)
    payload = fetch_week(day, args.district, args.building)
    if args.raw:
        json.dump(payload, sys.stdout, indent=2)
        return 0

    menu = day_menu(payload, day)
    if args.meal.lower() != "all":
        want = args.meal.lower()
        menu = {k: v for k, v in menu.items() if k.lower() == want}

    notes = day_notes(payload, day)
    if args.json:
        json.dump({"date": day.isoformat(), "notes": notes, "menu": menu}, sys.stdout, indent=2)
        print()
        return 0

    print(day.strftime("%A, %B %-d, %Y"))
    for note in notes:
        print(f"  * {note}")
    if not menu:
        print("  (no menu published)" if not notes else "  (no school)")
        return 0
    for session, categories in menu.items():
        print(f"\n{session}")
        for category, items in categories.items():
            print(f"  {category}:")
            for item in items:
                print(f"    - {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
