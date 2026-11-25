#!/bin/bash
# Draws the sidebar. Runs inside the sidebar pane. Only the sidebar you can
# see redraws (once a second); the others nap until their window is shown
# (sidebar.sh wakes them with SIGUSR1). The loop starts no helper processes
# per row: with many windows, forks are what make tmux slow to answer clicks.
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
BMAGENTA="${ESC}[1;35m"
CYAN="${ESC}[36m"
EL="${ESC}[K"

printf '%s' "${ESC}[?25l"
trap 'printf "%s" "${ESC}[?25h"' EXIT
trap 'exit 0' HUP TERM INT
trap 'woken=1' USR1
NL='
'

# The helpers below set a variable instead of printing, so calling them
# needs no subshell.
# pad_ N [CHAR] -> $P, N copies of CHAR (a space by default)
pad_() { local n="$1"; [ "$n" -gt 0 ] || n=0; printf -v P '%*s' "$n" ''; [ -n "$2" ] && P=${P// /$2}; }

# fit_ TEXT WIDTH -> $F, TEXT cut with an ellipsis and padded to WIDTH
fit_() {
  local t="$1" w="$2"
  if [ "${#t}" -gt "$w" ]; then t="${t:0:$((w - 1))}…"; fi
  pad_ $((w - ${#t})); F="$t$P"
}

# age_ SECONDS -> $A, "<1m", "5m", "3h", "2d"
age_() {
  local s="$1"
  if [ "$s" -lt 60 ]; then A='<1m'
  elif [ "$s" -lt 3600 ]; then A="$((s / 60))m"
  elif [ "$s" -lt 86400 ]; then A="$((s / 3600))h"
  elif [ "$s" -lt 8640000 ]; then A="$((s / 86400))d"
  else A='99d'; fi
}

# nap SECONDS: sleep, but wake early on SIGUSR1 (this window was shown).
nap() {
  woken=0
  sleep "$1" &
  wait $! 2>/dev/null
  [ "$woken" = 1 ] && kill $! 2>/dev/null
}

# stlabel STATE -> $st, the 11-column status label.
stlabel() {
  case "$1" in
    working) st="${BLUE}● working${R}  " ;;
    needs)   st="${BYELLOW}! needs you${R}" ;;
    # Finished since you last looked (why it needs you, if it does: search).
    review|followup) st="${YELLOW}◆ unread${R}   " ;;
    # You have looked, and Claude is waiting on your answer or decision.
    todo)    st="${MAGENTA}↩ to do${R}    " ;;
    # Nothing needed (done, idle), or no Claude Code in this window.
    *)       st="           " ;;
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
start=$(date +%s)
SECONDS=0
last_stall=0
while :; do
  info=$(tmux display-message -p -t "$pane" "#{pane_width}|#{pane_height}|#{pane_in_mode}|#{session_id}|#{window_active}|#{session_attached}|#{@claude_sidebar_sort}|#{@claude_sidebar_stall}|#{@claude_sched_file}|#{prefix}|#{session_name}" 2>/dev/null) || exit 0
  IFS='|' read -r width height inmode sid onwin attached sortmode stall schedf prefix sess <<EOF
$info
EOF
  # A stray scroll or drag can put the sidebar in copy mode, which freezes it.
  [ "$inmode" = 1 ] && tmux send-keys -t "$pane" -X cancel 2>/dev/null
  # Nobody can see this sidebar: nap (drawn once at start, so it is never
  # blank when shown; showing its window wakes it).
  if [ -n "$last_out" ] && { [ "$onwin" != 1 ] || [ "${attached:-0}" = 0 ]; }; then
    nap 3
    continue
  fi

  [ -n "$width" ] || width=30
  [ -n "$height" ] || height=40
  case "$stall" in ''|*[!0-9]*) stall=20 ;; esac
  inner=$((width - 1))           # never write the last column (avoids wrap)
  namew=$((inner - 22))          # " NN " + name + " " + status(11) + " " + age(3) + " ×"
  [ $namew -lt 4 ] && namew=4
  now=$((start + SECONDS))
  case "$prefix" in C-*) prefix="Ctrl-${prefix#C-}" ;; '') prefix="Ctrl-b" ;; esac
  prefix_label=$prefix
  # The stalled-Claude check reads screens: every 5 seconds is plenty.
  stallnow=0
  [ $((now - last_stall)) -ge 5 ] && stallnow=1 last_stall=$now

  # One line per window: sort key fields, then the window fields.
  rows=""
  nwin=0
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
    if [ "$state" = working ] && [ "$stallnow" = 1 ] && [ -n "$cp" ]; then
      # A working Claude redraws its spinner every second. A screen that has
      # not changed for a while means it was interrupted with Esc (which runs
      # no hook) and is waiting for you. Only the sidebar you can see checks.
      h=$(tmux capture-pane -p -t "$cp" 2>/dev/null | cksum | cut -d ' ' -f 1)
      if [ "$h" != "$hash" ] || [ -z "$hash_at" ]; then
        tmux set -w -t "$wid" @claude_hash "$h" \; set -w -t "$wid" @claude_hash_at "$now" 2>/dev/null
      elif [ $((now - hash_at)) -ge "$stall" ]; then
        tmux set -w -t "$wid" @claude_state idle \; set -w -t "$wid" @claude_updated "$hash_at" 2>/dev/null
        state=idle updated=$hash_at
      fi
    fi
    # ("review" becomes "done" when you look: sidebar.sh seen, from tmux hooks.)
    # A finished turn the judge says is not wrapped up: "follow up" while
    # unread, "to do" once you have looked at it.
    disp=$state
    if [ "$verdict" = followup ]; then
      case "$state" in review) disp=followup ;; done) disp=todo ;; esac
    fi
    if [ "$sortmode" = index ]; then
      tier=0 key=0
    else
      # Needs you, unseen results, follow-ups, then the rest by most recently
      # updated, then windows without Claude Code.
      case "$disp" in
        needs) tier=0 ;; review|followup) tier=1 ;; todo) tier=2 ;; working) tier=3 ;;
        '') tier=5 ;; *) tier=4 ;;
      esac
      key=$((9999999999 - updated))
      [ "$tier" = 5 ] && key=0
    fi
    # Windows a schedule opened are listed under SCHEDULED, not here.
    [ "$owned" = 1 ] && continue
    rows="$rows$tier|$key|$idx|$wid|$active|$state|$updated|$verdict|$disp|$sched|$name
"
    nwin=$((nwin + 1))
  done <<EOF
$(tmux list-windows -t "$pane" -F "$WFMT" 2>/dev/null)
EOF
  if [ "$sortmode" = index ]; then
    sorted=$rows                 # list-windows is already in index order
  else
    sorted=$(printf '%s' "$rows" | sort -t '|' -k1,1n -k2,2n -k3,3n)
  fi
  # Scheduled tasks (sched.py keeps this file up to date):
  #   id|paused|next run|run window|last run|note|name
  tasks="" ntasks=0 nruns=0
  if [ -n "$schedf" ] && [ -f "$schedf" ]; then
    while IFS= read -r tl; do
      [ -n "$tl" ] || continue
      tasks="$tasks$tl$NL"
      ntasks=$((ntasks + 1))
      IFS='|' read -r _ _ _ tw4 _ tn6 _ <<EOF
$tl
EOF
      [ -n "$tw4" ] && nruns=$((nruns + 1))
      [ -n "$tn6" ] && nruns=$((nruns + 1))
    done < "$schedf"
  fi
  nsched=$((2 + ntasks + nruns)); [ "$ntasks" = 0 ] && nsched=3

  out="${ESC}[H"
  row=0
  map=""
  rows_tbl=" "
  # Rows past the bottom of the pane are dropped.
  line() { [ "$row" -lt "$height" ] && out="$out$1$EL$NL"; row=$((row + 1)); }

  # "[−][+]" buttons at the right make the sidebar narrower or wider.
  fit_ "$sess" $((inner - 15))
  line " ${GREY}SESSION${R} ${BOLD}${F}${R}${GREY}[${R}−${GREY}][${R}+${GREY}]${R}"
  map="0=h:"
  # Sort order (click to switch) and the search button.
  if [ "$sortmode" = index ]; then label="window number"; else label="needs you first"; fi
  left=" sort: $label"
  pad=$((inner - 8 - ${#left})); [ $pad -lt 1 ] && pad=1
  pad_ $pad
  line " ${GREY}sort:${R} ${label}${P}${GREY}[${R}search${GREY}]${R}"
  map="$map 1=b:"
  line ""

  # Rows left for the reasons under scheduled runs, so the window and task
  # rows themselves always fit.
  spare=$((height - 3 - nwin - nsched))
  while IFS='|' read -r tier key idx wid active state updated verdict disp sched name; do
    [ -n "$wid" ] || continue
    name=${name//\\\$/\$}
    stlabel "$disp"
    fit_ "$name" "$namew"; n=$F
    # How long ago its status last changed.
    a="   "
    if [ -n "$state" ] && [ "$updated" != 0 ]; then age_ $((now - updated)); printf -v a '%3s' "$A"; fi
    printf -v i '%2s' "$idx"
    # The × in the last drawn column closes the window (see sidebar.sh click).
    if [ "$active" = 1 ]; then
      line "${ORANGE}▌${BOLD}${i} ${n}${R} ${st} ${GREY}${a} ×${R}"
    elif [ -n "$sched" ]; then
      # A schedule sent its prompt to this window of yours.
      line "${CYAN}↻${R}${GREY}${i}${R} ${n} ${st} ${GREY}${a} ×${R}"
    else
      line " ${GREY}${i}${R} ${n} ${st} ${GREY}${a} ×${R}"
    fi
    [ "$row" -le "$height" ] && map="$map $((row - 1))=w:$wid" &&
      rows_tbl="$rows_tbl$((row - 1))=$wid=$idx=$state=$verdict= "
  done <<EOF
$sorted
EOF

  # SCHEDULED: each task, when it runs next, and how its last run went.
  line ""
  pad=$((inner - 19)); [ $pad -lt 1 ] && pad=1
  pad_ $pad
  line " ${CYAN}↻ SCHEDULED${R}${P}${GREY}[${R}+ add${GREY}]${R}"
  [ "$row" -le "$height" ] && map="$map $((row - 1))=t:add"
  [ "$ntasks" = 0 ] && line "   ${GREY}none yet · ${prefix_label} T${R}"
  tw=$((inner - 14)); [ $tw -lt 4 ] && tw=4
  while IFS='|' read -r tid paused next twin last note tname; do
    [ -n "$tid" ] || continue
    tname=${tname//\\\$/\$}
    [ -n "$paused" ] && next=paused
    printf -v nx '%9s' "$next"
    fit_ "$tname" $tw
    line " ${CYAN}↻${R} ${F} ${GREY}${nx}${R}"
    [ "$row" -le "$height" ] && map="$map $((row - 1))=s:$tid"
    if [ -n "$twin" ]; then
      info=$(tmux display -p -t "$twin" '#{window_index}|#{window_active}|#{session_id}|#{@claude_state}|#{@claude_verdict}|#{@claude_note}' 2>/dev/null)
      IFS='|' read -r ridx ract rsid rstate rverdict rnote <<EOF
$info
EOF
      rnote=${rnote//\\\$/\$}
      if [ "$rverdict" = followup ]; then
        case "$rstate" in review) rstate=followup ;; done) rstate=todo ;; esac
      fi
      if [ -n "$rstate" ]; then stlabel "$rstate"; else st="${GREY}starting${R}   "; fi
      case "$last" in ''|*[!0-9]*) ran="" ;; *) age_ $((now - last)); ran="ran $A ago" ;; esac
      [ "$ran" = "ran <1m ago" ] && ran="ran just now"
      lead="   "; [ "$ract" = 1 ] && [ "$rsid" = "$sid" ] && lead="${ORANGE}▌${R}  "
      gap=$((inner - 3 - 3 - ${#ran} - 12)); [ $gap -lt 1 ] && gap=1
      printf -v ri '%2s' "$ridx"
      pad_ $gap
      line "${lead}${GREY}${ri} ${ran}${R}${P}${st}"
      [ "$row" -le "$height" ] && map="$map $((row - 1))=d:$twin"
      # Why it needs you, when it does.
      if [ -n "$rnote" ] && [ "$spare" -gt 0 ]; then
        case "$rstate" in review|followup|todo|needs)
          if [ "$rverdict" = followup ]; then mk="${MAGENTA}↩${GREY}"; else mk="${GREEN}✓${GREY}"; fi
          fit_ "$rnote" $((inner - 9))
          line "      ${mk} ${GREY}${F}${R}"
          [ "$row" -le "$height" ] && map="$map $((row - 1))=d:$twin"
          spare=$((spare - 1)) ;;
        esac
      fi
    fi
    # Missed, skipped, or why a run could not start.
    if [ -n "$note" ]; then
      fit_ "$note" $((inner - 7))
      line "      ${GREY}${F}${R}"
      [ "$row" -le "$height" ] && map="$map $((row - 1))=s:$tid"
    fi
  done <<EOF
$tasks
EOF

  line ""
  pad_ $((inner - 2)) '─'
  line " ${GREY}${P}${R}"
  line ""
  line " ${GREY}KEYS · press${R} ${ORANGE}${prefix}${R} ${GREY}first${R}"
  line ""
  while IFS='|' read -r key desc act; do
    fit_ "$key" 4; k4=$F
    fit_ "$desc" $((inner - 5))
    line " ${ORANGE}${k4}${R}${F}"
    [ -n "$act" ] && [ "$row" -le "$height" ] && map="$map $((row - 1))=k:$act"
  done <<EOF
$KEYS
EOF
  line ""
  line " ${GREY}Right-click a window for more${R}"

  # No newline after the last row (it would scroll the pane), then clear the
  # rest. Only draw when something changed.
  out=${out%"$NL"}
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
  nap 1
done
