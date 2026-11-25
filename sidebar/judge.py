#!/usr/bin/env python3
"""Decide whether a finished Claude Code turn wrapped up the work, or whether
you should come back to it. Started in the background by hook.sh on Stop.

    judge.py <pane> <turn>     the Stop hook's JSON on stdin

Writes on the pane's window:
    @claude_verdict   "wrapped" or "followup"
    @claude_note      a few words saying why

First a quick guess (the reply ends with a question), then a one-shot call to
Claude Code itself (`claude -p --model haiku`, your existing login; hooks,
tools, MCP servers and session saving are all off for that call), unless
turned off with `sidebar.sh judge off`. Results for a turn that has since been
followed by a new prompt are dropped. Always exits 0, silently.
"""
import json
import os
import re
import shutil
import time
import subprocess
import sys

MAX_CHARS = 6000
TIMEOUT = 90
SYSTEM = (
    "You triage finished Claude Code turns for a busy developer who runs many "
    "sessions in parallel and cannot reread them all. Decide whether the work "
    "in this turn is wrapped up, or whether the developer should come back to "
    "it. Come back (FOLLOWUP) when Claude asked a question, offered options or "
    "a choice, proposed a plan awaiting approval, hit an error or blocker it "
    "could not get past, left requested steps undone, or says what to do next "
    "and is waiting. WRAPPED when the request is done and nothing is pending "
    "from the developer (a summary of finished work, passing tests, an answer "
    "to a question). Reply with exactly one line, no other text: "
    "\"WRAPPED: <reason>\" or \"FOLLOWUP: <reason>\", the reason at most 8 "
    "words, specific (e.g. \"asks whether to open a PR\")."
)


def tmux(*args):
    return subprocess.run(["tmux"] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL).stdout.decode("utf-8", "replace").strip()


def quote(s):
    """A tmux command-line string literal."""
    for ch in "\\\"$":
        s = s.replace(ch, "\\" + ch)
    return '"' + s + '"'


def write(pane, turn, verdict, note):
    """Store the verdict, unless a newer prompt or turn owns the window now.
    Checked and written in one tmux command, so a prompt cannot slip in."""
    note = " ".join(note.replace("|", "/").split())[:80]
    if note.endswith(";"):
        note += " "        # tmux reads a trailing ";" as a command separator
    tmux("if-shell", "-F", "-t", pane, "#{==:#{@claude_turn},%s}" % turn,
         "set -w -t %s @claude_verdict %s ; set -w -t %s @claude_note %s"
         % (pane, verdict, pane, quote(note)))


def ring_if_scheduled(pane, turn):
    """A scheduled run that needs a follow-up rings the bell, so its tab
    flags it even if nobody is watching (once per turn)."""
    info = tmux("display", "-p", "-t", pane,
                "#{@claude_turn}|#{@claude_sched}|#{@claude_verdict}|#{@claude_rang}|#{pane_tty}")
    t, sched, verdict, rang, tty = (info.split("|") + [""] * 5)[:5]
    if t != turn or not sched or verdict != "followup" or rang == turn or not tty:
        return
    tmux("set", "-w", "-t", pane, "@claude_rang", turn)
    try:
        with open(tty, "w") as f:
            f.write("\a")
    except OSError:
        pass


def remember_report(pane, reply):
    """A scheduled run ends with "Reported: ..."; keep that line for the
    task's next run (sched.py shows it as already reported)."""
    tid = tmux("display", "-p", "-t", pane, "#{@claude_sched}")
    if not tid:
        return
    lines = [ln.strip() for ln in reply.strip().splitlines() if ln.strip()]
    found = [ln for ln in lines[-5:] if re.match(r"^[*_`]*reported\s*:", ln, re.I)]
    if not found:
        return
    text = re.sub(r"^[*_`]*reported\s*:[*_`]*\s*", "", found[-1], flags=re.I).strip()
    if not text or re.fullmatch(r"nothing( new)?\.?", text, re.I):
        return "nothing new"
    d = os.path.join(os.path.expanduser("~"), ".tmux", "claude-sidebar-notes")
    path = os.path.join(d, "%s.txt" % re.sub(r"[^A-Za-z0-9_-]", "", tid))
    try:
        os.makedirs(d, exist_ok=True)
        try:
            with open(path) as f:
                old = f.read().splitlines()
        except OSError:
            old = []
        old.append("%s %s" % (time.strftime("%Y-%m-%d %H:%M"), " ".join(text.split())[:300]))
        with open(path, "w") as f:
            f.write("\n".join(old[-200:]) + "\n")
    except OSError:
        pass


def last_reply_from_transcript(path):
    """The text of the last assistant message, if the hook did not send it."""
    text = ""
    try:
        with open(path, "rb") as f:
            for raw in f:
                if b'"assistant"' not in raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "assistant":
                    continue
                content = (obj.get("message") or {}).get("content")
                if isinstance(content, list):
                    parts = [c.get("text", "") for c in content
                             if isinstance(c, dict) and c.get("type") == "text"]
                    if any(p.strip() for p in parts):
                        text = "\n".join(parts)
    except OSError:
        pass
    return text


def quick_guess(reply):
    """A question at the end of the reply means it is waiting for you."""
    tail = reply.strip()[-300:]
    last = [ln for ln in tail.splitlines() if ln.strip()][-1:] or [""]
    if last[0].rstrip(" *_`)").endswith("?"):
        return "followup", "ends with a question"
    return None


def ask_claude(prompt, reply):
    exe = shutil.which("claude")
    if not exe:
        return None
    if len(reply) > MAX_CHARS:
        # The end matters most: that is where questions and next steps are.
        reply = reply[:1500] + "\n[...]\n" + reply[-(MAX_CHARS - 1500):]
    msg = ("Developer's last prompt:\n%s\n\nClaude's final message:\n%s"
           % (prompt[:2000] or "(unknown)", reply))
    env = dict(os.environ)
    # Not a session to track: our hooks do nothing without TMUX_PANE.
    for k in ("TMUX_PANE", "TMUX"):
        env.pop(k, None)
    try:
        r = subprocess.run(
            [exe, "-p", "--model", "haiku", "--no-session-persistence",
             "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
             "--settings", '{"disableAllHooks": true}',
             "--system-prompt", SYSTEM, msg],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, timeout=TIMEOUT, cwd=os.path.expanduser("~"))
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout.decode("utf-8", "replace")
    m = re.search(r"\b(WRAPPED|FOLLOWUP)\s*:\s*(.*)", out)
    if not m:
        return None
    verdict = "wrapped" if m.group(1) == "WRAPPED" else "followup"
    return verdict, m.group(2).strip().strip('"').rstrip(".")


def main():
    if len(sys.argv) != 3:
        return
    pane, turn = sys.argv[1], sys.argv[2]
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    reply = str(data.get("last_assistant_message") or "")
    if not reply.strip() and data.get("transcript_path"):
        reply = last_reply_from_transcript(str(data["transcript_path"]))
    if not reply.strip():
        return
    quiet = remember_report(pane, reply) == "nothing new"
    guess = quick_guess(reply)
    if guess:
        write(pane, turn, *guess)
        ring_if_scheduled(pane, turn)
    if tmux("show", "-gqv", "@claude_sidebar_judge") == "off":
        return
    prompt = tmux("display", "-p", "-t", pane, "#{@claude_prompt}")
    if tmux("display", "-p", "-t", pane, "#{@claude_sched}"):
        # Wording like "every hour" is the schedule's job, not pending work.
        prompt = ("(This turn was one automatic run of a scheduled task; the "
                  "scheduler repeats it, so judge only whether this run's result "
                  "needs the developer to act.)\n" + prompt)
    verdict = ask_claude(prompt, reply)
    if verdict:
        write(pane, turn, *verdict)
        ring_if_scheduled(pane, turn)
        if quiet and verdict[0] == "wrapped":
            # A scheduled run with nothing new: no need to make it unread.
            tmux("if-shell", "-F", "-t", pane,
                 "#{&&:#{==:#{@claude_turn},%s},#{==:#{@claude_state},review}}" % turn,
                 "set -w -t %s @claude_state done" % pane)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
