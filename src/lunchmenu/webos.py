#!/usr/bin/env python3
"""Pair with, toast to, and launch the browser on LG webOS TVs.

Two TVs matter here because neither is reachable the same way as the Google
Home speaker `announce.py` casts to:

  - The 2025 C5 (living-room-adjacent set) IS a Chromecast target too, but a
    toast/on-screen message isn't something Chromecast does -- webOS's own
    ssap://system.notifications/createToast is the only route to that.
  - The 2020 CX has **no Chromecast built-in at all**. webOS's websocket API
    (ports 3000 plain / 3001 TLS) is the *only* way to reach it from code.

So this module talks webOS directly via `pywebostv`, independent of
`announce.py`'s Chromecast path. It reuses `menu.py` for all
fetch/parse/timezone logic and `announce._speakable` for vendor-name
cleanup -- neither is duplicated here.

Registry, not a hardcoded map
------------------------------
`tvs.json` (in the config dir -- see config.config_dir()) is
`{host: {"room": ..., "model": ..., "mac": ...}}`.
A room name is only ever written by `--pair --host IP --name "Room"` -- the
person standing in front of that exact TV, accepting the pairing prompt on
its screen, is the one who gets to name it. Nothing in this module infers a
room from an IP or a model; there is no such mapping to keep in sync as TVs
get added, replaced, or moved between rooms.

`mac` is for Wake-on-LAN (`--show --wake`, see below) and **must be the ARP
MAC of the interface actually on this network, not the TV's self-reported
`device_id`.** These sets have separate wired and wireless interfaces, and
they don't have to agree: measured live on one set here, `device_id` and the
ARP MAC (`ip neigh` for its IP) reported two *different* MAC addresses --
the ARP one is correct, since that's the interface actually carrying the IP
the rest of this file addresses. A WoL packet built from `device_id` would
target hardware that isn't listening on this LAN and silently do nothing.
Don't "fix" this to read `device_id` instead.

Client keys are a *credential*, not a label, so they live separately in
`webos-keys.json` (mode 600, keyed by host, in the config dir) rather than in
`tvs.json`, which stays a plain hand-editable file -- also in the config dir,
never in this repo.

Hard operational rule: `--pair` is the ONLY code path in this module allowed
to run webOS's registration handshake against a TV that might not already
hold a valid client key -- because that handshake is what throws the "Allow
this app?" prompt onto the screen, and only an operator standing at the TV
should ever trigger that. `--toast` / `--show` only ever attempt a TV whose
key is already on file; a TV with no stored key is skipped, not paired.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import announce as announce_mod  # for _speakable, SPOKEN_CATEGORIES
from . import config
from . import menu as menu_mod


def keys_path() -> Path:
    """Where webos-keys.json lives. Resolved lazily (not at import time) so
    importing this module never creates a directory as a side effect, and so
    tests can redirect LUNCHMENU_CONFIG_DIR after import."""
    return config.config_dir() / "webos-keys.json"


def tvs_path() -> Path:
    """Where tvs.json lives. Resolved lazily -- see keys_path()."""
    return config.config_dir() / "tvs.json"


# LG's current firmware mandates TLS: connecting over plain ws:3000 (the
# pywebostv default) resets the connection at the handshake -- measured live
# against both TVs here on 2026-09-03. wss:3001 is the only port that
# actually completes one; see _connect(). ws:3000 is kept only as a
# last-resort fallback if the secure attempt itself can't connect at all.
WEBOS_SECURE_PORT = 3001
WEBOS_PLAIN_PORT = 3000
TOAST_MAX_CHARS = 180

# Where serve.py publishes the menu page, for `--show`. web.public_host is
# the address a TV browser uses to reach this host (not necessarily the
# bind address serve.py itself listens on -- see serve.py's web.bind_host).
# web.port is shared with serve.py's own listen port -- one config key, two
# readers, kept in sync by construction rather than by hand now.
SERVE_HOST = config.get("web", "public_host")
SERVE_PORT = config.get("web", "port")

# --discover: scans this /24 (tv.discover_subnet in config -- set it to your
# LAN) for anything answering webOS's websocket port. 3001 only -- 3000 is
# reset outright by current firmware (see above), so scanning it would find
# nothing useful.
DISCOVER_SUBNET = config.get("tv", "discover_subnet")
DISCOVER_TCP_TIMEOUT = 0.5
DISCOVER_WORKERS = 64
DISCOVER_SSDP_TIMEOUT = 2.5


def log(msg: str) -> None:
    print(f"webos: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------
# Registry (tvs.json) and keys (webos-keys.json)
# --------------------------------------------------------------------------


def load_registry() -> dict:
    """host -> {"room": str, "model": str|None}.

    Tolerant of a missing or malformed file -- this runs unattended at 7am
    and a bad tvs.json must not take the whole toast job down with it.
    """
    path = tvs_path()
    try:
        raw = path.read_text()
    except FileNotFoundError:
        log(f"{path.name} not found -- no TVs registered by name")
        return {}
    except OSError as exc:
        log(f"could not read {path.name} ({exc}) -- ignoring it")
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("must be a JSON object keyed by host")
        for host, info in data.items():
            if not isinstance(info, dict) or "room" not in info:
                raise ValueError(f'entry for {host!r} missing a "room"')
        return data
    except (json.JSONDecodeError, ValueError) as exc:
        log(f"{path.name} is malformed ({exc}) -- ignoring it")
        return {}


def save_registry(reg: dict) -> None:
    tvs_path().write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")


def load_keys() -> dict:
    """host -> client_key. Never logged, never printed."""
    path = keys_path()
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        log(f"could not read {path.name} ({exc}) -- treating as no keys")
        return {}


def save_keys(keys: dict) -> None:
    path = keys_path()
    path.write_text(json.dumps(keys, indent=2))
    path.chmod(0o600)  # a client key is a standing credential to control the TV


def port_open(host: str, port: int = WEBOS_SECURE_PORT, timeout: float = 2.0) -> bool:
    """Bare TCP connect only -- NOT a websocket/TLS handshake.

    A "yes" means something is listening on 3001, not that pairing or a
    toast will actually succeed there; a TV that accepts the TCP connection
    and then fails the TLS/websocket upgrade would still read as reachable
    here. --toast/--show use this only to skip a TV that's plainly off
    before attempting the real (slower) connect in _connect().
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_targets(
    host: str | None, room: str | None, registry: dict, keys: dict
) -> dict[str, str]:
    """room-label -> host, for --toast/--show. Never contacts the network."""
    if host:
        label = registry.get(host, {}).get("room", host)
        return {label: host}
    if room:
        for h, info in registry.items():
            if info.get("room", "").lower() == room.lower():
                return {info["room"]: h}
        log(f'no TV registered under room "{room}"')
        return {}
    if registry:
        return {info.get("room", h): h for h, info in registry.items()}
    if keys:
        log(f"{tvs_path().name} empty/unavailable -- falling back to hosts in {keys_path().name}")
        return {h: h for h in keys}
    return {}


# --------------------------------------------------------------------------
# Which day "--toast"/"--show" default to (reuses menu.py's SCHOOL_TZ/today())
# --------------------------------------------------------------------------

# After this Eastern hour, "today" in the default case means the next
# school day instead -- nobody wants to hear about a lunch that already
# happened. Only the literal default "today" is subject to this; an
# explicit date argument (a real YYYY-MM-DD, "tomorrow", "yesterday", ...)
# always wins and is never second-guessed.
ROLLOVER_HOUR = 14


def _is_school_day(day: dt.date, district: str, building: str) -> bool:
    """Not a weekend, and not reported closed via an AcademicCalendars note
    with no menu (the same "closed" test menu.py's CLI output uses)."""
    if day.weekday() >= 5:
        return False
    # allow_stale=True: --show must still be able to pick a day to display
    # (and launch the browser there) even if the menu server is down --
    # worst case this misjudges a same-day closure using a stale week, which
    # is far better than --show failing outright at 7am.
    payload = menu_mod.fetch_week(day, district, building, allow_stale=True)
    notes = menu_mod.day_notes(payload, day)
    menu = menu_mod.day_menu(payload, day)
    return bool(menu) or not notes


def _next_school_day(day: dt.date, district: str, building: str, max_tries: int = 10) -> dt.date:
    candidate = day
    for _ in range(max_tries):
        if _is_school_day(candidate, district, building):
            return candidate
        candidate += dt.timedelta(days=1)
    return candidate  # give up and return the last candidate rather than loop forever


def resolve_effective_day(date_arg: str, district: str, building: str) -> tuple[dt.date, str]:
    """(day, when-label) for --toast/--show.

    date_arg is the raw CLI value. Anything other than the literal "today"
    is parsed via menu_mod.parse_day and used exactly as given. Only
    "today" is subject to the after-ROLLOVER_HOUR rollover below.
    """
    if date_arg.strip().lower() != "today":
        day = menu_mod.parse_day(date_arg)
        when = "today" if day == menu_mod.today() else day.strftime("%A")
        return day, when

    base = menu_mod.today()
    now_eastern = dt.datetime.now(menu_mod.SCHOOL_TZ)
    if now_eastern.hour < ROLLOVER_HOUR:
        return base, "today"

    next_day = _next_school_day(base + dt.timedelta(days=1), district, building)
    when = "tomorrow" if next_day == base + dt.timedelta(days=1) else next_day.strftime("%A")
    return next_day, when


# --------------------------------------------------------------------------
# Toast text (reuses menu.py + announce._speakable, doesn't duplicate either)
# --------------------------------------------------------------------------

# A toast is a glance, not a menu board: cap each list to a few items and
# say explicitly when more were cut, rather than silently running out of
# room mid-list.
MAX_ENTREE_ITEMS = 3
MAX_SIDE_ITEMS = 3
ENTREE_CHAR_BUDGET = 100
SIDE_CHAR_BUDGET = 70

# Prefixed when the toast is built from a stale cached week (menu server
# unreachable) instead of a live fetch -- see menu.stale_age(). Short on
# purpose: it comes out of the same TOAST_MAX_CHARS budget as everything
# else, so build_toast_text() shrinks that budget by len(this) rather than
# appending it after the cap and risking blowing past TOAST_MAX_CHARS.
STALE_TOAST_MARKER = "[STALE] "


def build_toast_text(day: dt.date, when: str, meal: str, district: str, building: str) -> str:
    # allow_stale=True: a toast built from yesterday's cached week, clearly
    # marked as such, beats no toast at all when the menu server is down.
    payload = menu_mod.fetch_week(day, district, building, allow_stale=True)
    marker = STALE_TOAST_MARKER if menu_mod.stale_age(payload) is not None else ""
    budget = TOAST_MAX_CHARS - len(marker)

    notes = menu_mod.day_notes(payload, day)
    menus = menu_mod.day_menu(payload, day)
    menus = {k: v for k, v in menus.items() if k.lower() == meal.lower()}

    school = announce_mod.SCHOOL

    if not menus:
        if notes:
            text = f"{school}: no school {when}. {', '.join(notes)}"
        else:
            text = f"{school}: no {meal} menu published for {when}."
        return marker + text[:budget]

    categories = next(iter(menus.values()))
    entrees = [announce_mod._speakable(i) for i in categories.get("Main Entree", [])]
    sides = [
        announce_mod._speakable(item)
        for name in announce_mod.SPOKEN_CATEGORIES[1:]
        for item in categories.get(name, [])
    ]

    entree_part = _list_with_more(entrees, MAX_ENTREE_ITEMS, ENTREE_CHAR_BUDGET)
    text = f"{school} {meal} {when}: {entree_part or '(nothing listed)'}"

    if sides:
        sides_part = _list_with_more(sides, MAX_SIDE_ITEMS, SIDE_CHAR_BUDGET)
        text += f" · Sides: {sides_part}"

    if len(text) > budget:
        # Rare -- only if item names blew both per-list budgets. Cut at the
        # last complete item rather than mid-word.
        text = text[:budget].rsplit(",", 1)[0].rstrip() + "…"
    return marker + text


def _capped_list(items: list[str], max_items: int, budget: int) -> tuple[str, int]:
    """Join up to max_items items within a char budget, item-boundary only
    (never mid-word). Returns (text, shown_count)."""
    if not items:
        return "", 0
    capped = items[:max_items]
    out = capped[0][: max(budget, 0)]
    shown = 1
    for item in capped[1:]:
        candidate = f"{out}, {item}"
        if len(candidate) > budget:
            break
        out = candidate
        shown += 1
    return out, shown


def _list_with_more(items: list[str], max_items: int, budget: int) -> str:
    text, shown = _capped_list(items, max_items, budget)
    dropped = len(items) - shown
    if dropped > 0:
        text += f" +{dropped} more"
    return text


# --------------------------------------------------------------------------
# Custom toast text (--toast --message) -- arbitrary text, no menu fetch at
# all. Kept separate from build_toast_text above: that one is menu-shaped
# (fetches the week, understands categories/notes/staleness); this one is
# just "take the string the operator gave and make it toast-safe."
# --------------------------------------------------------------------------

# Appended when a custom message is truncated to fit TOAST_MAX_CHARS. Sized
# into the cut (see _cap_custom_message) so the final string is still
# <= TOAST_MAX_CHARS, never TOAST_MAX_CHARS + len(this).
CUSTOM_TOAST_ELLIPSIS = "…"


def _cap_custom_message(text: str) -> str:
    """Truncate arbitrary text to TOAST_MAX_CHARS with a trailing ellipsis
    that fits *within* the cap, warning on stderr about the cut rather than
    silently shortening what someone asked to display.

    Deliberately simpler than build_toast_text's item-aware truncation
    (MAX_ENTREE_ITEMS/ENTREE_CHAR_BUDGET/SIDE_CHAR_BUDGET/STALE_TOAST_MARKER)
    -- those are shaped around menu categories and don't mean anything for
    arbitrary text, so this doesn't reuse them.
    """
    if len(text) <= TOAST_MAX_CHARS:
        return text
    original_len = len(text)
    cut_to = TOAST_MAX_CHARS - len(CUSTOM_TOAST_ELLIPSIS)
    truncated = text[:cut_to] + CUSTOM_TOAST_ELLIPSIS
    log(
        f"--message is {original_len} characters, over the {TOAST_MAX_CHARS}-char "
        f"toast cap -- truncated by {original_len - len(truncated)} character(s)"
    )
    return truncated


def prepare_custom_message(raw: str) -> str:
    """Resolve --message's raw CLI value into toast-ready text, WITHOUT
    fetching anything -- no menu.get, no fetch_week, no district/building
    lookup. That's the whole point: a custom toast must work even with the
    school API completely unreachable.

    '-' means read the message from stdin instead of argv, so this is usable
    from a cron job, CI step, or any other script piping text in, not only a
    literal argv string.

    Only a single trailing newline is stripped (the one a shell `echo` or a
    text editor's save typically leaves) -- interior newlines are collapsed
    to a single space rather than preserved: pywebostv's
    SystemControl.notify() (see _send_toast) forwards the string as-is into
    the toast's JSON "message" field with no line-handling of its own, and
    webOS's toast UI is not documented to render multi-line text -- every
    other toast built in this file (build_toast_text) is a single line by
    construction, so a custom message is normalized the same way for
    predictable on-screen rendering.

    Raises ValueError if the result is empty or whitespace-only -- an
    accidentally blank message is a mistake to report, not an empty toast to
    send.
    """
    text = sys.stdin.read() if raw == "-" else raw
    if text.endswith("\n"):
        text = text[:-1]
    text = text.replace("\n", " ")
    if not text.strip():
        raise ValueError("--message is empty (or whitespace-only) -- nothing to send")
    return _cap_custom_message(text)


# --------------------------------------------------------------------------
# webOS wire calls -- each opens its own short-lived connection
# --------------------------------------------------------------------------


def _diagnose(host: str, exc: Exception | None) -> str:
    """Turn a raw connect exception into one printable line. Never let a
    ConnectionResetError/OSError traceback reach the operator directly."""
    if exc is None:
        return f"{host}: unknown connection failure"
    if isinstance(exc, ConnectionResetError):
        return (
            f"{host}: connection reset on both wss:{WEBOS_SECURE_PORT} and "
            f"ws:{WEBOS_PLAIN_PORT} -- TV is likely off, asleep, or refusing "
            f"the app manifest"
        )
    if isinstance(exc, TimeoutError):
        return f"{host}: connection timed out -- TV is likely off or unreachable"
    if isinstance(exc, ConnectionRefusedError):
        return f"{host}: connection refused -- webOS service not listening"
    if isinstance(exc, OSError):
        return f"{host}: network error ({exc})"
    return f"{host}: {type(exc).__name__}: {exc}"


def _connect(host: str):
    """Open a websocket connection to the TV: secure (wss:3001) first, since
    current LG firmware resets a plain ws:3000 connect outright (measured
    live 2026-09-03 against both TVs here -- see the constants above).
    Falls back to plain ws only if the secure attempt itself fails to
    connect. Returns (client, secure_used).

    ws4py disables TLS certificate verification by default for a wss:// URL
    here (pywebostv's WebOSClient never sets ssl_options["cert_reqs"], and
    ws4py treats an absent cert_reqs as "skip verification") -- correct for
    a LAN TV serving a self-signed cert; don't "fix" this by adding
    cert_reqs, it would just break every secure connect in this file.

    Raises ConnectionError with a one-line diagnosis (never a raw
    traceback) if both attempts fail.
    """
    from pywebostv.connection import WebOSClient

    last_exc: Exception | None = None
    for secure in (True, False):
        client = WebOSClient(host, secure=secure)
        try:
            client.connect()
            return client, secure
        except Exception as exc:
            last_exc = exc
    raise ConnectionError(_diagnose(host, last_exc))


def _port_label(secure: bool) -> str:
    """'wss:3001' or 'ws:3000', for log/status lines -- shared by
    _registered_client() and _pair() so the two don't drift apart."""
    return f"wss:{WEBOS_SECURE_PORT}" if secure else f"ws:{WEBOS_PLAIN_PORT}"


def _registered_client(host: str, client_key: str, timeout: float = 15.0):
    """Connect and complete the (already-keyed) register handshake.

    Only called with a client_key already on file, so this should never
    surface a pairing prompt on the TV -- see the module docstring. If the
    key has been revoked TV-side, webOS itself will re-prompt regardless of
    anything this code does; that risk is inherent to the protocol, not
    something --toast/--show can avoid, and is why --toast logs it as an
    error rather than hanging silently.
    """
    client, secure = _connect(host)
    log(f"{host}: connected via {_port_label(secure)}")
    store = {"client_key": client_key}
    try:
        for _ in client.register(store, timeout=timeout):
            pass
    except Exception as exc:
        client.close()
        if str(exc) == "Timeout.":
            raise ConnectionError(
                f"{host}: no register response within {int(timeout)}s -- "
                f"stored key may have been revoked"
            ) from None
        raise ConnectionError(f"{host}: register failed -- {exc}") from None
    return client


def _send_toast(host: str, client_key: str, text: str) -> None:
    from pywebostv.controls import SystemControl

    client = _registered_client(host, client_key)
    try:
        SystemControl(client).notify(text)
    finally:
        client.close()


def _launch_browser(host: str, client_key: str, url: str) -> None:
    """Launch the TV's browser at `url`.

    Not exercised live during this build (network calls to real TVs were
    restricted to a bare TCP check, and to a connect/close handshake check
    once TLS was found mandatory) -- content_id and params.target are both
    sent because different webOS firmware versions have been observed to
    key off either for the browser app; operator should confirm this
    physically after pairing.
    """
    from pywebostv.controls import ApplicationControl
    from pywebostv.model import Application

    client = _registered_client(host, client_key)
    try:
        app = Application({"id": "com.webos.app.browser", "title": "Browser"})
        ApplicationControl(client).launch(app, content_id=url, params={"target": url})
    finally:
        client.close()


LAUNCH_RETRY_ATTEMPTS = 5
LAUNCH_RETRY_DELAY = 3.0  # seconds between attempts


def _launch_browser_retrying(
    host: str,
    client_key: str,
    url: str,
    attempts: int = LAUNCH_RETRY_ATTEMPTS,
    delay: float = LAUNCH_RETRY_DELAY,
) -> None:
    """_launch_browser, retried a few times.

    A TV that just woke from standby (see wake_on_lan below) will accept a
    TCP connection on 3001 before its app launcher is actually ready to
    accept a launch request -- so a single fire-and-forget launch right
    after the socket opens can silently do nothing. Retrying with a short
    delay is cheap and correct for the common case (already-on TV: the
    first attempt just succeeds immediately) as well as the just-woke one.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            _launch_browser(host, client_key, url)
            return
        except Exception as exc:
            last_exc = exc
            if attempt < attempts:
                time.sleep(delay)
    raise last_exc  # type: ignore[misc] -- attempts >= 1 guarantees this is set


# --------------------------------------------------------------------------
# Wake-on-LAN (--show --wake)
# --------------------------------------------------------------------------

WOL_PORT = 9


def _wol_targets() -> tuple[tuple[str, int], ...]:
    """Broadcast to BOTH the limited broadcast address and this /24's own
    broadcast address -- some routers only forward one or the other, and
    sending twice is cheap. Derived from DISCOVER_SUBNET (tv.discover_subnet
    in config) rather than hardcoded again, so the two stay in sync if the
    house LAN ever changes. Computed lazily, not at import time, since
    DISCOVER_SUBNET is empty by default until configured."""
    targets: list[tuple[str, int]] = [("255.255.255.255", WOL_PORT)]
    if DISCOVER_SUBNET:
        net = ipaddress.ip_network(DISCOVER_SUBNET, strict=False)
        targets.append((str(net.broadcast_address), WOL_PORT))
    return tuple(targets)


DEFAULT_WAKE_TIMEOUT = 60.0
WAKE_POLL_HEARTBEAT = 10.0  # how often to log "still waiting for it to wake"


def build_magic_packet(mac: str) -> bytes:
    """6 bytes of 0xFF followed by the target MAC repeated 16 times --
    the standard Wake-on-LAN magic packet, per the spec. `mac` accepts
    ':'- or '-'-separated hex, or bare hex."""
    hex_mac = mac.replace(":", "").replace("-", "").strip().lower()
    if len(hex_mac) != 12:
        raise ValueError(f"not a MAC address: {mac!r}")
    mac_bytes = bytes.fromhex(hex_mac)
    return b"\xff" * 6 + mac_bytes * 16


def wake_on_lan(mac: str, targets=None) -> None:
    """Broadcast the magic packet for `mac` to every (ip, port) in
    `targets` (default _wol_targets() -- both broadcast addresses, see
    above). UDP, SO_BROADCAST. `targets` is overridable so this can be
    unit-tested against a local socket instead of the real broadcast
    addresses.

    Fire-and-forget by nature: a send failure to one target is logged, not
    raised -- the caller finds out whether waking actually worked by
    polling the TV's port, not from this function returning cleanly.
    """
    packet = build_magic_packet(mac)
    send_targets = _wol_targets() if targets is None else targets
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        for ip, port in send_targets:
            try:
                sock.sendto(packet, (ip, port))
            except OSError as exc:
                log(f"WoL send to {ip}:{port} failed: {exc}")
    finally:
        sock.close()


def _wait_for_port(
    host: str, port: int, timeout: float, heartbeat: float = WAKE_POLL_HEARTBEAT
) -> bool:
    """Poll host:port until it accepts a TCP connect, up to `timeout`
    seconds, logging roughly every `heartbeat` seconds. Returns whether it
    came up -- never raises; a TV that stays dark is not this function's
    error to report."""
    started = time.monotonic()
    deadline = started + timeout
    next_log = started + heartbeat
    while True:
        if port_open(host, port, timeout=2.0):
            return True
        now = time.monotonic()
        if now >= deadline:
            return False
        if now >= next_log:
            log(f"{host}: still waiting for it to wake -- {int(deadline - now)}s left")
            next_log = now + heartbeat
        time.sleep(min(2.0, max(deadline - now, 0)))


# 60s repeatedly proved too short to walk to a TV and find the remote
# (measured live against one set here: 60s timed out twice, 150s succeeded).
DEFAULT_PAIR_TIMEOUT = 150.0
PAIR_HEARTBEAT = 15.0  # how often to print "still waiting" while blocked


def _pair(
    host: str,
    existing_key: str | None,
    timeout: float = DEFAULT_PAIR_TIMEOUT,
    heartbeat: float = PAIR_HEARTBEAT,
):
    """Run the registration handshake, prompting on-screen if needed.

    The actual register() call happens in a background thread so this can
    print a periodic "still waiting" heartbeat without touching the
    pairing protocol -- an earlier design considered re-issuing register()
    in short chunks to get a heartbeat, but each chunk sends a fresh
    'register' request over the wire, an on-wire behavior change that was
    never tested against real hardware. Polling a thread's completion
    instead changes nothing protocol-level: exactly one register() call is
    made, exactly like before, just observed from outside instead of
    blocked on synchronously.

    Returns the new client_key and an open, registered client (caller must
    close it). Raises RuntimeError on failure with a message meant to be
    printed as-is -- never a raw traceback -- that distinguishes "no prompt
    ever appeared" from "a prompt appeared but wasn't accepted in time",
    since those have different fixes.
    """
    from pywebostv.connection import WebOSClient

    try:
        client, secure = _connect(host)
    except ConnectionError as exc:
        raise RuntimeError(str(exc)) from None
    mode = "secure" if secure else "plain"
    print(f"  connected via {_port_label(secure)} ({mode})")

    store = {}
    if existing_key:
        store["client_key"] = existing_key

    result: dict = {}

    def _run() -> None:
        try:
            for status in client.register(store, timeout=timeout):
                if status == WebOSClient.PROMPTED:
                    result["prompted"] = True
                    print(
                        f"  >> Accept the pairing request on the TV screen "
                        f"at {host} now ({int(timeout)}s to respond) <<"
                    )
                elif status == WebOSClient.REGISTERED:
                    result["registered"] = True
        except Exception as exc:
            result["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    started = time.monotonic()
    worker.start()
    while worker.is_alive():
        worker.join(timeout=heartbeat)
        if worker.is_alive():
            left = max(timeout - (time.monotonic() - started), 0)
            print(
                f"  ... still waiting for the prompt to be accepted on {host} -- {int(left)}s left"
            )

    if result.get("registered"):
        print("  Paired.")
        return store["client_key"], client

    client.close()
    exc = result.get("error")
    timed_out = exc is not None and str(exc) == "Timeout."
    if timed_out and result.get("prompted"):
        raise RuntimeError(
            f"No prompt was accepted on {host} within {int(timeout)}s. "
            f"Nothing was stored. A prompt DID appear on screen but wasn't "
            f"accepted in time -- re-run with a longer --timeout (e.g. "
            f"--timeout {int(timeout * 2)}) and accept it as soon as it "
            f"shows up."
        ) from None
    if timed_out:
        raise RuntimeError(
            f"No prompt appeared on {host} within {int(timeout)}s. Nothing "
            f"was stored. Check the TV is on and reachable (Quick Start+ "
            f"helps), then re-run -- if it's just slow to show the prompt, "
            f"a longer --timeout will also help."
        ) from None
    if exc is not None:
        raise RuntimeError(f"Pairing with {host} failed: {exc}") from None
    raise RuntimeError(
        f"Pairing with {host} failed: no prompt and no "
        f"registration (unexpected -- no exception raised either)."
    )


# --------------------------------------------------------------------------
# --discover: find webOS TVs on the LAN without hand-running a port scan
# --------------------------------------------------------------------------


def _probe_host(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _discover_hosts(subnet: str, port: int, timeout: float, workers: int) -> list[str]:
    """Bare TCP connect sweep of `subnet` on `port`, concurrent. Returns
    hosts sorted numerically. A hit means something is listening -- not
    that it's a TV, though in practice only webOS answers on 3001 here."""
    net = ipaddress.ip_network(subnet, strict=False)
    found: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_probe_host, str(ip), port, timeout): str(ip) for ip in net.hosts()}
        for fut in as_completed(futures):
            host = futures[fut]
            try:
                if fut.result():
                    found.append(host)
            except Exception:
                pass
    return sorted(found, key=lambda ip: tuple(int(p) for p in ip.split(".")))


def _ssdp_discover(timeout: float) -> dict[str, dict]:
    """Best-effort SSDP/UPnP probe: source IP -> {"friendlyName", "modelName"}.

    Some sets answer this and some don't (measured live against a TV on this
    LAN: it answers the webOS websocket on 3001 but returns no SSDP
    description at all) -- that is why --discover always prints the bare IP
    from the TCP sweep above
    rather than relying on this to find anything. Never raises; any network
    or parsing problem here just means fewer names, not a failed command.
    """
    import urllib.request
    import xml.etree.ElementTree as ET

    request = (
        b"M-SEARCH * HTTP/1.1\r\n"
        b"HOST: 239.255.255.250:1900\r\n"
        b'MAN: "ssdp:discover"\r\n'
        b"MX: 2\r\n"
        b"ST: ssdp:all\r\n\r\n"
    )

    results: dict[str, dict] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(request, ("239.255.255.250", 1900))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, (ip, _port) = sock.recvfrom(65535)
            except (TimeoutError, OSError):
                break
            if ip in results:
                continue
            headers = {}
            for line in data.decode(errors="ignore").split("\r\n")[1:]:
                if ":" in line:
                    k, _, v = line.partition(":")
                    headers[k.strip().upper()] = v.strip()
            location = headers.get("LOCATION")
            if not location:
                continue
            try:
                with urllib.request.urlopen(location, timeout=2.0) as resp:
                    root = ET.fromstring(resp.read())
                ns = {"u": "urn:schemas-upnp-org:device-1-0"}
                device = root.find(".//u:device", ns)
                friendly = (
                    device.findtext("u:friendlyName", namespaces=ns) if device is not None else None
                )
                model = (
                    device.findtext("u:modelName", namespaces=ns) if device is not None else None
                )
                if friendly or model:
                    results[ip] = {"friendlyName": friendly, "modelName": model}
            except Exception:
                continue
    except OSError as exc:
        log(f"SSDP discovery unavailable ({exc}) -- names/models will be blank")
    finally:
        sock.close()
    return results


def cmd_discover(args) -> int:
    print(
        f"Scanning {DISCOVER_SUBNET} port {WEBOS_SECURE_PORT} "
        f"(webOS websocket; 3000 is reset by current firmware, not worth "
        f"scanning) ..."
    )
    print(
        "(A TV only answers here if it's powered on, or has Quick Start+ "
        'enabled while nominally "off". Turning on one TV has been '
        "observed to surface more than one new host at once -- measured "
        "here, powering on one set also surfaced a second, "
        "still-unidentified host on the same network.)"
    )

    hosts = _discover_hosts(
        DISCOVER_SUBNET, WEBOS_SECURE_PORT, DISCOVER_TCP_TIMEOUT, DISCOVER_WORKERS
    )
    if not hosts:
        print("No hosts found listening on port 3001.")
        return 0

    print(
        f"Found {len(hosts)}. Probing SSDP/UPnP for names "
        f"(some sets won't answer -- that's normal, the IP is still shown) ..."
    )
    names = _ssdp_discover(DISCOVER_SSDP_TIMEOUT)

    registry = load_registry()
    print()
    print(f"{'HOST':<15} {'REGISTERED AS':<18} NAME / MODEL (SSDP, best-effort)")
    for host in hosts:
        info = registry.get(host)
        registered_as = info["room"] if info else "-"
        meta = names.get(host)
        if meta:
            label = " / ".join(x for x in (meta.get("friendlyName"), meta.get("modelName")) if x)
            label = label or "(SSDP responded, no name/model in it)"
        else:
            label = "(no SSDP response)"
        print(f"{host:<15} {registered_as:<18} {label}")
        if not info:
            print(
                f"    not registered -- to pair: lunchmenu-webos "
                f'--pair --host {host} --name "Room Name"'
            )
    return 0


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_pair(args) -> int:
    if not args.host:
        print("--pair requires --host IP", file=sys.stderr)
        return 1

    registry = load_registry()
    keys = load_keys()
    existing = registry.get(args.host)
    name = args.name or (existing or {}).get("room")
    if not name:
        print(
            f'{args.host} is not in {tvs_path().name} yet -- pass --name "Room" to register it.',
            file=sys.stderr,
        )
        return 1

    print(f"Connecting to {args.host} ...")
    try:
        client_key, client = _pair(args.host, keys.get(args.host), timeout=args.timeout)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # last resort -- never show a raw traceback
        print(f"Unexpected error pairing with {args.host}: {exc}", file=sys.stderr)
        return 1

    keys[args.host] = client_key
    save_keys(keys)
    registry[args.host] = {
        "room": name,
        "model": args.model or (existing or {}).get("model"),
    }
    save_registry(registry)

    # The one toast the pairing path may send: confirming the label on the
    # exact screen the operator is standing in front of, right now.
    try:
        from pywebostv.controls import SystemControl

        SystemControl(client).notify(f'Paired as "{name}"')
    except Exception as exc:
        print(f"Paired, but the confirmation toast failed: {exc}", file=sys.stderr)
    finally:
        client.close()

    print(f'Registered {args.host} as "{name}".')
    return 0


def cmd_list(args) -> int:
    registry = load_registry()
    keys = load_keys()
    hosts = sorted(set(registry) | set(keys))
    if not hosts:
        print(f"No TVs in {tvs_path().name} or {keys_path().name}.")
        return 0
    print(f"{'ROOM':<18} {'HOST':<15} {'MODEL':<32} {'KEY':<5} REACHABLE")
    for host in hosts:
        info = registry.get(host, {})
        room = info.get("room", "(unnamed)")
        model = info.get("model") or "?"
        has_key = "yes" if host in keys else "no"
        reachable = "yes" if port_open(host) else "no"
        print(f"{room:<18} {host:<15} {model:<32} {has_key:<5} {reachable}")
    print(
        f"(REACHABLE: bare TCP connect to {WEBOS_SECURE_PORT} only -- confirms "
        f"something is listening, not that the TLS/websocket handshake or "
        f"pairing will succeed)"
    )
    return 0


def cmd_toast(args) -> int:
    registry = load_registry()
    keys = load_keys()
    targets = resolve_targets(args.host, args.room, registry, keys)

    if args.message is not None:
        # Custom text: no menu fetch, no district/building involved at all.
        try:
            text = prepare_custom_message(args.message)
        except ValueError as exc:
            log(str(exc))
            return 1
    else:
        day, when = resolve_effective_day(args.date, args.district, args.building)
        text = build_toast_text(day, when, args.meal, args.district, args.building)

    if args.dry_run:
        who = list(targets.values()) or "(none)"
        print(f"[dry-run] would toast to {who}: {text!r}")
        return 0

    if not targets:
        log("no TVs to toast (nothing registered and no fallback keys)")
        return 0

    sent = errored = False
    for room, host in targets.items():
        key = keys.get(host)
        if not key:
            log(f"{room} ({host}): no stored key -- skipped (pair it first)")
            continue
        if not port_open(host):
            log(f"{room} ({host}): unreachable (port {WEBOS_SECURE_PORT} closed) -- skipped")
            continue
        try:
            _send_toast(host, key, text)
            log(f"{room} ({host}): toast sent")
            sent = True
        except Exception as exc:
            log(f"{room} ({host}): ERROR -- {exc}")
            errored = True

    if sent:
        return 0
    return 1 if errored else 0


def cmd_show(args) -> int:
    registry = load_registry()
    keys = load_keys()
    targets = resolve_targets(args.host, args.room, registry, keys)

    day, _when = resolve_effective_day(args.date, args.district, args.building)
    # A bare URL for the literal-today case, on purpose: serve.py now
    # auto-reloads every 15min (see its <meta refresh>), and a TV parked on
    # a URL pinned to today's explicit ?date= would keep reloading THAT
    # date forever, never rolling over at midnight. Bare "/" re-resolves
    # "today" server-side on every reload, so it rolls over correctly. Only
    # pin an explicit ?date= when this is genuinely a different day (an
    # explicit date argument, or the after-14:00 rollover) -- a deliberate
    # one-off view that should stay put, not one meant to be left running.
    if day == menu_mod.today():
        url = f"http://{SERVE_HOST}:{SERVE_PORT}/"
    else:
        url = f"http://{SERVE_HOST}:{SERVE_PORT}/?date={day.isoformat()}"

    if args.dry_run:
        who = list(targets.values()) or "(none)"
        wake_note = " (would send WoL first to any that are off)" if args.wake else ""
        print(f"[dry-run] would launch browser at {url} on: {who}{wake_note}")
        return 0

    if not targets:
        log("no TVs to show (nothing registered and no fallback keys)")
        return 0

    ok = errored = False
    for room, host in targets.items():
        key = keys.get(host)
        if not key:
            log(f"{room} ({host}): no stored key -- skipped (pair it first)")
            continue

        reachable = port_open(host)
        if not reachable and args.wake:
            mac = registry.get(host, {}).get("mac")
            if not mac:
                log(
                    f"{room} ({host}): --wake requested but no MAC on file "
                    f"-- proceeding without waking it"
                )
            else:
                log(f"{room} ({host}): sending Wake-on-LAN to {mac}")
                wake_on_lan(mac)
                reachable = _wait_for_port(host, WEBOS_SECURE_PORT, args.wake_timeout)
                if reachable:
                    log(f"{room} ({host}): woke up")
                else:
                    log(
                        f"{room} ({host}): never came up within "
                        f"{int(args.wake_timeout)}s -- skipped (not a "
                        f"failure; it may simply be off, or WoL may need "
                        f"enabling in the TV's network settings)"
                    )
                    continue

        if not reachable:
            log(f"{room} ({host}): unreachable (port {WEBOS_SECURE_PORT} closed) -- skipped")
            continue

        try:
            _launch_browser_retrying(host, key, url)
            log(f"{room} ({host}): browser launched at {url}")
            ok = True
        except Exception as exc:
            log(f"{room} ({host}): ERROR -- {exc}")
            errored = True

    if ok:
        return 0
    return 1 if errored else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "date",
        nargs="?",
        default="today",
        help="for --toast/--show: today | tomorrow | YYYY-MM-DD | ...",
    )
    ap.add_argument("-m", "--meal", default="lunch")
    ap.add_argument("--host", help="address one TV directly by IP")
    ap.add_argument("--room", help="address one TV by its registered room name (--toast/--show)")
    ap.add_argument(
        "--name",
        help="room name to register --host under "
        "(--pair; required for a host not yet in "
        f"{tvs_path().name})",
    )
    ap.add_argument("--model", help="optional model string to record (--pair)")
    ap.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_PAIR_TIMEOUT,
        help=f"seconds to wait for the pairing prompt to be "
        f"accepted (--pair only; default {int(DEFAULT_PAIR_TIMEOUT)} "
        f"-- 60s repeatedly proved too short to walk to the "
        f"TV and find the remote)",
    )
    ap.add_argument("--district", default=menu_mod.DEFAULT_DISTRICT)
    ap.add_argument("--building", default=menu_mod.DEFAULT_BUILDING)
    ap.add_argument(
        "--wake",
        action="store_true",
        help="(--show only) send Wake-on-LAN first to any target TV that isn't already reachable",
    )
    ap.add_argument(
        "--wake-timeout",
        type=float,
        default=DEFAULT_WAKE_TIMEOUT,
        help=f"seconds to wait for a woken TV to answer its "
        f"webOS port before giving up on it (default "
        f"{int(DEFAULT_WAKE_TIMEOUT)})",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="print what would be sent; touch no network"
    )
    ap.add_argument(
        "--message",
        metavar="TEXT",
        help="(--toast only) send this exact text instead of the generated "
        "menu -- no menu fetch happens at all. Pass '-' to read the message "
        f"from stdin. Over {TOAST_MAX_CHARS} characters is truncated with a "
        "warning; empty/whitespace-only text is an error.",
    )

    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--pair", action="store_true", help="pair with --host, prompting on its screen"
    )
    group.add_argument("--toast", action="store_true", help="send the menu as an on-screen toast")
    group.add_argument("--show", action="store_true", help="launch the TV browser at the menu page")
    group.add_argument(
        "--list", action="store_true", help="list registered TVs and their reachability"
    )
    group.add_argument(
        "--discover", action="store_true", help="scan the LAN for webOS TVs not yet in tvs.json"
    )

    args = ap.parse_args()

    if args.message is not None and not args.toast:
        ap.error("--message can only be used with --toast")

    try:
        if args.pair:
            return cmd_pair(args)
        if args.list:
            return cmd_list(args)
        if args.toast:
            return cmd_toast(args)
        if args.show:
            return cmd_show(args)
        if args.discover:
            return cmd_discover(args)
        return 1  # unreachable -- the mutually exclusive group is required
    except Exception as exc:
        # Last-resort net: every command above already catches and diagnoses
        # its own connection errors, but this guarantees the operator never
        # sees a raw traceback no matter what goes wrong underneath.
        log(f"unexpected error -- {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
