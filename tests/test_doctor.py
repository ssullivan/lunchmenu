"""Tests for lunchmenu.doctor.check_notify only -- the other checks in
doctor.py (LINQ API, cast speaker, TVs, web page, systemd, permissions,
cache) have no test coverage here and are out of scope for this change.

check_notify() is read-only like every other check in this module: it never
sends a test notification, and it must never print a configured ntfy topic
(or a full command template) verbatim, since either can be sensitive.
"""

from __future__ import annotations

from lunchmenu import config, doctor


def _configure(monkeypatch, tmp_path, *, ntfy_url: str | None = None, command: str | None = None):
    lines = ["[notify]"]
    if ntfy_url is not None:
        lines.append(f'ntfy_url = "{ntfy_url}"')
    if command is not None:
        lines.append(f'command = "{command}"')
    cfg = tmp_path / "notify-config.toml"
    cfg.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("LUNCHMENU_CONFIG", str(cfg))
    config.reload()


def test_unconfigured_warns():
    check = doctor.check_notify()
    assert check.status == "WARN"
    assert "not configured" in check.message


def test_ntfy_url_configured_passes_and_never_leaks_the_topic(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, ntfy_url="https://ntfy.sh/secret-topic-abc")

    check = doctor.check_notify()

    assert check.status == "PASS"
    assert "ntfy.sh" in check.message
    assert "secret-topic-abc" not in check.message


def test_command_configured_passes_and_shows_only_the_program_name(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, command="notify-send 'lunchmenu' {message}")

    check = doctor.check_notify()

    assert check.status == "PASS"
    assert "notify-send" in check.message
    assert "{message}" not in check.message
