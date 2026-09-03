# Install/uninstall/maintenance recipes for lunchmenu. See README.md's
# "Install" and "Commands" sections for the user-facing walkthrough this
# mirrors -- keep recipe names in sync with that table.
#
# Every recipe here is meant to be safe to re-run: none of them enable,
# start, stop, or restart a systemd unit (that's `just enable`/`disable`,
# which you run yourself when ready), and none of them ever overwrite an
# existing config.toml, tvs.json, or webos-keys.json.

set shell := ["bash", "-uc"]

# Same resolution order as config.py's config_dir(): an explicit override,
# then XDG_CONFIG_HOME, then the ~/.config default. Kept as a single
# variable so every recipe that touches the config dir agrees on where it
# is.
config_dir := env_var_or_default("LUNCHMENU_CONFIG_DIR", env_var_or_default("XDG_CONFIG_HOME", env_var_or_default("HOME", "") / ".config") / "lunchmenu")

bin_dir := env_var_or_default("HOME", "") / ".local" / "bin"
units_dir := env_var_or_default("HOME", "") / ".config" / "systemd" / "user"

# List available recipes.
default:
    just --list

# Everything needed for a fresh clone to run unattended: commands on
# $PATH, a seeded config, and the systemd units installed (but not
# enabled).

# Install commands, config, and systemd units (does not enable anything).
install: install-bins install-config install-units
    @echo
    @echo "Install complete. Next steps:"
    @echo "  1. Edit your config:   \$EDITOR '{{config_dir}}/config.toml'"
    @echo "  2. Pair your TVs / find your speaker (see README.md)."
    @echo "  3. Rehearse safely:    just dry-run"
    @echo "  4. Turn on the 7am timer and web page:   just enable"

# Install the lunchmenu commands onto $PATH via `uv tool install
# --editable`, so edits in this clone take effect immediately without
# reinstalling. Also installs the run-announce.sh wrapper as the
# standalone `lunchmenu-run` shim -- it is no longer read out of this
# clone at run time, only copied once here.

# Install commands + the lunchmenu-run shim into ~/.local/bin.
install-bins:
    uv tool install --editable .
    install -m 755 run-announce.sh "{{bin_dir}}/lunchmenu-run"
    @case ":$PATH:" in \
        *":{{bin_dir}}:"*) ;; \
        *) echo; \
           echo "WARNING: {{bin_dir}} is not on your \$PATH."; \
           echo "  Add it (e.g. in ~/.bashrc or ~/.zshrc):"; \
           echo "    export PATH=\"{{bin_dir}}:\$PATH\""; \
           echo "  lunchmenu, lunchmenu-run, and friends will not run without it."; \
           echo ;; \
    esac

# Create the private config directory (mode 700) and seed config.toml from
# config.example.toml if -- and only if -- one doesn't already exist there.

# Create the config dir and seed config.toml if it's missing.
install-config:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p "{{config_dir}}"
    chmod 700 "{{config_dir}}"
    dest="{{config_dir}}/config.toml"
    if [ -e "$dest" ]; then
        echo "install-config: $dest already exists -- leaving it alone"
    else
        install -m 644 config.example.toml "$dest"
        echo "install-config: seeded $dest from config.example.toml"
    fi

# Install the systemd user units and reload the daemon. Does NOT enable or
# start anything -- run `just enable` yourself when you're ready.

# Install the systemd units and daemon-reload (no enable/start).
install-units:
    mkdir -p "{{units_dir}}"
    install -m 644 contrib/systemd/lunch-announce.service "{{units_dir}}/lunch-announce.service"
    install -m 644 contrib/systemd/lunch-announce-failure.service "{{units_dir}}/lunch-announce-failure.service"
    install -m 644 contrib/systemd/lunch-announce.timer "{{units_dir}}/lunch-announce.timer"
    install -m 644 contrib/systemd/lunchmenu-web.service "{{units_dir}}/lunchmenu-web.service"
    systemctl --user daemon-reload
    @echo "install-units: units installed to {{units_dir}} (not enabled -- run 'just enable' when ready)"

# Move private files from older layouts into the config dir: a repo-root
# config.toml, and tvs.json/webos-keys.json out of the state dir. This is
# the manual/explicit path -- config.py's _migrate_legacy_files() already
# does the same moves automatically the first time anything reads config,
# so this recipe is normally a no-op; it exists for anyone who wants to
# migrate before running any command, or wants to see it happen
# explicitly. Never overwrites an existing destination, never prints file
# contents.

# Move private files from older layouts into the config dir (safe, idempotent).
migrate:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p "{{config_dir}}"
    chmod 700 "{{config_dir}}"
    moved=0
    move() {
        src="$1"; dst="$2"
        if [ ! -e "$src" ]; then
            return
        fi
        if [ -e "$dst" ]; then
            echo "migrate: $dst already exists -- leaving $src in place"
            return
        fi
        mv "$src" "$dst"
        moved=1
        echo "migrate: moved $(basename "$src") -> $dst"
    }
    move "config.toml" "{{config_dir}}/config.toml"
    state_dir="${LUNCHMENU_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/lunchmenu}"
    move "$state_dir/tvs.json" "{{config_dir}}/tvs.json"
    move "$state_dir/webos-keys.json" "{{config_dir}}/webos-keys.json"
    if [ -e "{{config_dir}}/webos-keys.json" ]; then
        chmod 600 "{{config_dir}}/webos-keys.json"
    fi
    if [ "$moved" -eq 0 ]; then
        echo "migrate: nothing to move"
    fi

# Enable and start the 7am timer and the always-on web page.
enable:
    systemctl --user enable --now lunch-announce.timer
    systemctl --user enable --now lunchmenu-web.service

# Stop and disable the 7am timer and the always-on web page.
disable:
    systemctl --user disable --now lunch-announce.timer
    systemctl --user disable --now lunchmenu-web.service

# Remove the installed commands, the lunchmenu-run shim, and the systemd
# units. Leaves your config dir and state dir (config.toml, tvs.json,
# webos-keys.json, caches, logs) completely untouched.

# Remove commands and units. Leaves config dir and state dir untouched.
uninstall:
    #!/usr/bin/env bash
    set -euo pipefail
    uv tool uninstall lunchmenu || true
    rm -f "{{bin_dir}}/lunchmenu-run"
    rm -f "{{units_dir}}/lunch-announce.service"
    rm -f "{{units_dir}}/lunch-announce-failure.service"
    rm -f "{{units_dir}}/lunch-announce.timer"
    rm -f "{{units_dir}}/lunchmenu-web.service"
    systemctl --user daemon-reload
    echo "uninstall: commands and units removed."
    echo "uninstall: left your config dir and state dir untouched (config.toml, tvs.json, webos-keys.json, caches, logs)."

# Run the test suite.
test:
    uv run pytest

# Lint.
lint:
    uv run ruff check .

# Auto-format.
fmt:
    uv run ruff format .

# Lint + test.
check: lint test

# Run the health check.
doctor:
    lunchmenu-doctor

# Full rehearsal of everything the 7am job does -- announces nothing,
# toasts nothing, takes over no screen. Safe with real devices configured.

# Full rehearsal: --dry-run everywhere, nothing casts/toasts/takes over a TV.
dry-run:
    lunchmenu-announce --dry-run
    lunchmenu-webos --toast --dry-run
    lunchmenu-webos --show --dry-run
    SHOW_ROOM="" lunchmenu-run --dry-run
