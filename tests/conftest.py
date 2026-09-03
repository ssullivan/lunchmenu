"""Shared pytest fixtures for the lunchmenu test suite.

Hard rule for this whole suite: **no test may touch the network, a TV, or the
speaker.** The autouse `isolated_env` fixture below points every
LUNCHMENU_* location at a throwaway tmp_path for every single test (not just
ones that ask for it), so a test can never read or write this machine's real
state dir, real config.toml, or real credentials -- even by accident.

Any test that needs a fake HTTP response monkeypatches `menu.get` (see
`fake_get`) rather than touching urllib for real.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make `import lunchmenu` work without an editable install.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def week_regular() -> dict:
    return json.loads((FIXTURES / "week_regular.json").read_text())


@pytest.fixture
def week_holiday() -> dict:
    return json.loads((FIXTURES / "week_holiday.json").read_text())


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point every LUNCHMENU_* location at tmp_path, for every test.

    This is autouse and unconditional: no test in this suite may read or
    write this machine's real ~/.local/state/lunchmenu, real
    ~/.config/lunchmenu (config.toml, tvs.json, webos-keys.json), or a real
    ./config.toml, even indirectly through a module-level `config.get(...)`
    call at import time.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text("")  # empty but present -- "missing file is not an error" is separate

    monkeypatch.setenv("LUNCHMENU_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LUNCHMENU_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LUNCHMENU_CONFIG", str(config_path))
    # Also neutralize any stray LUNCHMENU_<SECTION>_<KEY> the outer shell
    # might have set (e.g. from a real deployment's env) so it can't leak
    # into a test's expectations about defaults/precedence.
    allowed = ("LUNCHMENU_STATE_DIR", "LUNCHMENU_CONFIG_DIR", "LUNCHMENU_CONFIG")
    for var in list(__import__("os").environ):
        if var.startswith("LUNCHMENU_") and var not in allowed:
            monkeypatch.delenv(var, raising=False)

    # Never let a test accidentally run from a cwd containing a real
    # config.toml (module docstring's resolution order includes ./config.toml).
    monkeypatch.chdir(tmp_path)

    from lunchmenu import config

    # config._migrate_legacy_files() can move a repo-root config.toml into
    # the config dir (see config._legacy_repo_dir). Point it at a harmless,
    # nonexistent tmp path instead of this actual checkout -- without this,
    # the very first test in the whole run would move THIS repo's real
    # config.toml out from under the developer running the suite.
    monkeypatch.setattr(config, "_legacy_repo_dir", lambda: tmp_path / "fake-legacy-repo-root")

    config.reload()
    yield
    config.reload()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Hard backstop: any test that tries to open a real socket connection
    fails loudly instead of hanging or silently reaching a real host.

    Every test in this suite is expected to monkeypatch the call site
    instead (menu.get, urllib.request.urlopen, menu_mod.fetch_week, ...) --
    this fixture exists to catch the case where one doesn't, not as the
    primary mechanism for keeping tests offline.
    """
    import socket

    def _blocked(*_args, **_kwargs):
        raise RuntimeError(
            "test attempted a real network connection -- monkeypatch the "
            "call site (menu.get / urlopen / fetch_week / socket) instead"
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)


@pytest.fixture
def fake_get(monkeypatch):
    """Monkeypatch lunchmenu.menu.get so tests never touch the network.

    Usage: fake_get(module, {"FamilyMenu": some_payload, ...}) or pass a
    callable(path, **params) -> dict / raises. Patches the `get` name in
    every module that imported it (menu.get is called as menu_mod.get
    internally, so patching lunchmenu.menu.get is sufficient since every
    other module calls through menu_mod.fetch_week, not menu.get directly).
    """

    def _install(response_or_fn):
        from lunchmenu import menu

        if callable(response_or_fn) and not isinstance(response_or_fn, dict):
            fn = response_or_fn
        else:

            def fn(path, **params):
                return response_or_fn

        monkeypatch.setattr(menu, "get", fn)
        return fn

    return _install


@pytest.fixture
def freeze_datetime(monkeypatch):
    """Freeze datetime.datetime.now() (both bare and tz-aware) to a fixed
    instant, for the duration of one test.

    `datetime.datetime` is a builtin C type -- it doesn't allow attribute
    assignment (`monkeypatch.setattr(datetime.datetime, "now", ...)` raises
    TypeError), so this instead swaps the *module-level* `datetime` name for
    a subclass with `now()` overridden. Every module in this codebase does
    `import datetime as dt` and refers to `dt.datetime` at call time (never
    `from datetime import datetime`), so patching the shared stdlib
    `datetime` module's `datetime` attribute reaches every one of them for
    the life of the test, and monkeypatch restores the original afterward.

    `dt.date` and `dt.timedelta` are untouched -- only `dt.datetime.now()`
    (used by webos.py's ROLLOVER_HOUR check) is affected; menu.py's
    `today()` uses `dt.datetime.now(SCHOOL_TZ).date()` too, so freezing here
    also pins "today" consistently everywhere in the same test.
    """
    import datetime as real_dt

    def _freeze(instant: real_dt.datetime):
        assert instant.tzinfo is not None, "freeze_datetime needs a tz-aware instant"

        class _FrozenDateTime(real_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

        monkeypatch.setattr(real_dt, "datetime", _FrozenDateTime)
        return instant

    return _freeze
