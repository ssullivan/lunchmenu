#!/bin/bash
# Wrapper for the systemd user timer / cron: casts today's lunch menu to a
# Google Home speaker, toasts it to every registered webOS TV, AND puts the
# full menu page on one TV's screen (waking it if needed). Installed as a
# standalone shim at ~/.local/bin/lunchmenu-run (see the Justfile's
# install-bins recipe) -- it is no longer read out of a repo clone, so it
# resolves the other lunchmenu commands from $PATH rather than from its own
# location. lunch-announce.service sets Environment=PATH= explicitly for
# this reason, precisely because a systemd user manager's $PATH often does
# not include ~/.local/bin on its own -- but if these are missing here
# they're missing for real -- fail with a clear message rather than a bare
# "command not found".
#
# Override device/volume/meal/show-room via env vars (e.g. in the systemd
# unit's Environment= or a crontab line); each falls back to config.toml's
# speaker.device/speaker.volume/speaker.meal/tv.show_room (see config.py /
# config.example.toml) if unset OR empty -- same as the old hardcoded
# defaults this replaces. SHOW_ROOM is the one exception: set SHOW_ROOM=""
# (as opposed to leaving it unset) to skip the screen-takeover step
# entirely, same as before config.py existed.
#
# The three steps are independent: a TV problem doesn't stop the audio or
# the toast, a speaker problem doesn't stop either TV step, and so on. All
# three always run (unless SHOW_ROOM disables the third); this exits
# non-zero only if every step that ran failed.
set -uo pipefail

ANNOUNCE="lunchmenu-announce"
WEBOS="lunchmenu-webos"
NOTIFY="lunchmenu-notify"

missing=()
for cmd in "$ANNOUNCE" "$WEBOS" "$NOTIFY"; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
done
if [ "${#missing[@]}" -gt 0 ]; then
    echo "lunchmenu-run: missing on \$PATH: ${missing[*]}" >&2
    echo "lunchmenu-run: is ~/.local/bin on \$PATH, and did 'just install-bins' run?" >&2
    exit 1
fi

# State dir resolution mirrors config.py's:
# $LUNCHMENU_STATE_DIR -> $XDG_STATE_HOME/lunchmenu -> ~/.local/state/lunchmenu.
# The log lives there now, not in the repo, alongside the other working
# artifacts config.py moved out of it.
STATE_DIR="${LUNCHMENU_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/lunchmenu}"
mkdir -p "$STATE_DIR"
LOG="$STATE_DIR/announce.log"

# DEVICE/VOLUME/MEAL: passed through only when the env var is actually set
# and non-empty; otherwise omitted entirely so each script falls back to its
# own config-derived default (speaker.device / speaker.volume /
# speaker.meal) -- there is no hardcoded fallback value to keep in sync
# here any more.
DEVICE_ARGS=()
[ -n "${DEVICE:-}" ] && DEVICE_ARGS=(--device "$DEVICE")
VOLUME_ARGS=()
[ -n "${VOLUME:-}" ] && VOLUME_ARGS=(--volume "$VOLUME")
MEAL_ARGS=()
[ -n "${MEAL:-}" ] && MEAL_ARGS=(--meal "$MEAL")

# Resolve the python interpreter the lunchmenu package (and config.py) were
# installed into -- no bare "python3" here, since `uv tool install` puts the
# package in its own isolated venv, not on the system interpreter.
#
# Preferred: derive it from the venv layout. $WEBOS is a command we already
# required above, resolved through any symlink; a venv's/uv-tool's bin/
# directory always has the interpreter sitting right next to the console
# scripts, so this is deterministic and involves no text parsing.
#
# Fallback: parse the resolved script's shebang line, handling both the
# absolute form (#!/path/to/python) and "#!/usr/bin/env NAME".
#
# Either way: on any failure, print nothing and return non-zero. An empty
# TOOL_PYTHON must never be mistaken for an operator-supplied empty
# SHOW_ROOM -- see the lookup-failure handling below, which is exactly the
# reason this is a function instead of an inline one-liner.
resolve_tool_python() {
    local webos_path resolved bindir candidate shebang body first second

    webos_path="$(command -v "$WEBOS")" || return 1
    resolved="$(readlink -f "$webos_path")" || return 1
    bindir="$(dirname "$resolved")"

    for candidate in "$bindir/python3" "$bindir/python"; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    [ -r "$resolved" ] || return 1
    shebang="$(head -1 "$resolved")"
    case "$shebang" in
        '#!'*)
            body="${shebang#\#!}"
            read -r first second <<<"$body"
            if [ "$(basename -- "$first")" = "env" ] && [ -n "$second" ]; then
                candidate="$(command -v "$second")" || return 1
            else
                candidate="$first"
            fi
            if [ -x "$candidate" ]; then
                printf '%s\n' "$candidate"
                return 0
            fi
            ;;
    esac

    return 1
}

# SHOW_ROOM is different from DEVICE/VOLUME/MEAL above: *unset* means "use
# tv.show_room from config", but an *explicitly empty* SHOW_ROOM="" means
# "skip this step" and must stay that way -- so unset and empty are not the
# same case here (bash `${VAR+x}` distinguishes them; `${VAR:-...}` would
# not). A failure to resolve the config value is a THIRD, distinct case: it
# must never collapse into "skip" (an empty SHOW_ROOM) or the 7am job would
# silently stop putting the menu on the TV while still logging success --
# see show_room_lookup_failed below, checked at the actual TV-screen step.
show_room_lookup_failed=0
if [ -z "${SHOW_ROOM+x}" ]; then
    if TOOL_PYTHON="$(resolve_tool_python)"; then
        if ! SHOW_ROOM="$("$TOOL_PYTHON" -c 'from lunchmenu import config; print(config.get("tv", "show_room"))')"; then
            show_room_lookup_failed=1
            SHOW_ROOM_LOOKUP_ERROR="'$TOOL_PYTHON' could not import lunchmenu.config / read tv.show_room"
        fi
    else
        show_room_lookup_failed=1
        SHOW_ROOM_LOOKUP_ERROR="could not find the lunchmenu tool's python interpreter (checked next to $WEBOS and its shebang)"
    fi
fi

{
    echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') ====="

    echo "-- audio --"
    "$ANNOUNCE" "${DEVICE_ARGS[@]}" "${VOLUME_ARGS[@]}" "${MEAL_ARGS[@]}" "$@"
    audio_status=$?
    echo "audio exit status: $audio_status"

    echo "-- TV toast --"
    "$WEBOS" --toast "${MEAL_ARGS[@]}" "$@"
    toast_status=$?
    echo "toast exit status: $toast_status"

    if [ "$show_room_lookup_failed" -eq 1 ]; then
        # Deliberately NOT treated as "SHOW_ROOM is empty" -- this is a
        # failure of the lookup itself (see resolve_tool_python above), and
        # must show up as a failed step, not a silently skipped one.
        echo "-- TV screen: FAILED to resolve tv.show_room ($SHOW_ROOM_LOOKUP_ERROR) --"
        show_status=1
    elif [ -n "$SHOW_ROOM" ]; then
        echo "-- TV screen (room: $SHOW_ROOM, waking if needed) --"
        "$WEBOS" --show --room "$SHOW_ROOM" --wake "$@"
        show_status=$?
        echo "show exit status: $show_status"
    else
        echo "-- TV screen skipped (SHOW_ROOM is empty) --"
        show_status=0
    fi

    if [ "$audio_status" -eq 0 ] || [ "$toast_status" -eq 0 ] || [ "$show_status" -eq 0 ]; then
        overall=0
    else
        overall=1
    fi

    # A failed run is exactly the case an unattended 7am job needs to be
    # noticed without someone thinking to check announce.log -- notify.py
    # is a no-op if [notify] isn't configured, and its own exit status is
    # deliberately ignored here: a broken notifier must never turn a
    # (correctly reported) failed run into something this script treats as
    # more broken than it already is.
    if [ "$overall" -ne 0 ]; then
        echo "-- notifying failure --"
        "$NOTIFY" --subject "lunchmenu run failed" \
            "lunchmenu: all steps failed (audio=$audio_status toast=$toast_status show=$show_status). See $LOG."
    fi

    echo "overall exit status: $overall (audio=$audio_status toast=$toast_status show=$show_status)"
    exit "$overall"
} >>"$LOG" 2>&1
