#!/bin/sh
# Called by the Claude Code hooks that install.sh adds to settings.json.
#   hook.sh <event> <claude pid>
# Records the session's state on its tmux window:
#   @claude_state       idle | working | needs | review | done
#   @claude_updated     when the state last changed (epoch seconds)
#   @claude_pid         the Claude Code process, to notice when it is gone
#   @claude_pane        the pane Claude Code runs in
#   @claude_prompt      the last prompt, on one line
#   @claude_transcript  the session's transcript (read by search.py)
#   @claude_turn        which turn the verdict below belongs to
#   @claude_verdict     wrapped | followup, from judge.py after a turn ends
#   @claude_note        a few words on why
# Events: idle (SessionStart), prompt (UserPromptSubmit), working (after a
# tool), needs (permission prompt), done (Stop), failed (StopFailure),
# end (SessionEnd).
# Must be quick and silent, and never fail Claude Code: always exits 0.

[ -n "$TMUX_PANE" ] || exit 0
ev="$1" pid="$2" p="$TMUX_PANE"
DIR=$(cd "$(dirname "$0")" && pwd)
now=$(date +%s)
# Names this turn, so a late verdict for an older one is dropped (judge.py).
turn="$now-$$"

# Fields from the hook's JSON input, one per line. python3 is only started for
# the two events that need it (session start and each prompt).
json_fields() {
  python3 -I -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(str(d.get("transcript_path") or "").replace("\n", " "))
raw = str(d.get("prompt") or "")
# A scheduled run (sched.py): keep the task, not the header before it.
if raw.startswith("[Scheduled run") and "\nTask: " in raw:
    raw = raw.split("\nTask: ", 1)[1]
s = " ".join(raw.split())[:400]
# tmux reads an argument ending in ";" as a command separator.
print(s + " " if s.endswith(";") else s)
' 2>/dev/null
}

# A pid we can check later. Hooks run as "sh -c" children of Claude Code, so
# the caller passes $PPID, which is Claude Code itself.
case "$pid" in ''|*[!0-9]*|0|1) pid="" ;; esac

set_common() {
  if [ -n "$pid" ]; then
    tmux set -w -t "$p" @claude_pid "$pid" \; set -w -t "$p" @claude_pane "$p"
  else
    tmux set -w -t "$p" @claude_pane "$p"
  fi
}

# Set the state; the time only moves when the state really changes, so the
# sidebar's "updated" order does not churn on every tool call.
set_state() {
  cur=$(tmux display -p -t "$p" '#{@claude_state}' 2>/dev/null)
  [ "$cur" = "$1" ] && return 0
  tmux set -w -t "$p" @claude_state "$1" \; set -w -t "$p" @claude_updated "$now" \; \
       set -uw -t "$p" @claude_hash
}

# Is someone looking at this window right now? True when it is the current
# window of its session and a client showing that session has focus (tmux
# clears "focused" when the terminal loses focus, if the terminal reports it).
viewed() {
  info=$(tmux display -p -t "$p" '#{session_id} #{window_active}' 2>/dev/null) || return 1
  sess=${info% *} active=${info##* }
  [ "$active" = 1 ] || return 1
  tmux list-clients -F '#{session_id} #{client_flags}' 2>/dev/null |
    grep -q "^$sess .*focused"
}

case "$ev" in
  idle)
    { IFS= read -r tr; } <<EOF
$(json_fields)
EOF
    set_common
    [ -n "$tr" ] && tmux set -w -t "$p" @claude_transcript "$tr"
    set_state idle ;;
  prompt)
    fields=$(json_fields)
    tr=$(printf '%s\n' "$fields" | sed -n 1p)
    pr=$(printf '%s\n' "$fields" | sed -n 2p)
    set_common
    [ -n "$tr" ] && tmux set -w -t "$p" @claude_transcript "$tr"
    # A new prompt always counts as an update, even if already working.
    tmux set -w -t "$p" @claude_prompt "$pr" \; set -w -t "$p" @claude_prompt_at "$now" \; \
         set -w -t "$p" @claude_turn "$turn" \; set -uw -t "$p" @claude_verdict \; \
         set -uw -t "$p" @claude_note \; \
         set -w -t "$p" @claude_state working \; set -w -t "$p" @claude_updated "$now" \; \
         set -uw -t "$p" @claude_hash
    # Scheduled runs (sched.py): the prompt it typed into a window you own
    # keeps the window marked; your own next prompt there clears the mark.
    sch=$(tmux display -p -t "$p" '#{@claude_sched_pending}|#{@claude_sched_owned}')
    if [ "${sch%%|*}" = 1 ]; then
      tmux set -uw -t "$p" @claude_sched_pending
    elif [ "${sch#*|}" != 1 ]; then
      tmux set -uw -t "$p" @claude_sched \; set -uw -t "$p" @claude_sched_run
    fi ;;
  working|needs)
    # The busiest path (after every tool call): one tmux call. The state and
    # its time only change when the state really changes.
    tmux set -w -t "$p" @claude_pane "$p" \; set -w -t "$p" @claude_pid "$pid" \; \
         if -F -t "$p" "#{!=:#{@claude_state},$ev}" \
         "set -w -t $p @claude_state $ev ; set -w -t $p @claude_updated $now ; set -uw -t $p @claude_hash" ;;
  done|failed)
    input=$(cat)
    set_common
    tmux set -w -t "$p" @claude_turn "$turn" \; set -uw -t "$p" @claude_verdict \; \
         set -uw -t "$p" @claude_note
    if viewed; then set_state done; else set_state review; fi
    if [ "$ev" = failed ]; then
      tmux set -w -t "$p" @claude_verdict followup \; set -w -t "$p" @claude_note "stopped with an error"
    else
      # Wrapped up, or come back to it? Decided in the background (a few
      # seconds) so Claude Code is not held up.
      printf '%s' "$input" | nohup python3 -I "$DIR/judge.py" "$p" "$turn" >/dev/null 2>&1 &
    fi ;;
  end)
    tmux set -uw -t "$p" @claude_state \; set -uw -t "$p" @claude_pid \; \
         set -uw -t "$p" @claude_updated \; set -uw -t "$p" @claude_verdict \; \
         set -uw -t "$p" @claude_note \; set -uw -t "$p" @claude_hash ;;
esac
exit 0
