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
  "$HOME/.claude/settings.json" "$HOME/.tmux/claude-sidebar-state.json"

if tmux list-sessions >/dev/null 2>&1; then
  for h in window-linked session-created pane-exited after-kill-pane \
           window-resized client-resized after-select-pane; do
    tmux set-hook -gu "$h[70]" 2>/dev/null || true
  done
  tmux list-panes -a -F '#{?@sidebar,#{pane_id},}' | grep . |
    while read -r p; do tmux kill-pane -t "$p"; done || true
  # Back to tmux's defaults, then re-read your config so any bindings of your
  # own for these keys come back.
  tmux unbind-key S 2>/dev/null || true
  tmux unbind-key -n MouseDragEnd1Border 2>/dev/null || true
  tmux bind-key -n MouseDown1Pane select-pane -t = \; send-keys -M
  if tmux list-commands new-pane >/dev/null 2>&1; then
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
rm -f "$HOME/.tmux/claude-sidebar-width"
echo "Done. Restart Claude Code sessions to drop the hooks."
