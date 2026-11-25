#!/usr/bin/env python3
"""Search the prompts of every Claude Code window and jump to one.

    search.py <client>            the popup UI (prefix F, or [search])
    search.py --list [words...]   print matches, one per line (for tests)

With nothing typed it lists each Claude window once, with your last prompt:
the ones that need you first, then the most recently updated. Typing searches
every prompt of every window (and the window names); all words must match.
Enter switches to that window, Esc closes.

Prompts come from each session's transcript (its path is recorded on the
window by hook.sh), so the history includes prompts from before a resume.
Python 3.7+, standard library only.
"""
import json
import os
import subprocess
import sys
import time
import unicodedata
from datetime import datetime

STATE_LABEL = {
    "needs": "! needs you", "review": "◆ review", "working": "● working",
    "done": "✓ done", "idle": "○ idle", "followup": "↩ follow up", "": "",
}
TIER = {"needs": 0, "review": 1, "followup": 2}


def tmux(*args):
    r = subprocess.run(["tmux", "-u"] + list(args), stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL)
    return r.stdout.decode("utf-8", "replace")


def field(name):
    """{window_id: value} for one format, across all sessions.

    One field per call: tmux 3.4 escapes control characters in its output,
    so values cannot be joined with a separator. Window ids never contain
    spaces, so "id value" splits safely.
    """
    out = {}
    for ln in tmux("list-windows", "-a", "-F", "#{window_id} " + name).splitlines():
        wid, _, val = ln.partition(" ")
        if wid:
            out[wid] = val.replace("\\$", "$")    # tmux 3.4 prints "$" as "\$"
    return out


def as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def windows():
    ids = field("#{session_name}:#{window_index}")
    cols = {k: field(f) for k, f in (
        ("name", "#{window_name}"), ("state", "#{@claude_state}"),
        ("updated", "#{@claude_updated}"), ("transcript", "#{@claude_transcript}"),
        ("prompt", "#{@claude_prompt}"), ("prompt_at", "#{@claude_prompt_at}"),
        ("verdict", "#{@claude_verdict}"), ("note", "#{@claude_note}"))}
    out = []
    for wid, where in ids.items():
        w = {k: cols[k].get(wid, "") for k in cols}
        w.update(id=wid, where=where, updated=as_int(w["updated"]),
                 prompt_at=as_int(w["prompt_at"]))
        if w["state"] == "done" and w["verdict"] == "followup":
            w["state"] = "followup"
        out.append(w)
    return out


def parse_time(ts):
    if not ts:
        return 0
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    except ValueError:
        try:
            return int(datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
                       - time.timezone)
        except ValueError:
            return 0


def prompt_text(obj):
    """The text you typed, or None for tool results, commands and the like."""
    if obj.get("type") != "user" or obj.get("isMeta") or obj.get("isSidechain"):
        return None
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, list):
        # Text blocks only: a list of tool results is Claude's, not yours.
        parts = [c.get("text", "") for c in content
                 if isinstance(c, dict) and c.get("type") == "text"]
        if not parts:
            return None
        content = " ".join(parts)
    if not isinstance(content, str):
        return None
    text = " ".join(content.split())
    # Slash-command wrappers, local command output, system notes.
    if not text or text.startswith("<") or text.startswith("[Request interrupted"):
        return None
    return text


def transcript_prompts(path):
    """[(time, text)] in order, from a Claude Code transcript (JSON lines)."""
    out = []
    try:
        with open(path, "rb") as f:
            for raw in f:
                if b'"user"' not in raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                text = prompt_text(obj)
                if text:
                    out.append((parse_time(obj.get("timestamp")), text))
    except OSError:
        pass
    return out


def collect():
    """Every Claude window and its prompts (oldest first)."""
    wins = []
    for w in windows():
        prompts = transcript_prompts(w["transcript"]) if w["transcript"] else []
        if w["prompt"] and not any(t == w["prompt"] for _, t in prompts[-3:]):
            prompts.append((w["prompt_at"], w["prompt"]))
        if not (prompts or w["state"]):
            continue                      # not a Claude window
        w["prompts"] = prompts
        wins.append(w)
    return wins


def recency(w):
    return max([w["updated"]] + [t for t, _ in w["prompts"][-1:]])


def results(wins, query, history=False):
    """Rows of (window, time, text) for the current query."""
    words = query.lower().split()
    if not words and not history:
        # One row per window: needs you, review, follow up, then most recent.
        rows = []
        for w in sorted(wins, key=lambda w: (TIER.get(w["state"], 3), -recency(w))):
            t, text = w["prompts"][-1] if w["prompts"] else (w["updated"], "")
            rows.append((w, t, text))
        return rows
    rows, seen = [], set()
    for w in wins:
        name = w["name"].lower()
        for t, text in w["prompts"]:
            hay = name + " " + text.lower()
            if all(x in hay for x in words) and (w["id"], text) not in seen:
                seen.add((w["id"], text))
                rows.append((w, t, text))
        if words and not w["prompts"] and all(x in name for x in words):
            rows.append((w, w["updated"], ""))
    rows.sort(key=lambda r: -r[1])
    return rows


def age(t, now=None):
    if not t:
        return ""
    s = max(0, int((now or time.time()) - t))
    if s < 60:
        return "<1m"
    if s < 3600:
        return "%dm" % (s // 60)
    if s < 86400:
        return "%dh" % (s // 3600)
    return "%dd" % (s // 86400)


def jump(client, wid):
    args = ["switch-client", "-t", wid]
    if client:
        args[1:1] = ["-c", client]
    subprocess.run(["tmux"] + args, stderr=subprocess.DEVNULL)


# ---- the popup ------------------------------------------------------------

def cwidth(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def clip(text, width):
    """Text cut to `width` terminal columns, with an ellipsis if cut."""
    out, used = [], 0
    for ch in text:
        w = cwidth(ch)
        if used + w > width:
            if out and width > 0:
                while out and used + 1 > width:
                    used -= cwidth(out.pop())
                out.append("…")
            break
        out.append(ch)
        used += w
    return "".join(out)


def wrap(text, width, lines):
    out, cur, used = [], "", 0
    for ch in text:
        w = cwidth(ch)
        if used + w > width:
            out.append(cur)
            cur, used = "", 0
            if len(out) == lines:
                break
        cur += ch
        used += w
    else:
        out.append(cur)
    if len(out) > lines:
        out = out[:lines]
    if len(out) == lines and sum(len(x) for x in out) < len(text):
        out[-1] = clip(out[-1], width - 1) + "…"
    return out


def ui(stdscr, client, wins):
    import curses
    try:
        curses.curs_set(1)
        curses.use_default_colors()
    except curses.error:
        pass
    pairs = {}
    for i, (key, fg) in enumerate((("needs", curses.COLOR_YELLOW), ("review", curses.COLOR_YELLOW),
                                   ("followup", curses.COLOR_MAGENTA),
                                   ("working", curses.COLOR_BLUE), ("done", curses.COLOR_GREEN),
                                   ("idle", -1), ("match", curses.COLOR_YELLOW),
                                   ("accent", 208 if curses.COLORS >= 256 else curses.COLOR_RED)), 1):
        curses.init_pair(i, fg, -1)
        pairs[key] = curses.color_pair(i)
    dim = curses.A_DIM
    query, sel, top, history = "", 0, 0, False
    while True:
        rows = results(wins, query, history)
        h, w = stdscr.getmaxyx()
        listh = max(1, h - 8)
        sel = max(0, min(sel, len(rows) - 1))
        if sel < top:
            top = sel
        if sel >= top + listh:
            top = sel - listh + 1
        stdscr.erase()

        def put(y, x, s, attr=0):
            if 0 <= y < h and x < w - 1:
                try:
                    stdscr.addstr(y, x, clip(s, w - 1 - x), attr)
                except curses.error:
                    pass

        nprompts = sum(len(x["prompts"]) for x in wins)
        info = "%d windows · %d prompts" % (len(wins), nprompts)
        put(0, max(0, w - 2 - len(info)), info, dim)
        put(0, 1, "Search:", pairs["accent"] | curses.A_BOLD)
        put(0, 9, query)
        mode = ("all prompts, newest first" if (query or history)
                else "last prompt per window, needs you first")
        put(1, 1, mode, dim)
        put(2, 1, "─" * (w - 3), dim)
        now = time.time()
        words = query.lower().split()
        namew = max(8, min(24, w // 5))
        for i, (win, t, text) in enumerate(rows[top:top + listh]):
            y = 3 + i
            chosen = top + i == sel
            base = curses.A_REVERSE if chosen else 0
            if chosen:
                put(y, 0, " " * (w - 1), base)
            x = 1
            put(y, x, "%-*s" % (namew, clip(win["name"], namew)), base | curses.A_BOLD)
            x += namew + 1
            st = STATE_LABEL.get(win["state"], "")
            put(y, x, st, base | pairs.get(win["state"], 0))
            x += 12
            a = age(t, now)
            put(y, x, "%4s" % a, base | dim)
            x += 6
            avail = w - 2 - x
            shown = clip(text, avail)
            put(y, x, shown, base)
            # Highlight the matched words.
            low = shown.lower()
            for word in words:
                start = low.find(word)
                while start >= 0 and word:
                    put(y, x + sum(cwidth(c) for c in shown[:start]),
                        shown[start:start + len(word)], base | pairs["match"] | curses.A_BOLD)
                    start = low.find(word, start + len(word))
        if not rows:
            put(3, 1, "No matches." if query else "No Claude Code windows yet.", dim)
        # The chosen prompt in full (as much as fits), and the keys.
        put(h - 5, 1, "─" * (w - 3), dim)
        if rows:
            win, t, text = rows[sel]
            head = "%s  %s" % (win["where"], win["name"])
            if win["note"]:
                head += "  ·  %s %s" % ("↩" if win["verdict"] == "followup" else "✓", win["note"])
            put(h - 4, 1, head, pairs["accent"])
            for j, ln in enumerate(wrap(text or "(no prompt yet)", w - 3, 2)):
                put(h - 3 + j, 1, ln)
        put(h - 1, 1, "↑↓ choose · Enter go to window · Tab all prompts · Esc close", dim)
        stdscr.move(0, min(w - 2, 9 + sum(cwidth(c) for c in query)))
        stdscr.refresh()

        try:
            k = stdscr.get_wch()
        except curses.error:
            continue
        except KeyboardInterrupt:
            return
        if k in ("\x1b", "\x03", "\x07"):           # Esc, Ctrl-C, Ctrl-G
            return
        if k in ("\n", "\r", curses.KEY_ENTER):
            if rows:
                jump(client, rows[sel][0]["id"])
            return
        if k in (curses.KEY_UP, "\x10"):
            sel -= 1
        elif k in (curses.KEY_DOWN, "\x0e"):
            sel += 1
        elif k == curses.KEY_PPAGE:
            sel -= listh
        elif k == curses.KEY_NPAGE:
            sel += listh
        elif k in (curses.KEY_HOME,):
            sel = 0
        elif k in (curses.KEY_END,):
            sel = len(rows) - 1
        elif k == "\t":
            history = not history
            sel = top = 0
        elif k in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
            query, sel, top = query[:-1], 0, 0
        elif k == "\x15":                              # Ctrl-U
            query, sel, top = "", 0, 0
        elif k == "\x17":                              # Ctrl-W
            query, sel, top = query.rstrip().rpartition(" ")[0], 0, 0
        elif k == curses.KEY_RESIZE:
            pass
        elif isinstance(k, str) and k.isprintable():
            query, sel, top = query + k, 0, 0


def main():
    a = sys.argv[1:]
    if a and a[0] == "--list":
        now = time.time()
        for w, t, text in results(collect(), " ".join(a[1:])):
            print("\t".join([w["id"], w["where"], w["state"], age(t, now), w["name"], text,
                             w.get("note", "")]))
        return
    client = a[0] if a else ""
    os.environ.setdefault("ESCDELAY", "25")
    wins = collect()
    import curses
    try:
        curses.wrapper(ui, client, wins)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
