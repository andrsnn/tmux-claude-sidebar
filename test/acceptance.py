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
LABELS = {"working": "● working", "needs": "! needs you", "review": "◆ review",
          "done": "✓ done", "idle": "○ idle"}


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
    faulthandler.dump_traceback_later(900, exit=True)  # a hang prints where it is
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
    check("sidebar has the configured width (%d)" % W, fmt(sb1, "#{pane_width}") == str(W))
    if not os.path.exists(WIDTH_FILE):
        check("default width is 36", W == 36, str(W))
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
    c.click(40, 5)
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
    menu_checks(c, W)
    search_checks(c, W)
    judge_checks(c, W)

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
                  "@claude_hash_at", "@claude_turn", "@claude_verdict", "@claude_note"):
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
    check("review: sidebar shows the prompt under a working window",
          wait(lambda: any("tidy the README" in l for l in sidebar_lines()), 3), "\n".join(sidebar_lines()))
    run_hook(pa, "done")
    check("review: Stop in a background window -> review",
          wait(lambda: fmt(a, "#{@claude_state}") == "review", 2.5), fmt(a, "#{@claude_state}"))
    check("review: sidebar shows ◆ review",
          wait(lambda: any(" %s " % an in l and "◆ review" in l for l in sidebar_lines()), 3))
    drawn = lambda w: fmt(w, "#{E:window-status-format}").lower()
    check("review: its tab is drawn yellow", "bg=#f9e2af" in drawn(a), drawn(a))
    check("review: a quiet window's tab is not yellow", "#f9e2af" not in drawn(ws[3]), drawn(ws[3]))
    tmux("select-window", "-t", a)
    check("review: switching to it marks it done",
          wait(lambda: fmt(a, "#{@claude_state}") == "done", 3), fmt(a, "#{@claude_state}"))
    check("review: then the sidebar shows ✓ done",
          wait(lambda: any(" %s " % an in l and "✓ done" in l for l in sidebar_lines()), 3))
    check("review: no prompt line under a done window",
          wait(lambda: not any("tidy the README" in l for l in sidebar_lines()), 3))
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
    check("judge: a reply ending in a question is a follow-up at once",
          wait(lambda: fmt(t, "#{@claude_verdict}") == "followup", 2), fmt(t, "#{@claude_verdict}"))
    check("judge: Claude's verdict and reason are stored",
          wait(lambda: fmt(t, "#{@claude_note}") == "asks whether to open a PR", 5), fmt(t, "#{@claude_note}"))
    calls = open(log).read() if os.path.exists(log) else ""
    check("judge: asks haiku, with hooks, tools and session saving off",
          all(x in calls for x in ("--model\nhaiku", "--no-session-persistence",
                                   '{"disableAllHooks": true}', "--tools\n\n")), calls[:400])
    check("judge: the call is not tracked as a session (no TMUX_PANE)", "TMUX_PANE=unset" in calls)
    check("judge: the call sees the prompt and the reply",
          "fix the flaky login test" in calls and "Want me to open a PR?" in calls)
    check("judge: review row shows the reason",
          wait(lambda: any("↩ asks whether to open" in l for l in sidebar_lines()), 3),
          "\n".join(sidebar_lines()))
    tmux("select-window", "-t", t)
    check("judge: after you look, it is 'follow up', not 'done'",
          wait(lambda: any("↩ follow up" in l for l in sidebar_lines()), 3), "\n".join(sidebar_lines()))
    tmux("select-window", "-t", cur)
    other = [w for w in ws if w not in (t, cur)][-1]
    tmux("set", "-w", "-t", other, "@claude_state", "done")
    tmux("set", "-w", "-t", other, "@claude_updated", str(int(time.time()) + 5))

    def follow_first():
        lines = sidebar_lines()
        f = [i for i, l in enumerate(lines) if "↩ follow up" in l]
        d = [i for i, l in enumerate(lines) if "✓ done" in l and "×" in l]
        return f and d and f[0] < d[0]
    check("judge: a follow-up sorts above done windows, even newer ones",
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
    check("judge: marking it wrapped up -> done",
          wait(lambda: fmt(t, "#{@claude_verdict}") == "wrapped" and
               any(" %s " % fmt(t, "#{window_name}")[:10] in l and "✓ done" in l
                   for l in sidebar_lines()), 3), "\n".join(sidebar_lines()))

    answer("WRAPPED: tests pass, nothing pending\n")
    turn("All done: the flaky test is fixed and CI is green.")
    check("judge: a finished job is wrapped up",
          wait(lambda: fmt(t, "#{@claude_verdict}") == "wrapped", 5) and
          fmt(t, "#{@claude_note}") == "tests pass, nothing pending", fmt(t, "#{@claude_note}"))
    check("judge: shown as ✓ reason under the review row",
          wait(lambda: any("✓ tests pass" in l for l in sidebar_lines()), 3), "\n".join(sidebar_lines()))

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

    # Turned off: only the quick guess, no call.
    tmux("set", "-g", "@claude_sidebar_judge", "off")
    try:
        os.remove(log)
        turn("Which database should I use, Postgres or SQLite?")
        check("judge: off -> still the quick guess",
              wait(lambda: fmt(t, "#{@claude_verdict}") == "followup", 3))
        time.sleep(1.5)
        check("judge: off -> Claude is not called", not os.path.exists(log))
    finally:
        tmux("set", "-gu", "@claude_sidebar_judge")
    # StopFailure: an error is always a follow-up.
    subprocess.run(["sh", "-c", load_ec().hook_command("failed")], env=e, input="{}", text=True)
    check("judge: a turn that failed is a follow-up",
          fmt(t, "#{@claude_verdict}") == "followup" and fmt(t, "#{@claude_note}") == "stopped with an error")
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
    check("5. sidebar shows review", wait(lambda: row_of(sb, "◆ review") is not None, 3))
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
