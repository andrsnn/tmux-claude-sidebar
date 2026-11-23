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

    def run_ec(self, action):
        args = [sys.executable, "-I", SCRIPT, action, self.conf, self.sett]
        args += [BLOCK, self.state] if action == "install" else [self.state]
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

    def test_crlf_file_gets_one_block(self):
        self.write(self.conf, USER_CONF.replace("\n", "\r\n"))
        self.run_ec("install")
        self.run_ec("install")
        self.assertEqual(self.read(self.conf).count("# >>> tmux-claude-sidebar >>>"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
