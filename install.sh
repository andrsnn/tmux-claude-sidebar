#!/bin/sh
# Install tmux-claude-sidebar for the current user.
#   ./install.sh              install, then reload tmux if a server is running
#   ./install.sh --no-reload  install only
# Re-running is safe: it replaces what it installed before.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
NO_RELOAD=0
[ "${1:-}" = "--no-reload" ] && NO_RELOAD=1

TMUX_CONF="$HOME/.tmux.conf"
SETTINGS="$HOME/.claude/settings.json"
SIDEBAR_DIR="$HOME/.tmux/sidebar"
PLUGIN_DIR="$HOME/.tmux/plugins"
STATE_FILE="$HOME/.tmux/claude-sidebar-state.json"

# name|repo|tag|commit
PLUGINS="tmux-sensible|https://github.com/tmux-plugins/tmux-sensible.git|v3.0.0|2ce7679f9dcf42dffa60abd59a3405b87cb8213b
catppuccin|https://github.com/catppuccin/tmux.git|v2.3.2|32d0a8d95d562dd064a62a09d41af136561ae659"

say() { printf '%s\n' "$*"; }
die() { printf 'install: %s\n' "$*" >&2; exit 1; }

command -v tmux >/dev/null || die "tmux not found"
command -v git >/dev/null || die "git not found"
command -v python3 >/dev/null || die "python3 not found (needed to edit settings.json)"
case "$(tmux -V)" in
  "tmux 3."[4-9]*|"tmux 3."[1-9][0-9]*|"tmux "[4-9]*|"tmux next"*) ;;
  *) die "tmux 3.4 or newer is required (found $(tmux -V))" ;;
esac

# 1. Back up configs. Never overwrite an existing backup.
for f in "$TMUX_CONF" "$SETTINGS" "$HOME/.claude.json"; do
  [ -f "$f" ] || continue
  b="$f.bak-$(date +%Y%m%d)"
  [ -e "$b" ] && b="$b-$(date +%H%M%S)"
  cp -p "$f" "$b"
  say "backed up $f -> $b"
done

# 2. Scripts.
mkdir -p "$SIDEBAR_DIR"
for f in render.sh sidebar.sh hook.sh search.py judge.py sched.py sidebar.conf menu.conf; do
  cp "$HERE/sidebar/$f" "$SIDEBAR_DIR/$f"
done
chmod 755 "$SIDEBAR_DIR/render.sh" "$SIDEBAR_DIR/sidebar.sh" "$SIDEBAR_DIR/hook.sh"
say "installed scripts in $SIDEBAR_DIR"

# 3. Plugins, pinned.
mkdir -p "$PLUGIN_DIR"
printf '%s\n' "$PLUGINS" | while IFS='|' read -r name url tag sha; do
  dest="$PLUGIN_DIR/$name"
  if [ -d "$dest/.git" ]; then
    if [ "$(git -C "$dest" rev-parse HEAD)" != "$sha" ]; then
      git -C "$dest" fetch --quiet origin "$sha" 2>/dev/null || git -C "$dest" fetch --quiet --tags origin
      git -C "$dest" -c advice.detachedHead=false checkout --quiet "$sha"
    fi
  elif [ -e "$dest" ]; then
    die "$dest exists but is not a git clone; move it aside and re-run"
  else
    git clone --quiet "$url" "$dest"
    git -C "$dest" -c advice.detachedHead=false checkout --quiet "$sha"
  fi
  [ "$(git -C "$dest" rev-parse HEAD)" = "$sha" ] || die "$name is not at $sha"
  [ -z "$(git -C "$dest" status --porcelain)" ] || die "$dest has local changes; move it aside and re-run"
  say "plugin $name at $tag ($sha)"
done

# 4. ~/.tmux.conf block and Claude Code hooks.
python3 -I "$HERE/lib/edit_configs.py" install "$TMUX_CONF" "$SETTINGS" \
  "$HERE/lib/block.conf" "$STATE_FILE" --claude-json "$HOME/.claude.json"

# 5. Apply to a running tmux server, restarting sidebars on the new script.
say ""
if [ "$NO_RELOAD" = 0 ] && tmux list-sessions >/dev/null 2>&1; then
  # Restart the scheduler on the new code (the config starts it again).
  python3 -I "$SIDEBAR_DIR/sched.py" stop 2>/dev/null || true
  sleep 1
  tmux source-file "$TMUX_CONF"
  tmux list-panes -a -F '#{?@sidebar,#{pane_id},}' | grep . |
    while read -r p; do tmux respawn-pane -k -t "$p"; done || true
  "$SIDEBAR_DIR/sidebar.sh" resize
  say "Reloaded your running tmux server."
else
  say "Start tmux to see the sidebar."
fi
say "Restart any Claude Code sessions that are already running so they pick up the hooks."
say "Note: this turns on tmux mouse mode. Hold Option (iTerm2), Fn (Terminal.app)"
say "or Shift (most other terminals) while dragging to select text the usual way."
