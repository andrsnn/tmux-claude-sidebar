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

results = []


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
    faulthandler.dump_traceback_later(540, exit=True)  # a hang prints where it is
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
    tmux("new-window", "-d", "-t", "work:")
    victim = tmux("list-windows", "-t", "work", "-F", "#{window_id}").split()[-1]
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

    for ev, m, action, state in ec.STATES:
        if state is None:
            continue
        rc = hook(action)
        check("hook %s%s -> %r" % (ev, " (%s)" % m if m else "", state),
              rc == 0 and wait(lambda: fmt(bg, "#{@claude_state}") == state, 2.5),
              fmt(bg, "#{@claude_state}"))
    labels = {"working": "● working", "needs": "! needs you", "done": "✓ done", "idle": "○ idle"}
    for state, label in labels.items():
        hook(ec.tset(state))
        check("sidebar shows %r" % label, wait(lambda: row_of(sb, label) is not None, 2.5))
    hook('tmux set -uw -t "$TMUX_PANE" @claude_state')
    bg_name = fmt(bg, "#{window_name}")[:8]
    check("plain shell window shows no status",
          wait(lambda: not any(l in (tmux("capture-pane", "-p", "-t", sb).splitlines()[row_of(sb, bg_name)])
                               for l in labels.values()), 2.5))
    hook('tmux set -uw -t "$TMUX_PANE" @claude_state')
    r = subprocess.run(["sh", "-c", ec.hook_command(ec.tset("working"))],
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

    width_checks(c, W)

    if args.claude:
        claude_checks(c, codez, close_via_x)


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


def claude_checks(c, codez, close_via_x):
    # × on a window running Claude Code stops Claude and everything under it.
    tmux("new-window", "-d", "-t", "work:", "-c", codez, "claude --model haiku")
    xw = tmux("list-windows", "-t", "work", "-F", "#{window_id}").split()[-1]
    xp = tmux("list-panes", "-t", xw, "-F", "#{?@sidebar,,#{pane_id}}").split()[0]
    scr = lambda: tmux("capture-pane", "-p", "-t", xp)
    wait(lambda: "trust" in scr().lower() or "❯" in scr(), 30)
    if "trust" in scr().lower():
        tmux("send-keys", "-t", xp, "Enter")
    wait(lambda: fmt(xw, "#{@claude_state}") == "idle", 40)
    pids = descendants(int(fmt(xp, "#{pane_pid}")))
    print("   claude window processes before ×: %d" % len(pids))
    close_via_x(xw, "y")
    check("x: closing a Claude window removes it", wait(lambda: xw not in windows(), 10))
    check("x: every process in that window has exited",
          wait(lambda: not any(alive(p) for p in pids), 15),
          str([p for p in pids if alive(p)]))

    # A Claude window in the background; the current window is a plain shell.
    tmux("new-window", "-d", "-t", "work:", "-c", codez,
         "claude --model haiku --permission-mode default")
    cw = tmux("list-windows", "-t", "work", "-F", "#{window_id}").split()[-1]
    cp = tmux("list-panes", "-t", cw, "-F", "#{?@sidebar,,#{pane_id}}").split()[0]
    screen = lambda: tmux("capture-pane", "-p", "-t", cp)
    # Workspace trust dialog on first run in a new directory.
    wait(lambda: "trust" in screen().lower() or "❯" in screen(), 30)
    if "trust" in screen().lower():
        tmux("send-keys", "-t", cp, "Enter")
    check("claude: SessionStart -> idle", wait(lambda: fmt(cw, "#{@claude_state}") == "idle", 30),
          fmt(cw, "#{@claude_state}"))

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
    check("5. Claude finishes -> done", wait(lambda: fmt(cw, "#{@claude_state}") == "done", 90),
          fmt(cw, "#{@claude_state}"))
    check("5. sidebar shows done", wait(lambda: row_of(sb, "✓ done") is not None, 3))
    check("claude: the tool really ran", os.path.exists(os.path.join(codez, "tcs-probe.txt")))
    name = fmt(cw, "#{window_name}")
    check("2b. Claude window named from its title (not dir, no glyph)",
          name and name != "churn" and name[0].isalnum(), name)
    print("   claude window name: %r" % name)

    tmux("send-keys", "-t", cp, "C-d")
    tmux("send-keys", "-t", cp, "C-d")
    check("2. exiting claude (its only process) closes the window",
          wait(lambda: cw not in windows(), 20), str(windows()))


if __name__ == "__main__":
    main()
