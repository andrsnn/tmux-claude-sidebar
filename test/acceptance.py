#!/usr/bin/env python3
"""Acceptance checks for tmux-claude-sidebar.

Runs against a separate tmux server (-L sidebar-test), never your own.
A real tmux client is attached through a pty so keys, mouse clicks and
terminal resizes go through tmux exactly as they would from a terminal.

    python3 test/acceptance.py [--home DIR] [--claude]

--home   HOME the test server uses (default: $HOME). Install there first.
--claude also run the Claude Code checks (needs a logged-in `claude`;
         sends two short prompts with --model haiku).
"""
import argparse
import fcntl
import json
import re
import os
import pty
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time

SOCK = "sidebar-test"
ap = argparse.ArgumentParser()
ap.add_argument("--home", default=os.environ["HOME"])
ap.add_argument("--claude", action="store_true")
args = ap.parse_args()
HOME = os.path.abspath(args.home)
CONF = os.path.join(HOME, ".tmux.conf")
WIDTH_FILE = os.path.join(HOME, ".tmux", "claude-sidebar-width")
ENV = dict(os.environ, HOME=HOME, TERM="xterm-256color")
ENV.pop("TMUX", None)
ENV.pop("TMUX_PANE", None)
# Some hosted environments turn off the terminal title Claude Code sets.
ENV.pop("CLAUDE_CODE_DISABLE_TERMINAL_TITLE", None)

results = []
LABELS = {"working": "● working", "needs": "! needs you", "review": "◆ unread"}
QUIET = ("done", "idle")      # nothing needed: no label at all


def tmux(*a, check=True):
    r = subprocess.run(["tmux", "-L", SOCK, "-f", CONF, *a], env=ENV,
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError("tmux %s: %s" % (" ".join(a), r.stderr.strip()))
    return r.stdout.rstrip("\n")


def fmt(target, f):
    return tmux("display-message", "-p", "-t", target, f)


def wait(cond, timeout=6.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = cond()
            if v:
                return v
        except RuntimeError:
            pass
        time.sleep(step)
    return cond()


def check(name, ok, detail=""):
    # A run-shell job that prints anything drops its pane into view-mode,
    # which swallows keys. That is always a bug in our scripts.
    modes = tmux("list-panes", "-a", "-F", "#{?#{==:#{pane_mode},view-mode},#{pane_id},}", check=False)
    if modes.strip() and not getattr(check, "warned", False):
        check.warned = True
        results.append(("no pane stuck in view-mode (seen at: %s)" % name, False))
        print("FAIL a pane is in view-mode (run-shell printed output): %s" % modes.split())
    results.append((name, bool(ok)))
    if not isinstance(detail, str):
        detail = repr(detail)
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (" - " + detail) if detail and not ok else ""))


class Client:
    """A tmux client attached through a pty, like a terminal window."""

    def __init__(self, cols=160, rows=42):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execvpe("tmux", ["tmux", "-L", SOCK, "-f", CONF, "attach", "-t", "work"], ENV)
        self.resize(cols, rows)
        self.buf = b""
        self.alive = True
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        while self.alive:
            r, _, _ = select.select([self.fd], [], [], 0.2)
            if r:
                try:
                    d = os.read(self.fd, 65536)
                except OSError:
                    return
                if not d:
                    return
                self.buf = (self.buf + d)[-200000:]

    def resize(self, cols, rows):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        try:
            os.kill(self.pid, 28)  # SIGWINCH
        except ProcessLookupError:
            pass

    def send(self, data):
        os.write(self.fd, data if isinstance(data, bytes) else data.encode())
        time.sleep(0.15)

    def prefix(self, key):
        self.send(b"\x02")
        self.send(key)

    def click(self, x, y):
        """Left click at 0-based terminal cell (x, y), SGR mouse encoding."""
        self.send("\x1b[<0;%d;%dM" % (x + 1, y + 1))
        self.send("\x1b[<0;%d;%dm" % (x + 1, y + 1))

    def close(self):
        self.alive = False
        try:
            os.kill(self.pid, 9)
            os.waitpid(self.pid, 0)
        except Exception:
            pass


def sidebar_panes(target):
    out = tmux("list-panes", "-t", target, "-F", "#{?@sidebar,#{pane_id},}")
    return [p for p in out.split() if p]


def all_sidebars():
    out = tmux("list-panes", "-a", "-F", "#{?@sidebar,#{pane_id},}")
    return [p for p in out.split() if p]


def windows():
    return tmux("list-windows", "-t", "work", "-F", "#{window_id}").split()


def current_window():
    return fmt("work", "#{window_id}")


def row_of(sb, text):
    """Pane-relative row in sidebar pane sb whose text contains `text`."""
    for i, line in enumerate(tmux("capture-pane", "-p", "-t", sb).splitlines()):
        if text in line:
            return i
    return None


def state_snapshot():
    return "\n".join([
        tmux("show-options", "-g"), tmux("show-options", "-gw"), tmux("show-options", "-s"),
        tmux("show-hooks", "-g"), tmux("list-keys"),
        tmux("list-panes", "-a", "-F", "#{window_id} #{pane_id} #{pane_width} #{@sidebar}"),
    ])


def main():
    import faulthandler
    faulthandler.dump_traceback_later(1500, exit=True)  # a hang prints where it is
    if not os.path.exists(os.path.join(HOME, ".tmux", "sidebar", "sidebar.sh")):
        sys.exit("not installed under %s (run install.sh first)" % HOME)
    tmux("kill-server", check=False)
    work = tempfile.mkdtemp(prefix="tcs-")
    codez = os.path.join(work, "codez-churn")
    os.mkdir(codez)
    tmux("new-session", "-d", "-s", "work", "-x", "160", "-y", "42", "-c", codez)
    c = Client()
    try:
        run(c, work, codez)
    finally:
        c.close()
        tmux("kill-server", check=False)
        shutil.rmtree(work, ignore_errors=True)
    failed = [n for n, ok in results if not ok]
    print("\n%d/%d passed" % (len(results) - len(failed), len(results)))
    sys.exit(1 if failed else 0)


def run(c, work, codez):
    w1 = wait(lambda: sidebar_panes("work:1") and "work:1")
    check("first window gets a sidebar", w1)
    sb1 = sidebar_panes("work:1")[0]
    W = int(wait(lambda: tmux("show-options", "-gqv", "@claude_sidebar_width")))
    check("sidebar has the configured width (%d)" % W,
          wait(lambda: fmt(sb1, "#{pane_width}") == str(W), 4), fmt(sb1, "#{pane_width}"))
    if not os.path.exists(WIDTH_FILE):
        check("default width is 40", W == 40, str(W))
    check("sidebar is not the active pane", fmt("work:1", "#{@sidebar}") != "1")
    check("window named from directory, codez- stripped",
          wait(lambda: fmt("work:1", "#{window_name}") == "churn"),
          fmt("work:1", "#{window_name}"))

    # 1. prefix c: new window with a sidebar; typing goes to the main pane.
    c.prefix("c")
    wait(lambda: len(windows()) == 2)
    w2 = current_window()
    check("1. prefix c opens a window with a sidebar", wait(lambda: sidebar_panes(w2)))
    c.send("echo typed-$((40+2))\r")
    main2 = fmt(w2, "#{pane_id}")
    check("1. typing goes to the main pane",
          wait(lambda: "typed-42" in tmux("capture-pane", "-p", "-t", main2))
          and fmt(w2, "#{@sidebar}") != "1")

    # Window naming from the pane title.
    def title(t):
        # The way Claude Code sets it: an OSC 2 escape written by the pane.
        # (select-pane -T alone does not trigger tmux's rename check.)
        tmux("send-keys", "-t", main2, "printf '\\033]2;%s\\007'; clear" % t, "Enter")
    title("✳ Fix churn tests")
    check("2b. spinner glyph stripped from title",
          wait(lambda: fmt(w2, "#{window_name}") == "Fix churn tests"), fmt(w2, "#{window_name}"))
    title("✻ Claude Code")
    check("2b. generic 'Claude Code' falls back to directory",
          wait(lambda: fmt(w2, "#{window_name}") not in ("Claude Code", "Fix churn tests")),
          fmt(w2, "#{window_name}"))
    title("⠂ Fix churn tests")
    wait(lambda: fmt(w2, "#{window_name}") == "Fix churn tests")
    sbn = sidebar_panes(w2)[0]
    check("2b. sidebar row shows window_name",
          wait(lambda: row_of(sbn, "Fix churn") is not None))

    # Selecting the sidebar bounces focus back to the main pane.
    tmux("select-pane", "-t", sbn)
    check("focus guard: selecting sidebar returns to main",
          wait(lambda: fmt(w2, "#{@sidebar}") != "1"))

    # 2. Exiting the shell closes the window.
    c.send("exit\r")
    check("2. exiting the main shell closes the window",
          wait(lambda: w2 not in windows()), str(windows()))

    # 3. prefix S toggles in all windows; new windows follow.
    c.prefix("c")
    wait(lambda: len(windows()) == 2)
    wait(lambda: all(sidebar_panes(w) for w in windows()))
    c.prefix("S")
    check("3. prefix S hides sidebars in all windows", wait(lambda: not all_sidebars()))
    c.prefix("c")
    wait(lambda: len(windows()) == 3)
    time.sleep(1)
    check("3. new window follows hidden state", not sidebar_panes(current_window()))
    c.prefix("S")
    check("3. prefix S shows them again in every window",
          wait(lambda: all(sidebar_panes(w) for w in windows())))
    check("3. after re-show focus stays on main panes",
          all(fmt(w, "#{@sidebar}") != "1" for w in windows()))

    # 4. Mouse. The sidebar is the left W columns starting at terminal row 0.
    sb = sidebar_panes(current_window())[0]
    wait(lambda: row_of(sb, "close window") is not None)
    first = windows()[0]
    y = row_of(sb, " 1 ")
    c.click(3, y)
    check("4. clicking a window row switches windows",
          wait(lambda: current_window() == first), current_window())
    check("4. click does not focus the sidebar", fmt("work", "#{@sidebar}") != "1")
    sb = sidebar_panes(first)[0]
    wait(lambda: row_of(sb, "new window") is not None)
    n = len(windows())
    c.click(3, row_of(sb, "new window"))
    check("4. the c row opens a window", wait(lambda: len(windows()) == n + 1))
    cur = current_window()
    sb = wait(lambda: sidebar_panes(cur) and sidebar_panes(cur)[0])
    wait(lambda: row_of(sb, "close window") is not None)
    c.buf = b""
    c.click(3, row_of(sb, "close window"))
    check("4. the & row shows the kill-window confirm",
          wait(lambda: b"kill-window" in c.buf), c.buf[-300:])
    c.send("n")
    check("4. answering n keeps the window", cur in windows())

    # × on a window row: confirm, then close that window.
    def close_via_x(win, answer):
        sbp = sidebar_panes(current_window())[0]
        name = fmt(win, "#{window_name}")[:6]
        y = wait(lambda: next((i for i, l in enumerate(
            tmux("capture-pane", "-p", "-t", sbp).splitlines())
            if l.rstrip().endswith("×") and name in l and
            ("%2s" % fmt(win, "#{window_index}")) in l), None))
        c.buf = b""
        c.click(W - 2, y)  # last drawn column of the sidebar
        shown = wait(lambda: b"stop everything running" in c.buf, 4)
        c.send(answer)
        return shown
    victim = tmux("new-window", "-d", "-P", "-F", "#{window_id}", "-t", "work:")
    wait(lambda: sidebar_panes(victim))
    check("x: clicking × asks before closing", close_via_x(victim, "n"))
    check("x: answering n keeps the window", victim in windows())
    close_via_x(victim, "y")
    check("x: answering y closes that window", wait(lambda: victim not in windows()))
    check("x: other windows untouched", cur in windows())
    # Clicking a main pane still does tmux's default (select that pane).
    main = fmt(cur, "#{pane_id}")
    tmux("split-window", "-h", "-t", main)
    other = fmt(cur, "#{pane_id}")
    c.click(W + 4, 5)  # just right of the sidebar
    check("5. clicks outside the sidebar keep default behaviour",
          wait(lambda: fmt(cur, "#{pane_id}") == main), fmt(cur, "#{pane_id}"))
    tmux("kill-pane", "-t", other)

    # Status from hooks (the same commands install writes to settings.json).
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ec", os.path.join(os.path.dirname(__file__), "..", "lib", "edit_configs.py"))
    ec = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ec)
    ws = windows()
    bg = ws[0]
    bg_pane = tmux("list-panes", "-t", bg, "-F", "#{?@sidebar,,#{pane_id}}").split()[0]
    sb = sidebar_panes(current_window())[0]
    e = dict(ENV, TMUX_PANE=bg_pane, TMUX=fmt(bg_pane, "#{socket_path},#{pid},0"))

    def hook(action):
        return subprocess.run(["sh", "-c", ec.hook_command(action)], env=e, input="{}",
                              text=True, capture_output=True).returncode

    # The window is in the background, so Stop gives "review", not "done".
    for ev, m, action, state in ec.STATES:
        rc = hook(action)
        check("hook %s%s -> %r" % (ev, " (%s)" % m if m else "", state),
              rc == 0 and wait(lambda: fmt(bg, "#{@claude_state}") == state, 2.5),
              fmt(bg, "#{@claude_state}"))
    for state, label in LABELS.items():
        tmux("set", "-w", "-t", bg, "@claude_state", state)
        check("sidebar shows %r" % label, wait(lambda: row_of(sb, label) is not None, 2.5))
    bgn = fmt(bg, "#{window_name}")[:8]
    for state in QUIET:
        tmux("set", "-w", "-t", bg, "@claude_state", state)
        check("sidebar shows no label for %r (nothing needed)" % state,
              wait(lambda: not any(x in (tmux("capture-pane", "-p", "-t", sb).splitlines()[row_of(sb, bgn)])
                                   for x in ("◆", "●", "!", "↩", "✓", "○")), 2.5))
    tmux("set", "-uw", "-t", bg, "@claude_state")
    bg_name = fmt(bg, "#{window_name}")[:8]
    check("plain shell window shows no status",
          wait(lambda: not any(l in (tmux("capture-pane", "-p", "-t", sb).splitlines()[row_of(sb, bg_name)])
                               for l in LABELS.values()), 2.5))
    r = subprocess.run(["sh", "-c", ec.hook_command("working")],
                       env={k: v for k, v in ENV.items() if k != "TMUX_PANE"}, capture_output=True)
    check("hooks are no-ops outside tmux and exit 0", r.returncode == 0)

    # 6. Bell in a background window -> "!" flag, styled yellow by catppuccin.
    tmux("send-keys", "-t", bg_pane, "printf '\\a'", "Enter")
    check("6. bell in background window sets the ! flag",
          wait(lambda: "!" in fmt(bg, "#{window_flags}")), fmt(bg, "#{window_flags}"))
    # Expand the tab format the way tmux draws it, for the bell window and for
    # a window without the bell.
    drawn = lambda w: fmt(w, "#{E:window-status-format}").lower()
    check("6. bell window's tab is drawn yellow (catppuccin #f9e2af)",
          "bg=#f9e2af" in drawn(bg), drawn(bg))
    quiet = [w for w in windows() if w != bg and "!" not in fmt(w, "#{window_flags}")][0]
    check("6. other tabs are not yellow", "#f9e2af" not in drawn(quiet), drawn(quiet))

    # 8. Resize the terminal; sidebars stay 30 wide.
    c.resize(120, 35)
    time.sleep(1.5)
    c.resize(200, 50)
    check("8. after resizing, every sidebar keeps its width",
          wait(lambda: all(fmt(p, "#{pane_width}") == str(W) for p in all_sidebars())),
          str([fmt(p, "#{pane_width}") for p in all_sidebars()]))

    # 9. source-file twice changes nothing.
    tmux("source-file", CONF)
    time.sleep(1.5)
    a = state_snapshot()
    tmux("source-file", CONF)
    time.sleep(1.5)
    b = state_snapshot()
    if a != b:
        import difflib
        print("\n".join(list(difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm=""))[:20]))
    check("9. sourcing the config twice changes nothing", a == b)
    check("9. no duplicate sidebars after reloads",
          all(len(sidebar_panes(w)) == 1 for w in windows()))

    modifier_drag_checks(c, W)
    width_checks(c, W)
    attention_checks(c, W, ec)
    keys_checks(c, W)
    menu_checks(c, W)
    search_checks(c, W)
    judge_checks(c, W)
    sched_checks(c, W)

    if args.claude:
        claude_checks(c, codez, close_via_x)


def modifier_drag_checks(c, W):
    """Ctrl/Option+drag must not open a floating shell or move panes (tmux 3.8 defaults)."""
    win = tmux("display-message", "-p", "#{window_id}")
    layout = lambda: tmux("list-panes", "-t", win, "-F",
                          "#{pane_id} #{pane_left} #{pane_floating_flag} #{@sidebar}")
    before = layout()
    for mod, name in ((16, "Ctrl"), (8, "Option")):
        for x0 in (W + 20, 5):  # main pane, then the sidebar
            c.send("\x1b[<%d;%d;10M" % (mod, x0))
            for dx in range(1, 16, 3):
                c.send("\x1b[<%d;%d;12M" % (mod + 32, x0 + dx))
            c.send("\x1b[<%d;%d;12m" % (mod, x0 + 16))
            tmux("send-keys", "-X", "-t", win, "cancel", check=False)
        time.sleep(0.5)
        check("drag: %s+drag opens no pane and moves none" % name, layout() == before,
              "before %r after %r" % (before, layout()))


def width_checks(c, W):
    """[−][+] buttons and border drags change every sidebar and persist."""
    saved = open(WIDTH_FILE).read() if os.path.exists(WIDTH_FILE) else None
    widths = lambda: {fmt(p, "#{pane_width}") for p in all_sidebars()}
    try:
        c.click(W - 3, 0)  # the + of [−][+] in the header row
        check("width: [+] widens every sidebar by 2", wait(lambda: widths() == {str(W + 2)}), str(widths()))
        check("width: saved for the next tmux start",
              open(WIDTH_FILE).read().strip() == str(W + 2) if os.path.exists(WIDTH_FILE) else False)
        c.click(W + 2 - 6, 0)  # the − (button positions follow the new width)
        check("width: [−] narrows it again", wait(lambda: widths() == {str(W)}), str(widths()))
        # Drag the border (terminal column W) six columns to the right.
        x = W + 1
        c.send("\x1b[<0;%d;10M" % x)
        for dx in range(1, 7):
            c.send("\x1b[<32;%d;10M" % (x + dx))
        c.send("\x1b[<0;%d;10m" % (x + 6))
        check("width: dragging the border sets the width for all sidebars",
              wait(lambda: widths() == {str(W + 6)}), str(widths()))
        c.prefix("c")
        cur = wait(lambda: sidebar_panes(current_window()) and current_window())
        check("width: new windows use the dragged width",
              wait(lambda: widths() == {str(W + 6)}), str(widths()))
    finally:
        # Put back the user's own width.
        if saved is None:
            os.path.exists(WIDTH_FILE) and os.remove(WIDTH_FILE)
            tmux("set-option", "-gu", "@claude_sidebar_width")
        else:
            with open(WIDTH_FILE, "w") as f:
                f.write(saved)
            tmux("set-option", "-g", "@claude_sidebar_width", saved.strip())
        subprocess.run([os.path.join(HOME, ".tmux", "sidebar", "sidebar.sh"), "resize"],
                       env=dict(ENV, TMUX=fmt("work", "#{socket_path},#{pid},0")))


def load_ec():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ec", os.path.join(os.path.dirname(__file__), "..", "lib", "edit_configs.py"))
    ec = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ec)
    return ec


def main_pane(w):
    return tmux("list-panes", "-t", w, "-F", "#{?@sidebar,,#{pane_id}}").split()[0]


def tmux_env(pane=None):
    e = dict(ENV, TMUX=fmt("work", "#{socket_path},#{pid},0"))
    if pane:
        e["TMUX_PANE"] = pane
    return e


def run_hook(pane, event, payload=None, pid=None):
    """Run a hook the way Claude Code does: the installed command, JSON on stdin.
    With pid, call hook.sh directly with that as Claude Code's pid."""
    if pid is None:
        cmd = load_ec().hook_command(event)
    else:
        cmd = '"$HOME/.tmux/sidebar/hook.sh" %s %d' % (event, pid)
    return subprocess.run(["sh", "-c", cmd], env=tmux_env(pane), input=json.dumps(payload or {}),
                          text=True, capture_output=True).returncode


def clear_states():
    for w in windows():
        for o in ("@claude_state", "@claude_pid", "@claude_updated", "@claude_prompt",
                  "@claude_prompt_at", "@claude_transcript", "@claude_pane", "@claude_hash",
                  "@claude_hash_at", "@claude_turn", "@claude_verdict", "@claude_note",
                  "@claude_sched", "@claude_sched_run", "@claude_sched_pending", "@claude_rang"):
            tmux("set", "-uw", "-t", w, o, check=False)


def sidebar_lines():
    sb = sidebar_panes(current_window())[0]
    return tmux("capture-pane", "-p", "-t", sb).splitlines()


def name_rows(names):
    """Row of each window name in the current sidebar (None if not shown)."""
    lines = sidebar_lines()
    return [next((i for i, l in enumerate(lines) if (" %s " % n) in l), None) for n in names]


def attention_checks(c, W, ec):
    """review / done, prompt lines, sort order, stale and stalled sessions."""
    clear_states()
    while len(windows()) < 5:
        tmux("new-window", "-d", "-t", "work:")
    ws = windows()
    wait(lambda: all(sidebar_panes(w) for w in ws))
    cur = ws[0]
    tmux("select-window", "-t", cur)
    names = ["alpha", "bravo", "charlie", "delta", "echo"]
    for w, n in zip(ws, names):
        tmux("rename-window", "-t", w, n)
    a, b = ws[1], ws[2]
    pa, pb, pc = main_pane(a), main_pane(b), main_pane(cur)
    an = names[1]

    # A finished background window: review (yellow) until you look at it.
    run_hook(pa, "prompt", {"prompt": "tidy   the README\nand fix links;"})
    check("review: prompt -> working", wait(lambda: fmt(a, "#{@claude_state}") == "working", 2.5),
          fmt(a, "#{@claude_state}"))
    check("review: prompt stored on one line", fmt(a, "#{@claude_prompt}") == "tidy the README and fix links; ",
          repr(fmt(a, "#{@claude_prompt}")))
    check("rows: one line per window, with how long ago (no prompt line)",
          wait(lambda: any(" %s " % an in l and "● working" in l and "<1m ×" in l for l in sidebar_lines()), 3)
          and not any("tidy the README" in l for l in sidebar_lines()), "\n".join(sidebar_lines()))
    run_hook(pa, "done")
    check("review: Stop in a background window -> review",
          wait(lambda: fmt(a, "#{@claude_state}") == "review", 2.5), fmt(a, "#{@claude_state}"))
    check("review: sidebar shows ◆ unread",
          wait(lambda: any(" %s " % an in l and "◆ unread" in l for l in sidebar_lines()), 3))
    drawn = lambda w: fmt(w, "#{E:window-status-format}").lower()
    check("review: its tab is drawn yellow", "bg=#f9e2af" in drawn(a), drawn(a))
    check("review: a quiet window's tab is not yellow", "#f9e2af" not in drawn(ws[3]), drawn(ws[3]))
    tmux("select-window", "-t", a)
    check("review: switching to it marks it done",
          wait(lambda: fmt(a, "#{@claude_state}") == "done", 3), fmt(a, "#{@claude_state}"))
    check("review: then it is quiet (no label, still the age)",
          wait(lambda: any(" %s " % an in l and "◆" not in l and "<1m ×" in l for l in sidebar_lines()), 3),
          "\n".join(sidebar_lines()))
    check("rows: the age column lines up with the ×",
          all(l.rstrip().endswith("×") for l in sidebar_lines() if " %s " % an in l))
    tmux("select-window", "-t", cur)

    # Finishing in the window you are looking at is done straight away.
    run_hook(pc, "prompt", {"prompt": "x"})
    run_hook(pc, "done")
    check("review: Stop in the window you are looking at -> done",
          wait(lambda: fmt(cur, "#{@claude_state}") == "done", 2.5), fmt(cur, "#{@claude_state}"))
    # ...unless the terminal has lost focus (you are in another app).
    c.send("\x1b[O")
    check("focus: tmux sees the terminal lose focus",
          wait(lambda: "focused" not in tmux("list-clients", "-F", "#{client_flags}"), 2),
          tmux("list-clients", "-F", "#{client_flags}"))
    run_hook(pc, "prompt", {"prompt": "y"})
    run_hook(pc, "done")
    check("focus: Stop while the terminal is in the background -> review",
          wait(lambda: fmt(cur, "#{@claude_state}") == "review", 2.5), fmt(cur, "#{@claude_state}"))
    time.sleep(1.5)
    check("focus: stays review while you are away", fmt(cur, "#{@claude_state}") == "review")
    c.send("\x1b[I")
    check("focus: coming back to the terminal marks it done",
          wait(lambda: fmt(cur, "#{@claude_state}") == "done", 3), fmt(cur, "#{@claude_state}"))

    # Order: needs you, then review, then the rest by most recently updated,
    # then windows without Claude Code.
    now = int(time.time())
    for w, st, t in ((ws[1], "done", now - 50), (ws[2], "review", now - 3000),
                     (ws[3], "needs", now - 9000), (ws[4], "idle", now - 10)):
        tmux("set", "-w", "-t", w, "@claude_state", st)
        tmux("set", "-w", "-t", w, "@claude_updated", str(t))
    tmux("set", "-uw", "-t", cur, "@claude_state")
    want = ["delta", "charlie", "echo", "bravo", "alpha"]
    ok = wait(lambda: name_rows(want) == sorted(name_rows(want)) and None not in name_rows(want), 3)
    check("sort: needs you, review, then most recent, then plain windows", ok,
          "\n".join(sidebar_lines()))
    sort_file = os.path.join(HOME, ".tmux", "claude-sidebar-sort")
    saved = open(sort_file).read() if os.path.exists(sort_file) else None
    c.click(3, 1)  # "sort: ..." row
    by_index = ["alpha", "bravo", "charlie", "delta", "echo"]
    check("sort: clicking 'sort' switches to window number order",
          wait(lambda: None not in name_rows(by_index) and name_rows(by_index) == sorted(name_rows(by_index)), 3),
          "\n".join(sidebar_lines()))
    check("sort: the choice is saved", os.path.exists(sort_file) and open(sort_file).read().strip() == "index")
    check("sort: the row says so", any("window number" in l for l in sidebar_lines()))
    c.click(3, 1)
    check("sort: clicking again goes back",
          wait(lambda: name_rows(want) == sorted(name_rows(want)), 3) and
          open(sort_file).read().strip() == "attention", "\n".join(sidebar_lines()))
    if saved is None:
        os.remove(sort_file)
    else:
        open(sort_file, "w").write(saved)
    # The row number on screen is still the tmux window index.
    lines = sidebar_lines()
    check("sort: rows keep their window numbers",
          " 4 delta" in lines[name_rows(["delta"])[0]], lines[name_rows(["delta"])[0]])
    y = name_rows(["charlie"])[0]
    c.click(8, y)
    check("sort: clicking a sorted row switches to that window",
          wait(lambda: current_window() == ws[2]), current_window())
    tmux("select-window", "-t", cur)
    clear_states()

    # Claude Code killed (no SessionEnd): its status goes away.
    victim = subprocess.Popen(["sleep", "300"])
    run_hook(pb, "prompt", {"prompt": "z"}, pid=victim.pid)
    check("stale: hook records Claude's pid", fmt(b, "#{@claude_pid}") == str(victim.pid))
    time.sleep(1.5)
    check("stale: kept while that process lives", fmt(b, "#{@claude_state}") == "working")
    victim.kill()
    victim.wait()
    check("stale: cleared once it is gone and the pane is back at a shell",
          wait(lambda: fmt(b, "#{@claude_state}") == "", 4), fmt(b, "#{@claude_state}"))

    # Esc runs no hook: a "working" window whose screen stops changing is idle.
    tmux("set", "-g", "@claude_sidebar_stall", "3")
    try:
        run_hook(pb, "prompt", {"prompt": "long job"})
        check("stall: starts working", fmt(b, "#{@claude_state}") == "working")
        tmux("send-keys", "-t", pb, "for i in 1 2 3 4 5 6; do echo tick $i; sleep 1; done", "Enter")
        time.sleep(4.5)
        check("stall: still working while the screen changes", fmt(b, "#{@claude_state}") == "working",
              fmt(b, "#{@claude_state}"))
        check("stall: idle once the screen has been still for a while",
              wait(lambda: fmt(b, "#{@claude_state}") == "idle", 10), fmt(b, "#{@claude_state}"))
    finally:
        tmux("set", "-gu", "@claude_sidebar_stall")
    clear_states()


def menu_checks(c, W):
    """Right-click menu on sidebar rows; tmux's own menu elsewhere."""
    clear_states()
    tmux("set", "-g", "@claude_sidebar_sort", "index")
    try:
        ws = windows()
        cur = current_window()
        target = [w for w in ws if w != cur][0]
        name = fmt(target, "#{window_name}")
        idx = int(fmt(target, "#{window_index}"))
        y = wait(lambda: name_rows([name])[0])

        def right_click(x, y):
            c.buf = b""
            c.send("\x1b[<2;%d;%dM" % (x + 1, y + 1))
            c.send("\x1b[<2;%d;%dm" % (x + 1, y + 1))
        right_click(5, y)
        check("menu: right-click on a row opens its menu",
              wait(lambda: b"Move down" in c.buf, 3), c.buf[-400:])
        check("menu: does not switch windows", current_window() == cur)
        c.send("d")
        check("menu: Move down swaps it with the next window",
              wait(lambda: fmt(target, "#{window_index}") == str(idx + 1), 3),
              fmt(target, "#{window_index}"))
        y = wait(lambda: name_rows([name])[0])
        right_click(5, y)
        wait(lambda: b"Move to the top" in c.buf, 3)
        c.send("t")
        base = int(tmux("show", "-gv", "base-index"))
        check("menu: Move to the top makes it the first window",
              wait(lambda: fmt(target, "#{window_index}") == str(base), 3), fmt(target, "#{window_index}"))
        check("menu: no windows lost", sorted(windows()) == sorted(ws))
        check("menu: current window unchanged", current_window() == cur)
        tmux("set", "-w", "-t", target, "@claude_state", "review")
        sbp = sidebar_panes(cur)[0]
        wait(lambda: "=review=" in tmux("show", "-pqv", "-t", sbp, "@sidebar_rows"), 3)
        y = wait(lambda: name_rows([name])[0])
        right_click(5, y)
        check("menu: offers 'Mark as reviewed' on a review window",
              wait(lambda: b"Mark as reviewed" in c.buf, 3), c.buf[-400:])
        c.send("r")
        check("menu: marking reviewed -> done",
              wait(lambda: fmt(target, "#{@claude_state}") == "done", 3), fmt(target, "#{@claude_state}"))
        # Outside the sidebar the right-click menu is tmux's own.
        right_click(W + 20, 8)
        check("menu: right-click in a main pane still shows tmux's pane menu",
              wait(lambda: b"Horizontal Split" in c.buf or b"Split" in c.buf, 3), c.buf[-400:])
        c.send("\x1b")
        time.sleep(0.3)
        check("menu: that menu is not the sidebar's", b"Move down" not in c.buf)
    finally:
        tmux("set", "-gu", "@claude_sidebar_sort")
        clear_states()


def search_checks(c, W):
    """prefix F / [search]: find a prompt in any window and jump to it."""
    clear_states()
    ws = windows()
    cur = current_window()
    target = [w for w in ws if w != cur][-1]
    pt = main_pane(target)
    tr = os.path.join(tempfile.mkdtemp(prefix="tcs-tr-"), "t.jsonl")
    with open(tr, "w") as f:
        for i, (kind, content) in enumerate([
                ("user", "look at the flamingo logs and summarise errors"),
                ("user", [{"type": "tool_result", "content": "flamingo tool output"}]),
                ("user", "<command-name>/clear</command-name>"),
                ("assistant", "flamingo answer from Claude"),
                ("user", [{"type": "text", "text": "now plan the zebra migration"}])]):
            f.write(json.dumps({"type": kind, "timestamp": "2026-01-0%dT10:00:00.000Z" % (i + 1),
                                "message": {"role": kind, "content": content}}) + "\n")
    run_hook(pt, "idle", {"transcript_path": tr})
    run_hook(pt, "prompt", {"transcript_path": tr, "prompt": "now plan the zebra migration"})
    check("search: transcript path recorded", fmt(target, "#{@claude_transcript}") == tr)
    py = os.path.join(HOME, ".tmux", "sidebar", "search.py")
    out = lambda *q: subprocess.run(["python3", "-I", py, "--list", *q], env=tmux_env(),
                                    capture_output=True, text=True).stdout
    hits = out("flamingo").splitlines()
    check("search: finds an older prompt from the transcript",
          len(hits) == 1 and hits[0].startswith(target + "\t") and "flamingo logs" in hits[0], hits)
    check("search: skips tool results, commands and Claude's replies", len(hits) == 1, hits)
    check("search: all words must match", out("zebra", "flamingo") == "" and out("zebra", "plan") != "")
    name = fmt(target, "#{window_name}")
    check("search: empty query lists each Claude window once",
          [l.split("\t")[0] for l in out().splitlines()] == [target], out())

    # The popup: type, Enter, and you are in that window.
    c.buf = b""
    c.prefix("F")
    check("search: prefix F opens the popup", wait(lambda: b"Search:" in c.buf, 4), c.buf[-300:])
    check("search: popup shows the window's last prompt", wait(lambda: b"zebra" in c.buf, 3))
    c.send("flamin")
    time.sleep(0.5)
    c.send("\r")
    check("search: Enter jumps to the matching window",
          wait(lambda: current_window() == target, 4), current_window())
    tmux("select-window", "-t", cur)
    # [search] in the sidebar opens it too; Esc closes it without moving.
    c.buf = b""
    c.click(W - 5, 1)
    check("search: [search] in the sidebar opens the popup", wait(lambda: b"Search:" in c.buf, 4),
          c.buf[-300:])
    c.send("\x1b")
    time.sleep(0.8)
    check("search: Esc closes it and stays put", current_window() == cur)
    c.send("echo after-popup\r")
    check("search: keys go to the pane again after the popup",
          wait(lambda: "after-popup" in tmux("capture-pane", "-p", "-t", main_pane(cur))), 3)
    clear_states()


def judge_checks(c, W):
    """After a turn: wrapped up, or come back to it (judge.py)."""
    clear_states()
    stub_dir = tempfile.mkdtemp(prefix="tcs-stub-")
    log = os.path.join(stub_dir, "calls")
    with open(os.path.join(stub_dir, "claude"), "w") as f:
        f.write('#!/bin/sh\n{ echo "TMUX_PANE=${TMUX_PANE:-unset}"; printf "%%s\\n" "$@"; } >> %s\n'
                'cat "%s/answer"\n' % (log, stub_dir))
    os.chmod(os.path.join(stub_dir, "claude"), 0o755)
    answer = lambda t: open(os.path.join(stub_dir, "answer"), "w").write(t)
    env_path = stub_dir + os.pathsep + ENV["PATH"]
    ws = windows()
    cur = current_window()
    t = [w for w in ws if w != cur][0]
    pt = main_pane(t)

    def turn(reply, prompt="fix the flaky login test"):
        e = dict(tmux_env(pt), PATH=env_path)
        for ev, payload in (("prompt", {"prompt": prompt}),
                            ("done", {"last_assistant_message": reply})):
            subprocess.run(["sh", "-c", load_ec().hook_command(ev)], env=e,
                           input=json.dumps(payload), text=True, capture_output=True)

    answer("FOLLOWUP: asks whether to open a PR\n")
    turn("Fixed the race and tests pass. Want me to open a PR?")
    check("judge: the system prompt treats optional offers as wrapped up",
          wait(lambda: os.path.exists(log), 3) and "optional offer is not a follow-up" in open(log).read())
    check("judge: Claude's verdict and reason are stored",
          wait(lambda: fmt(t, "#{@claude_note}") == "asks whether to open a PR", 5), fmt(t, "#{@claude_note}"))
    calls = open(log).read() if os.path.exists(log) else ""
    check("judge: asks haiku, with hooks, tools and session saving off",
          all(x in calls for x in ("--model\nhaiku", "--no-session-persistence",
                                   '{"disableAllHooks": true}', "--tools\n\n")), calls[:400])
    check("judge: the call is not tracked as a session (no TMUX_PANE)", "TMUX_PANE=unset" in calls)
    check("judge: the call sees the prompt and the reply",
          "fix the flaky login test" in calls and "Want me to open a PR?" in calls)
    tname = fmt(t, "#{window_name}")[:10]
    row = lambda: next((l for l in sidebar_lines() if tname in l and "×" in l), "")
    check("judge: unread and not wrapped up -> still just 'unread' (status only, no reason)",
          wait(lambda: fmt(t, "#{@claude_verdict}") == "followup" and "◆ unread" in row(), 5)
          and not any("asks whether to open" in l for l in sidebar_lines()), "\n".join(sidebar_lines()))
    check("judge: an unread follow-up turns its tab yellow",
          "bg=#f9e2af" in fmt(t, "#{E:window-status-format}").lower())
    check("judge: the menu still offers 'Mark as reviewed' for it",
          wait(lambda: "=review=followup=" in tmux("show", "-pqv", "-t", sidebar_panes(cur)[0], "@sidebar_rows"), 3),
          tmux("show", "-pqv", "-t", sidebar_panes(cur)[0], "@sidebar_rows"))
    tmux("select-window", "-t", t)
    check("judge: once you have looked, it is 'to do' (read, not done)",
          wait(lambda: "↩ to do" in row() and "unread" not in row(), 3), "\n".join(sidebar_lines()))
    tmux("select-window", "-t", cur)
    other = [w for w in ws if w not in (t, cur)][-1]
    tmux("set", "-w", "-t", other, "@claude_state", "done")
    tmux("set", "-w", "-t", other, "@claude_updated", str(int(time.time()) + 5))

    oname = fmt(other, "#{window_name}")[:10]

    def follow_first():
        lines = sidebar_lines()
        f = [i for i, l in enumerate(lines) if "↩ to do" in l]
        d = [i for i, l in enumerate(lines) if oname in l and "×" in l]
        return f and d and f[0] < d[0]
    check("judge: a 'to do' sorts above done windows, even newer ones",
          wait(follow_first, 3), "\n".join(sidebar_lines()))
    check("judge: the tab is not yellow once seen",
          "#f9e2af" not in fmt(t, "#{E:window-status-format}").lower())

    # Right-click > Mark as wrapped up.
    y = name_rows([fmt(t, "#{window_name}")])[0]
    c.buf = b""
    c.send("\x1b[<2;6;%dM" % (y + 1))
    c.send("\x1b[<2;6;%dm" % (y + 1))
    check("judge: menu offers 'Mark as wrapped up'", wait(lambda: b"Mark as wrapped up" in c.buf, 3))
    c.send("w")
    check("judge: marking it wrapped up -> quiet",
          wait(lambda: fmt(t, "#{@claude_verdict}") == "wrapped" and "↩" not in row(), 3),
          "\n".join(sidebar_lines()))

    answer("WRAPPED: tests pass, nothing pending\n")
    turn("All done: the flaky test is fixed and CI is green.")
    check("judge: a finished job is wrapped up",
          wait(lambda: fmt(t, "#{@claude_verdict}") == "wrapped", 5) and
          fmt(t, "#{@claude_note}") == "tests pass, nothing pending", fmt(t, "#{@claude_note}"))
    check("judge: search shows the reason",
          "tests pass, nothing pending" in subprocess.run(
              ["python3", "-I", os.path.join(HOME, ".tmux", "sidebar", "search.py"), "--list", "flaky"],
              env=tmux_env(), capture_output=True, text=True).stdout)

    # A new prompt clears the verdict; a late answer for the old turn is dropped.
    answer("FOLLOWUP: late answer\n")
    e = dict(tmux_env(pt), PATH=env_path)
    subprocess.run(["sh", "-c", load_ec().hook_command("done")], env=e,
                   input=json.dumps({"last_assistant_message": "ok"}), text=True)
    subprocess.run(["sh", "-c", load_ec().hook_command("prompt")], env=e,
                   input=json.dumps({"prompt": "next thing"}), text=True)
    time.sleep(2)
    check("judge: a new prompt clears the verdict, late answers are dropped",
          fmt(t, "#{@claude_verdict}") == "" and fmt(t, "#{@claude_state}") == "working",
          fmt(t, "#{@claude_verdict}|#{@claude_state}|#{@claude_note}|#{@claude_turn}|#{@claude_prompt}"))

    # Turned off: no call, no verdict (a trailing "?" alone is not a to-do).
    tmux("set", "-g", "@claude_sidebar_judge", "off")
    try:
        os.remove(log)
        turn("Which database should I use, Postgres or SQLite?")
        time.sleep(1.5)
        check("judge: off -> no verdict", fmt(t, "#{@claude_verdict}") == "", fmt(t, "#{@claude_verdict}"))
        check("judge: off -> Claude is not called", not os.path.exists(log))
    finally:
        tmux("set", "-gu", "@claude_sidebar_judge")
    # StopFailure: an error is always a follow-up.
    subprocess.run(["sh", "-c", load_ec().hook_command("failed")], env=e, input="{}", text=True)
    check("judge: a turn that failed is a follow-up",
          fmt(t, "#{@claude_verdict}") == "followup" and fmt(t, "#{@claude_note}") == "stopped with an error")
    clear_states()


def load_sched():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "schedmod", os.path.join(HOME, ".tmux", "sidebar", "sched.py"))
    m = importlib.util.module_from_spec(spec)
    old = os.environ.get("HOME")
    os.environ["HOME"] = HOME           # sched.py reads its files under HOME
    try:
        spec.loader.exec_module(m)
    finally:
        os.environ["HOME"] = old
    return m


def sched_checks(c, W):
    """Scheduled tasks: the popup, runs, reuse, missed and skipped runs."""
    clear_states()
    sm = load_sched()
    tasks_file = os.path.join(HOME, ".tmux", "claude-sidebar-schedules.json")
    saved = open(tasks_file).read() if os.path.exists(tasks_file) else None
    if saved is not None:
        os.remove(tasks_file)
    stub = tempfile.mkdtemp(prefix="tcs-sched-")
    route = os.path.join(stub, "answer")
    runs = os.path.join(stub, "runs")
    with open(os.path.join(stub, "claude"), "w") as f:
        # -p: a Haiku call (routing, schedule text, the judge). Otherwise a
        # Claude Code session: log the prompt, draw an empty input box, wait.
        f.write('#!/bin/sh\nif [ "$1" = "-p" ]; then cat %s; exit 0; fi\n'
                'printf "%%s\\n" "$@" >> %s\nprintf "\\n\\342\\235\\257 "\nexec sleep 600\n'
                % (route, runs))
    os.chmod(os.path.join(stub, "claude"), 0o755)
    answer = lambda t: open(route, "w").write(t)
    answer("NEW\n")
    env = dict(tmux_env(), PATH=stub + os.pathsep + ENV["PATH"])
    py = os.path.join(HOME, ".tmux", "sidebar", "sched.py")
    cli = lambda *a: subprocess.run(["python3", "-I", py, *a], env=env, capture_output=True, text=True)
    import hashlib
    key = hashlib.sha1(fmt("work", "#{socket_path}").encode()).hexdigest()[:10]
    state_file, lock = sm.paths(key)["state"], sm.paths(key)["lock"]
    tasks = lambda: json.load(open(tasks_file))["tasks"] if os.path.exists(tasks_file) else []
    state = lambda: json.load(open(state_file)) if os.path.exists(state_file) else {}
    lines = sidebar_lines
    cur = current_window()
    server_path = ""
    try:
        # The scheduler runs once per tmux server.
        pid = int(open(lock).read().strip() or 0) if os.path.exists(lock) else 0
        check("sched: the scheduler is running for this tmux server", pid > 0 and alive(pid), str(pid))
        tmux("source-file", CONF)
        time.sleep(1.5)
        check("sched: reloading the config does not start a second one",
              int(open(lock).read().strip() or 0) == pid and
              alive(pid))
        # From here on, passes are run by the test (with a stub Claude), so
        # stop the scheduler; it is started again at the end.
        cli("stop")
        check("sched: stop", wait(lambda: not alive(pid), 3))
        check("sched: empty section says how to add one",
              wait(lambda: any("↻ SCHEDULED" in l for l in lines()) and
                   any("none yet" in l for l in lines()), 3), "\n".join(lines()))

        # Popups take the tmux server's environment: use the stub Claude there.
        server_path = tmux("show-environment", "-g", "PATH").partition("=")[2]
        tmux("set-environment", "-g", "PATH", env["PATH"])
        # Add a task in the popup (prefix T, then a).
        c.buf = b""
        c.prefix("T")
        check("sched: prefix T opens the scheduled tasks popup",
              wait(lambda: b"Scheduled tasks" in c.buf, 4), c.buf[-300:])
        c.send("a")
        time.sleep(0.4)
        for text in ("check my open PRs are rebased on main\r", "\x15PRs rebased\r",
                     "weekdays 5:30pm\r", "\r", "y\r", "\r", "\r"):
            c.send(text)
            time.sleep(0.3)
        check("sched: the popup shows how it read the schedule",
              b"Runs weekdays 17:30" in c.buf, c.buf[-300:])
        check("sched: the popup saved the task",
              wait(lambda: [t.get("name") for t in tasks()] == ["PRs rebased"], 3), str(tasks()))
        t1 = tasks()[0] if tasks() else {}
        check("sched: with its prompt, schedule and the current folder",
              t1.get("prompt") == "check my open PRs are rebased on main" and
              t1.get("when") == "weekdays 5:30pm" and t1.get("reuse") == "auto" and
              t1.get("dir") == fmt(main_pane(cur), "#{pane_current_path}"), str(t1))
        # Plain words it cannot read go to Haiku, which answers with cron.
        answer("15 8 * * 2\n")
        c.send("a")
        time.sleep(0.4)
        for text in ("/standup-notes\r", "\r", "tuesdays at quarter past eight\r"):
            c.send(text)
            time.sleep(0.5)
        check("sched: a schedule in plain words is translated",
              wait(lambda: b"Understood" in c.buf, 4), c.buf[-300:])
        for text in ("\r", "n\r", "\r", "\r"):
            c.send(text)
            time.sleep(0.3)
        t2 = wait(lambda: len(tasks()) == 2 and tasks()[1], 3) or {}
        check("sched: ... and saved as cron, new window, named from the skill",
              t2.get("when") == "15 8 * * 2" and t2.get("reuse") == "new" and
              t2.get("name") == "standup-notes", str(t2))
        c.send("\x1b")
        time.sleep(0.5)
        check("sched: the sidebar lists both tasks with their next run",
              wait(lambda: any(re.search(r"↻ PRs rebased\s+(\S.*)?(in \d+m|\d+:\d\d)", l) for l in lines()) and
                   any("↻ standup-notes" in l for l in lines()), 4), "\n".join(lines()))

        # Run now -> a new window of its own (stub Claude), listed under SCHEDULED.
        answer("NEW\n")
        r = cli("run", t2["id"])
        check("sched: run now opens a new window", r.returncode == 0 and "new window" in r.stdout,
              r.stdout + r.stderr)
        rw = r.stdout.split()[0] if r.stdout else ""
        check("sched: Claude Code started there with the prompt",
              wait(lambda: os.path.exists(runs) and "/standup-notes" in open(runs).read(), 4))
        check("sched: the window is marked as the task's",
              fmt(rw, "#{@claude_sched}") == t2["id"] and fmt(rw, "#{@claude_sched_owned}") == "1")
        check("sched: it is shown under SCHEDULED, not in your list",
              wait(lambda: any(("%2s ran" % fmt(rw, "#{window_index}")) in l for l in lines()), 3) and
              not any((" %s " % fmt(rw, "#{window_index}")) in l and "×" in l for l in lines()),
              "\n".join(lines()))
        check("sched: its tab is drawn like any other window", rw in windows())
        y = next(i for i, l in enumerate(lines()) if ("%2s ran" % fmt(rw, "#{window_index}")) in l)
        c.click(6, y)
        check("sched: clicking the run line goes to that window", wait(lambda: current_window() == rw, 3))
        tmux("select-window", "-t", cur)

        # An always-new task starts fresh each run, replacing its finished
        # previous window...
        tmux("set", "-w", "-t", rw, "@claude_state", "done")
        r = cli("run", t2["id"])
        nw = r.stdout.split()[0] if r.stdout else ""
        check("sched: an always-new task replaces its finished last run window",
              r.returncode == 0 and nw and nw != rw and "replaced" in r.stdout and
              wait(lambda: rw not in windows(), 3), r.stdout + r.stderr)
        # ...but keeps one you have not read yet.
        tmux("set", "-w", "-t", nw, "@claude_state", "review")
        r = cli("run", t2["id"])
        nw2 = r.stdout.split()[0] if r.stdout else ""
        check("sched: ...but keeps it while it is unread",
              r.returncode == 0 and nw2 not in ("", nw) and nw in windows(), r.stdout + r.stderr)
        tmux("kill-window", "-t", nw)
        rw = nw2
        # ...but not while it is still working.
        tmux("set", "-w", "-t", rw, "@claude_state", "working")
        r = cli("run", t2["id"])
        check("sched: a run is skipped while the last one is still working",
              r.returncode != 0 and "still working" in r.stderr, r.stderr)
        check("sched: and the sidebar says so",
              wait(lambda: any("skipped" in l for l in lines()), 4), "\n".join(lines()))
        tmux("set", "-w", "-t", rw, "@claude_state", "done")

        # Reusing one of your windows: Haiku picks it, only if it is free.
        mine = tmux("new-window", "-d", "-P", "-F", "#{window_id}", "-t", "work:",
                    "printf '\\n\\342\\235\\257 '; exec cat")
        wait(lambda: sidebar_panes(mine), 5)
        time.sleep(1)
        mp = main_pane(mine)
        for o, v in (("@claude_state", "idle"), ("@claude_pane", mp), ("@claude_prompt", "deploy the api")):
            tmux("set", "-w", "-t", mine, o, v)
        answer("REUSE %s\n" % mine)
        r = cli("run", t1["id"])
        check("sched: Haiku can send a run to a free window of yours",
              r.returncode == 0 and r.stdout.startswith(mine) and "Haiku" in r.stdout, r.stdout + r.stderr)
        check("sched: the prompt arrives there",
              wait(lambda: "rebased on main" in tmux("capture-pane", "-p", "-t", mp), 3))
        check("sched: your window stays in your list, marked ↻",
              wait(lambda: any(l.startswith("↻") and ("%2s" % fmt(mine, "#{window_index}")) in l
                               for l in lines()), 3), "\n".join(lines()))
        e = dict(tmux_env(mp), PATH=env["PATH"])
        subprocess.run(["sh", "-c", load_ec().hook_command("prompt")], env=e,
                       input=json.dumps({"prompt": "check my open PRs are rebased on main"}), text=True)
        check("sched: the scheduled prompt itself keeps the mark", fmt(mine, "#{@claude_sched}") == t1["id"])
        subprocess.run(["sh", "-c", load_ec().hook_command("prompt")], env=e,
                       input=json.dumps({"prompt": "thanks, now do X"}), text=True)
        check("sched: your own next prompt there clears it", fmt(mine, "#{@claude_sched}") == "")
        # Its next run goes back to the same window, without asking Haiku.
        tmux("set", "-w", "-t", mine, "@claude_state", "done")
        tmux("respawn-pane", "-k", "-t", mp, "printf '\\n\\342\\235\\257 '; exec cat")
        time.sleep(0.5)
        answer("NEW\n")
        r = cli("run", t1["id"])
        check("sched: a reusing task goes back to its previous run window",
              r.returncode == 0 and r.stdout.startswith(mine) and "previous run" in r.stdout,
              r.stdout + r.stderr)
        tmux("set", "-w", "-t", mine, "@claude_state", "idle")
        # Never a window with text in its input box, or one being looked at.
        tmux("set", "-w", "-t", mine, "@claude_state", "idle")
        # A fresh, empty input box, then some text typed into it.
        tmux("respawn-pane", "-k", "-t", mp, "printf '\\n\\342\\235\\257 '; exec cat")
        time.sleep(0.5)
        tmux("send-keys", "-t", mp, "-l", "half typed")
        tmux("set", "-uw", "-t", mine, "@claude_sched")
        st = state()
        st.get(t1["id"], {}).pop("window", None)
        json.dump(st, open(state_file, "w"))
        r = cli("run", t1["id"])
        check("sched: never a window with text in its input box",
              r.returncode == 0 and not r.stdout.startswith(mine), r.stdout + r.stderr)
        tmux("kill-window", "-t", r.stdout.split()[0])
        tmux("send-keys", "-t", mp, "C-u")
        tmux("select-window", "-t", mine)
        r = cli("run", t1["id"])
        check("sched: never the window you are looking at",
              r.returncode == 0 and not r.stdout.startswith(mine), r.stdout + r.stderr)
        tmux("kill-window", "-t", r.stdout.split()[0])
        tmux("select-window", "-t", cur)
        tmux("kill-window", "-t", mine)

        # Due, missed, paused: what the daemon does each pass (run directly).
        now = time.time()
        three_h = time.localtime(now - 3 * 3600)
        due = json.loads(cli("list").stdout)
        due.append({"id": "tdue", "name": "due", "prompt": "due-run", "when": "*/5 * * * *",
                    "dir": "~", "reuse": "new"})
        due.append({"id": "tmiss", "name": "missed one", "prompt": "miss-run",
                    "when": "%d %d * * *" % (three_h.tm_min, three_h.tm_hour), "dir": "~", "reuse": "new"})
        due.append({"id": "tpause", "name": "paused one", "prompt": "pause-run", "when": "*/5 * * * *",
                    "dir": "~", "reuse": "new", "paused": True})
        json.dump({"tasks": due}, open(tasks_file, "w"))
        st = state()
        for tid in ("tdue", "tmiss", "tpause"):
            st[tid] = {"last_slot": 0}
        json.dump(st, open(state_file, "w"))
        cli("tick")
        log = open(runs).read()
        check("sched: a task that is due runs", "due-run" in log, log)
        check("sched: a run says it is one run of an existing schedule (no loops, no questions)",
              '[Scheduled run of "due"' in log and "Do not set up loops" in log and "Task: due-run" in log,
              log[-600:])
        check("sched: a skill is sent as it is, command first",
              "\n/standup-notes\n" in "\n" + log)
        check("sched: a run more than 2 hours late is skipped as missed",
              "miss-run" not in log and state().get("tmiss", {}).get("note", "").startswith("missed"),
              str(state().get("tmiss")))
        check("sched: a paused task does not run", "pause-run" not in log)
        n = log.count("due-run")
        cli("tick")
        check("sched: and runs once per slot", open(runs).read().count("due-run") == n)
        check("sched: the sidebar shows missed and paused",
              wait(lambda: any("missed" in l for l in lines()) and
                   any("↻ paused one" in l and "paused" in l.split("one", 1)[1] for l in lines()), 4),
              "\n".join(lines()))
        cli("pause", "tdue")
        check("sched: pause from the command line", any(t.get("paused") for t in tasks() if t["id"] == "tdue"))

        # A scheduled run that needs a follow-up rings the bell (yellow "!" tab).
        dw = state()["tdue"]["window"]
        dp = main_pane(dw)
        answer("FOLLOWUP: asks which branch to rebase\n")
        e = dict(tmux_env(dp), PATH=env["PATH"])
        header = log[log.index('[Scheduled run of "due"'):log.index("Task: due-run")]
        for ev, payload in (("prompt", {"prompt": header + "Task: due-run"}),
                            ("done", {"last_assistant_message":
                                      "Rebased 3 of 4.\nReported: PR 12 rebased; PR 14 has conflicts\n"
                                      "Which branch for the last?"})):
            subprocess.run(["sh", "-c", load_ec().hook_command(ev)], env=e, input=json.dumps(payload), text=True)
        check("sched: the header is not kept as your prompt", fmt(dw, "#{@claude_prompt}") == "due-run",
              fmt(dw, "#{@claude_prompt}"))
        check("sched: a scheduled follow-up rings the bell",
              wait(lambda: "!" in fmt(dw, "#{window_flags}"), 6), fmt(dw, "#{window_flags}"))
        check("sched: ...and shows under its task with the reason",
              wait(lambda: any("↩ asks which branch" in l for l in lines()), 4), "\n".join(lines()))
        notes = os.path.join(HOME, ".tmux", "claude-sidebar-notes", "tdue.txt")
        check("sched: what a run reported is remembered",
              wait(lambda: os.path.exists(notes) and "PR 12 rebased; PR 14 has conflicts" in open(notes).read(), 3))
        tmux("set", "-w", "-t", dw, "@claude_state", "done")
        cli("resume", "tdue")
        os.remove(runs)
        cli("run", "tdue")
        check("sched: the next run is told what was already reported",
              wait(lambda: os.path.exists(runs) and "Already reported" in open(runs).read() or
                   "PR 12 rebased" in tmux("capture-pane", "-p", "-S", "-50", "-t", dp), 3))
        # A run with nothing new that is wrapped up does not stay unread.
        nw = state()["tdue"]["window"]
        np_ = main_pane(nw)
        answer("WRAPPED: nothing new this run\n")
        e2 = dict(tmux_env(np_), PATH=env["PATH"])
        for ev, payload in (("prompt", {"prompt": "due-run"}),
                            ("done", {"last_assistant_message": "No changes.\nReported: nothing new"})):
            subprocess.run(["sh", "-c", load_ec().hook_command(ev)], env=e2, input=json.dumps(payload), text=True)
        check("sched: a run with nothing new is marked read (done), not unread",
              wait(lambda: fmt(nw, "#{@claude_state}") == "done", 6), fmt(nw, "#{@claude_state}"))
        check("sched: and adds nothing to what was reported",
              open(notes).read().count("\n") == 1, open(notes).read())
        cli("pause", "tdue")

        # Old run windows that are done, seen and not the latest get closed.
        old = rw
        st = state()
        st[t2["id"]]["window"] = "@99999"
        json.dump(st, open(state_file, "w"))
        tmux("set", "-w", "-t", old, "@claude_state", "done")
        tmux("set", "-uw", "-t", old, "@claude_verdict")
        tmux("set", "-w", "-t", old, "@claude_updated", str(int(time.time()) - 2 * 86400))
        cli("tick")
        check("sched: an old, finished run window is closed", wait(lambda: old not in windows(), 3))

        r = cli("delete", "tdue")
        check("sched: delete", "tdue" not in [t["id"] for t in tasks()])
    finally:
        for w in windows():
            if fmt(w, "#{@claude_sched_owned}") == "1":
                tmux("kill-window", "-t", w, check=False)
        if saved is None:
            os.path.exists(tasks_file) and os.remove(tasks_file)
        else:
            open(tasks_file, "w").write(saved)
        shutil.rmtree(os.path.join(HOME, ".tmux", "claude-sidebar-notes"), ignore_errors=True)
        clear_states()
        if server_path:
            tmux("set-environment", "-g", "PATH", server_path)
        tmux("source-file", CONF)          # starts the scheduler again


def keys_checks(c, W):
    """Optional: prefix 1-9 jump to rows of the list, not window numbers."""
    clear_states()
    keys_file = os.path.join(HOME, ".tmux", "claude-sidebar-keys")
    saved = open(keys_file).read() if os.path.exists(keys_file) else None
    before = [tmux("list-keys", "-T", "prefix", str(d)) for d in range(1, 10)]
    ws = windows()
    cur = current_window()
    # Make the list order differ from the window numbers.
    first, second = ws[-1], ws[-2]
    now = int(time.time())
    tmux("set", "-w", "-t", first, "@claude_state", "needs")
    tmux("set", "-w", "-t", first, "@claude_updated", str(now))
    tmux("set", "-w", "-t", second, "@claude_state", "working")
    tmux("set", "-w", "-t", second, "@claude_updated", str(now))
    fname, sname = fmt(first, "#{window_name}")[:10], fmt(second, "#{window_name}")[:10]
    try:
        check("keys: off by default (shows window numbers)",
              tmux("show", "-gqv", "@claude_sidebar_keys") == "" and
              "select-window" in before[0], before[0])
        y = wait(lambda: row_of(sidebar_panes(current_window())[0], "jump to window"), 3)
        c.click(3, y)
        check("keys: clicking the 1-9 row turns it on",
              wait(lambda: tmux("show", "-gqv", "@claude_sidebar_keys") == "position", 3) and
              os.path.exists(keys_file) and open(keys_file).read().strip() == "position")
        check("keys: the legend says so",
              wait(lambda: any("jump to list row" in l for l in sidebar_lines()), 3))
        check("keys: prefix 1-9 are rebound", all("sidebar.sh" in tmux("list-keys", "-T", "prefix", str(d))
                                                  for d in range(1, 10)))
        rows = lambda: [l for l in sidebar_lines() if "×" in l]
        check("keys: the list shows positions 1, 2, ... for its rows",
              wait(lambda: rows() and rows()[0][1:3] == " 1" and fname in rows()[0]
                   and rows()[1][1:3] == " 2" and sname in rows()[1], 3), "\n".join(sidebar_lines()))
        c.prefix("1")
        check("keys: prefix 1 goes to the top row", wait(lambda: current_window() == first, 3),
              current_window())
        c.prefix("2")
        check("keys: prefix 2 goes to the second row", wait(lambda: current_window() == second, 3),
              current_window())
        tmux("select-window", "-t", cur)
        tmux("source-file", CONF)
        time.sleep(1)
        check("keys: kept after reloading the config",
              tmux("show", "-gqv", "@claude_sidebar_keys") == "position" and
              "sidebar.sh" in tmux("list-keys", "-T", "prefix", "1"))
        y = wait(lambda: row_of(sidebar_panes(current_window())[0], "jump to list row"), 3)
        c.click(3, y)
        check("keys: clicking again turns it off",
              wait(lambda: tmux("show", "-gqv", "@claude_sidebar_keys") == "", 3))
        check("keys: your own 1-9 bindings are back exactly",
              [tmux("list-keys", "-T", "prefix", str(d)) for d in range(1, 10)] == before)
        check("keys: the list shows window numbers again",
              wait(lambda: any(("%2s %s" % (fmt(first, "#{window_index}"), fname)) in l
                               for l in sidebar_lines()), 3), "\n".join(sidebar_lines()))
    finally:
        if tmux("show", "-gqv", "@claude_sidebar_keys") == "position":
            subprocess.run([os.path.join(HOME, ".tmux", "sidebar", "sidebar.sh"), "keys", "index"], env=tmux_env())
        if saved is None:
            os.path.exists(keys_file) and os.remove(keys_file)
        else:
            open(keys_file, "w").write(saved)
        clear_states()


def descendants(pid):
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True).stdout
    kids = {}
    for line in out.splitlines():
        p, pp = line.split()
        kids.setdefault(int(pp), []).append(int(p))
    found, todo = [pid], [pid]
    while todo:
        for k in kids.get(todo.pop(), []):
            found.append(k)
            todo.append(k)
    return found


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def accept_trust(pane):
    """Answer Claude Code's workspace trust dialog with "Yes" (newer versions
    highlight "No, exit" first)."""
    scr = lambda: tmux("capture-pane", "-p", "-t", pane)
    # Either the dialog, or Claude's prompt box ("? for shortcuts").
    wait(lambda: "trust this folder" in scr().lower() or "for shortcuts" in scr(), 40)
    if "trust this folder" not in scr().lower():
        return
    if any(l.strip().startswith("❯ No") for l in scr().splitlines()):
        tmux("send-keys", "-t", pane, "Down")
        time.sleep(0.3)
    tmux("send-keys", "-t", pane, "Enter")


def claude_checks(c, codez, close_via_x):
    # × on a window running Claude Code stops Claude and everything under it.
    xw = tmux("new-window", "-d", "-P", "-F", "#{window_id}", "-t", "work:", "-c", codez,
              "claude --model haiku")
    wait(lambda: sidebar_panes(xw), 10)
    time.sleep(1)
    xp = main_pane(xw)
    scr = lambda: tmux("capture-pane", "-p", "-t", xp)
    accept_trust(xp)
    wait(lambda: fmt(xw, "#{@claude_state}") == "idle", 40)
    pids = descendants(int(fmt(xp, "#{pane_pid}")))
    print("   claude window processes before ×: %d" % len(pids))
    close_via_x(xw, "y")
    check("x: closing a Claude window removes it", wait(lambda: xw not in windows(), 10))
    check("x: every process in that window has exited",
          wait(lambda: not any(alive(p) for p in pids), 15),
          str([p for p in pids if alive(p)]))

    # A Claude window in the background; the current window is a plain shell.
    cw = tmux("new-window", "-d", "-P", "-F", "#{window_id}", "-t", "work:", "-c", codez,
              "claude --model haiku --permission-mode default")
    wait(lambda: sidebar_panes(cw), 10)
    time.sleep(1)
    cp = main_pane(cw)
    screen = lambda: tmux("capture-pane", "-p", "-t", cp)
    # Workspace trust dialog on first run in a new directory.
    accept_trust(cp)
    check("claude: SessionStart -> idle", wait(lambda: fmt(cw, "#{@claude_state}") == "idle", 30),
          fmt(cw, "#{@claude_state}") + "\n" + screen()[-1200:])

    # 7. Shift+Enter inserts a newline instead of submitting.
    time.sleep(2)
    tmux("send-keys", "-t", cp, "-l", "line-one")
    tmux("send-keys", "-t", cp, "S-Enter")
    tmux("send-keys", "-t", cp, "-l", "line-two")
    time.sleep(1.5)
    s = screen()
    lines = s.splitlines()
    i1 = next((i for i, l in enumerate(lines) if "line-one" in l), None)
    i2 = next((i for i, l in enumerate(lines) if "line-two" in l), None)
    check("7. Shift+Enter inserts a newline in Claude Code",
          i1 is not None and i2 is not None and i2 == i1 + 1
          and fmt(cw, "#{@claude_state}") == "idle", s[-600:])
    tmux("send-keys", "-t", cp, "C-u")
    tmux("send-keys", "-t", cp, "C-u")
    time.sleep(0.5)

    # 5. working -> needs you (permission) -> done.
    tmux("send-keys", "-t", cp, "-l",
         "Use the Bash tool to run exactly: touch tcs-probe.txt  (no other tools)")
    tmux("send-keys", "-t", cp, "Enter")
    seen = set()
    def saw(state):
        s = fmt(cw, "#{@claude_state}")
        seen.add(s)
        return state in seen
    check("5. prompt -> working", wait(lambda: saw("working"), 30), str(seen))
    check("5. permission prompt -> needs you", wait(lambda: saw("needs"), 60), str(seen))
    check("6. permission prompt rings the bell in a background window",
          wait(lambda: "!" in fmt(cw, "#{window_flags}"), 10), fmt(cw, "#{window_flags}"))
    sb = sidebar_panes(current_window())[0]
    check("5. sidebar shows needs you", wait(lambda: row_of(sb, "needs you") is not None, 3))
    tmux("send-keys", "-t", cp, "Enter")  # approve
    check("5. Claude finishes in a background window -> review",
          wait(lambda: fmt(cw, "#{@claude_state}") == "review", 90), fmt(cw, "#{@claude_state}"))
    check("5. sidebar shows unread", wait(lambda: row_of(sb, "◆ unread") is not None, 3))
    check("claude: the tool really ran", os.path.exists(os.path.join(codez, "tcs-probe.txt")))
    pid = fmt(cw, "#{@claude_pid}")
    check("claude: hooks record Claude Code's own pid (it runs in the pane)",
          pid.isdigit() and int(pid) in descendants(int(fmt(cp, "#{pane_pid}"))) and alive(int(pid)),
          pid)
    check("claude: last prompt recorded", "tcs-probe" in fmt(cw, "#{@claude_prompt}"),
          fmt(cw, "#{@claude_prompt}"))
    tr = fmt(cw, "#{@claude_transcript}")
    check("claude: transcript recorded", tr.endswith(".jsonl") and os.path.exists(tr), tr)
    py = os.path.join(HOME, ".tmux", "sidebar", "search.py")
    hits = subprocess.run(["python3", "-I", py, "--list", "tcs-probe"], env=tmux_env(),
                          capture_output=True, text=True).stdout
    check("claude: search finds the prompt in the real transcript", hits.startswith(cw + "\t"), hits)
    check("claude: the AI check gives a verdict",
          wait(lambda: fmt(cw, "#{@claude_verdict}") in ("wrapped", "followup"), 90),
          fmt(cw, "#{@claude_verdict}|#{@claude_note}"))
    print("   verdict: %s (%s)" % (fmt(cw, "#{@claude_verdict}"), fmt(cw, "#{@claude_note}")))
    tmux("select-window", "-t", cw)
    check("5. looking at it -> done", wait(lambda: fmt(cw, "#{@claude_state}") == "done", 3),
          fmt(cw, "#{@claude_state}"))

    # Esc runs no hook; the sidebar notices the spinner has stopped.
    tmux("set", "-g", "@claude_sidebar_stall", "6")
    try:
        tmux("send-keys", "-t", cp, "-l", "Write a 2000 word essay about the history of terminals. No tools.")
        tmux("send-keys", "-t", cp, "Enter")
        check("esc: prompt -> working", wait(lambda: fmt(cw, "#{@claude_state}") == "working", 30))
        time.sleep(4)
        tmux("send-keys", "-t", cp, "Escape")
        time.sleep(3)
        check("esc: still shown working right after Esc (no hook)", fmt(cw, "#{@claude_state}") == "working",
              fmt(cw, "#{@claude_state}"))
        check("esc: shown idle once Claude's screen is still",
              wait(lambda: fmt(cw, "#{@claude_state}") == "idle", 25), fmt(cw, "#{@claude_state}"))
    finally:
        tmux("set", "-gu", "@claude_sidebar_stall")
    tmux("select-window", "-t", windows()[0])
    name = fmt(cw, "#{window_name}")
    check("2b. Claude window named from its title (not dir, no glyph)",
          name and name != "churn" and name[0].isalnum(), name)
    print("   claude window name: %r" % name)

    # A scheduled task, run by real Claude Code in a window of its own.
    tasks_file = os.path.join(HOME, ".tmux", "claude-sidebar-schedules.json")
    saved = open(tasks_file).read() if os.path.exists(tasks_file) else None
    py = os.path.join(HOME, ".tmux", "sidebar", "sched.py")
    cli = lambda *a: subprocess.run(["python3", "-I", py, *a], env=tmux_env(), capture_output=True, text=True)
    try:
        tid = cli("add", "--name", "live check", "--prompt", "Reply with just: scheduled-ok",
                  "--when", "weekdays 5:30pm", "--dir", codez, "--reuse", "new").stdout.strip()
        tmux("select-window", "-t", windows()[0])
        r = cli("run", tid)
        sw = r.stdout.split()[0] if r.stdout else ""
        check("sched live: run now starts Claude Code in a new window", r.returncode == 0 and sw, r.stderr)
        check("sched live: the run finishes, unread -> review",
              wait(lambda: fmt(sw, "#{@claude_state}") == "review", 90), fmt(sw, "#{@claude_state}"))
        check("sched live: Claude got the prompt", "scheduled-ok" in fmt(sw, "#{@claude_prompt}"))
        check("sched live: the AI check gives a verdict",
              wait(lambda: fmt(sw, "#{@claude_verdict}") in ("wrapped", "followup"), 90),
              fmt(sw, "#{@claude_verdict}|#{@claude_note}"))
        print("   scheduled run verdict: %s (%s)" % (fmt(sw, "#{@claude_verdict}"), fmt(sw, "#{@claude_note}")))
        sb2 = sidebar_panes(current_window())[0]
        check("sched live: shown under SCHEDULED with its status",
              wait(lambda: row_of(sb2, "↻ live check") is not None and
                   any("ran" in l and "◆ unread" in l for l in tmux("capture-pane", "-p", "-t", sb2).splitlines()), 4))
        tmux("kill-window", "-t", sw)
    finally:
        if saved is None:
            os.path.exists(tasks_file) and os.remove(tasks_file)
        else:
            open(tasks_file, "w").write(saved)

    # After Esc, Claude puts the interrupted prompt back in its input box;
    # Ctrl-D only exits from an empty one.
    for _ in range(3):
        tmux("send-keys", "-t", cp, "C-u")
        time.sleep(0.3)
    tmux("send-keys", "-t", cp, "C-d")
    tmux("send-keys", "-t", cp, "C-d")
    check("2. exiting claude (its only process) closes the window",
          wait(lambda: cw not in windows(), 20), str(windows()))


if __name__ == "__main__":
    main()
