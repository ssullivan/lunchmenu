"""Configuration loading for lunchmenu.

Every value that used to be a hardcoded constant in this codebase -- the
school's district/building GUIDs, real LAN addresses, room names, the speaker
name -- lives in config now, because the source is public and none of that
belongs in it.

Resolution order, first hit wins **per key**:

    environment variable  ->  $LUNCHMENU_CONFIG file
        ->  ~/.config/lunchmenu/config.toml  ->  built-in neutral default

A `./config.toml` sitting in a repo clone's cwd is deliberately **not** in
that list -- see `_candidate_files()`. A missing config file at any of those
locations is not an error -- it's the normal case for a fresh checkout. Env
vars are named
``LUNCHMENU_<SECTION>_<KEY>`` uppercased, e.g. ``LUNCHMENU_SPEAKER_DEVICE`` or
``LUNCHMENU_WEB_PORT``, and are coerced to the type of that key's built-in
default.

Config is loaded once and cached at module scope; call `reload()` to force a
fresh read (tests do this so one test's environment doesn't leak into the
next).

Stdlib only, on purpose -- `menu.py` imports this and must stay free of
third-party dependencies. `tomllib` is stdlib as of Python 3.12.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Schema: (type, default) per section/key. Every default here is neutral --
# an empty string, or a documented, clearly-generic value -- never a real
# value from this (or anyone's) house. Real values belong in a config.toml
# that is gitignored, in ~/.config/lunchmenu/config.toml, or in environment
# variables -- never in this file. See config.example.toml for the
# documented, fillable version of this schema.
# ---------------------------------------------------------------------------
_SCHEMA: dict[str, dict[str, tuple[type, Any]]] = {
    "school": {
        "identifier": (str, ""),
        "district_id": (str, ""),
        "building_id": (str, ""),
        "name": (str, ""),
        "timezone": (str, "America/New_York"),
    },
    "speaker": {
        "device": (str, ""),
        # None, not 0.0 -- the neutral behavior is "leave the speaker's
        # current volume alone", matching announce.py's long-standing
        # --volume default before this key existed.
        "volume": (float, None),
        "meal": (str, "lunch"),
    },
    "web": {
        "port": (int, 8090),
        "bind_host": (str, "0.0.0.0"),
        "public_host": (str, ""),
    },
    "tv": {
        "discover_subnet": (str, ""),
        "show_room": (str, ""),
    },
    "notify": {
        # Both keys are independent and optional -- either, both, or
        # neither may be set. Unconfigured (both empty) means notify.py's
        # notify() is a no-op.
        "ntfy_url": (str, ""),
        "command": (str, ""),
    },
}

_cache: dict[str, dict[str, Any]] | None = None
_migrated = False

# Filenames that used to live at the repo root before secrets and working
# artifacts were moved out of the tree entirely (the repo is about to be
# public). Migrated once, in place, the first time anything asks for the
# state directory.
_LEGACY_FILENAMES = ("tvs.json", "webos-keys.json", "menu.json", "ident.json")


def _coerce(value: Any, kind: type) -> Any:
    if value is None:
        return None
    try:
        return kind(value)
    except (TypeError, ValueError):
        return value


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"config: could not read {path} ({exc}) -- ignoring it", file=sys.stderr)
        return {}


def _candidate_files() -> list[Path]:
    """Config file locations, lowest priority first -- later entries in this
    list win when merged, matching the resolution order in the module
    docstring (env var beats $LUNCHMENU_CONFIG beats
    ~/.config/lunchmenu/config.toml).

    Deliberately does NOT include a cwd-relative ./config.toml. It used to,
    but a private config.toml sitting inside a public repo clone is exactly
    the failure mode this file exists to prevent -- see
    `_migrate_legacy_files()`, which moves one out the first time it's
    found, and `doctor.py`'s stray-config-in-clone check, which warns if one
    still exists after that.
    """
    candidates = [config_dir() / "config.toml"]
    env_path = os.environ.get("LUNCHMENU_CONFIG")
    if env_path:
        candidates.append(Path(env_path))
    return candidates


def _env_overrides() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for section, keys in _SCHEMA.items():
        for key in keys:
            var = f"LUNCHMENU_{section.upper()}_{key.upper()}"
            if var in os.environ:
                out.setdefault(section, {})[key] = os.environ[var]
    return out


def load_config(force_reload: bool = False) -> dict[str, dict[str, Any]]:
    """Return the fully-resolved config as {section: {key: value}}. Cached
    after the first call; pass force_reload=True (or call `reload()`) to
    read again."""
    global _cache
    if _cache is not None and not force_reload:
        return _cache

    # The state dir must exist (and any legacy repo-root files migrated
    # onward) before anything downstream reads the cache from it. The config
    # dir (tvs.json, webos-keys.json, config.toml) is resolved below, via
    # _candidate_files() -> config_dir().
    state_dir()

    resolved: dict[str, dict[str, Any]] = {
        section: {key: default for key, (_kind, default) in keys.items()}
        for section, keys in _SCHEMA.items()
    }

    for path in _candidate_files():
        data = _read_toml(path)
        for section, keys in data.items():
            if section not in _SCHEMA or not isinstance(keys, dict):
                continue
            for key, value in keys.items():
                if key not in _SCHEMA[section]:
                    continue
                kind, _default = _SCHEMA[section][key]
                resolved[section][key] = _coerce(value, kind)

    for section, keys in _env_overrides().items():
        for key, value in keys.items():
            kind, _default = _SCHEMA[section][key]
            resolved[section][key] = _coerce(value, kind)

    _cache = resolved
    return resolved


def reload() -> dict[str, dict[str, Any]]:
    """Clear the cache and reload from scratch. For tests, and for anything
    that changes LUNCHMENU_* env vars or config files at runtime."""
    return load_config(force_reload=True)


def get(section: str, key: str) -> Any:
    return load_config()[section][key]


# ---------------------------------------------------------------------------
# State directory -- where cached API payloads (menu.json, ident.json, and
# the week-* cache) and announce.log live. Regenerable working artifacts
# only now -- credentials and the hand-edited TV registry moved to the
# config dir below -- but it still lives outside the repo entirely, resolved
# the same way XDG state dirs normally are.
# ---------------------------------------------------------------------------


def _resolve_state_dir() -> Path:
    env = os.environ.get("LUNCHMENU_STATE_DIR")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "lunchmenu"
    return Path.home() / ".local" / "state" / "lunchmenu"


def state_dir() -> Path:
    """Return the per-user state directory, creating it (mode 700) on
    first use, and migrating any legacy repo-root/state-dir files into their
    current homes the first time this runs in a process."""
    d = _resolve_state_dir()
    d.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # best-effort; e.g. a read-only or shared mount
        d.chmod(0o700)
    _migrate_legacy_files(d, _resolve_config_dir())
    return d


# ---------------------------------------------------------------------------
# Config directory -- where config.toml, the hand-edited TV registry
# (tvs.json), and the webOS pairing credentials (webos-keys.json) all live.
# None of that belongs inside a public repo, and none of it is regenerable
# the way a cache is, so it gets its own directory, separate from state_dir,
# resolved the same way XDG config dirs normally are.
# ---------------------------------------------------------------------------


def _resolve_config_dir() -> Path:
    env = os.environ.get("LUNCHMENU_CONFIG_DIR")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "lunchmenu"
    return Path.home() / ".config" / "lunchmenu"


def config_dir() -> Path:
    """Return the per-user config directory, creating it (mode 700) on
    first use, and migrating any legacy repo-root/state-dir files into their
    current homes the first time this runs in a process."""
    d = _resolve_config_dir()
    d.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # best-effort; e.g. a read-only or shared mount
        d.chmod(0o700)
    _migrate_legacy_files(_resolve_state_dir(), d)
    return d


# Filenames that used to live only in the state dir (tvs.json,
# webos-keys.json -- see _migrate_legacy_files) before it was split from the
# config dir.
_STATE_TO_CONFIG_FILENAMES = ("tvs.json", "webos-keys.json")


def _legacy_repo_dir() -> Path:
    """Where top-level scripts (and, in the oldest layout, config.toml and
    the state-dir files) used to live: this module is at
    <repo>/src/lunchmenu/config.py, so three parents up is the repo root.
    Its own function (rather than inlined) so tests can monkeypatch it
    without writing into this actual checkout."""
    return Path(__file__).resolve().parents[2]


def _migrate_legacy_files(state_target: Path, config_target: Path) -> None:
    """One-time move (not copy) of legacy files into their current homes,
    covering every past layout in a single pass so an old install lands in
    the right place even if it skips a generation:

      repo root  -> state dir   (menu.json, ident.json, and, transiently,
                                  tvs.json/webos-keys.json from before the
                                  state-dir-only layout existed)
      state dir  -> config dir  (tvs.json, webos-keys.json)
      repo root  -> config dir  (a repo-root config.toml, from before
                                  ~/.config/lunchmenu/config.toml existed)

    Never touches the *contents* of webos-keys.json -- only its path and
    mode -- since it's a live credential.
    """
    global _migrated
    if _migrated:
        return
    _migrated = True

    legacy_repo_dir = _legacy_repo_dir()

    def _move(src: Path, dst: Path, from_where: str, to_where: str) -> None:
        if not src.exists():
            return
        if dst.exists():
            print(
                f"config: {src.name} exists at both {from_where} and {to_where} -- "
                f"leaving {src} in place rather than overwriting {dst}",
                file=sys.stderr,
            )
            return
        dst.parent.mkdir(parents=True, exist_ok=True)  # in case migration order raced ahead
        mode = src.stat().st_mode & 0o777
        shutil.move(str(src), str(dst))
        if src.name == "webos-keys.json":
            dst.chmod(0o600)  # a client key is a standing credential
        else:
            dst.chmod(mode)
        print(f"config: moved {src.name} out of {from_where} to {to_where}", file=sys.stderr)

    if legacy_repo_dir != state_target:
        for name in _LEGACY_FILENAMES:
            _move(
                legacy_repo_dir / name,
                state_target / name,
                f"the repo dir ({legacy_repo_dir})",
                f"the state dir ({state_target})",
            )

    if state_target != config_target:
        for name in _STATE_TO_CONFIG_FILENAMES:
            _move(
                state_target / name,
                config_target / name,
                f"the state dir ({state_target})",
                f"the config dir ({config_target})",
            )

    if legacy_repo_dir != config_target:
        _move(
            legacy_repo_dir / "config.toml",
            config_target / "config.toml",
            f"the repo dir ({legacy_repo_dir})",
            f"the config dir ({config_target})",
        )
