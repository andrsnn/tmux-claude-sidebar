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
# POSIX sh. Never fails loudly: hooks should not spam the status line.

DIR=$(cd "$(dirname "$0")" && pwd)

width() {
  w=$(tmux show-option -gqv @claude_sidebar_width)
  case "$w" in ''|*[!0-9]*) w=30 ;; esac
  echo "$w"
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
    k:S) toggle "$(tmux display-message -p -t "$pane" '#{session_id}')" ;;
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
  *) echo "usage: $0 ensure|ensure-session|ensure-all|toggle|click|guard|resize|cleanup" >&2; exit 2 ;;
esac
exit 0
