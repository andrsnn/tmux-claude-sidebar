#!/bin/sh
# Remove tmux-claude-sidebar: the ~/.tmux.conf block, the Claude Code hooks,
# the sidebar panes and scripts. Lines install.sh disabled are turned back on
# and your previous preferredNotifChannel is restored. Plugins in
# ~/.tmux/plugins are left in place; delete them yourself if you like.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
TMUX_CONF="$HOME/.tmux.conf"

for f in "$TMUX_CONF" "$HOME/.claude/settings.json"; do
  [ -f "$f" ] || continue
  b="$f.bak-$(date +%Y%m%d)"
  [ -e "$b" ] && b="$b-$(date +%H%M%S)"
  cp -p "$f" "$b"
  echo "backed up $f -> $b"
done

python3 -I "$HERE/lib/edit_configs.py" uninstall "$TMUX_CONF" \
  "$HOME/.claude/settings.json" "$HOME/.tmux/claude-sidebar-state.json" \
  --claude-json "$HOME/.claude.json"

if tmux list-sessions >/dev/null 2>&1; then
  [ -f "$HOME/.tmux/sidebar/sched.py" ] && python3 -I "$HOME/.tmux/sidebar/sched.py" stop 2>/dev/null || true
  for h in window-linked session-created pane-exited after-kill-pane \
           window-resized client-resized after-select-pane \
           session-window-changed client-session-changed client-focus-in \
           client-attached; do
    tmux set-hook -gu "$h[70]" 2>/dev/null || true
  done
  tmux list-panes -a -F '#{?@sidebar,#{pane_id},}' | grep . |
    while read -r p; do tmux kill-pane -t "$p"; done || true
  # Back to tmux's defaults, then re-read your config so any bindings of your
  # own for these keys come back.
  tmux unbind-key S 2>/dev/null || true
  tmux unbind-key F 2>/dev/null || true
  tmux unbind-key T 2>/dev/null || true
  # prefix 1-9: your own bindings, if "jump to list rows" was on.
  if [ "$(tmux show-option -gqv @claude_sidebar_keys)" = position ] &&
     [ -f "$HOME/.tmux/sidebar/digit-keys.conf" ]; then
    tmux source-file "$HOME/.tmux/sidebar/digit-keys.conf" 2>/dev/null || true
  fi
  tmux set-option -gu @claude_sidebar_keys 2>/dev/null || true
  # Right-click: the binding that was there before install.
  if [ -f "$HOME/.tmux/sidebar/right-click.conf" ]; then
    tmux source-file "$HOME/.tmux/sidebar/right-click.conf" 2>/dev/null || true
  fi
  tmux unbind-key -n MouseDragEnd1Border 2>/dev/null || true
  # One string, so both commands go into the binding (a bare \; would run
  # send-keys -M right away, which fails outside a mouse event).
  tmux bind-key -n MouseDown1Pane 'select-pane -t = ; send-keys -M'
  # (list-commands NAME succeeds even for an unknown name, so search the list.)
  if tmux list-commands 2>/dev/null | grep -q "^new-pane "; then
    # tmux 3.8 and newer: put back the floating-pane drag bindings.
    tmux bind-key -n C-MouseDrag1Pane new-pane -M
    tmux bind-key -n M-MouseDrag1Pane move-pane -M
    tmux bind-key -n M-MouseDrag1Border move-pane -M
  else
    for k in C-MouseDrag1Pane M-MouseDrag1Pane M-MouseDrag1Border; do
      tmux unbind-key -n "$k" 2>/dev/null || true
    done
  fi
  [ -f "$TMUX_CONF" ] && tmux source-file "$TMUX_CONF"
  echo "Removed sidebars from your running tmux server."
fi

rm -rf "$HOME/.tmux/sidebar"
rm -f "$HOME/.tmux/claude-sidebar-width" "$HOME/.tmux/claude-sidebar-sort" \
      "$HOME/.tmux/claude-sidebar-judge" "$HOME/.tmux/claude-sidebar-keys" \
      "$HOME/.tmux/claude-sidebar-stale-days" \
      "$HOME"/.tmux/claude-sidebar-sched-*
# Your scheduled tasks are kept, in case you install again.
[ -f "$HOME/.tmux/claude-sidebar-schedules.json" ] &&
  echo "Kept your scheduled tasks in ~/.tmux/claude-sidebar-schedules.json."
echo "Done. Restart Claude Code sessions to drop the hooks."
