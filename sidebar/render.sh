#!/bin/bash
# Draws the sidebar once a second. Runs inside the sidebar pane.
# bash 3.2 compatible (macOS /bin/bash): no associative arrays.

# A UTF-8 locale so ${#name} counts characters. Use one that exists here.
case "${LC_ALL:-${LC_CTYPE:-$LANG}}" in
  *UTF-8*|*utf8*|*UTF8*|*utf-8*) ;;
  *)
    loc=$(locale -a 2>/dev/null | grep -i -m 1 -E '^(en_US|C)\.utf-?8$')
    export LC_ALL="${loc:-en_US.UTF-8}" ;;
esac

pane="$TMUX_PANE"
[ -n "$pane" ] || { echo "tmux-claude-sidebar: not inside tmux" >&2; exit 1; }

ESC=$(printf '\033')
R="${ESC}[0m"
BOLD="${ESC}[1m"
GREY="${ESC}[90m"
BLUE="${ESC}[34m"
GREEN="${ESC}[32m"
YELLOW="${ESC}[33m"
BYELLOW="${ESC}[1;33m"
ORANGE="${ESC}[38;5;208m"
MAGENTA="${ESC}[35m"
CYAN="${ESC}[36m"
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

# age SECONDS -> "<1m", "5m", "3h", "2d"
age() {
  local s="$1"
  if [ "$s" -lt 60 ]; then printf '<1m'
  elif [ "$s" -lt 3600 ]; then printf '%sm' $((s / 60))
  elif [ "$s" -lt 86400 ]; then printf '%sh' $((s / 3600))
  else printf '%sd' $((s / 86400)); fi
}

# stlabel STATE -> $st, the 11-column status label.
stlabel() {
  case "$1" in
    working) st="${BLUE}● working${R}  " ;;
    needs)   st="${BYELLOW}! needs you${R}" ;;
    review)  st="${YELLOW}◆ review${R}   " ;;
    followup) st="${MAGENTA}↩ follow up${R}" ;;
    done)    st="${GREEN}✓ done${R}     " ;;
    idle)    st="${GREY}○ idle${R}     " ;;
    *)       st="           " ;;   # no Claude Code session in this window
  esac
}

# Shells: if Claude's pane is back at one of these, Claude Code has exited.
is_shell() {
  case "${1#-}" in
    sh|bash|zsh|fish|dash|ksh|mksh|tcsh|csh|nu|xonsh|elvish|pwsh) return 0 ;;
  esac
  return 1
}

KEYS="1-9|jump to window|
n|next window|n
p|previous window|p
l|last window|l
w|pick from list|w
c|new window|c
,|rename window|,
&|close window|&
F|search prompts|F
T|scheduled tasks|T
d|detach|d
S|hide sidebar|S"

# Fields are split on "|": tmux 3.4 prints control characters such as \037
# as octal text, so they cannot be used. Free text always comes last.
WFMT='#{window_index}|#{window_id}|#{window_active}|#{@claude_state}|#{@claude_updated}|#{@claude_pid}|#{@claude_pane}|#{@claude_hash}|#{@claude_hash_at}|#{@claude_verdict}|#{@claude_sched}|#{@claude_sched_owned}|#{window_name}'

last_map=""
last_rows=""
last_out=""
while :; do
  info=$(tmux display-message -p -t "$pane" "#{pane_width}|#{pane_height}|#{pane_in_mode}|#{session_id}|#{window_active}|#{@claude_sidebar_sort}|#{@claude_sidebar_stall}|#{@claude_sched_file}|#{session_name}" 2>/dev/null) || exit 0
  IFS='|' read -r width height inmode sid onwin sortmode stall schedf sess <<EOF
$info
EOF
  # A stray scroll or drag can put the sidebar in copy mode, which freezes it.
  [ "$inmode" = 1 ] && tmux send-keys -t "$pane" -X cancel 2>/dev/null

  [ -n "$width" ] || width=30
  [ -n "$height" ] || height=40
  case "$stall" in ''|*[!0-9]*) stall=20 ;; esac
  inner=$((width - 1))           # never write the last column (avoids wrap)
  namew=$((inner - 18))          # " NN " + name + " " + 11-col status + " ×"
  [ $namew -lt 4 ] && namew=4
  now=$(date +%s)
  prefix=$(tmux show-option -gv prefix 2>/dev/null)
  case "$prefix" in C-*) prefix="Ctrl-${prefix#C-}" ;; '') prefix="Ctrl-b" ;; esac
  prefix_label=$prefix

  # Is a client with focus showing this session? Then its current window is
  # being looked at, and a "review" there becomes "done".
  focused=0
  tmux list-clients -F '#{session_id} #{client_flags}' 2>/dev/null |
    grep -q "^$sid .*focused" && focused=1

  # One line per window: sort key fields, then the window fields.
  rows=""
  while IFS='|' read -r idx wid active state updated pid cp hash hash_at verdict sched owned name; do
    [ -n "$wid" ] || continue
    case "$updated" in ''|*[!0-9]*) updated=0 ;; esac
    if [ -n "$state" ] && [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
      # Claude Code's process is gone without a SessionEnd (killed, crashed).
      # Clear the state once its pane is gone or back at a shell prompt.
      cmd=$(tmux display -p -t "$cp" '#{pane_current_command}' 2>/dev/null)
      if [ -z "$cmd" ] || is_shell "$cmd"; then
        tmux set -uw -t "$wid" @claude_state \; set -uw -t "$wid" @claude_pid 2>/dev/null
        state=""
      fi
    fi
    if [ "$state" = working ] && [ "$onwin" = 1 ] && [ -n "$cp" ]; then
      # A working Claude redraws its spinner every second. A screen that has
      # not changed for a while means it was interrupted with Esc (which runs
      # no hook) and is waiting for you. Only the sidebar in the session's
      # current window checks, to keep the cost down.
      h=$(tmux capture-pane -p -t "$cp" 2>/dev/null | cksum | cut -d ' ' -f 1)
      if [ "$h" != "$hash" ] || [ -z "$hash_at" ]; then
        tmux set -w -t "$wid" @claude_hash "$h" \; set -w -t "$wid" @claude_hash_at "$now" 2>/dev/null
      elif [ $((now - hash_at)) -ge "$stall" ]; then
        tmux set -w -t "$wid" @claude_state idle \; set -w -t "$wid" @claude_updated "$hash_at" 2>/dev/null
        state=idle updated=$hash_at
      fi
    fi
    if [ "$state" = review ] && [ "$active" = 1 ] && [ "$focused" = 1 ]; then
      tmux set -w -t "$wid" @claude_state done 2>/dev/null
      state=done
    fi
    # A finished turn you have seen, that the judge says is not wrapped up.
    [ "$state" = done ] && [ "$verdict" = followup ] && state=followup
    if [ "$sortmode" = index ]; then
      tier=0 key=0
    else
      # Needs you, unseen results, follow-ups, then the rest by most recently
      # updated, then windows without Claude Code.
      case "$state" in
        needs) tier=0 ;; review) tier=1 ;; followup) tier=2 ;; '') tier=4 ;; *) tier=3 ;;
      esac
      key=$((9999999999 - updated))
      [ "$tier" = 4 ] && key=0
    fi
    # Windows a schedule opened are listed under SCHEDULED, not here.
    [ "$owned" = 1 ] && continue
    rows="$rows$tier|$key|$idx|$wid|$active|$state|$updated|$verdict|$sched|$name
"
  done <<EOF
$(tmux list-windows -t "$pane" -F "$WFMT" 2>/dev/null)
EOF
  sorted=$(printf '%s' "$rows" | sort -t '|' -k1,1n -k2,2n -k3,3n)
  nwin=$(printf '%s' "$rows" | grep -c .)
  # Scheduled tasks (sched.py keeps this file up to date):
  #   id|paused|next run|run window|last run|note|name
  tasks=""
  [ -n "$schedf" ] && [ -f "$schedf" ] && tasks=$(cat "$schedf" 2>/dev/null)
  ntasks=$(printf '%s' "$tasks" | grep -c .)
  nruns=$(printf '%s\n' "$tasks" | awk -F '|' '{ n += ($4 != "") + ($6 != "") } END { print n + 0 }')
  nsched=$((2 + ntasks + nruns)); [ "$ntasks" = 0 ] && nsched=3

  out="${ESC}[H"
  row=0
  map=""
  rows_tbl=" "
  line() { out="$out$1$EL
"; row=$((row + 1)); }

  # "[−][+]" buttons at the right make the sidebar narrower or wider.
  line " ${GREY}SESSION${R} ${BOLD}$(fit "$sess" $((inner - 15)))${R}${GREY}[${R}−${GREY}][${R}+${GREY}]${R}"
  map="0=h:"
  # Sort order (click to switch) and the search button.
  if [ "$sortmode" = index ]; then label="window number"; else label="needs you first"; fi
  left=" sort: $label"
  pad=$((inner - 8 - ${#left})); [ $pad -lt 1 ] && pad=1
  line " ${GREY}sort:${R} ${label}$(rep ' ' $pad)${GREY}[${R}search${GREY}]${R}"
  map="$map 1=b:"
  line ""

  # Rows left for prompt lines under windows that want attention, so the
  # window rows themselves always fit.
  spare=$((height - 3 - nwin - nsched))
  while IFS='|' read -r tier key idx wid active state updated verdict sched name; do
    [ -n "$wid" ] || continue
    name=${name//\\\$/\$}
    stlabel "$state"
    n=$(fit "$name" "$namew")
    i=$(printf '%2s' "$idx")
    # The × in the last drawn column closes the window (see sidebar.sh click).
    if [ "$active" = 1 ]; then
      line "${ORANGE}▌${BOLD}${i} ${n}${R} ${st} ${GREY}×${R}"
    elif [ -n "$sched" ]; then
      # A schedule sent its prompt to this window of yours.
      line "${CYAN}↻${R}${GREY}${i}${R} ${n} ${st} ${GREY}×${R}"
    else
      line " ${GREY}${i}${R} ${n} ${st} ${GREY}×${R}"
    fi
    [ "$row" -le "$height" ] && map="$map $((row - 1))=w:$wid" &&
      rows_tbl="$rows_tbl$((row - 1))=$wid=$idx=$state=$verdict= "
    # Under a window that wants attention: how long ago, and either the
    # judge's note on a finished turn or your last prompt.
    case "$state" in needs|review|working|followup) ;; *) continue ;; esac
    [ "$spare" -gt 0 ] || continue
    text=$(tmux display -p -t "$wid" '#{@claude_note}|#{@claude_prompt}' 2>/dev/null)
    # tmux 3.4 prints "$" as "\$".
    text=${text//\\\$/\$}
    note=${text%%|*} prompt=${text#*|}
    mark=""
    case "$state" in
      review|followup)
        if [ -n "$note" ]; then
          prompt=$note
          if [ "$verdict" = followup ]; then mark="${MAGENTA}↩${GREY} "; else mark="${GREEN}✓${GREY} "; fi
        fi ;;
    esac
    [ -n "$prompt" ] || continue
    a=$(age $((now - updated)))
    [ "$updated" = 0 ] && a=""
    w=$((inner - 5 - ${#a})); [ -n "$mark" ] && w=$((w - 2))
    line "    ${GREY}${a} ${mark}$(fit "$prompt" $w)${R}"
    spare=$((spare - 1))
    [ "$row" -le "$height" ] && map="$map $((row - 1))=d:$wid" &&
      rows_tbl="$rows_tbl$((row - 1))=$wid=$idx=$state=$verdict= "
  done <<EOF
$sorted
EOF

  # SCHEDULED: each task, when it runs next, and how its last run went.
  line ""
  pad=$((inner - 19)); [ $pad -lt 1 ] && pad=1
  line " ${CYAN}↻ SCHEDULED${R}$(rep ' ' $pad)${GREY}[${R}+ add${GREY}]${R}"
  [ "$row" -le "$height" ] && map="$map $((row - 1))=t:add"
  [ "$ntasks" = 0 ] && line "   ${GREY}none yet · ${prefix_label} T${R}"
  tw=$((inner - 14)); [ $tw -lt 4 ] && tw=4
  while IFS='|' read -r tid paused next twin last note tname; do
    [ -n "$tid" ] || continue
    tname=${tname//\\\$/\$}
    if [ -n "$paused" ]; then nx="${GREY}$(printf '%9s' paused)${R}"; else nx="${GREY}$(printf '%9s' "$next")${R}"; fi
    line " ${CYAN}↻${R} $(fit "$tname" $tw) ${nx}"
    [ "$row" -le "$height" ] && map="$map $((row - 1))=s:$tid"
    if [ -n "$twin" ]; then
      info=$(tmux display -p -t "$twin" '#{window_index}|#{window_active}|#{session_id}|#{@claude_state}|#{@claude_verdict}|#{@claude_note}' 2>/dev/null)
      IFS='|' read -r ridx ract rsid rstate rverdict rnote <<EOF
$info
EOF
      rnote=${rnote//\\\$/\$}
      [ "$rstate" = done ] && [ "$rverdict" = followup ] && rstate=followup
      if [ -n "$rstate" ]; then stlabel "$rstate"; else st="${GREY}starting${R}   "; fi
      case "$last" in ''|*[!0-9]*) ran="" ;; *) ran="ran $(age $((now - last))) ago" ;; esac
      [ "$ran" = "ran <1m ago" ] && ran="ran just now"
      lead="   "; [ "$ract" = 1 ] && [ "$rsid" = "$sid" ] && lead="${ORANGE}▌${R}  "
      gap=$((inner - 3 - 3 - ${#ran} - 12)); [ $gap -lt 1 ] && gap=1
      line "${lead}${GREY}$(printf '%2s' "$ridx") ${ran}${R}$(rep ' ' $gap)${st}"
      [ "$row" -le "$height" ] && map="$map $((row - 1))=d:$twin"
      # Why it needs you, when it does.
      if [ -n "$rnote" ] && [ "$spare" -gt 0 ]; then
        case "$rstate" in review|followup|needs)
          if [ "$rverdict" = followup ]; then mk="${MAGENTA}↩${GREY}"; else mk="${GREEN}✓${GREY}"; fi
          line "      ${mk} ${GREY}$(fit "$rnote" $((inner - 9)))${R}"
          [ "$row" -le "$height" ] && map="$map $((row - 1))=d:$twin"
          spare=$((spare - 1)) ;;
        esac
      fi
    fi
    # Missed, skipped, or why a run could not start.
    if [ -n "$note" ]; then
      line "      ${GREY}$(fit "$note" $((inner - 7)))${R}"
      [ "$row" -le "$height" ] && map="$map $((row - 1))=s:$tid"
    fi
  done <<EOF
$tasks
EOF

  line ""
  line " ${GREY}$(rep '─' $((inner - 2)))${R}"
  line ""
  line " ${GREY}KEYS · press${R} ${ORANGE}${prefix}${R} ${GREY}first${R}"
  line ""
  while IFS='|' read -r key desc act; do
    line " ${ORANGE}$(fit "$key" 4)${R}$(fit "$desc" $((inner - 5)))"
    [ -n "$act" ] && [ "$row" -le "$height" ] && map="$map $((row - 1))=k:$act"
  done <<EOF
$KEYS
EOF
  line ""
  line " ${GREY}Right-click a window for more${R}"

  # Drop rows that would run past the bottom of the pane, then clear the rest.
  out=$(printf '%s' "$out" | head -n "$height")
  # Only draw when something changed.
  if [ "$out" != "$last_out" ]; then
    printf '%s%s' "$out" "${ESC}[J"
    last_out="$out"
  fi

  # Row -> action map, read by `sidebar.sh click`, and the window rows for
  # the right-click menu (menu.conf). Stored on the pane itself.
  if [ "$map" != "$last_map" ] || [ "$rows_tbl" != "$last_rows" ]; then
    tmux set-option -p -t "$pane" @sidebar_map "$map" \; \
         set-option -p -t "$pane" @sidebar_rows "$rows_tbl" 2>/dev/null
    last_map="$map" last_rows="$rows_tbl"
  fi
  sleep 1
done
