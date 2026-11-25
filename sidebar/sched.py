#!/usr/bin/env python3
"""Scheduled Claude Code tasks for tmux-claude-sidebar.

    sched.py daemon              start the scheduler for this tmux server
    sched.py stop                stop it
    sched.py tick                one pass (the daemon does this every 15s)
    sched.py ui [--client C] [--dir D] [--add] [--task ID]   the Ctrl-b T popup
    sched.py add --name N --prompt P --when W [--dir D] [--reuse auto|new]
                 [--model M] [--permission MODE]
    sched.py run ID              run a task now
    sched.py pause|resume|delete ID
    sched.py list                tasks as JSON
    sched.py parse WHEN          what a schedule means, and its next runs

A task runs a prompt (or a skill, like /wrap-up) on a schedule such as
"weekdays 5:30pm", "every 2h 9am-6pm", "mon,thu 9:00" or a cron line. Each
run goes to the task's previous run window if that is free, else to an open
Claude window that Haiku judges to have the right context, else to a new
window. Windows that are working, need you, are unread or need a follow-up
are never used, nor the window you are looking at, nor one whose input box
has text in it.

Tasks live in ~/.tmux/claude-sidebar-schedules.json. What has run is kept
per tmux server. Runs missed while the computer slept run once if they are
less than two hours late, otherwise they are skipped and shown as missed.
Python 3.7+, standard library only.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timedelta

HOME = os.path.expanduser("~")
TASKS_FILE = os.path.join(HOME, ".tmux", "claude-sidebar-schedules.json")
GRACE = 2 * 3600            # how late a missed run may still start
TICK = 15                   # seconds between daemon passes
BUSY = ("working", "needs")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_ALIASES = {d: i for i, d in enumerate(DAYS)}
DAY_ALIASES.update({"monday": 0, "tuesday": 1, "tues": 1, "wednesday": 2, "thursday": 3,
                    "thur": 3, "thurs": 3, "friday": 4, "saturday": 5, "sunday": 6})
HAIKU_ROUTE = (
    "You route a scheduled task to a terminal running Claude Code. You get the "
    "task and the sessions that are free. Reuse a session only when its recent "
    "work is clearly the context this task needs: the same project and topic, "
    "for example checking on a deploy that session did, or continuing its "
    "work. Otherwise start a new one. Reply with exactly one line: "
    "\"REUSE <window id>\" or \"NEW\".")
HAIKU_WHEN = (
    "Convert the schedule the user describes into one standard 5-field cron "
    "line (minute hour day-of-month month day-of-week, local time, 0=Sunday). "
    "Reply with only the cron line, or NONE if it is not a schedule.")


# ---- tmux ------------------------------------------------------------------

def tmux(*args, check=False):
    r = subprocess.run(["tmux", "-u"] + list(args), stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    if check and r.returncode:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip())
    # tmux 3.4 prints "$" as "\$".
    return r.stdout.decode("utf-8", "replace").rstrip("\n").replace("\\$", "$")


def tmux_ok(*args):
    return subprocess.run(["tmux"] + list(args), stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def server_key():
    sock = tmux("display", "-p", "#{socket_path}")
    if not sock:
        return None
    return hashlib.sha1(sock.encode()).hexdigest()[:10]


def paths(key):
    base = os.path.join(HOME, ".tmux", "claude-sidebar-sched-%s" % key)
    return {"state": base + ".json", "summary": base + ".txt", "lock": base + ".lock"}


def wopt(win, name):
    return tmux("display", "-p", "-t", win, "#{%s}" % name)


def window_exists(win):
    return bool(win) and tmux_ok("display", "-p", "-t", win, "")


# ---- schedules -----------------------------------------------------------

class Schedule:
    """When a task runs, to the minute, in local time.

    Either a list of times of day on some weekdays, or a cron line.
    """

    def __init__(self, text):
        self.text = " ".join(text.lower().split())
        self.cron = None
        self.days = set(range(7))
        self.minutes = set()
        self.desc = ""
        if not self.text:
            raise ValueError("empty schedule")
        if re.fullmatch(r"(\S+\s+){4}\S+", self.text) and \
                re.fullmatch(r"[0-9*/,\- ]+", self.text):
            self.cron = parse_cron(self.text)
            self.desc = "cron " + self.text
            return
        self._parse_words(self.text)

    def _parse_words(self, t):
        t = t.replace(" and ", ",").replace(" at ", " ").replace(" on ", " ")
        t = re.sub(r"^(at|on)\s+", "", t)
        t = re.sub(r"\s*,\s*", ",", t)
        t = re.sub(r"\s*-\s*", "-", t)
        days_desc = "daily"
        # Days.
        m = re.search(r"\b(every\s*)?(weekdays?|weekends?|daily|every\s*day|everyday)\b", t)
        if m:
            word = m.group(2)
            if word.startswith("weekday"):
                self.days, days_desc = {0, 1, 2, 3, 4}, "weekdays"
            elif word.startswith("weekend"):
                self.days, days_desc = {5, 6}, "weekends"
            t = (t[:m.start()] + " " + t[m.end():]).strip()
        else:
            dm = re.search(r"\b(?:every\s+)?((?:%s)(?:[,-](?:%s))*)\b" % (
                "|".join(sorted(DAY_ALIASES, key=len, reverse=True)),
                "|".join(sorted(DAY_ALIASES, key=len, reverse=True))), t)
            if dm:
                self.days = parse_days(dm.group(1))
                days_desc = ",".join(DAYS[d] for d in sorted(self.days))
                t = (t[:dm.start()] + " " + t[dm.end():]).strip()
        t = re.sub(r"^every\s+", "every ", t.strip())
        # Intervals: "every 30m", "every 2 hours 9am-6pm", "hourly".
        im = re.search(r"\b(?:every\s+(\d+)?\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)|hourly)\b", t)
        if im:
            n = int(im.group(1) or 1)
            unit = im.group(2) or "h"
            step = n * (60 if unit.startswith("h") else 1)
            if step < 5:
                raise ValueError("runs at most every 5 minutes")
            rest = (t[:im.start()] + " " + t[im.end():]).strip()
            rest = re.sub(r"\b(from|between)\s+", "", rest).replace(" to ", "-")
            start, end = 0, 23 * 60 + 59
            if rest:
                rm = re.fullmatch(r"(\S+)-(\S+)", rest.replace(" ", ""))
                if not rm:
                    raise ValueError("don't understand %r" % rest)
                start, end = parse_time(rm.group(1)), parse_time(rm.group(2))
                if end <= start:
                    raise ValueError("the time range must end after it starts")
            self.minutes = set(range(start, end + 1, step))
            every = ("%dm" % step) if step < 60 or step % 60 else ("%dh" % (step // 60))
            rng = "" if not rest else " %s-%s" % (hhmm(start), hhmm(end))
            self.desc = ("every %s%s" % (every, rng)) + ("" if days_desc == "daily" else " " + days_desc)
            return
        # Times of day: "17:30", "5:30pm", "9am,1pm", "noon".
        t = t.replace("every ", "").strip()
        if not t:
            raise ValueError("needs a time, like 9:00 or 5:30pm")
        times = [parse_time(x) for x in t.replace(" ", ",").split(",") if x]
        self.minutes = set(times)
        self.desc = "%s %s" % (days_desc, ",".join(hhmm(x) for x in sorted(times)))

    def day_ok(self, dt):
        if self.cron:
            _, _, dom, mon, dow = self.cron
            return dt.month in mon and cron_day_ok(dom, dow, dt.day, (dt.weekday() + 1) % 7)
        return dt.weekday() in self.days

    def hour_ok(self, dt):
        if self.cron:
            return dt.hour in self.cron[1]
        return any(dt.hour * 60 <= m < dt.hour * 60 + 60 for m in self.minutes)

    def matches(self, dt):
        if not self.day_ok(dt):
            return False
        if self.cron:
            return dt.hour in self.cron[1] and dt.minute in self.cron[0]
        return dt.hour * 60 + dt.minute in self.minutes

    def _search(self, dt, step, limit_days):
        """Walk minute by minute from dt (forward if step > 0), skipping whole
        days and hours that cannot match."""
        end = dt + timedelta(days=limit_days) * step
        while (dt <= end) if step > 0 else (dt >= end):
            if not self.day_ok(dt):
                dt = (dt.replace(hour=0, minute=0) + timedelta(days=1)) if step > 0 else \
                    (dt.replace(hour=23, minute=59) - timedelta(days=1))
            elif not self.hour_ok(dt):
                dt = (dt.replace(minute=0) + timedelta(hours=1)) if step > 0 else \
                    (dt.replace(minute=59) - timedelta(hours=1))
            elif self.matches(dt):
                return int(dt.timestamp())
            else:
                dt += timedelta(minutes=step)
        return None

    def prev(self, now):
        """The latest run time at or before `now` (within 8 days), or None."""
        dt = datetime.fromtimestamp(now).replace(second=0, microsecond=0)
        return self._search(dt, -1, 8)

    def next(self, now):
        """The first run time after `now` (within 400 days), or None."""
        dt = datetime.fromtimestamp(now).replace(second=0, microsecond=0) + timedelta(minutes=1)
        return self._search(dt, 1, 400)


class CronField(set):
    def __init__(self, values, restricted):
        super().__init__(values)
        self.restricted = restricted


def parse_cron(text):
    f = text.split()
    spans = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    out = []
    for field, (lo, hi) in zip(f, spans):
        vals = set()
        for part in field.split(","):
            step = 1
            if "/" in part:
                part, s = part.split("/", 1)
                step = int(s)
                if step < 1:
                    raise ValueError("bad step in %r" % field)
            if part == "*":
                a, b = lo, hi
            elif "-" in part:
                a, b = (int(x) for x in part.split("-", 1))
            else:
                a = b = int(part)
            if a < lo or b > hi or a > b:
                raise ValueError("%r is out of range" % field)
            vals.update(range(a, b + 1, step))
        out.append(CronField(vals, field != "*"))
    dow = out[4]
    if 7 in dow:
        dow.add(0)
    if not out[0] or not out[1]:
        raise ValueError("bad cron line")
    if len(out[0]) * len(out[1]) * (len(out[4]) if out[4].restricted else 7) > 24 * 60 * 7 / 5:
        raise ValueError("runs at most every 5 minutes")
    return out


def cron_day_ok(dom, dow, day, wd):
    # Standard cron: if both day fields are restricted, either may match.
    if dom.restricted and dow.restricted:
        return day in dom or wd in dow
    if dom.restricted:
        return day in dom
    if dow.restricted:
        return wd in dow
    return True


def parse_days(text):
    days = set()
    for part in text.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = DAY_ALIASES[a], DAY_ALIASES[b]
            d = a
            while True:
                days.add(d)
                if d == b:
                    break
                d = (d + 1) % 7
        else:
            days.add(DAY_ALIASES[part])
    return days


def parse_time(s):
    """Minutes after midnight for "17:30", "5:30pm", "5pm", "9", "noon"."""
    s = s.strip().lower().replace(".", ":")
    if s in ("noon", "midday"):
        return 12 * 60
    if s == "midnight":
        return 0
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)?", s)
    if not m:
        raise ValueError("don't understand the time %r" % s)
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap:
        if not 1 <= h <= 12:
            raise ValueError("bad time %r" % s)
        h = h % 12 + (12 if ap.startswith("p") else 0)
    if h > 23 or mi > 59:
        raise ValueError("bad time %r" % s)
    return h * 60 + mi


def hhmm(minutes):
    return "%d:%02d" % (minutes // 60, minutes % 60)


def when_label(ts, now):
    """Compact "next run" label: "in 12m", "17:30", "Mon 9:00", "Dec 3"."""
    if ts is None:
        return "never"
    d = ts - now
    if d < 3600:
        return "in %dm" % max(1, (d + 59) // 60)
    a, b = datetime.fromtimestamp(ts), datetime.fromtimestamp(now)
    t = "%d:%02d" % (a.hour, a.minute)
    if a.date() == b.date():
        return t
    if (a.date() - b.date()).days < 7:
        return "%s %s" % (DAYS[a.weekday()].capitalize(), t)
    return a.strftime("%b %d").replace(" 0", " ")


def hhmm_label(ts):
    a = datetime.fromtimestamp(ts)
    return "%d:%02d" % (a.hour, a.minute)


# ---- files ------------------------------------------------------------------

class Locked:
    """An exclusive lock on a file, for read-modify-write of our JSON files."""

    def __init__(self, path):
        self.path = path + ".lk"

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def load_tasks():
    data = load(TASKS_FILE, {})
    tasks = data.get("tasks", []) if isinstance(data, dict) else []
    return [t for t in tasks if isinstance(t, dict) and t.get("id")]


def save_tasks(tasks):
    save(TASKS_FILE, {"tasks": tasks})


def new_id():
    return "t" + hashlib.sha1(("%f%d" % (time.time(), os.getpid())).encode()).hexdigest()[:7]


# ---- running a task -------------------------------------------------------

def focused_windows():
    """Windows someone is looking at: the current window of a focused client."""
    out = set()
    for ln in tmux("list-clients", "-F", "#{client_flags} #{window_id}").splitlines():
        flags, _, win = ln.rpartition(" ")
        if "focused" in flags:
            out.add(win)
    return out


SGR = re.compile(r"\x1b\[[0-9;:]*m")


def input_empty(pane):
    """Is Claude's input box empty? Its placeholder ("Try ...") is drawn dim or
    grey, so a styled-dim remainder counts as empty."""
    raw = tmux("capture-pane", "-p", "-e", "-t", pane)
    lines = [ln for ln in raw.splitlines() if "❯" in SGR.sub("", ln)]
    if not lines:
        return False
    line = lines[-1]
    after = line.split("❯", 1)[1]
    text = SGR.sub("", after).strip()
    if not text:
        return True
    codes = SGR.findall(after.split(text[0], 1)[0]) if text else []
    for c in codes:
        params = c[2:-1].replace(":", ";").split(";")
        if "2" in params or "90" in params or (len(params) >= 3 and params[0] == "38"):
            return True
    return False


def eligible(win, viewed):
    """Can a scheduled prompt be sent to this window right now?"""
    info = tmux("display", "-p", "-t", win,
                "#{@claude_state}|#{@claude_verdict}|#{@claude_pane}|#{@claude_pid}")
    if not info:
        return False
    state, verdict, pane, pid = (info.split("|") + ["", "", "", ""])[:4]
    if state not in ("idle", "done") or verdict == "followup":
        return False
    if win in viewed or not pane:
        return False
    if pid.isdigit():
        try:
            os.kill(int(pid), 0)
        except OSError:
            return False
    if not tmux_ok("display", "-p", "-t", pane, ""):
        return False
    return input_empty(pane)


def target_session():
    best, sid = -1, ""
    for ln in tmux("list-sessions", "-F", "#{session_last_attached} #{session_id}").splitlines():
        t, _, s = ln.partition(" ")
        t = int(t) if t.isdigit() else 0
        if t > best:
            best, sid = t, s
    return sid


def candidates(task, viewed):
    out = []
    fmt = "#{window_id}|#{session_name}:#{window_index}|#{pane_current_path}|#{window_name}"
    for ln in tmux("list-windows", "-a", "-F", fmt).splitlines():
        parts = ln.split("|", 3)
        if len(parts) < 4:
            continue
        wid, where, path, name = parts
        if not eligible(wid, viewed):
            continue
        cp = wopt(wid, "@claude_pane")
        out.append({"id": wid, "where": where, "name": name,
                    "dir": tmux("display", "-p", "-t", cp, "#{pane_current_path}") or path,
                    "prompt": wopt(wid, "@claude_prompt"), "note": wopt(wid, "@claude_note")})
    return out


def claude_exe():
    return shutil.which("claude")


def ask_haiku(system, msg, timeout=60):
    exe = claude_exe()
    if not exe:
        return ""
    env = dict(os.environ)
    for k in ("TMUX_PANE", "TMUX"):
        env.pop(k, None)
    try:
        r = subprocess.run(
            [exe, "-p", "--model", "haiku", "--no-session-persistence", "--tools", "",
             "--strict-mcp-config", "--disable-slash-commands",
             "--settings", '{"disableAllHooks": true}', "--system-prompt", system, msg],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, timeout=timeout, cwd=HOME)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.decode("utf-8", "replace").strip()


def choose_window(task, prev_win, viewed):
    """Where this run goes: a window id, or None for a new window."""
    if task.get("reuse") == "new":
        return None, "new window (set to always use one)"
    if prev_win and eligible(prev_win, viewed):
        return prev_win, "its previous run window"
    cands = [c for c in candidates(task, viewed) if c["id"] != prev_win]
    if not cands:
        return None, "new window (no free Claude window)"
    lines = ["Task: %s" % task.get("name", ""),
             "Prompt: %s" % task.get("prompt", ""),
             "Folder: %s" % (task.get("dir") or "(any)"), "", "Free sessions:"]
    for c in cands:
        lines.append("- %s  name: %s  folder: %s  last prompt: %s  outcome: %s" % (
            c["id"], c["name"], c["dir"], c["prompt"][:200] or "-", c["note"] or "-"))
    answer = ask_haiku(HAIKU_ROUTE, "\n".join(lines))
    m = re.search(r"\bREUSE\s+(@\d+)", answer)
    if m and any(c["id"] == m.group(1) for c in cands):
        return m.group(1), "Haiku: same context as %s" % m.group(1)
    return None, "new window (Haiku: no window has this context)" if answer else "new window"


def send_prompt(win, prompt):
    """Type a prompt into the Claude Code window and submit it."""
    pane = wopt(win, "@claude_pane")
    tmux("set", "-w", "-t", win, "@claude_sched_pending", "1")
    buf = "tcs-sched-%d" % os.getpid()
    subprocess.run(["tmux", "load-buffer", "-b", buf, "-"], input=prompt.encode())
    # -p: bracketed paste, so a multi-line prompt arrives as one message.
    tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", pane)
    time.sleep(0.3)
    tmux("send-keys", "-t", pane, "Enter")
    return pane


def new_window(task):
    sess = target_session()
    if not sess:
        raise RuntimeError("no tmux session")
    exe = claude_exe() or "claude"
    cmd = [exe]
    if task.get("model"):
        cmd += ["--model", task["model"]]
    if task.get("permission"):
        cmd += ["--permission-mode", task["permission"]]
    cmd.append(task.get("prompt", ""))
    d = os.path.expanduser(task.get("dir") or HOME)
    if not os.path.isdir(d):
        raise RuntimeError("folder %s does not exist" % d)
    win = tmux("new-window", "-d", "-P", "-F", "#{window_id}", "-t", sess + ":", "-c", d,
               " ".join(shlex.quote(c) for c in cmd), check=True)
    tmux("set", "-w", "-t", win, "@claude_sched_owned", "1")
    return win


def run_task(task, key):
    """Start one run. Returns (window, how) or raises RuntimeError."""
    p = paths(key)
    with Locked(p["state"]):
        state = load(p["state"], {})
        st = state.get(task["id"], {})
        prev = st.get("window")
    if prev and not window_exists(prev):
        prev = None
    if prev and wopt(prev, "@claude_state") in BUSY:
        raise RuntimeError("skipped: the last run is still %s" % wopt(prev, "@claude_state"))
    viewed = focused_windows()
    win, how = choose_window(task, prev, viewed)
    if not win and task.get("reuse") == "new" and prev and \
            wopt(prev, "@claude_sched_owned") == "1" and prev not in viewed:
        # A fresh session each run, in place of the last run's window once
        # that is finished and read (kept if it is unread or needs you).
        st = wopt(prev, "@claude_state")
        if st in ("done", "idle", "") and wopt(prev, "@claude_verdict") != "followup":
            tmux("kill-window", "-t", prev)
            how = "new window (replaced its last run)"
    if win:
        tmux("set", "-w", "-t", win, "@claude_sched", task["id"])
        send_prompt(win, task.get("prompt", ""))
    else:
        win = new_window(task)
        tmux("set", "-w", "-t", win, "@claude_sched", task["id"])
    now = int(time.time())
    tmux("set", "-w", "-t", win, "@claude_sched_run", str(now))
    with Locked(p["state"]):
        state = load(p["state"], {})
        st = state.setdefault(task["id"], {})
        st.update(window=win, last_run=now, how=how, note="", note_at=0)
        save(p["state"], state)
    return win, how


def set_note(key, tid, note):
    p = paths(key)
    with Locked(p["state"]):
        state = load(p["state"], {})
        st = state.setdefault(tid, {})
        st.update(note=note, note_at=int(time.time()))
        save(p["state"], state)


def tick(key, now=None):
    """One pass: start what is due, note what was missed, refresh the summary."""
    now = int(now or time.time())
    p = paths(key)
    tasks = load_tasks()
    for task in tasks:
        if task.get("paused"):
            continue
        try:
            sch = Schedule(task.get("when", ""))
        except ValueError:
            continue
        slot = sch.prev(now)
        if slot is None:
            continue
        with Locked(p["state"]):
            state = load(p["state"], {})
            st = state.setdefault(task["id"], {})
            if "last_slot" not in st:
                # New task: start from the next slot, not one in the past.
                st["last_slot"] = max(slot, int(task.get("created", 0)) // 60 * 60)
                save(p["state"], state)
                continue
            if st["last_slot"] >= slot:
                continue
            st["last_slot"] = slot
            save(p["state"], state)
        if now - slot > GRACE:
            set_note(key, task["id"], "missed %s" % hhmm_label(slot))
            continue
        try:
            run_task(task, key)
        except Exception as e:  # noqa: BLE001 - shown in the sidebar
            set_note(key, task["id"], str(e)[:60])
    cleanup(key, tasks, now)
    write_summary(key, tasks, now)


def cleanup(key, tasks, now):
    """Close run windows that are not a task's latest run, are wrapped up and
    seen, and have not changed for a day."""
    state = load(paths(key)["state"], {})
    latest = {st.get("window") for st in state.values() if isinstance(st, dict)}
    fmt = "#{window_id}|#{@claude_sched_owned}|#{@claude_state}|#{@claude_verdict}|#{@claude_updated}"
    for ln in tmux("list-windows", "-a", "-F", fmt).splitlines():
        wid, owned, st, verdict, upd = (ln.split("|") + [""] * 5)[:5]
        if owned != "1" or wid in latest:
            continue
        if st in ("done", "idle", "") and verdict != "followup" and upd.isdigit() and \
                now - int(upd) > 24 * 3600:
            tmux("kill-window", "-t", wid)


def write_summary(key, tasks=None, now=None):
    """One line per task for render.sh:
    id|paused|next label|window|last run|note|name"""
    now = int(now or time.time())
    tasks = load_tasks() if tasks is None else tasks
    p = paths(key)
    state = load(p["state"], {})
    lines = []
    for t in tasks:
        st = state.get(t["id"], {})
        try:
            nxt = when_label(Schedule(t.get("when", "")).next(now), now)
        except ValueError:
            nxt = "bad time"
        win = st.get("window", "")
        if win and not window_exists(win):
            win = ""
        note = st.get("note", "") if st.get("note_at", 0) >= st.get("last_run", 0) else ""
        clean = lambda s: str(s).replace("|", "/").replace("\n", " ")
        lines.append("|".join([t["id"], "1" if t.get("paused") else "", "paused" if t.get("paused") else nxt,
                               win, str(st.get("last_run", "")), clean(note), clean(t.get("name", ""))]))
    text = "\n".join(lines) + ("\n" if lines else "")
    old = None
    try:
        with open(p["summary"]) as f:
            old = f.read()
    except OSError:
        pass
    if old != text:
        tmp = p["summary"] + ".tmp"
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, p["summary"])
    if tmux("show", "-gqv", "@claude_sched_file") != p["summary"]:
        tmux("set", "-g", "@claude_sched_file", p["summary"])


# ---- daemon ------------------------------------------------------------------

def daemon(foreground=False):
    key = server_key()
    if not key:
        return
    p = paths(key)
    os.makedirs(os.path.dirname(p["lock"]), exist_ok=True)
    fd = os.open(p["lock"], os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return                                  # already running for this server
    if not foreground:
        # Detach from run-shell, so tmux is not left waiting on a job.
        if os.fork():
            os._exit(0)
        os.setsid()
        if os.fork():
            os._exit(0)
        devnull = os.open(os.devnull, os.O_RDWR)
        for i in (0, 1, 2):
            os.dup2(devnull, i)
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    write_summary(key)
    while True:
        if not tmux_ok("display", "-p", ""):
            return                              # the tmux server is gone
        try:
            tick(key)
        except Exception:                       # noqa: BLE001 - keep running
            pass
        t = tmux("show", "-gqv", "@claude_sched_tick")
        time.sleep(int(t) if t.isdigit() and int(t) > 0 else TICK)


def stop():
    key = server_key()
    if not key:
        return
    try:
        with open(paths(key)["lock"]) as f:
            pid = int(f.read().strip() or 0)
    except (OSError, ValueError):
        return
    if pid > 1:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


# ---- the popup ----------------------------------------------------------------

def cwidth(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def clip(text, width):
    out, used = [], 0
    for ch in text:
        w = cwidth(ch)
        if used + w > width:
            while out and used + 1 > width:
                used -= cwidth(out.pop())
            if width > 0:
                out.append("…")
            break
        out.append(ch)
        used += w
    return "".join(out)


STATE_LABEL = {"needs": "! needs you", "review": "◆ review", "working": "● working",
               "done": "✓ done", "idle": "○ idle", "followup": "↩ follow up",
               "todo": "↩ to do"}


def run_status(win):
    if not win or not window_exists(win):
        return "", ""
    st, verdict, note = (tmux("display", "-p", "-t", win,
                              "#{@claude_state}|#{@claude_verdict}|#{@claude_note}").split("|") + ["", "", ""])[:3]
    if verdict == "followup":
        st = {"review": "followup", "done": "todo"}.get(st, st)
    return st or "starting", note


def ago(ts, now):
    s = max(0, int(now - ts))
    if s < 60:
        return "just now"
    if s < 3600:
        return "%dm ago" % (s // 60)
    if s < 86400:
        return "%dh ago" % (s // 3600)
    return "%dd ago" % (s // 86400)


def ui(stdscr, args):
    import curses
    try:
        curses.curs_set(0)
        curses.use_default_colors()
    except curses.error:
        pass
    colors = {}
    for i, (k, fg) in enumerate((("needs", curses.COLOR_YELLOW), ("review", curses.COLOR_YELLOW),
                                 ("followup", curses.COLOR_MAGENTA), ("todo", curses.COLOR_MAGENTA),
                                 ("working", curses.COLOR_BLUE),
                                 ("done", curses.COLOR_GREEN), ("accent", 208 if curses.COLORS >= 256 else curses.COLOR_RED),
                                 ("cyan", curses.COLOR_CYAN), ("err", curses.COLOR_RED)), 1):
        curses.init_pair(i, fg, -1)
        colors[k] = curses.color_pair(i)
    dim = curses.A_DIM
    key = server_key()
    sel, msg = 0, ""
    tasks = load_tasks()
    if args.task:
        sel = next((i for i, t in enumerate(tasks) if t["id"] == args.task), 0)

    def draw():
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        def put(y, x, s, a=0):
            if 0 <= y < h and x < w - 1:
                try:
                    stdscr.addstr(y, x, clip(s, w - 1 - x), a)
                except curses.error:
                    pass
        now = time.time()
        state = load(paths(key)["state"], {}) if key else {}
        put(0, 1, "↻ Scheduled tasks", colors["cyan"] | curses.A_BOLD)
        put(0, 20, "run Claude Code prompts or skills on a schedule", dim)
        put(1, 1, "─" * (w - 3), dim)
        namew = max(12, min(28, w // 4))
        for i, t in enumerate(tasks[:max(0, h - 10)]):
            y = 2 + i
            base = curses.A_REVERSE if i == sel else 0
            if i == sel:
                put(y, 0, " " * (w - 1), base)
            st = state.get(t["id"], {})
            try:
                sch = Schedule(t.get("when", ""))
                desc, nxt = sch.desc, when_label(sch.next(now), now)
            except ValueError:
                desc, nxt = t.get("when", ""), "bad time"
            if t.get("paused"):
                nxt = "paused"
            put(y, 1, "↻", base | colors["cyan"])
            put(y, 3, clip(t.get("name", ""), namew), base | curses.A_BOLD)
            x = 4 + namew
            put(y, x, clip(desc, 22), base)
            x += 23
            put(y, x, clip("next " + nxt if nxt not in ("paused", "bad time") else nxt, 14), base | dim)
            x += 15
            status, note = run_status(st.get("window"))
            if status:
                put(y, x, STATE_LABEL.get(status, status), base | colors.get(status, 0))
                put(y, x + 12, "ran " + ago(st.get("last_run", now), now), base | dim)
            elif st.get("note"):
                put(y, x, st["note"], base | dim)
        if not tasks:
            put(3, 3, "No scheduled tasks yet. Press a to add one, for example:", dim)
            put(5, 5, "wrap up the day    weekdays 5:30pm    /wrap-up")
            put(6, 5, "PRs rebased?       every 2h 9am-6pm   check my open PRs are rebased on main")
            put(7, 5, "prod check         daily 9:30         check what I deployed yesterday works in prod")
        # Details of the chosen task.
        put(h - 6, 1, "─" * (w - 3), dim)
        if tasks:
            t = tasks[sel]
            st = state.get(t["id"], {})
            put(h - 5, 1, "prompt  ", dim)
            put(h - 5, 9, t.get("prompt", ""))
            put(h - 4, 1, "runs    ", dim)
            d = os.path.expanduser(t.get("dir") or "~")
            d = "~" + d[len(HOME):] if d == HOME or d.startswith(HOME + "/") else d
            put(h - 4, 9, "reuse: %s   model: %s   permissions: %s   in %s" % (
                "Haiku decides" if t.get("reuse", "auto") == "auto" else "always new",
                t.get("model") or "default", t.get("permission") or "default", d))
            last = st.get("how", "")
            _, note = run_status(st.get("window"))
            put(h - 3, 1, "last    ", dim)
            if st.get("last_run"):
                text = ago(st["last_run"], now) + (" (%s)" % last if last else "")
            else:
                text = st.get("note") or "never run"
            put(h - 3, 9, text + (("  ·  " + note) if note else ""))
        put(h - 2, 1, msg, colors["accent"] if msg else 0)
        put(h - 1, 1, "a add · e edit · r run now · p pause · x delete · Enter go to last run · Esc close", dim)
        stdscr.refresh()

    def ask(label, value=""):
        """A one-line text field at the bottom. None on Esc."""
        curses.curs_set(1)
        buf = list(value)
        while True:
            h, w = stdscr.getmaxyx()
            if msg:
                # What the last answer meant (e.g. how a schedule was read).
                stdscr.move(h - 3, 0)
                stdscr.clrtoeol()
                try:
                    stdscr.addstr(h - 3, 1, clip(msg, w - 3), colors["cyan"])
                except curses.error:
                    pass
            stdscr.move(h - 2, 0)
            stdscr.clrtoeol()
            txt = "".join(buf)
            shown = txt[-max(1, w - len(label) - 4):]
            try:
                stdscr.addstr(h - 2, 1, label, colors["accent"] | curses.A_BOLD)
                stdscr.addstr(h - 2, 2 + len(label), shown)
            except curses.error:
                pass
            stdscr.move(h - 2, min(w - 2, 2 + len(label) + sum(cwidth(c) for c in shown)))
            stdscr.refresh()
            try:
                k = stdscr.get_wch()
            except curses.error:
                continue
            if k in ("\x1b", "\x03", "\x07"):
                curses.curs_set(0)
                return None
            if k in ("\n", "\r", curses.KEY_ENTER):
                curses.curs_set(0)
                return "".join(buf).strip()
            if k in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
                if buf:
                    buf.pop()
            elif k == "\x15":
                buf = []
            elif isinstance(k, str) and k.isprintable():
                buf.append(k)

    def edit(task):
        """Fill in a task, field by field. Returns the task, or None."""
        nonlocal msg
        t = dict(task)
        prompt = ask("What should Claude do? (a prompt, or a skill like /wrap-up):", t.get("prompt", ""))
        if not prompt:
            return None
        t["prompt"] = prompt
        name = ask("Name:", t.get("name") or clip(prompt.lstrip("/"), 28).rstrip("…"))
        if name is None:
            return None
        t["name"] = name or prompt[:28]
        when = t.get("when", "")
        while True:
            when = ask("When? (weekdays 5:30pm, every 2h 9am-6pm, mon,thu 9:00, cron):", when)
            if when is None:
                return None
            try:
                sch = Schedule(when)
                msg = "Runs %s, next %s." % (sch.desc, when_label(sch.next(time.time()), time.time()))
            except ValueError as e:
                msg = "Asking Claude what %r means..." % when
                draw()
                cron = ask_haiku(HAIKU_WHEN, when, timeout=40).strip().strip("`")
                try:
                    sch = Schedule(cron)
                    msg = "Understood %r as %s, next %s." % (
                        when, sch.desc, when_label(sch.next(time.time()), time.time()))
                    when = cron
                except ValueError:
                    msg = "Can't read that (%s). Try e.g. 'weekdays 5:30pm'." % e
                    draw()
                    continue
            t["when"] = when
            break
        d = ask("Folder:", t.get("dir") or args.dir or "~")
        if d is None:
            return None
        t["dir"] = d or "~"
        cur = t.get("reuse", "auto")
        reuse = ask("Reuse an open Claude window when it fits? y = Haiku decides, n = always a new "
                    "window (Enter keeps %s):" % ("y" if cur == "auto" else "n"))
        if reuse is None:
            return None
        if reuse:
            t["reuse"] = "new" if reuse.lower().startswith("n") else "auto"
        else:
            t["reuse"] = cur
        model = ask("Model (blank = your default):", t.get("model", ""))
        if model is None:
            return None
        t["model"] = model
        perm = ask("Permission mode (blank = default; e.g. acceptEdits, plan):", t.get("permission", ""))
        if perm is None:
            return None
        t["permission"] = perm
        return t

    if args.add:
        draw()
        t = edit({"id": new_id(), "created": int(time.time())})
        if t:
            tasks.append(t)
            save_tasks(tasks)
            if key:
                write_summary(key)
            sel = len(tasks) - 1
            msg = "Added %r: %s" % (t["name"], Schedule(t["when"]).desc)
    while True:
        tasks = load_tasks()
        sel = max(0, min(sel, len(tasks) - 1))
        draw()
        try:
            k = stdscr.get_wch()
        except curses.error:
            continue
        except KeyboardInterrupt:
            return
        msg = ""
        if k in ("\x1b", "\x03", "\x07", "q"):
            return
        if k in (curses.KEY_UP, "k", "\x10"):
            sel -= 1
        elif k in (curses.KEY_DOWN, "j", "\x0e"):
            sel += 1
        elif k == "a":
            t = edit({"id": new_id(), "created": int(time.time())})
            if t:
                tasks.append(t)
                save_tasks(tasks)
                sel = len(tasks) - 1
                msg = "Added %r: %s" % (t["name"], Schedule(t["when"]).desc)
        elif not tasks:
            continue
        elif k == "e":
            t = edit(tasks[sel])
            if t:
                tasks[sel] = t
                save_tasks(tasks)
                msg = "Saved."
        elif k == "p":
            tasks[sel]["paused"] = not tasks[sel].get("paused")
            save_tasks(tasks)
            msg = "Paused." if tasks[sel]["paused"] else "Resumed."
        elif k == "x":
            if ask("Delete %r? (y/n):" % tasks[sel].get("name", "")) in ("y", "Y", "yes"):
                del tasks[sel]
                save_tasks(tasks)
                msg = "Deleted."
        elif k == "r" and key:
            msg = "Starting..."
            draw()
            try:
                win, how = run_task(tasks[sel], key)
                msg = "Started in %s (%s)." % (tmux("display", "-p", "-t", win, "#{window_index}"), how)
            except Exception as e:  # noqa: BLE001
                msg = str(e)
        elif k in ("\n", "\r", curses.KEY_ENTER) and key:
            win = load(paths(key)["state"], {}).get(tasks[sel]["id"], {}).get("window")
            if win and window_exists(win):
                cmd = ["switch-client", "-t", win]
                if args.client:
                    cmd[1:1] = ["-c", args.client]
                tmux(*cmd)
                return
            msg = "No run window yet. Press r to run it now."
        if key:
            write_summary(key)


# ---- command line ---------------------------------------------------------

def find(tid):
    tasks = load_tasks()
    for t in tasks:
        if t["id"] == tid or t.get("name") == tid:
            return tasks, t
    sys.exit("no task %r" % tid)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("daemon").add_argument("--foreground", action="store_true")
    sub.add_parser("stop")
    sub.add_parser("tick")
    sub.add_parser("list")
    p = sub.add_parser("ui")
    p.add_argument("--client", default="")
    p.add_argument("--dir", default="")
    p.add_argument("--add", action="store_true")
    p.add_argument("--task", default="")
    p = sub.add_parser("add")
    for f in ("name", "prompt", "when"):
        p.add_argument("--" + f, required=True)
    p.add_argument("--dir", default="~")
    p.add_argument("--reuse", choices=("auto", "new"), default="auto")
    p.add_argument("--model", default="")
    p.add_argument("--permission", default="")
    for c in ("run", "pause", "resume", "delete"):
        sub.add_parser(c).add_argument("id")
    sub.add_parser("parse").add_argument("when", nargs="+")
    a = ap.parse_args()
    if a.cmd == "daemon":
        daemon(a.foreground)
    elif a.cmd == "stop":
        stop()
    elif a.cmd == "tick":
        key = server_key()
        if key:
            tick(key)
    elif a.cmd == "list":
        print(json.dumps(load_tasks(), indent=2, ensure_ascii=False))
    elif a.cmd == "parse":
        try:
            sch = Schedule(" ".join(a.when))
        except ValueError as e:
            sys.exit("can't read that schedule: %s" % e)
        now, out = time.time(), []
        t = now
        for _ in range(3):
            t = sch.next(t)
            if t is None:
                break
            out.append(datetime.fromtimestamp(t).strftime("%a %Y-%m-%d %H:%M"))
        print(sch.desc)
        print("\n".join(out))
    elif a.cmd == "add":
        try:
            Schedule(a.when)                  # check it before saving
        except ValueError as e:
            sys.exit("can't read that schedule: %s" % e)
        tasks = load_tasks()
        t = {"id": new_id(), "created": int(time.time()), "name": a.name, "prompt": a.prompt,
             "when": a.when, "dir": a.dir, "reuse": a.reuse, "model": a.model,
             "permission": a.permission}
        tasks.append(t)
        save_tasks(tasks)
        key = server_key()
        if key:
            write_summary(key)
        print(t["id"])
    elif a.cmd in ("pause", "resume", "delete"):
        tasks, t = find(a.id)
        if a.cmd == "delete":
            tasks.remove(t)
        else:
            t["paused"] = a.cmd == "pause"
        save_tasks(tasks)
        key = server_key()
        if key:
            write_summary(key)
    elif a.cmd == "run":
        _, t = find(a.id)
        key = server_key()
        if not key:
            sys.exit("not inside tmux")
        try:
            win, how = run_task(t, key)
        except RuntimeError as e:
            set_note(key, t["id"], str(e)[:60])
            write_summary(key)
            sys.exit(str(e))
        write_summary(key)
        print(win, how)
    elif a.cmd == "ui":
        os.environ.setdefault("ESCDELAY", "25")
        import curses
        try:
            curses.wrapper(ui, a)
        except KeyboardInterrupt:
            pass
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
