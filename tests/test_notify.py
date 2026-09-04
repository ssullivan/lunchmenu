"""Tests for lunchmenu.notify: the [notify] "unconfigured -> no-op",
ntfy-POST, and shell-command-template paths, plus the call-time-not-
import-time config read that _channels() exists to guarantee.

No test here makes a real HTTP request or runs a real subprocess --
urllib.request.urlopen and subprocess.run are always monkeypatched with a
fake that records what it was called with. The `no_network` autouse fixture
in conftest.py is the backstop that would fail loudly if one of these
forgot to patch a real network call.
"""

from __future__ import annotations

import urllib.error

from lunchmenu import config, notify


def _configure(monkeypatch, tmp_path, *, ntfy_url: str | None = None, command: str | None = None):
    """Write a temp config.toml with [notify] set, point LUNCHMENU_CONFIG at
    it, and reload -- same pattern as test_config.py's
    test_config_file_beats_builtin_default."""
    lines = ["[notify]"]
    if ntfy_url is not None:
        lines.append(f'ntfy_url = "{ntfy_url}"')
    if command is not None:
        lines.append(f'command = "{command}"')
    cfg = tmp_path / "notify-config.toml"
    cfg.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("LUNCHMENU_CONFIG", str(cfg))
    config.reload()


class _FakeResponse:
    """Minimal stand-in for what urlopen returns, used only as a context
    manager by _notify_ntfy (`with urllib.request.urlopen(req, ...):`)."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_unconfigured_returns_false_and_calls_nothing(monkeypatch):
    ntfy_calls = []
    command_calls = []
    monkeypatch.setattr(notify, "_notify_ntfy", lambda *a, **kw: ntfy_calls.append((a, kw)))
    monkeypatch.setattr(notify, "_notify_command", lambda *a, **kw: command_calls.append((a, kw)))

    assert notify.notify("hello") is False
    assert ntfy_calls == []
    assert command_calls == []


def test_ntfy_configured_posts_message_body_and_subject_title(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, ntfy_url="https://example.invalid/topic")

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["full_url"] = req.full_url
        captured["data"] = req.data
        captured["title"] = req.get_header("Title")
        captured["method"] = req.get_method()
        return _FakeResponse()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)

    result = notify.notify("today's menu is pizza", subject="lunch alert")

    assert result is True
    assert captured["full_url"] == "https://example.invalid/topic"
    assert captured["data"] == b"today's menu is pizza"
    assert captured["title"] == "lunch alert"
    assert captured["method"] == "POST"


def test_command_configured_passes_message_as_one_unsplit_argv_element(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, command="echo {message}")

    calls = []

    class _FakeResult:
        returncode = 0
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _FakeResult()

    monkeypatch.setattr(notify.subprocess, "run", fake_run)

    dangerous = "boom; rm -rf /tmp/nope"
    result = notify.notify(dangerous)

    assert result is True
    assert len(calls) == 1
    argv = calls[0]
    assert len(argv) == 2
    assert argv == ["echo", dangerous]


def test_ntfy_failure_returns_false_without_raising(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, ntfy_url="https://example.invalid/topic")

    def fake_urlopen(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)

    assert notify.notify("hello") is False


def test_ntfy_url_error_returns_false_without_raising(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, ntfy_url="https://example.invalid/topic")

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)

    assert notify.notify("hello") is False


def test_one_channel_failing_other_succeeding_returns_true(monkeypatch, tmp_path):
    _configure(
        monkeypatch,
        tmp_path,
        ntfy_url="https://example.invalid/topic",
        command="echo {message}",
    )

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("unreachable")

    class _FakeResult:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(notify.subprocess, "run", lambda argv, **kw: _FakeResult())

    assert notify.notify("hello") is True


def test_config_is_read_at_call_time_not_import_time(monkeypatch, tmp_path):
    """Regression test for the bug this change fixes: NTFY_URL/COMMAND used
    to be module-level constants read once at import, so a config change
    (or a test's config.reload()) after that first import was invisible.
    _channels() reads config.get(...) fresh on every call instead."""
    # Unconfigured to start (isolated_env's default) -- notify() must be a
    # no-op, with no module reimport involved anywhere in this test.
    assert notify.notify("first, while unconfigured") is False

    # Configure [notify] purely via config.reload() -- no re-import of the
    # notify module -- and confirm the very next call now attempts delivery.
    _configure(monkeypatch, tmp_path, command="echo {message}")

    calls = []

    class _FakeResult:
        returncode = 0
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _FakeResult()

    monkeypatch.setattr(notify.subprocess, "run", fake_run)

    result = notify.notify("second, now configured")

    assert result is True
    assert len(calls) == 1
