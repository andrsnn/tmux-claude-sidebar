#!/bin/bash
# Draws the sidebar once a second. Runs inside the sidebar pane.
# bash 3.2 compatible (macOS /bin/bash): no associative arrays.

case "${LC_ALL:-${LC_CTYPE:-$LANG}}" in
  *UTF-8*|*utf8*|*UTF8*|*utf-8*) ;;
  *) export LC_ALL=en_US.UTF-8 ;;
esac

pane="$TMUX_PANE"
[ -n "$pane" ] || { echo "tmux-claude-sidebar: not inside tmux" >&2; exit 1; }

US=$(printf '\037')
ESC=$(printf '\033')
R="${ESC}[0m"
BOLD="${ESC}[1m"
DIM="${ESC}[2m"
GREY="${ESC}[90m"
BLUE="${ESC}[34m"
GREEN="${ESC}[32m"
YELLOW="${ESC}[1;33m"
ORANGE="${ESC}[38;5;208m"
EL="${ESC}[K"

printf '%s' "${ESC}[?25l"
trap 'printf "%s" "${ESC}[?25h"' EXIT
trap 'exit 0' HUP TERM INT

# repeat CHAR N
rep() { local s="" i=0; while [ $i -lt "$2" ]; do s="$s$1"; i=$((i + 1)); done; printf '%s' "$s"; }

# fit TEXT WIDTH -> TEXT truncated with an ellipsis and right-padded to WIDTH
fit() {
  local t="$1" w="$2"
  if [ "${#t}" -gt "$w" ]; then t="${t:0:$((w - 1))}…"; fi
  printf '%s%s' "$t" "$(rep ' ' $((w - ${#t})))"
}

KEYS="1-9|jump to window|
n|next window|n
p|previous window|p
l|last window|l
w|pick from list|w
c|new window|c
,|rename window|,
&|close window|&
d|detach|d
S|hide sidebar|S"

last_map=""
while :; do
  info=$(tmux display-message -p -t "$pane" "#{session_name}${US}#{pane_width}${US}#{pane_height}${US}#{pane_in_mode}" 2>/dev/null) || exit 0
  IFS="$US" read -r sess width height inmode <<EOF
$info
EOF
  # A stray scroll or drag can put the sidebar in copy mode, which freezes it.
  [ "$inmode" = 1 ] && tmux send-keys -t "$pane" -X cancel 2>/dev/null

  [ -n "$width" ] || width=30
  [ -n "$height" ] || height=40
  inner=$((width - 1))           # never write the last column (avoids wrap)
  namew=$((inner - 18))          # " NN " + name + " " + 11-col status + " ×"
  [ $namew -lt 4 ] && namew=4

  out="${ESC}[H"
  row=0
  map=""
  line() { out="$out$1$EL
"; row=$((row + 1)); }

  line " ${GREY}SESSION${R} ${BOLD}$(fit "$sess" $((inner - 9)))${R}"
  line ""

  while IFS="$US" read -r idx wid active state name; do
    [ -n "$wid" ] || continue
    case "$state" in
      working) st="${BLUE}● working${R}  " ;;
      needs)   st="${YELLOW}! needs you${R}" ;;
      done)    st="${GREEN}✓ done${R}     " ;;
      idle)    st="${GREY}○ idle${R}     " ;;
      *)       st="           " ;;   # no Claude Code session in this window
    esac
    n=$(fit "$name" "$namew")
    i=$(printf '%2s' "$idx")
    # The × in the last drawn column closes the window (see sidebar.sh click).
    if [ "$active" = 1 ]; then
      line "${ORANGE}▌${BOLD}${i} ${n}${R} ${st} ${GREY}×${R}"
    else
      line " ${GREY}${i}${R} ${n} ${st} ${GREY}×${R}"
    fi
    [ "$row" -le "$height" ] && map="$map $((row - 1))=w:$wid"
  done <<EOF
$(tmux list-windows -t "$pane" -F "#{window_index}${US}#{window_id}${US}#{window_active}${US}#{@claude_state}${US}#{window_name}" 2>/dev/null)
EOF

  line ""
  line " ${GREY}$(rep '─' $((inner - 2)))${R}"
  line ""
  prefix=$(tmux show-option -gv prefix 2>/dev/null)
  case "$prefix" in C-*) prefix="Ctrl-${prefix#C-}" ;; '') prefix="Ctrl-b" ;; esac
  line " ${GREY}KEYS · press${R} ${ORANGE}${prefix}${R} ${GREY}first${R}"
  line ""
  while IFS='|' read -r key desc act; do
    line " ${ORANGE}$(fit "$key" 4)${R}$(fit "$desc" $((inner - 5)))"
    [ -n "$act" ] && [ "$row" -le "$height" ] && map="$map $((row - 1))=k:$act"
  done <<EOF
$KEYS
EOF

  # Drop rows that would run past the bottom of the pane, then clear the rest.
  out=$(printf '%s' "$out" | head -n "$height")
  printf '%s%s' "$out" "${ESC}[J"

  # Row -> action map, read by `sidebar.sh click`. Stored on the pane itself.
  if [ "$map" != "$last_map" ]; then
    tmux set-option -p -t "$pane" @sidebar_map "$map" 2>/dev/null
    last_map="$map"
  fi
  sleep 1
done
