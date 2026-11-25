#!/bin/sh
# tmux-claude-sidebar control script. Called from tmux hooks and bindings.
#   sidebar.sh ensure <window>        add a sidebar to a window if it needs one
#   sidebar.sh ensure-session <sess>  ensure every window in a session
#   sidebar.sh ensure-all             ensure every window on the server
#   sidebar.sh toggle <session>       hide/show sidebars in a session
#   sidebar.sh click <pane> <x> <y> <client>
#   sidebar.sh guard <window>         move focus off the sidebar
#   sidebar.sh resize [window]        keep sidebars at their fixed width
#   sidebar.sh cleanup                close windows left with only a sidebar
#   sidebar.sh width +N|-N|N          change the sidebar width everywhere
#   sidebar.sh dragged <window>       keep the width the user dragged to
#   sidebar.sh seen                   windows people are looking at count as
#                                     reviewed ("review" becomes "done")
#   sidebar.sh sort [index|attention|toggle|load]  window order in the list
#   sidebar.sh search <client>        open the prompt search popup
#   sidebar.sh act <what> <client>    a right-click menu choice (menu.conf)
#   sidebar.sh move <window> up|down|top           reorder windows
#   sidebar.sh reviewed <window>|all  mark as reviewed without visiting
#   sidebar.sh save-right-click       keep tmux's own right-click elsewhere
#   sidebar.sh wrapped <window>       mark a follow-up as wrapped up
#   sidebar.sh judge on|off|toggle|load   the AI wrapped-up / follow-up check
#   sidebar.sh sched <client> [add|<task id>]  the scheduled tasks popup
# POSIX sh. Never fails loudly: hooks should not spam the status line.

DIR=$(cd "$(dirname "$0")" && pwd)
# The chosen width survives tmux restarts in this file.
WIDTH_FILE="$HOME/.tmux/claude-sidebar-width"
# So does the window order: "attention" (the default) or "index".
SORT_FILE="$HOME/.tmux/claude-sidebar-sort"
# And whether finished turns are checked by Claude (judge.py): "on" or "off".
JUDGE_FILE="$HOME/.tmux/claude-sidebar-judge"
# tmux's own right-click binding, used outside the sidebar.
RC_FILE="$DIR/right-click.conf"
DEFAULT_WIDTH=40
MIN_WIDTH=24
MAX_WIDTH=80

valid_width() {
  case "$1" in ''|*[!0-9]*) return 1 ;; esac
  [ "$1" -ge "$MIN_WIDTH" ] && [ "$1" -le "$MAX_WIDTH" ]
}

width() {
  w=$(tmux show-option -gqv @claude_sidebar_width)
  valid_width "$w" && { echo "$w"; return; }
  w=$(cat "$WIDTH_FILE" 2>/dev/null)
  valid_width "$w" || w=$DEFAULT_WIDTH
  tmux set-option -g @claude_sidebar_width "$w"
  echo "$w"
}

set_width() {
  case "$1" in
    +*|-*) w=$(( $(width) $1 )) ;;
    *) w="$1" ;;
  esac
  case "$w" in ''|*[!0-9-]*) return 0 ;; esac
  [ "$w" -lt "$MIN_WIDTH" ] && w=$MIN_WIDTH
  [ "$w" -gt "$MAX_WIDTH" ] && w=$MAX_WIDTH
  tmux set-option -g @claude_sidebar_width "$w"
  mkdir -p "$(dirname "$WIDTH_FILE")" && echo "$w" > "$WIDTH_FILE"
  resize
}

# After a border drag: if this window's sidebar changed width, make that the
# width for every sidebar.
dragged() {
  p=$(window_sidebars "$1" | head -n 1)
  [ -n "$p" ] || return 0
  w=$(tmux display-message -p -t "$p" '#{pane_width}')
  [ "$w" = "$(width)" ] || set_width "$w"
}

# Sidebar pane ids in a target (window, session, or -a for all).
sidebars() {
  if [ "$1" = -a ]; then
    tmux list-panes -a -F '#{?@sidebar,#{pane_id},}' 2>/dev/null | grep .
  else
    tmux list-panes -s -t "$1" -F '#{?@sidebar,#{pane_id},}' 2>/dev/null | grep .
  fi
}

window_sidebars() {
  tmux list-panes -t "$1" -F '#{?@sidebar,#{pane_id},}' 2>/dev/null | grep .
}

ensure() {
  win="$1"
  [ "$(tmux display-message -p -t "$win" '#{@claude_sidebar_hidden}' 2>/dev/null)" = 1 ] && return 0
  window_sidebars "$win" >/dev/null && return 0
  # Splitting would unzoom it; the next ensure (new window, reload) adds it.
  [ "$(tmux display-message -p -t "$win" '#{window_zoomed_flag}')" = 1 ] && return 0
  # -d keeps focus on the main pane; -f -b puts it full height on the left.
  new=$(tmux split-window -hbfd -l "$(width)" -t "$win" -P -F '#{pane_id}' "exec '$DIR/render.sh'" 2>/dev/null) || return 0
  tmux set-option -p -t "$new" @sidebar 1 \; \
       set-option -p -t "$new" allow-set-title off \; \
       select-pane -t "$new" -T "sidebar" 2>/dev/null
  # Two hooks racing can each add one. Keep the first.
  window_sidebars "$win" | sed 1d | while read -r extra; do tmux kill-pane -t "$extra"; done
  return 0
}

ensure_session() {
  tmux list-windows -t "$1" -F '#{window_id}' 2>/dev/null | while read -r w; do ensure "$w"; done
}

ensure_all() {
  tmux list-windows -a -F '#{window_id}' 2>/dev/null | while read -r w; do ensure "$w"; done
}

toggle() {
  sess="$1"
  if [ "$(tmux show-option -qv -t "$sess" @claude_sidebar_hidden)" = 1 ]; then
    tmux set-option -u -t "$sess" @claude_sidebar_hidden
    ensure_session "$sess"
  else
    tmux set-option -t "$sess" @claude_sidebar_hidden 1
    sidebars "$sess" | while read -r p; do tmux kill-pane -t "$p"; done
  fi
}

# Run what the clicked row stands for. Rows come from the map render.sh keeps.
click() {
  pane="$1" x="$2" y="$3" client="$4"
  map=$(tmux show-option -pqv -t "$pane" @sidebar_map)
  act=""
  for item in $map; do
    case "$item" in "$y="*) act="${item#*=}"; break ;; esac
  done
  case "$act" in
    w:*)
      win="${act#w:}"
      pw=$(tmux display-message -p -t "$pane" '#{pane_width}')
      if [ "$x" -ge $((pw - 3)) ]; then
        # The × column: close the window, which hangs up every process in it.
        # Ask first so a misclick can't kill a running Claude session.
        name=$(tmux display-message -p -t "$win" '#{window_name}' | sed 's/#/##/g')
        tmux confirm-before -t "$client" \
          -p "Close \"$name\" and stop everything running in it? (y/n)" \
          "kill-window -t $win"
      else
        tmux select-window -t "$win"
      fi ;;
    d:*)
      # The line under a window row, or a scheduled run: go to that window
      # (it may be in another session).
      tmux switch-client -c "$client" -t "${act#d:}" ;;
    t:add) sched "$client" add ;;
    s:*) sched "$client" "${act#s:}" ;;
    b:)
      # "sort: ..." on the left switches the order, "[search]" on the right.
      pw=$(tmux display-message -p -t "$pane" '#{pane_width}')
      if [ "$x" -ge $((pw - 9)) ]; then search "$client"; else sort_mode toggle; fi ;;
    h:)
      # Header row: "[−][+]" sits in the last 6 drawn columns.
      pw=$(tmux display-message -p -t "$pane" '#{pane_width}')
      if [ "$x" -ge $((pw - 7)) ] && [ "$x" -le $((pw - 5)) ]; then set_width -2
      elif [ "$x" -ge $((pw - 4)) ]; then set_width +2
      fi ;;
    k:S) toggle "$(tmux display-message -p -t "$pane" '#{session_id}')" ;;
    k:F) search "$client" ;;
    k:T) sched "$client" ;;
    k:?)
      # Same as typing Ctrl-b <key>: whatever the user has bound runs, so
      # rename, the kill-window confirm and choose-tree behave as when typed.
      tmux switch-client -c "$client" -T prefix \; send-keys -c "$client" -K "${act#k:}" ;;
  esac
  return 0
}

guard() {
  win="$1"
  [ "$(tmux display-message -p -t "$win" '#{@sidebar}')" = 1 ] || return 0
  # Pane 0 is the sidebar (it is created on the far left), so "next" is the
  # first main pane. This also lets prefix-o cycle through main panes.
  tmux select-pane -t "$win.+" 2>/dev/null
  return 0
}

resize() {
  w=$(width)
  if [ -n "$1" ]; then list=$(window_sidebars "$1"); else list=$(sidebars -a); fi
  for p in $list; do
    [ "$(tmux display-message -p -t "$p" '#{pane_width}')" = "$w" ] || tmux resize-pane -t "$p" -x "$w" 2>/dev/null
  done
  return 0
}

cleanup() {
  tmux list-panes -a -F '#{window_id} #{?@sidebar,1,0}' 2>/dev/null |
    awk '{ seen[$1] = 1; if ($2 == 0) main[$1] = 1 } END { for (w in seen) if (!main[w]) print w }' |
    while read -r w; do tmux kill-window -t "$w" 2>/dev/null; done
  resize
}

# Windows that someone is looking at (current window of a session shown by a
# focused client) have been reviewed: "review" becomes "done".
seen() {
  tmux list-clients -F '#{client_flags} #{window_id} #{@claude_state}' 2>/dev/null |
    while read -r flags win state; do
      case "$flags" in *focused*) ;; *) continue ;; esac
      [ "$state" = review ] && tmux set -w -t "$win" @claude_state done
    done
  wake
  return 0
}

# Hidden sidebars nap; redraw the ones that can be seen now, right away.
wake() {
  for pid in $(tmux list-panes -a -F \
      '#{?#{&&:#{@sidebar},#{&&:#{window_active},#{session_attached}}},#{pane_pid},}' 2>/dev/null); do
    kill -USR1 "$pid" 2>/dev/null
  done
  return 0
}

reviewed() {
  if [ "$1" = all ]; then
    tmux list-windows -a -F '#{window_id} #{@claude_state}' 2>/dev/null |
      while read -r win state; do
        [ "$state" = review ] && tmux set -w -t "$win" @claude_state done
      done
  else
    [ "$(tmux display -p -t "$1" '#{@claude_state}')" = review ] &&
      tmux set -w -t "$1" @claude_state done
  fi
  return 0
}

# You have dealt with it: no longer "follow up" (and no longer "review").
wrapped() {
  if [ "$1" = all ]; then
    tmux list-windows -a -F '#{window_id} #{@claude_verdict}' 2>/dev/null |
      while read -r win verdict; do
        [ "$verdict" = followup ] && wrapped "$win"
      done
    return 0
  fi
  tmux set -w -t "$1" @claude_verdict wrapped \; set -w -t "$1" @claude_note "marked done by you"
  reviewed "$1"
}

judge() {
  m="$1"
  if [ "$m" = toggle ]; then
    if [ "$(tmux show-option -gqv @claude_sidebar_judge)" = off ]; then m=on; else m=off; fi
  fi
  case "$m" in
    on) tmux set-option -gu @claude_sidebar_judge ;;
    off) tmux set-option -g @claude_sidebar_judge off ;;
    load)
      [ "$(cat "$JUDGE_FILE" 2>/dev/null)" = off ] && tmux set-option -g @claude_sidebar_judge off
      return 0 ;;
    *) return 0 ;;
  esac
  mkdir -p "$(dirname "$JUDGE_FILE")" && echo "$m" > "$JUDGE_FILE"
  tmux display-message "Claude follow-up check: $m"
  return 0
}

sort_mode() {
  m="$1"
  if [ "$m" = toggle ]; then
    cur=$(tmux show-option -gqv @claude_sidebar_sort)
    if [ "$cur" = index ]; then m=attention; else m=index; fi
  fi
  case "$m" in
    index) tmux set-option -g @claude_sidebar_sort index ;;
    attention) tmux set-option -gu @claude_sidebar_sort ;;
    load)
      # At config load: the saved choice, if any.
      [ "$(cat "$SORT_FILE" 2>/dev/null)" = index ] && tmux set-option -g @claude_sidebar_sort index
      return 0 ;;
    *) return 0 ;;
  esac
  mkdir -p "$(dirname "$SORT_FILE")" && echo "$m" > "$SORT_FILE"
  return 0
}

search() {
  client="$1"
  # -E closes the popup when search.py exits (after a jump, or Esc).
  tmux display-popup -c "$client" -E -w 90% -h 80% -T " Search Claude Code prompts " \
    "python3 -I '$DIR/search.py' '$client'" 2>/dev/null
  return 0
}

# Scheduled tasks (sched.py): the popup, optionally adding one or showing one.
sched() {
  client="$1" what="$2"
  dir=$(tmux display -p -c "$client" '#{pane_current_path}' 2>/dev/null)
  extra=""
  case "$what" in
    add) extra="--add" ;;
    t*) extra="--task '$what'" ;;
  esac
  tmux display-popup -c "$client" -E -w 90% -h 70% -T " Scheduled tasks " \
    "python3 -I '$DIR/sched.py' ui --client '$client' --dir '$dir' $extra" 2>/dev/null
  return 0
}

# Swap a window with its neighbour in index order, or move it to the front.
move() {
  win="$1" where="$2"
  sess=$(tmux display -p -t "$win" '#{session_id}') || return 0
  list=$(tmux list-windows -t "$sess" -F '#{window_id}')
  case "$where" in
    up) other=$(printf '%s\n' "$list" | grep -B 1 -x -- "$win" | head -n 1) ;;
    down) other=$(printf '%s\n' "$list" | grep -A 1 -x -- "$win" | tail -n 1) ;;
    top) other=$(printf '%s\n' "$list" | head -n 1) ;;
    *) return 0 ;;
  esac
  [ -n "$other" ] && [ "$other" != "$win" ] || return 0
  if [ "$where" = top ]; then
    # Shift the others up one place, keeping their order.
    tmux move-window -d -b -s "$win" -t "$other"
  else
    tmux swap-window -d -s "$win" -t "$other"
  fi
  return 0
}

# A right-click menu choice (see menu.conf) for the window in @sidebar_menu_win.
act() {
  what="$1" client="$2"
  win=$(tmux show-option -gqv @sidebar_menu_win)
  case "$win" in @*) ;; *) return 0 ;; esac
  tmux display -p -t "$win" '#{window_id}' >/dev/null 2>&1 || return 0
  case "$what" in
    select) tmux switch-client -c "$client" -t "$win" ;;
    reviewed) reviewed "$win" ;;
    reviewed-all) reviewed all ;;
    wrapped) wrapped "$win" ;;
    wrapped-all) wrapped all ;;
    up|down|top) move "$win" "$what" ;;
    rename)
      tmux command-prompt -t "$client" -p "Rename window:" "rename-window -t $win -- '%%'" ;;
    close)
      name=$(tmux display-message -p -t "$win" '#{window_name}' | sed 's/#/##/g')
      tmux confirm-before -t "$client" \
        -p "Close \"$name\" and stop everything running in it? (y/n)" "kill-window -t $win" ;;
  esac
  return 0
}

# Right-clicks outside the sidebar keep doing what tmux did before: save that
# binding once, then bind menu.conf with it as the "not the sidebar" branch.
save_right_click() {
  cur=$(tmux list-keys -T root MouseDown3Pane 2>/dev/null)
  case "$cur" in
    *@sidebar_rows*) ;;                    # ours already (a reload)
    "") echo "unbind-key -T root MouseDown3Pane" > "$RC_FILE" ;;
    *) printf '%s\n' "$cur" > "$RC_FILE" ;;
  esac
  orig=$(sed -n '1s/^bind-key  *-T root  *MouseDown3Pane  *//p' "$RC_FILE" 2>/dev/null)
  [ -n "$orig" ] || orig="select-pane -t = ; send-keys -M"
  # The saved binding is tmux command syntax, so it is pasted into a file
  # that tmux reads, rather than passed through the shell.
  tmp="$DIR/.right-click.$$"
  { cat "$DIR/menu.conf"; printf '  %s\n}\n' "$orig"; } > "$tmp"
  tmux source-file "$tmp"
  rm -f "$tmp"
  return 0
}

cmd="$1"; [ $# -gt 0 ] && shift
case "$cmd" in
  ensure) ensure "$@" ;;
  ensure-session) ensure_session "$@" ;;
  ensure-all) ensure_all ;;
  toggle) toggle "$@" ;;
  click) click "$@" ;;
  guard) guard "$@" ;;
  resize) resize "$@" ;;
  cleanup) cleanup ;;
  width) set_width "$@" ;;
  dragged) dragged "$@" ;;
  seen) seen ;;
  sort) sort_mode "${1:-toggle}" ;;
  search) search "$@" ;;
  act) act "$@" ;;
  move) move "$@" ;;
  reviewed) reviewed "$@" ;;
  save-right-click) save_right_click ;;
  wrapped) wrapped "$@" ;;
  judge) judge "${1:-toggle}" ;;
  sched) sched "$@" ;;
  *) echo "usage: $0 ensure|ensure-session|ensure-all|toggle|click|guard|resize|cleanup|width|dragged|seen|sort|search|act|move|reviewed|save-right-click|wrapped|judge|sched" >&2; exit 2 ;;
esac
exit 0
