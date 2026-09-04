#!/usr/bin/env python3
"""Read-only health check for a lunchmenu deployment.

Runs a fixed list of checks -- config, the LINQ API, the cast speaker, each
registered webOS TV, the web page, systemd, filesystem permissions, and the
disk cache -- and prints one pass/warn/fail line per check.

**This command is strictly read-only.** It is meant to be run any time,
including while people are in the room: it never sends a toast, casts
audio, launches a TV browser, or triggers a pairing prompt. A TV check is a
bare TCP connect to its webOS port (see webos.port_open) followed by closing
the socket immediately -- nothing that could put anything on a screen. The
cast-speaker check is mDNS discovery only (pychromecast.get_chromecasts),
never play_media. If a check needs a mutating operation to verify properly,
it is either skipped with a note, or downgraded to "can't confirm" rather
than performing that operation.

Exit status is 0 unless at least one check FAILs; warnings alone exit 0.
"""

from __future__ import annotations

import argparse
import contextlib
import json as json_mod
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import announce as announce_mod
from . import config
from . import menu as menu_mod
from . import webos as webos_mod

# Values straight out of config.example.toml -- present but never actually
# filled in is exactly as broken as empty, so both are treated as
# "not configured" by the config check below.
PLACEHOLDER_VALUES = {
    "ABCD12",
    "00000000-0000-0000-0000-000000000000",
    "Example Elementary",
    "192.0.2.10",
    "192.0.2.0/24",
}

STATUS_ORDER = {"PASS": 0, "WARN": 1, "FAIL": 2, "SKIP": 0}


@dataclass
class Check:
    name: str
    status: str  # PASS | WARN | FAIL | SKIP
    message: str
    details: dict = field(default_factory=dict)


def _check(name: str, status: str, message: str, **details) -> Check:
    assert status in ("PASS", "WARN", "FAIL", "SKIP")
    return Check(name=name, status=status, message=message, details=details)


# ---------------------------------------------------------------------------
# 1. Config
# ---------------------------------------------------------------------------


def _config_source() -> str:
    import os

    env_overrides = [
        f"LUNCHMENU_{section.upper()}_{key.upper()}"
        for section, keys in config._SCHEMA.items()
        for key in keys
        if f"LUNCHMENU_{section.upper()}_{key.upper()}" in os.environ
    ]
    for path in reversed(config._candidate_files()):
        if path.exists():
            file_source = str(path)
            break
    else:
        file_source = "none found -- built-in defaults only"
    if env_overrides:
        overrides = ", ".join(sorted(env_overrides))
        return f"{file_source} (+ {len(env_overrides)} env var override(s): {overrides})"
    return file_source


def check_config() -> Check:
    source = _config_source()
    required = {
        "school.identifier": config.get("school", "identifier"),
        "school.district_id": config.get("school", "district_id"),
        "school.building_id": config.get("school", "building_id"),
        "school.name": config.get("school", "name"),
    }
    missing = [k for k, v in required.items() if not v]
    placeholder = [k for k, v in required.items() if v in PLACEHOLDER_VALUES]
    if missing:
        return _check(
            "config", "FAIL", f"missing required key(s): {', '.join(missing)} (source: {source})"
        )
    if placeholder:
        return _check(
            "config",
            "FAIL",
            f"still set to the config.example.toml placeholder: {', '.join(placeholder)} "
            f"(source: {source})",
        )
    return _check("config", "PASS", f"located at {source}; all required keys set")


# ---------------------------------------------------------------------------
# 2. LINQ API + building_id
# ---------------------------------------------------------------------------


def check_linq_api() -> tuple[Check, dict | None]:
    identifier = config.get("school", "identifier")
    if not identifier:
        return _check("linq_api", "FAIL", "school.identifier is not configured"), None
    try:
        ident = menu_mod.get("FamilyMenuIdentifier", identifier=identifier)
    except Exception as exc:
        return _check("linq_api", "FAIL", f"could not reach the LINQ API: {exc}"), None
    return (
        _check("linq_api", "PASS", f"reachable; district {ident.get('DistrictName', '?')!r}"),
        ident,
    )


def check_building_id(ident: dict | None) -> Check:
    building_id = config.get("school", "building_id")
    if ident is None:
        return _check("building_id", "SKIP", "skipped -- the LINQ API check above failed")
    if not building_id:
        return _check("building_id", "FAIL", "school.building_id is not configured")
    buildings = ident.get("Buildings", [])
    match = next((b for b in buildings if b.get("BuildingId") == building_id), None)
    if match is None:
        return _check(
            "building_id",
            "FAIL",
            f"{building_id} not found among {len(buildings)} building(s) for this district "
            f"(run `lunchmenu --list-buildings` to see valid ids)",
        )
    return _check("building_id", "PASS", f"resolves to {match.get('Name', '?')!r}")


# ---------------------------------------------------------------------------
# 3. Cast speaker
# ---------------------------------------------------------------------------


def check_speaker(discovery_timeout: float = 8.0) -> Check:
    device = config.get("speaker", "device")
    if not device:
        return _check("speaker", "WARN", "[speaker] not configured -- announce.py has no target")
    try:
        import pychromecast
    except ImportError as exc:
        return _check("speaker", "FAIL", f"pychromecast not importable: {exc}")

    try:
        casts, browser = pychromecast.get_chromecasts(timeout=discovery_timeout)
    except Exception as exc:
        return _check("speaker", "FAIL", f"cast discovery failed: {exc}")
    try:
        names = sorted(c.cast_info.friendly_name for c in casts)
        if device in names:
            return _check("speaker", "PASS", f"{device!r} is discoverable")
        return _check("speaker", "FAIL", f"{device!r} not found among discovered devices: {names}")
    finally:
        with contextlib.suppress(Exception):
            pychromecast.discovery.stop_discovery(browser)


# ---------------------------------------------------------------------------
# 4. webOS TVs
# ---------------------------------------------------------------------------


def check_tvs() -> list[Check]:
    registry = webos_mod.load_registry()
    keys = webos_mod.load_keys()
    hosts = sorted(set(registry) | set(keys))
    if not hosts:
        return [_check("tv", "WARN", "no TVs registered (tvs.json / webos-keys.json both empty)")]

    checks = []
    for host in hosts:
        info = registry.get(host, {})
        room = info.get("room", host)
        has_key = host in keys
        mac = info.get("mac")
        # Bare TCP connect+close only -- see webos.port_open's own docstring.
        # Never a websocket/TLS handshake, never register(), never a toast.
        reachable = webos_mod.port_open(host)

        if not has_key:
            checks.append(
                _check(f"tv:{room}", "FAIL", f"{host}: no stored pairing key -- pair it first")
            )
            continue
        if not reachable:
            checks.append(
                _check(
                    f"tv:{room}",
                    "WARN",
                    f"{host}: port {webos_mod.WEBOS_SECURE_PORT} not reachable (may simply be off)",
                )
            )
            continue
        if not mac:
            checks.append(
                _check(
                    f"tv:{room}",
                    "WARN",
                    f"{host}: reachable, keyed, but no MAC on file (--wake won't work)",
                )
            )
            continue
        checks.append(_check(f"tv:{room}", "PASS", f"{host}: reachable, keyed, MAC on file"))
    return checks


# ---------------------------------------------------------------------------
# 5. Web page
# ---------------------------------------------------------------------------


def check_web() -> list[Check]:
    port = config.get("web", "port")
    public_host = config.get("web", "public_host")
    checks = []

    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/", headers={"User-Agent": "lunchmenu-doctor"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = resp.status
        if status == 200:
            checks.append(_check("web:local", "PASS", f"http://127.0.0.1:{port}/ answers (200)"))
        else:
            checks.append(
                _check("web:local", "WARN", f"http://127.0.0.1:{port}/ answered {status}")
            )
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        checks.append(
            _check("web:local", "FAIL", f"nothing answering on port {port} locally: {exc}")
        )

    if not public_host:
        checks.append(_check("web:public_host", "WARN", "web.public_host is not configured"))
    else:
        if webos_mod.port_open(public_host, port, timeout=3.0):
            checks.append(_check("web:public_host", "PASS", f"{public_host}:{port} is reachable"))
        else:
            checks.append(
                _check(
                    "web:public_host",
                    "WARN",
                    f"{public_host}:{port} not reachable from here "
                    f"(may still be fine for a TV elsewhere on the LAN)",
                )
            )
    return checks


# ---------------------------------------------------------------------------
# 6. systemd
# ---------------------------------------------------------------------------

SYSTEMD_UNITS = ("lunch-announce.timer", "lunch-announce.service", "lunchmenu-web.service")


def _systemctl(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["systemctl", "--user", *args], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def check_systemd() -> list[Check]:
    if shutil.which("systemctl") is None:
        return [_check("systemd", "SKIP", "systemctl not found on this host")]

    checks = []

    timer_props = _systemctl(
        "show", "lunch-announce.timer", "-p", "UnitFileState", "-p", "ActiveState"
    )
    if timer_props is None:
        checks.append(_check("systemd:timer", "SKIP", "could not query systemd --user"))
    else:
        props = dict(line.split("=", 1) for line in timer_props.splitlines() if "=" in line)
        enabled = props.get("UnitFileState", "?")
        active = props.get("ActiveState", "?")
        if enabled == "enabled" and active == "active":
            checks.append(
                _check("systemd:timer", "PASS", f"lunch-announce.timer {enabled}/{active}")
            )
        else:
            checks.append(
                _check("systemd:timer", "FAIL", f"lunch-announce.timer {enabled}/{active}")
            )

    service_props = _systemctl(
        "show", "lunch-announce.service", "-p", "Result", "-p", "ExecMainStatus"
    )
    if service_props is None:
        checks.append(_check("systemd:announce", "SKIP", "could not query systemd --user"))
    else:
        props = dict(line.split("=", 1) for line in service_props.splitlines() if "=" in line)
        result = props.get("Result", "?")
        if result in ("success", "?"):
            checks.append(_check("systemd:announce", "PASS", f"last run result: {result}"))
        else:
            checks.append(_check("systemd:announce", "WARN", f"last run result: {result}"))

    web_props = _systemctl(
        "show", "lunchmenu-web.service", "-p", "ActiveState", "-p", "UnitFileState"
    )
    if web_props is None:
        checks.append(_check("systemd:web", "SKIP", "could not query systemd --user"))
    else:
        props = dict(line.split("=", 1) for line in web_props.splitlines() if "=" in line)
        active = props.get("ActiveState", "?")
        enabled = props.get("UnitFileState", "?")
        if active == "active":
            checks.append(
                _check("systemd:web", "PASS", f"lunchmenu-web.service {enabled}/{active}")
            )
        else:
            checks.append(
                _check("systemd:web", "FAIL", f"lunchmenu-web.service {enabled}/{active}")
            )

    return checks


# ---------------------------------------------------------------------------
# 7. Permissions
# ---------------------------------------------------------------------------


def check_permissions() -> list[Check]:
    checks = []

    state_dir = config.state_dir()
    mode = state_dir.stat().st_mode & 0o777
    if mode == 0o700:
        checks.append(_check("perms:state_dir", "PASS", f"{state_dir} is mode 700 (caches only)"))
    else:
        checks.append(
            _check("perms:state_dir", "FAIL", f"{state_dir} is mode {oct(mode)}, expected 700")
        )

    config_dir = config.config_dir()
    mode = config_dir.stat().st_mode & 0o777
    if mode == 0o700:
        checks.append(_check("perms:config_dir", "PASS", f"{config_dir} is mode 700"))
    else:
        checks.append(
            _check("perms:config_dir", "FAIL", f"{config_dir} is mode {oct(mode)}, expected 700")
        )

    keys_path = webos_mod.keys_path()
    if not keys_path.exists():
        checks.append(_check("perms:webos_keys", "WARN", f"{keys_path.name} does not exist yet"))
    else:
        mode = keys_path.stat().st_mode & 0o777
        if mode == 0o600:
            checks.append(_check("perms:webos_keys", "PASS", f"{keys_path.name} is mode 600"))
        else:
            checks.append(
                _check(
                    "perms:webos_keys",
                    "FAIL",
                    f"{keys_path.name} is mode {oct(mode)}, expected 600",
                )
            )

    stray = Path(__file__).resolve().parents[2] / "config.toml"
    if stray.exists() and stray.resolve() != (config_dir / "config.toml").resolve():
        checks.append(
            _check(
                "perms:stray_config",
                "WARN",
                f"{stray} exists but is silently ignored -- config.toml is only read from "
                f"{config_dir}; move it there (or remove it) to avoid confusion",
            )
        )

    return checks


# ---------------------------------------------------------------------------
# 8. Cache
# ---------------------------------------------------------------------------


def check_cache() -> Check:
    cache_dir = config.state_dir() / menu_mod.CACHE_SUBDIR
    files = sorted(cache_dir.glob("week-*.json")) if cache_dir.is_dir() else []
    if not files:
        return _check("cache", "WARN", "no cached week found yet (normal on a fresh install)")

    newest_age = None
    newest_path = None
    import datetime as dt

    for f in files:
        try:
            record = json_mod.loads(f.read_text())
            cached_at = dt.datetime.fromisoformat(record["cached_at"])
        except (OSError, json_mod.JSONDecodeError, KeyError, ValueError):
            continue
        age = dt.datetime.now(dt.UTC) - cached_at
        if newest_age is None or age < newest_age:
            newest_age = age
            newest_path = f
    if newest_age is None:
        return _check("cache", "WARN", f"{len(files)} cache file(s) found but none were readable")
    hours = newest_age.total_seconds() / 3600
    return _check(
        "cache", "PASS", f"newest cache ({newest_path.name}) is about {hours:.1f} hour(s) old"
    )


# ---------------------------------------------------------------------------
# 9. Notify
# ---------------------------------------------------------------------------


def check_notify() -> Check:
    ntfy_url = config.get("notify", "ntfy_url")
    command = config.get("notify", "command")
    if not (ntfy_url or command):
        return _check("notify", "WARN", "[notify] not configured -- failure alerts are off")

    active = []
    if ntfy_url:
        parsed = urllib.parse.urlsplit(ntfy_url)
        # Only scheme+host, NEVER the full URL -- for ntfy.sh (and similar
        # webhook receivers) the topic path IS the bearer credential: anyone
        # who knows it can post to (or, on some deployments, read) that
        # topic. doctor output is exactly the kind of thing that ends up
        # pasted into a bug report or a chat, so the topic must never appear
        # here even though this check has it in hand via config.get().
        host_only = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else "(unparseable URL)"
        active.append(f"ntfy ({host_only})")
    if command:
        try:
            argv0 = shlex.split(command)[0]
        except (ValueError, IndexError):
            argv0 = "(unparseable command)"
        active.append(f"command ({argv0})")
    return _check("notify", "PASS", f"configured: {', '.join(active)}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_all_checks() -> list[Check]:
    checks: list[Check] = []
    checks.append(check_config())

    linq_check, ident = check_linq_api()
    checks.append(linq_check)
    checks.append(check_building_id(ident))

    checks.append(check_speaker())
    checks.extend(check_tvs())
    checks.extend(check_web())
    checks.extend(check_systemd())
    checks.extend(check_permissions())
    checks.append(check_cache())
    checks.append(check_notify())
    return checks


def _print_human(checks: list[Check]) -> None:
    school = announce_mod.SCHOOL or "(unconfigured school)"
    print(f"lunchmenu-doctor -- {school}")
    print()
    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f"[{c.status:<4}] {c.name:<{width}}  {c.message}")
    print()
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
    for c in checks:
        counts[c.status] += 1
    print(
        f"{counts['PASS']} passed, {counts['WARN']} warned, {counts['FAIL']} failed, "
        f"{counts['SKIP']} skipped"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON instead of text"
    )
    args = ap.parse_args()

    checks = run_all_checks()

    if args.json:
        json_mod.dump(
            {
                "checks": [
                    {"name": c.name, "status": c.status, "message": c.message, **c.details}
                    for c in checks
                ],
                "ok": not any(c.status == "FAIL" for c in checks),
            },
            sys.stdout,
            indent=2,
        )
        print()
    else:
        _print_human(checks)

    return 1 if any(c.status == "FAIL" for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
