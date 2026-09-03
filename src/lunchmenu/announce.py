#!/usr/bin/env python3
"""Speak the day's school menu on a Google Home / Nest speaker.

Google shut down Conversational Actions in 2023, so there is no "Hey Google, ask
..." path any more. This instead treats the speaker as what it also is -- a
Chromecast -- and pushes audio at it:

    menu.py -> spoken sentence -> gTTS mp3 -> local HTTP server -> cast device

The mp3 is served from this machine rather than handed over as bytes because a
cast device only ever *fetches* media from a URL. The server binds to whichever
local address can actually reach the speaker (found by opening a throwaway UDP
socket toward it), lives for the length of one playback, and then stops.
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.server
import io
import secrets
import socket
import sys
import threading
import time

from . import config
from . import menu as menu_mod

DEFAULT_DEVICE = config.get("speaker", "device")
DEFAULT_VOLUME = config.get("speaker", "volume")
DEFAULT_MEAL = config.get("speaker", "meal")
SCHOOL = config.get("school", "name")

# Categories worth hearing out loud. Milk is the same three cartons every day and
# turns a 10-second announcement into a 20-second one.
SPOKEN_CATEGORIES = ("Main Entree", "Hot Vegetable", "Cold Vegetable", "Fruit")

# Prepended when fetch_week had to fall back to a cached (possibly outdated)
# week because the menu server was unreachable -- see menu.stale_age(). This
# must never be silently swallowed: a wrong "today's lunch is X" is worse
# than no announcement at all if the school actually served something else.
STALE_CAVEAT = "Heads up, the menu server is unreachable, so this may be out of date."


def spoken_text(day: dt.date, meal: str, district: str, building: str) -> str:
    # allow_stale=True: an unreachable menu server at 7am should still
    # produce an announcement (from the last cached week) rather than
    # nothing at all -- see menu.fetch_week's docstring.
    payload = menu_mod.fetch_week(day, district, building, allow_stale=True)
    stale = menu_mod.stale_age(payload)
    notes = menu_mod.day_notes(payload, day)
    menus = menu_mod.day_menu(payload, day)
    menus = {k: v for k, v in menus.items() if k.lower() == meal.lower()}

    when = "Today" if day == menu_mod.today() else day.strftime("%A")
    if not menus:
        if notes:
            day_word = "today" if when == "Today" else f"on {when}"
            text = f"No school {day_word}. {', '.join(notes)}."
        else:
            text = f"There is no {meal} menu published for {SCHOOL} {when.lower()}."
    else:
        categories = next(iter(menus.values()))
        entrees = categories.get("Main Entree", [])
        sides = [item for name in SPOKEN_CATEGORIES[1:] for item in categories.get(name, [])]

        parts = [f"{when}'s {meal.lower()} at {SCHOOL}"]
        if entrees:
            parts.append(_join(entrees))
        if sides:
            parts.append(f"On the side: {_join(sides)}")
        text = ". ".join(parts) + "."
        if notes:
            text = f"{', '.join(notes)}. {text}"

    return f"{STALE_CAVEAT} {text}" if stale is not None else text


# Packaging words that are on the label but not on the plate.
_PACKAGING = {"box", "pack", "cup", "bowl", "bag", "container", "combo"}


def _speakable(name: str) -> str:
    """Turn a vendor recipe name into something that reads aloud.

    The catalog is written inventory-first -- "Lettuce, Romaine", "Raisins,
    Seedless, Box" -- so the commas are inside single items and would otherwise
    be heard as extra list entries. Reversing the clauses and dropping the
    packaging noun recovers the spoken form.
    """
    name = name.replace(" WG", " whole grain").replace(" w/ ", " with ").strip()
    parts = [p.strip() for p in name.split(",") if p.strip()]
    if len(parts) > 1:
        parts = [p for p in parts if p.lower() not in _PACKAGING] or parts[:1]
        name = " ".join(reversed(parts))
    return name.replace("&", "and")


def _join(items: list[str]) -> str:
    """Natural-language list; commas inside an item are resolved first."""
    clean = [_speakable(item) for item in items]
    if len(clean) == 1:
        return clean[0]
    return ", ".join(clean[:-1]) + ", or " + clean[-1]


def local_ip_toward(host: str) -> str:
    """The address on this box that `host` would see. No packet is sent."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, 8009))
        return sock.getsockname()[0]
    finally:
        sock.close()


def serve_once(data: bytes, bind_ip: str) -> tuple[str, http.server.HTTPServer]:
    path = "/" + secrets.token_urlsafe(12) + ".mp3"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != path:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer((bind_ip, 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://{bind_ip}:{server.server_port}{path}", server


def synthesize(text: str) -> bytes:
    from gtts import gTTS

    buf = io.BytesIO()
    gTTS(text=text, lang="en", tld="us").write_to_fp(buf)
    return buf.getvalue()


def cast_audio(device: str, audio: bytes, volume: float | None, timeout: float) -> int:
    import pychromecast

    casts, browser = pychromecast.get_chromecasts(timeout=timeout)
    try:
        match = next((c for c in casts if c.cast_info.friendly_name == device), None)
        if match is None:
            names = sorted(c.cast_info.friendly_name for c in casts)
            print(f"No cast device named {device!r}. Found: {names}", file=sys.stderr)
            return 1

        match.wait()
        previous = match.status.volume_level
        if volume is not None:
            match.set_volume(volume)

        url, server = serve_once(audio, local_ip_toward(match.cast_info.host))
        try:
            controller = match.media_controller
            controller.play_media(url, "audio/mp3")
            controller.block_until_active(timeout=30)
            deadline = time.time() + 120
            while time.time() < deadline:
                controller.update_status()
                if controller.status.player_state in ("PLAYING", "BUFFERING"):
                    break
                time.sleep(0.4)
            while time.time() < deadline:
                controller.update_status()
                if controller.status.player_state not in ("PLAYING", "BUFFERING"):
                    break
                time.sleep(0.5)
            time.sleep(1.0)  # let the tail of the clip actually leave the speaker
        finally:
            server.shutdown()
            if volume is not None and previous is not None:
                match.set_volume(previous)
            match.quit_app()
        return 0
    finally:
        pychromecast.discovery.stop_discovery(browser)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("date", nargs="?", default="today")
    ap.add_argument("-d", "--device", default=DEFAULT_DEVICE)
    ap.add_argument("-m", "--meal", default=DEFAULT_MEAL)
    ap.add_argument(
        "-v",
        "--volume",
        type=float,
        default=DEFAULT_VOLUME,
        help="0.0-1.0 for this announcement; prior volume is restored "
        "(default: config's speaker.volume, or leave volume "
        "unchanged if that's unset)",
    )
    ap.add_argument("--dry-run", action="store_true", help="print the sentence, cast nothing")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--discovery-timeout", type=float, default=15.0)
    ap.add_argument("--district", default=menu_mod.DEFAULT_DISTRICT)
    ap.add_argument("--building", default=menu_mod.DEFAULT_BUILDING)
    args = ap.parse_args()

    if args.list_devices:
        import pychromecast

        casts, browser = pychromecast.get_chromecasts(timeout=args.discovery_timeout)
        for cast in casts:
            info = cast.cast_info
            print(f"{info.friendly_name!r}\t{info.model_name}\t{info.host}")
        pychromecast.discovery.stop_discovery(browser)
        return 0

    day = menu_mod.parse_day(args.date)
    text = spoken_text(day, args.meal, args.district, args.building)
    print(text)
    if args.dry_run:
        return 0
    return cast_audio(args.device, synthesize(text), args.volume, args.discovery_timeout)


if __name__ == "__main__":
    raise SystemExit(main())
