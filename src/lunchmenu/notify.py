#!/usr/bin/env python3
"""Best-effort failure notifications for lunchmenu.

An unattended 7am job that breaks silently is one you find out about weeks
later. This gives run-announce.sh (and lunch-announce-failure.service, wired
up via the systemd unit's OnFailure=) a single, simple way to say "something
went wrong" out of band, via either or both of two independent, both-optional
[notify] config keys:

    ntfy_url  -- HTTP POST the message body to this URL. Works with ntfy.sh
                 and most other webhook receivers.
    command   -- a shell command template; the literal text "{message}" is
                 replaced with the notification text before running it.

Neither is required; unconfigured, notify() is a silent no-op. Stdlib only
(urllib, subprocess, shlex) -- this has to work even if nothing else in the
venv does.

Safety note on `command`: the message is substituted as a single argv
element via list-form subprocess.run, never built into a string and run
through shell=True. A menu item, an exception message, or anything else that
ends up in `message` must never be able to break out and run something
else -- that's a command-injection footgun this deliberately avoids.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import urllib.error
import urllib.request

from . import config

# Short on purpose: this must never be the thing that hangs the 7am job.
TIMEOUT = 10.0


def _channels() -> tuple[str, str]:
    """Read [notify]'s two keys at call time, not import time, so a config
    change (or a test's config.reload()) is picked up without reimporting
    this module. config.py caches its own parse, so this adds no extra I/O."""
    return config.get("notify", "ntfy_url"), config.get("notify", "command")


def notify(message: str, *, subject: str | None = None) -> bool:
    """Best-effort delivery of `message` via whichever of [notify]'s two
    keys are configured. Never raises -- a broken notifier must not become
    a second failure on top of whatever this is reporting; any problem
    sending is caught and logged to stderr instead. Returns whether at
    least one channel reported success; False (including "nothing is
    configured") is not itself an error worth acting on.
    """
    ntfy_url, command = _channels()
    sent = False
    if ntfy_url:
        sent = _notify_ntfy(ntfy_url, message, subject) or sent
    if command:
        sent = _notify_command(command, message) or sent
    return sent


def _notify_ntfy(url: str, message: str, subject: str | None) -> bool:
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if subject:
        headers["Title"] = subject  # ntfy's title header; harmless elsewhere
    req = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT):
            return True
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"notify: POST to {url} failed: {exc}", file=sys.stderr)
        return False


def _notify_command(template: str, message: str) -> bool:
    """Run `template` with every "{message}" token replaced by `message`,
    as one argv element -- never shell=True with the message interpolated
    into a string. See the module docstring for why."""
    try:
        argv = shlex.split(template)
    except ValueError as exc:
        print(f"notify: could not parse [notify].command ({exc})", file=sys.stderr)
        return False
    argv = [part.replace("{message}", message) for part in argv]
    try:
        result = subprocess.run(argv, timeout=TIMEOUT, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"notify: command {argv!r} failed to run: {exc}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(
            f"notify: command {argv!r} exited {result.returncode}: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("message", nargs="?", help="message text (default: read from stdin)")
    ap.add_argument("--subject", help="optional subject/title (ntfy only)")
    args = ap.parse_args()

    message = args.message if args.message is not None else sys.stdin.read().strip()
    if not message:
        print("notify: no message given (pass it as an argument or on stdin)", file=sys.stderr)
        return 1

    ntfy_url, command = _channels()
    if not (ntfy_url or command):
        print("notify: [notify] not configured -- no-op", file=sys.stderr)
        return 0

    notify(message, subject=args.subject)
    # Deliberately always 0: a notifier that can't notify must not itself
    # become a reason for the caller (run-announce.sh, a systemd unit) to
    # treat the run as more broken than it already is.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
