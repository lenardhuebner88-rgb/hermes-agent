#!/bin/sh
# Usage: scripts/tmux-stamp-identity.sh SESSION_ID TASK_ID [KIND]
# Stamp correlation identity on the current tmux window.

if [ -z "${TMUX_PANE:-}" ]; then
    echo "tmux-stamp-identity: run this inside the tmux pane you want to stamp" >&2
    exit 1
fi

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
    echo "Usage: $0 SESSION_ID TASK_ID [KIND]" >&2
    exit 1
fi

tmux set-option -w -t "$TMUX_PANE" @hermes_session_id "$1" || exit 1
tmux set-option -w -t "$TMUX_PANE" @hermes_task_id "$2" || exit 1

if [ -n "${3:-}" ]; then
    tmux set-option -w -t "$TMUX_PANE" @hermes_kind "$3" || exit 1
fi
