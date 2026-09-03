# CLAUDE.md

Guidance for Claude Code working in this repository.

## Delegate all code changes to Sonnet

**Every change to code in this repo must be made by a Sonnet subagent, not by
the main session.** This is a hard rule, not a preference.

Applies to any write to a tracked source or config file here — anything under
`src/lunchmenu/`, `tests/`, `run-announce.sh`, `pyproject.toml`, `Justfile`,
`config.example.toml`, `.gitignore`, the `contrib/systemd/` units, and the
CI workflow. It applies to one-line edits and typo fixes too; "it's small" is
not an exemption.

How to comply:

- Use the Agent tool with `model: "sonnet"` and a `subagent_type` that can
  write files (`general-purpose`, or `claude` for a catch-all). Never
  `subagent_type: "fork"` — a fork ignores the model override and runs on the
  parent's model.
- Give the subagent everything it needs in the prompt: the file paths, the
  exact behavior change, the constraints from this file and the README, and
  how to verify. It starts with no knowledge of this conversation.
- The main session may still read files, search, run commands, reproduce bugs,
  design the change, review the subagent's diff, and report back. Investigation
  and explanation are not code changes.
- If a delegated change comes back wrong, send corrections to that same agent
  with SendMessage rather than fixing it yourself.

The only edits the main session may make directly are non-code documentation
files, `README.md` and `CLAUDE.md` included.

## What this project is

Fetches a school menu from LINQ Connect's public JSON API, then announces it
three ways at 7am on weekdays: spoken audio to a Google Cast speaker, a toast
to the LG webOS TVs, and the full menu page on one TV's screen.

`README.md` is the real documentation and is unusually detailed. **Read it
before changing anything**, and pass the relevant sections to any subagent you
delegate to. Its Troubleshooting section records hard-won specifics — the
CloudFront User-Agent requirement, why a whole week is always fetched, the
TLS-only webOS port, why the ARP MAC and not `device_id` is used for
Wake-on-LAN, the 150s pairing default, and why the 7am TV URL is deliberately
bare. Those are answers, not open questions; do not rediscover or "fix" them.

Keep the README current when behavior changes. A change that makes a README
statement false is not finished until the README is updated.

## This repo is published publicly

**No real IP address, MAC address, room name, home directory path, school
name, district slug, or district/building GUID may appear in a tracked file.**
That includes source, comments, docstrings, the README, example config, and
systemd templates. Use placeholders and RFC 5737 documentation addresses
(`192.0.2.x`) in anything committed.

Everything house-specific lives outside the repo, in two directories:

- **The config directory** — `$LUNCHMENU_CONFIG_DIR`, else
  `$XDG_CONFIG_HOME/lunchmenu`, else `~/.config/lunchmenu` (mode 700) — holds
  `config.toml`, `tvs.json` (TV hosts, rooms, MACs), and `webos-keys.json`
  (**pairing credentials, mode 600 — never print, log, echo, or commit its
  contents**). `config.example.toml` is the committed, placeholder-only
  counterpart to `config.toml`; when you add a config key, add it to both.
- **The state directory** — `$LUNCHMENU_STATE_DIR`, else
  `$XDG_STATE_HOME/lunchmenu`, else `~/.local/state/lunchmenu` — holds only
  regenerable working data: the cached API payloads and `announce.log`.

**A `config.toml` in the repo directory is deliberately NOT read.** That was
removed on purpose: reading config from the working tree is how a private file
ends up committed to a public repo. Do not add the cwd back to the config
search path. `lunchmenu-doctor` warns if it finds a stray one.

Before any commit, re-run the leak check in "Verifying a change" below.

## Layout and conventions

- `src/lunchmenu/menu.py` is the shared core. It is **stdlib-only on purpose**
  — a test asserts it imports nothing third-party. Do not add third-party
  imports to it. Every other module reuses its fetch, parse, and timezone logic
  rather than duplicating it; new code should do the same.
- `src/lunchmenu/config.py` is also stdlib-only (`tomllib`). Resolution order is
  env var → `$LUNCHMENU_CONFIG` → `<config dir>/config.toml` → built-in defaults;
  `config_dir()` and `state_dir()` resolve the two directories above. Shipped
  defaults must stay **neutral placeholders**, never this house's values.
- Everything is installed as a package with console scripts: `lunchmenu`,
  `lunchmenu-announce`, `lunchmenu-webos`, `lunchmenu-serve`, `lunchmenu-doctor`,
  `lunchmenu-notify`, plus `lunchmenu-run` (the wrapper). `just install` puts
  them in `~/.local/bin` via `uv tool install --editable`, so they run from any
  directory and pick up clone edits with no reinstall. `uv run <cmd>` still works
  inside the clone. There are no loose top-level scripts and no `sys.path`
  manipulation — modules import each other with `from . import ...`.
- **The `Justfile` is the install path.** `just install` / `install-bins` /
  `install-config` / `install-units` / `enable` / `migrate` / `uninstall` /
  `check` / `dry-run`. Recipes must stay idempotent and must never overwrite an
  existing `config.toml`, `tvs.json`, or `webos-keys.json`.
- The systemd units use systemd's `%h` specifier (`%h/.local/bin/...`) rather
  than absolute clone paths, so moving the repo doesn't break the timer. They
  are committed as real `.service` files, not templates.
- The host clock is UTC and the school is Eastern. Anything date-related goes
  through the school timezone resolved in `menu.py`. Never use naive local dates.
- Room names in `tvs.json` are written only by `lunchmenu-webos --pair`; there
  is no IP-to-room table to maintain, and nothing infers a room from an IP.
- The web page port is a **single config key** (`web.port`), read by both
  `serve.py` and `webos.py`. They can no longer drift apart — do not reintroduce
  two constants.

## Verifying a change

Prefer dry runs — nothing casts, toasts, or takes over a TV screen:

```
export PATH="$HOME/.local/bin:$PATH"
just check                       # ruff + the full offline test suite
just doctor
just dry-run                     # all four dry runs in one
lunchmenu-webos --show --dry-run # must print the BARE URL for today
SHOW_ROOM="" lunchmenu-run --dry-run
```

Tests are offline and run against recorded fixtures in `tests/fixtures/`. No
test may touch the network, a TV, or the speaker.

Leak check, before any commit:

```
git status --ignored --short
grep -rnEI "([0-9]{1,3}\.){3}[0-9]{1,3}|([0-9a-f]{2}:){5}[0-9a-f]{2}|$HOME|$USER" \
  --exclude-dir=.git --exclude-dir=.venv --exclude-dir=.ruff_cache \
  --exclude=config.toml --exclude=uv.lock .
```

That flags any IPv4 address, any MAC, and this machine's home path or
username. Expected survivors are the RFC 5737 documentation addresses
(`192.0.2.x`) in `config.example.toml` and the README; `config.toml` is
gitignored and is supposed to hold real values. Hits anywhere else are bugs.

The TVs and the speaker are real devices in a real house. Do not send audio,
toasts, or screen takeovers to them to test something unless the user asks —
they are disruptive to whoever is in the room.

## Repository state

The `master` branch has **no commits yet**. `.gitignore` exists and covers the
credentials, config, caches, and logs. Do not commit, add files, or initialize
history unless the user asks for it — and if asked, verify the leak check above
passes first, because the first commit is what would make any leak permanent.
