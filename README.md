# lunchmenu

Announce your school's lunch menu around the house every weekday morning.

`lunchmenu` fetches the daily menu from [LINQ Connect](https://linqconnect.com)'s
public JSON API — the system a large number of US school districts use to
publish their menus — and at 7am on weekdays announces it three independent
ways:

1. **Spoken audio** on a Google Home / Nest / Chromecast speaker.
2. **An on-screen toast** on every paired LG webOS TV.
3. **The full menu page** on one TV's screen, waking it from standby first.

Each channel runs independently: a TV that is unplugged doesn't stop the audio,
and a speaker that has wandered off the network doesn't stop either TV. It also
works fine as a plain CLI — `lunchmenu tomorrow` prints tomorrow's menu — and as
a small web page you can leave up on any screen in the house.

Everything specific to your home (which school, which speaker, which TVs, which
network) lives in `~/.config/lunchmenu/`, outside the repo entirely. No API key
or account is required; the menu API is public.

---

## Requirements

- **Python 3.12+**
- [**uv**](https://docs.astral.sh/uv/) for dependency management
- [**just**](https://just.systems/) for the install recipes
- A school district published on LINQ Connect
- Optional, per feature:
  - a Chromecast-capable speaker for spoken audio
  - one or more LG webOS TVs (2018 or newer) for toasts and the on-screen page
  - `systemd` user services for the 7am schedule

Only the core menu CLI needs nothing but Python — `lunchmenu` itself is
stdlib-only and works with no speaker, no TV, and no network configuration.

## Install

```bash
git clone https://github.com/<you>/lunchmenu.git
cd lunchmenu
just install
```

`just install` does three things: installs the commands into `~/.local/bin` (via
`uv tool install --editable`, so edits in your clone take effect immediately),
creates `~/.config/lunchmenu/` and seeds a `config.toml` there if you don't have
one, and installs the systemd units. It does **not** enable or start anything —
see [Scheduling](#scheduling-the-7am-announcement).

`~/.local/bin` must be on your `PATH`; `just install` warns if it isn't.

| Command | What it does |
|---|---|
| `lunchmenu` | Print the menu for a day |
| `lunchmenu-announce` | Speak the menu on a cast speaker |
| `lunchmenu-webos` | Pair with / toast / take over LG webOS TVs |
| `lunchmenu-serve` | Serve the menu as an HTML page |
| `lunchmenu-doctor` | Check that everything above is actually working |
| `lunchmenu-notify` | Send a failure notification (used by the scheduler) |
| `lunchmenu-run` | The three-in-one wrapper the 7am timer executes |

Because they're on your `PATH`, all of these work from any directory — you don't
need to be inside the clone.

### Recipes

| Recipe | Does |
|---|---|
| `just install` | Everything below: bins, config dir, units |
| `just install-bins` | Just the commands into `~/.local/bin` |
| `just install-config` | Create `~/.config/lunchmenu/`, seed `config.toml` if absent |
| `just install-units` | Install the systemd units and reload (no enable/start) |
| `just enable` / `just disable` | Turn the 7am timer and web page on / off |
| `just migrate` | Move private files from older layouts into `~/.config/lunchmenu` |
| `just doctor` | Health check |
| `just check` | Lint and tests |
| `just dry-run` | Full rehearsal — announces nothing, touches no device |
| `just uninstall` | Remove commands and units. **Leaves your config and state alone** |

No recipe ever overwrites an existing `config.toml`, `tvs.json`, or
`webos-keys.json`.

---

## Configure

All of your private configuration lives in **`~/.config/lunchmenu/`** (mode
700). `just install` creates it and seeds `config.toml` from the example; edit
that file:

```bash
$EDITOR ~/.config/lunchmenu/config.toml
```

Every key can also be set by environment variable as
`LUNCHMENU_<SECTION>_<KEY>` (e.g. `LUNCHMENU_SPEAKER_DEVICE="Kitchen"`), which
takes precedence over the file. Settings are resolved per key in this order:

```
environment variable
  → $LUNCHMENU_CONFIG          (an explicit file path)
    → ~/.config/lunchmenu/config.toml
      → built-in defaults
```

> **A `config.toml` sitting in the repo directory is deliberately ignored.**
> This is a public repo; reading config from the working tree invites a private
> file being committed by accident. Keep it in `~/.config/lunchmenu/`, or point
> `$LUNCHMENU_CONFIG` somewhere explicit. `lunchmenu-doctor` warns if it finds a
> stray one.

Override the location with `$LUNCHMENU_CONFIG_DIR` (or `$XDG_CONFIG_HOME`) if
you keep dotfiles somewhere unusual.

You don't have to fill in everything. Configure only the sections for the
features you want; skip `[speaker]` if you have no speaker, skip `[tv]` if you
have no TVs.

### 1. Find your school

Open your district's public menu page on LINQ Connect. The URL looks like
`https://linqconnect.com/public/menu/XXXXXX`, and that trailing slug is your
district **identifier**. Feed it to:

```bash
lunchmenu --list-buildings --identifier XXXXXX
```

which prints the district id and every school in it with its building id:

```
Example County Public Schools  (districtId aaaaaaaa-1111-2222-3333-444444444444)
  bbbbbbbb-2222-3333-4444-555555555555  Example Elementary School
  cccccccc-6666-7777-8888-999999999999  Example Middle School
```

Put the three values in `config.toml`:

```toml
[school]
identifier  = "XXXXXX"
district_id = "aaaaaaaa-1111-2222-3333-444444444444"
building_id = "bbbbbbbb-2222-3333-4444-555555555555"
name        = "Example Elementary"   # how it's spoken and displayed
timezone    = "America/New_York"     # the SCHOOL's timezone, see note below
```

Verify:

```bash
lunchmenu today
lunchmenu tomorrow -m breakfast
```

> **Set `timezone` to the school's timezone, not the server's.** Many home
> servers run their clock in UTC. If you leave this wrong, "today" rolls over
> at 8pm local and the 7am announcement reads the wrong day's menu.

### 2. Find your speaker

```bash
lunchmenu-announce --list-devices
```

This lists every Chromecast-capable device on the LAN by its friendly name.
Copy the one you want verbatim:

```toml
[speaker]
device = "Kitchen speaker"
volume = 0.4      # 0.0-1.0 for the announcement; the prior volume is restored
meal   = "lunch"  # lunch | breakfast | snack
```

Verify without making a sound:

```bash
lunchmenu-announce --dry-run
```

That prints the exact sentence it would speak and casts nothing.

### 3. Find and pair your TVs

Scan the LAN for LG webOS sets:

```bash
lunchmenu-webos --discover
```

It prints the exact `--pair` command for anything it finds that isn't already
registered. Then, **standing in front of that TV with the remote**:

```bash
lunchmenu-webos --pair --host 192.0.2.10 --name "Living Room"
```

The TV shows an "Allow this app?" prompt; accept it. On success the TV gets one
toast confirming the name it was just given — if that's wrong, re-run `--pair`
with the correct `--name`.

```toml
[tv]
discover_subnet = "192.0.2.0/24"   # the LAN --discover scans
show_room       = "Living Room"    # the one room whose screen the 7am job takes over
```

Check what's registered:

```bash
lunchmenu-webos --list
lunchmenu-webos --toast --dry-run
```

**A few things that will save you time:**

- **Pairing takes longer than you'd expect.** The default window is 150
  seconds, not 60 — 60 repeatedly proved too short to walk to the TV and find
  the remote. It prints a "still waiting" line every ~15s so you can tell it
  hasn't hung. On timeout it says explicitly whether a prompt appeared at all
  (TV reachable, just not accepted in time) or never appeared (TV off or
  unreachable) — those are different problems.
- **A TV only shows up in `--discover` when it's powered on,** or when it has
  Quick Start+ enabled while nominally "off." Turning one TV on has been
  observed to wake others on the same circuit, so don't assume a single new hit
  is the only new one.
- **Room names come from pairing, not from a table in the code.** Nothing
  infers a room from an IP or a model, so there's no mapping to keep in sync as
  TVs are added, replaced, or moved.

### 4. Point the TVs at the web page

`lunchmenu-webos --show` launches the TV's browser at the page served by
`lunchmenu-serve`, so the TV needs an address it can reach this host on:

```toml
[web]
bind_host   = "0.0.0.0"
port        = 8090
public_host = "192.0.2.5"   # this machine's LAN address, as the TV sees it
```

`port` is used by both the server and the TV launcher — one key, so they cannot
drift apart. Change it if 8090 is taken on your host.

```bash
lunchmenu-serve                    # then open http://<public_host>:8090/
lunchmenu-webos --show --dry-run   # prints the URL it would launch
```

### 5. Optional: get told when the 7am job fails

An unattended morning job that breaks silently is one you find out about weeks
later. Configure either or both:

```toml
[notify]
ntfy_url = "https://ntfy.sh/your-private-topic"
command  = "notify-send 'lunchmenu' {message}"
```

Unconfigured, notification is a no-op. In `command`, the literal token
`{message}` is replaced with the notification text as a single argument — it is
never interpolated into a shell string, so a message containing shell
metacharacters is safe.

Notifications fire from two places: `run-announce.sh` sends one when the run
finishes with every step failed, and a `lunch-announce-failure.service` unit is
wired to the main unit via `OnFailure=` to catch a crash that never reaches the
script's own exit path. Test yours without waiting for a real failure:

```bash
lunchmenu-notify "test from lunchmenu"
```

---

## Running it

```bash
# Menu CLI
lunchmenu                             # today
lunchmenu tomorrow
lunchmenu 2026-09-15 -m all
lunchmenu today --json
lunchmenu --list-buildings

# Speaker
lunchmenu-announce                    # speak today's lunch
lunchmenu-announce tomorrow -v 0.3
lunchmenu-announce --dry-run          # print the sentence, cast nothing

# TVs
lunchmenu-webos --list
lunchmenu-webos --toast                        # toast every paired TV
lunchmenu-webos --toast --room "Living Room"
lunchmenu-webos --toast --message "Bus is running late"   # custom text, no menu fetch
echo "build failed" | lunchmenu-webos --toast --message -  # read the message from stdin
lunchmenu-webos --show --room "Living Room" --wake
lunchmenu-webos --discover

# Web page
lunchmenu-serve

# Health check
lunchmenu-doctor
```

`--dry-run` works on `--toast`, `--show`, and `lunchmenu-announce`: it prints
exactly what would be sent and touches no network and no device. Use it freely
— the real commands interrupt whoever is in the room.

`--toast --message TEXT` sends that exact text instead of the generated menu
— useful for a cron job, CI step, or anything else that wants to put an
arbitrary line on the TVs. It never fetches the menu (so it works even if the
school API is down) and is only valid alongside `--toast`; `-m`/`--room`/etc.
still target TVs the same way. `--message -` reads the text from stdin
instead of argv, which is the point of the `echo | lunchmenu-webos` form
above. A message over 180 characters (`TOAST_MAX_CHARS`) is truncated to fit
with a trailing `…`, and a warning is printed to stderr saying so — it is
never silently cut. An empty or whitespace-only message is rejected as an
error rather than sent as a blank toast.

`run-announce.sh` is the wrapper the scheduler runs. It performs all three
announcements, appends timestamped output to `announce.log` in the state
directory, and exits non-zero only if *every* step failed.

```bash
SHOW_ROOM="" ./run-announce.sh --dry-run    # full rehearsal, screen step skipped
```

### After 2pm, "today" means tomorrow

For `--toast` and `--show`, if you don't pass an explicit date and it's past 2pm
in the school's timezone, the default rolls forward to the next school day —
skipping weekends and any day the API reports as closed — and says "tomorrow" or
the weekday name instead of "today." Nobody wants to be told what lunch *was*.

Pass an explicit **calendar** date to override it — `lunchmenu-webos --show
2026-09-03`. The literal word `today` does *not* override it, because that's
also the default when you pass nothing at all, and the two are
indistinguishable on the command line.

---

## Scheduling the 7am announcement

`just install` already put the units in `~/.config/systemd/user/` without
enabling them. Turn them on when you're ready:

```bash
just enable        # the 7am timer + the always-on web page
```

or individually:

```bash
systemctl --user enable --now lunch-announce.timer     # the 7am job
systemctl --user enable --now lunchmenu-web.service    # the always-on page
```

The units reference `%h/.local/bin/...`, so they don't depend on where your
clone lives — you can move or rename the repo without breaking the timer.

Enable lingering so the timer fires when nobody is logged in:

```bash
sudo loginctl enable-linger "$USER"
```

Useful afterwards:

```bash
systemctl --user list-timers lunch-announce.timer   # next / last fire
systemctl --user status lunch-announce.service      # last run result
journalctl --user -u lunch-announce.service         # logs
systemctl --user status lunchmenu-web.service
```

The timer is pinned to `OnCalendar=Mon..Fri 07:00 America/New_York` (change the
zone to your own). Pinning it explicitly is what keeps 7am meaning 7am *at the
house* across the daylight-saving shift and regardless of the server's clock.
`Persistent=false` is deliberate: a 7am announcement missed because the machine
was asleep is stale by the time it wakes, and should not fire late.

---

## Troubleshooting

Start here:

```bash
lunchmenu-doctor
```

It checks, read-only, without toasting or casting anything: config found and
complete; the menu API reachable and your building id valid; the speaker
discoverable by name; each registered TV reachable, keyed, and WoL-capable; the
web page answering on its port; the systemd timer's state and last result; and
that your stored TV credentials are still mode 600.

**The menu API returns 403.** CloudFront rejects a default `urllib`/`curl`
User-Agent. A browser-like UA and `Referer` are set on every request already —
if you're extending the fetch code, keep them.

**A holiday shows no "no school" note.** Menu requests always fetch the whole
Mon–Sun week containing the requested day and then filter down to it, because
the `AcademicCalendars` note for a closure only comes back when the range spans
more than the closed day itself. Requesting one day would silently lose it.
Don't narrow the range.

**A TV won't connect.** Current LG firmware requires TLS: plain `ws://host:3000`
is reset at the handshake. Connections always try `wss://host:3001` first and
only fall back to plain if the secure attempt can't connect at all. `--discover`
scans 3001 only, for the same reason.

**`--show --wake` sends the packet but the TV never wakes.** Wake-on-LAN has to
be enabled on the TV itself: Settings → All Settings → Connection → **"Turn on
via Wi-Fi"** and/or **"Turn on via Ethernet"** (sometimes filed under "Mobile
Connection Management" or "LG ThinQ"; naming varies by webOS version). With it
off, the magic packet is correct and the TV simply never sees it. A TV that
doesn't wake within the timeout is skipped with one log line — it doesn't fail
the run.

**Wake-on-LAN does nothing even though the setting is on.** The MAC recorded for
a TV must be the **ARP MAC of the interface actually on your network**, not the
TV's self-reported `device_id`. These sets have separate wired and wireless
interfaces and the two addresses need not agree — on one set here, `device_id`
reported a completely different MAC from the one ARP saw. A packet built from
`device_id` targets hardware that isn't listening. To get the right one: ping
the TV, then `ip neigh show <ip>`.

**The TV page is stuck on yesterday.** The page carries a 15-minute
`<meta refresh>`, and `--show` deliberately launches the **bare** `/` URL when
the target day is genuinely today, so each reload re-resolves "today"
server-side and rolls over at midnight. An explicit `?date=` is only used when
the requested day is *not* today. Adding `?date=` unconditionally looks more
explicit and quietly breaks the rollover.

**Upstream is down at 7am.** Fetches retry with backoff, and the last good week
is cached on disk. If the API can't be reached, the cached menu is announced
with an explicit "may be out of date" caveat rather than silently announcing
nothing.

---

## Where things live

| Path | What |
|---|---|
| `src/lunchmenu/menu.py` | Fetch, parse, timezone. **Stdlib-only, deliberately** |
| `src/lunchmenu/config.py` | Config resolution, the config and state directories |
| `src/lunchmenu/announce.py` | Spoken audio via gTTS + Chromecast |
| `src/lunchmenu/webos.py` | LG webOS pairing, toasts, browser launch, Wake-on-LAN |
| `src/lunchmenu/serve.py` | The HTML menu page |
| `src/lunchmenu/notify.py` | Failure notifications |
| `src/lunchmenu/doctor.py` | Health checks |
| `contrib/systemd/` | The systemd units |
| `Justfile` | Install and maintenance recipes |

**Nothing private lives in the repo.** Your configuration is in
`~/.config/lunchmenu/` (mode 700; override with `$LUNCHMENU_CONFIG_DIR` or
`$XDG_CONFIG_HOME`):

| File | What |
|---|---|
| `config.toml` | Your settings |
| `tvs.json` | Registered TVs: host, room, model, MAC |
| `webos-keys.json` | **TV pairing credentials — mode 600, never logged or printed** |

Regenerable working data is in `~/.local/state/lunchmenu/` (override with
`$LUNCHMENU_STATE_DIR` or `$XDG_STATE_HOME`):

| File | What |
|---|---|
| `menu.json`, `ident.json`, `cache/` | Cached API payloads |
| `announce.log` | Timestamped output of each scheduled run |

The split is deliberate: the config directory is small, private, and worth
backing up; the state directory is a cache and a log that can be deleted at any
time. Neither belongs anywhere near a commit, and one of them is a live
credential.

Upgrading from an older layout? `just migrate` moves private files out of the
repo and the state directory into `~/.config/lunchmenu` for you — moving, not
copying, so no credential is left behind.

`menu.py` is the shared core — every other module reuses its fetch, parse, and
timezone logic rather than duplicating it, and it stays importable with nothing
but the standard library. A test enforces that.

---

## Development

```bash
uv sync              # dev venv in the clone, separate from the installed tool
just check           # ruff + the test suite
just test            # tests only
just lint            # ruff only
just dry-run         # full rehearsal: no audio, no toast, no screen takeover
```

`just install` uses `uv tool install --editable`, so your edits take effect in
the installed commands immediately — no reinstall between changes.

Tests run against recorded API payloads in `tests/fixtures/`, so they're
offline, deterministic, and safe to run anywhere. Nothing in the suite casts
audio, toasts a TV, or takes over a screen.

If you're adding a feature, the useful invariants to preserve are the ones
called out in **Troubleshooting** above — they each cost real debugging time.

## About the API

Unauthenticated JSON behind the LINQ Connect Angular front end:

```
GET /api/FamilyMenuIdentifier?identifier=<slug>
    → district id + building list

GET /api/FamilyMenu?districtId=&buildingId=&startDate=&endDate=
    → the menu; dates go in as M-D-YYYY and come back as M/D/YYYY
```

It's a public endpoint serving public information, and this project reads it at
roughly the rate a parent refreshing the page would, with responses cached to
keep it that way. No credentials are involved.
