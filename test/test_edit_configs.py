#!/usr/bin/env python3
"""Unit tests for lib/edit_configs.py. Uses temp dirs only.

    python3 test/test_edit_configs.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "lib", "edit_configs.py")
BLOCK = os.path.join(HERE, "..", "lib", "block.conf")

USER_CONF = """set -g mouse on
set -as terminal-features 'xterm*:extkeys'
set -g status-style bg=black
setw -g window-status-format \\
  '#I:#W'
set -g history-limit 100000
"""


class EditConfigs(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.conf = os.path.join(self.d, ".tmux.conf")
        self.sett = os.path.join(self.d, "settings.json")
        self.state = os.path.join(self.d, "state.json")

    def run_ec(self, action, claude_json=None):
        args = [sys.executable, "-I", SCRIPT, action, self.conf, self.sett]
        args += [BLOCK, self.state] if action == "install" else [self.state]
        if claude_json:
            args += ["--claude-json", claude_json]
        return subprocess.run(args, capture_output=True, text=True)

    def write(self, path, text):
        with open(path, "w", newline="") as f:
            f.write(text)

    def read(self, path):
        with open(path, newline="") as f:
            return f.read()

    def test_round_trip_restores_both_files(self):
        self.write(self.conf, USER_CONF)
        settings = {"model": "opus", "preferredNotifChannel": "iterm2",
                    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}
        self.write(self.sett, json.dumps(settings))
        self.assertEqual(self.run_ec("install").returncode, 0)
        self.assertEqual(self.run_ec("install").returncode, 0)  # idempotent
        installed = json.loads(self.read(self.sett))
        self.assertEqual(installed["preferredNotifChannel"], "terminal_bell")
        self.assertEqual(installed["hooks"]["Stop"][0]["hooks"][0]["command"], "echo mine")
        self.assertIn("# [tmux-claude-sidebar] set -g status-style", self.read(self.conf))
        r = self.run_ec("uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read(self.conf), USER_CONF)
        self.assertEqual(json.loads(self.read(self.sett)), settings)
        self.assertFalse(os.path.exists(self.state))

    def test_missing_files(self):
        self.assertEqual(self.run_ec("install").returncode, 0)
        self.assertTrue(self.read(self.conf).startswith("# >>> tmux-claude-sidebar >>>"))
        self.assertEqual(self.run_ec("uninstall").returncode, 0)
        self.assertEqual(self.read(self.conf), "")
        self.assertNotIn("preferredNotifChannel", json.loads(self.read(self.sett)))

    def test_unterminated_marker_changes_nothing(self):
        text = "set -g mouse on\n# >>> tmux-claude-sidebar >>>\nset -g history-limit 5\n"
        self.write(self.conf, text)
        self.write(self.sett, "{}")
        for action in ("install", "uninstall"):
            r = self.run_ec(action)
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(self.read(self.conf), text)
            self.assertEqual(self.read(self.sett), "{}")

    def test_invalid_json_changes_nothing(self):
        self.write(self.conf, USER_CONF)
        self.write(self.sett, "{ // comment\n}")
        self.assertNotEqual(self.run_ec("install").returncode, 0)
        self.assertEqual(self.read(self.conf), USER_CONF)

    def test_symlinks_are_kept(self):
        real = os.path.join(self.d, "dotfiles-tmux.conf")
        self.write(real, USER_CONF)
        os.symlink(real, self.conf)
        self.assertEqual(self.run_ec("install").returncode, 0)
        self.assertTrue(os.path.islink(self.conf))
        self.assertIn("tmux-claude-sidebar", self.read(real))
        self.assertEqual(self.run_ec("uninstall").returncode, 0)
        self.assertTrue(os.path.islink(self.conf))
        self.assertEqual(self.read(real), USER_CONF)

    def test_hooks_call_hook_sh(self):
        self.assertEqual(self.run_ec("install").returncode, 0)
        hooks = json.loads(self.read(self.sett))["hooks"]
        expect = {"SessionStart": "idle", "UserPromptSubmit": "prompt", "Stop": "done",
                  "StopFailure": "failed", "SessionEnd": "end", "PermissionRequest": "needs",
                  "PostToolUse": "working"}
        for event, arg in expect.items():
            cmd = hooks[event][0]["hooks"][0]["command"]
            self.assertIn('"$HOME/.tmux/sidebar/hook.sh" %s "$PPID"' % arg, cmd)
            self.assertTrue(cmd.startswith('[ -n "$TMUX_PANE" ]'), cmd)
            self.assertIn("exit 0", cmd)
        # Auto-compaction can run mid-turn: it must not reset the state.
        self.assertEqual(hooks["SessionStart"][0]["matcher"], "startup|resume|clear")

    def test_hook_is_silent_without_tmux(self):
        self.assertEqual(self.run_ec("install").returncode, 0)
        cmd = json.loads(self.read(self.sett))["hooks"]["Stop"][0]["hooks"][0]["command"]
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        env["HOME"] = self.d   # no hook.sh here either
        r = subprocess.run(["sh", "-c", cmd], input="{}", capture_output=True, text=True, env=env)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        env["TMUX_PANE"] = "%999"   # inside tmux, but hook.sh is missing
        r = subprocess.run(["sh", "-c", cmd], input="{}", capture_output=True, text=True, env=env)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_diff_panel_off_and_restored(self):
        cj = os.path.join(self.d, ".claude.json")
        self.write(cj, json.dumps({"numStartups": 3, "theme": "dark"}, indent=2))
        self.assertEqual(self.run_ec("install", cj).returncode, 0)
        self.assertIs(json.loads(self.read(cj))["diffSidebarOpen"], False)
        self.assertEqual(json.loads(self.read(cj))["numStartups"], 3)
        self.assertEqual(self.run_ec("install", cj).returncode, 0)   # idempotent
        self.assertEqual(self.run_ec("uninstall", cj).returncode, 0)
        self.assertEqual(json.loads(self.read(cj)), {"numStartups": 3, "theme": "dark"})

    def test_diff_panel_keeps_your_value(self):
        cj = os.path.join(self.d, ".claude.json")
        self.write(cj, json.dumps({"diffSidebarOpen": True}))
        self.run_ec("install", cj)
        self.assertIs(json.loads(self.read(cj))["diffSidebarOpen"], False)
        self.run_ec("uninstall", cj)
        self.assertIs(json.loads(self.read(cj))["diffSidebarOpen"], True)
        # Changed by you after install: uninstall leaves it alone.
        self.write(cj, json.dumps({}))
        self.run_ec("install", cj)
        self.write(cj, json.dumps({"diffSidebarOpen": True}))
        self.run_ec("uninstall", cj)
        self.assertIs(json.loads(self.read(cj))["diffSidebarOpen"], True)

    def test_diff_panel_on_upgrade_from_older_install(self):
        # An install from before this setting: state file without the key.
        cj = os.path.join(self.d, ".claude.json")
        self.write(cj, json.dumps({"a": 1}))
        self.write(self.sett, json.dumps({"preferredNotifChannel": "iterm2"}))
        self.run_ec("install")                  # older install, no --claude-json
        self.run_ec("install", cj)
        self.assertIs(json.loads(self.read(cj))["diffSidebarOpen"], False)
        self.assertEqual(json.loads(self.read(self.state)).get("preferredNotifChannel"), "iterm2")
        self.run_ec("uninstall", cj)
        self.assertEqual(json.loads(self.read(cj)), {"a": 1})
        self.assertEqual(json.loads(self.read(self.sett)).get("preferredNotifChannel"), "iterm2")

    def test_missing_or_bad_claude_json_is_left_alone(self):
        cj = os.path.join(self.d, ".claude.json")
        self.assertEqual(self.run_ec("install", cj).returncode, 0)
        self.assertFalse(os.path.exists(cj))
        self.write(cj, "{ not json")
        self.assertEqual(self.run_ec("install", cj).returncode, 0)
        self.assertEqual(self.read(cj), "{ not json")

    def test_crlf_file_gets_one_block(self):
        self.write(self.conf, USER_CONF.replace("\n", "\r\n"))
        self.run_ec("install")
        self.run_ec("install")
        self.assertEqual(self.read(self.conf).count("# >>> tmux-claude-sidebar >>>"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
