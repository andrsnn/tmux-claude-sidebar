#!/usr/bin/env python3
"""Edit ~/.tmux.conf and ~/.claude/settings.json for tmux-claude-sidebar.

    edit_configs.py install   <tmux.conf> <settings.json> <block-file> <state-file>
    edit_configs.py uninstall <tmux.conf> <settings.json> <state-file>

Both actions are idempotent. Both files are checked before either is
written, and each is replaced atomically (through symlinks, so dotfiles
managed by stow etc. keep working). If settings.json is not valid JSON, or
~/.tmux.conf has a start marker with no end marker, nothing is written.

<state-file> remembers the user's previous preferredNotifChannel so that
uninstall can put it back.
"""
import json
import os
import re
import sys
import tempfile

BEGIN = "# >>> tmux-claude-sidebar >>>"
END = "# <<< tmux-claude-sidebar <<<"
DISABLED = "# [tmux-claude-sidebar] "
MARK = "# tmux-claude-sidebar"

# User lines that would fight catppuccin's status bar or our window naming.
CONFLICT = re.compile(
    r"^\s*(set|set-option|setw|set-window-option)\b[^#]*?\s"
    r"(status-style|window-status-[a-z-]*(style|format)|automatic-rename-format)\b"
)
# "set -as terminal-features ...extkeys" appends again on every reload. The
# block sets the same feature at a fixed index, so this line is redundant.
EXTKEYS_APPEND = re.compile(
    r"^\s*(set|set-option)\s+-[a-z]*a[a-z]*\s+terminal-features\s+\S*extkeys")


def tset(state):
    return 'tmux set -w -t "$TMUX_PANE" @claude_state %s' % state


# Known gap: interrupting Claude with Esc fires no hook (checked on 2.1.330:
# not Stop, not Notification), so the window keeps showing "working" until
# the next prompt.
STATES = [
    # (event, matcher or None, shell action, state the sidebar should show)
    ("SessionStart", None, tset("idle"), "idle"),
    ("UserPromptSubmit", None, tset("working"), "working"),
    ("PermissionRequest", None, tset("needs"), "needs"),
    ("Notification", "permission_prompt|elicitation_dialog", tset("needs"), "needs"),
    # After a prompt is answered (or a tool is denied) the turn goes on.
    ("PostToolUse", None, tset("working"), "working"),
    ("PostToolUseFailure", None, tset("working"), "working"),
    ("PermissionDenied", None, tset("working"), "working"),
    ("Stop", None, tset("done"), "done"),
    ("StopFailure", None, tset("done"), "done"),
    ("SessionEnd", None, 'tmux set -uw -t "$TMUX_PANE" @claude_state', ""),
]


def hook_command(action):
    # Does nothing outside tmux and always exits 0. Stdin JSON is ignored.
    return '[ -n "$TMUX_PANE" ] && { %s; } >/dev/null 2>&1; exit 0 %s' % (action, MARK)


def read(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.read()


def write_atomic(path, text):
    path = os.path.realpath(path)  # write through symlinks, don't replace them
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tcs-")
    with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(text)
    if os.path.exists(path):
        os.chmod(tmp, os.stat(path).st_mode & 0o7777)
    else:
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)
    os.replace(tmp, path)


def bare(line):
    return line.rstrip("\r\n")


def strip_block(lines):
    starts = [i for i, ln in enumerate(lines) if bare(ln) == BEGIN]
    ends = [i for i, ln in enumerate(lines) if bare(ln) == END]
    if len(starts) != len(ends) or any(e < s for s, e in zip(starts, ends)):
        sys.exit("~/.tmux.conf has an unmatched '%s' or '%s' line; fix it by hand "
                 "and re-run. Nothing was changed." % (BEGIN, END))
    out, inside = [], False
    for ln in lines:
        if bare(ln) == BEGIN:
            inside = True
        elif inside and bare(ln) == END:
            inside = False
        elif not inside:
            out.append(ln)
    return out


def enable_lines(lines):
    return [ln[len(DISABLED):] if ln.startswith(DISABLED) else ln for ln in lines]


def disable_conflicts(lines):
    out, cont, disabled = [], False, []
    for ln in lines:
        body = bare(ln)
        if cont or CONFLICT.match(body) or EXTKEYS_APPEND.match(body):
            out.append(DISABLED + ln)
            disabled.append(body)
            cont = body.endswith("\\")
        else:
            out.append(ln)
    return out, disabled


def plan_tmux_conf(path, block):
    old = read(path) or ""
    lines = old.splitlines(True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    rest = enable_lines(strip_block(lines))
    disabled = []
    if block is None:
        new = rest
    else:
        # The block goes first so tmux-sensible and catppuccin load before
        # the user's own settings, and explicit user values win.
        rest, disabled = disable_conflicts(rest)
        new = [BEGIN + "\n"] + block.splitlines(True) + [END + "\n"] + rest
    text = "".join(new)
    return (text if text != old else None), disabled


def ours(group):
    return any(MARK in h.get("command", "") for h in group.get("hooks", []))


def plan_settings(path, install, state_file):
    raw = read(path)
    data = {}
    if raw and raw.strip():
        try:
            data = json.loads(raw)
        except ValueError as e:
            sys.exit("%s is not valid JSON (%s). Nothing was changed." % (path, e))
    before = json.dumps(data, sort_keys=True)
    hooks = data.get("hooks", {})
    for event in list(hooks):
        hooks[event] = [g for g in hooks[event] if not ours(g)]
        if not hooks[event]:
            del hooks[event]
    state = None
    if install:
        for event, matcher, action, _ in STATES:
            group = {"hooks": [{"type": "command", "command": hook_command(action)}]}
            if matcher:
                group = {"matcher": matcher, **group}
            hooks.setdefault(event, []).append(group)
        if not os.path.exists(state_file):
            state = {"preferredNotifChannel": data.get("preferredNotifChannel")}
        data["preferredNotifChannel"] = "terminal_bell"
    elif os.path.exists(state_file):
        with open(state_file) as f:
            prev = json.load(f).get("preferredNotifChannel")
        if prev is None:
            data.pop("preferredNotifChannel", None)
        else:
            data["preferredNotifChannel"] = prev
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    if json.dumps(data, sort_keys=True) == before:
        return None, state
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n", state


def main():
    a = sys.argv[1:]
    if len(a) == 5 and a[0] == "install":
        _, conf, sett, block_file, state_file = a
        with open(block_file, encoding="utf-8") as f:
            block = f.read()
        install = True
    elif len(a) == 4 and a[0] == "uninstall":
        _, conf, sett, state_file = a
        block, install = None, False
    else:
        sys.exit(__doc__)
    # Plan both edits first, so a problem with either file changes nothing.
    s_text, state = plan_settings(sett, install, state_file)
    t_text, disabled = plan_tmux_conf(conf, block)
    if state is not None:
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w") as f:
            json.dump(state, f)
    if s_text is not None:
        write_atomic(sett, s_text)
    if t_text is not None:
        write_atomic(conf, t_text)
    if not install and os.path.exists(state_file):
        os.remove(state_file)
    print("%s: %s" % (conf, "updated" if t_text is not None else "unchanged"))
    print("%s: %s" % (sett, "updated" if s_text is not None else "unchanged"))
    if disabled:
        print("Disabled these lines in %s (they conflict with the theme or window"
              " naming; uninstall.sh turns them back on):" % conf)
        for d in disabled:
            print("    " + d)


if __name__ == "__main__":
    main()
