# tmux-claude-sidebar

A window list on the left of every tmux window, for running several Claude Code
sessions side by side (one tmux window per git worktree). Each row shows what
that session is doing, so you can see which one is waiting for you: the ones
that need you are at the top, a finished turn stays yellow until you have
looked at it, and an AI check tells you whether the work is wrapped up or you
should come back to it.

![The sidebar next to a Claude Code session. Four windows show done, needs you, done and idle, and window 2's tab in the status bar is yellow because its bell rang.](docs/screenshot.png)

## What the sidebar shows

<img align="right" width="300" src="docs/sidebar.png" alt="Close-up of the sidebar. Sorted by what needs you: a window that needs you, two windows to review (one finished and wrapped up, one asking which channel to post in), one to follow up, one working, then done and idle windows. Each window that wants attention has a second line with how long ago and the reason or your last prompt.">

From the top:

- **Session name**, and `[−]` `[+]` to make the sidebar narrower or wider.
- **Sort order** (click to switch) and `[search]`.
- **One row per window**: its number, name, Claude Code status and a `×`
  to close it. The orange bar marks the window you are in.
- **Status**:
  - `! needs you` (bold yellow): a permission prompt or a question dialog.
  - `◆ review` (yellow): Claude finished while you were not looking.
  - `↩ follow up` (magenta): you have seen it, but the turn is not wrapped up
    (Claude asked something, offered options, hit an error, or left steps
    undone).
  - `● working` (blue), `✓ done` (green, wrapped up and seen), `○ idle` (grey).
- **A second line** under windows that want attention: how long ago, and
  either why it needs a follow-up (`↩ asks whether to open a PR`), what it
  finished (`✓ tests pass, PR opened`), or your last prompt.
- **Keys**: the tmux keys you use most. Click one, or press `Ctrl-b` and
  then the key.

<br clear="right">

## Features

- **Status per window**: `working` after you send a prompt, `needs you` when
  Claude asks for permission or asks you a question, `review` when it
  finishes while you are elsewhere, `done` once you have looked, `idle` when a
  session has started but has no prompt yet (or you stopped it with Esc).
  Windows without Claude Code show no status.
- **Review until you look**: a turn that ends in a window you are not looking
  at shows `◆ review` and turns its tab in the status bar yellow. Switching to
  the window marks it `done`. "Looking" means it is the current window and the
  terminal has focus, so a turn that ends while you are in another app also
  waits for you.
- **Wrapped up, or come back to it?** When a turn ends, a short background
  call to Claude (`claude -p --model haiku`, with your existing login) reads
  your last prompt and Claude's final message and decides. A turn that needs
  you shows `↩ follow up` and stays near the top even after you have looked,
  until you send another prompt or right-click it and choose "Mark as wrapped
  up". Before that call answers (a few seconds), a reply that ends with a
  question already counts as a follow-up. It costs one small Haiku request per
  finished turn; turn it off from the right-click menu, or with
  `~/.tmux/sidebar/sidebar.sh judge off`. That call runs with hooks, tools,
  MCP servers and session saving switched off.
- **Sorted by what needs you**: `needs you`, then `review`, then `follow up`,
  then the rest by when they last changed, then windows without Claude Code.
  Click `sort:` at the top to switch to window-number order (kept after tmux
  restarts). The numbers shown are always the tmux window numbers, so
  `Ctrl-b 3` still goes to window 3.
- **Search every prompt** (`Ctrl-b F`, or click `[search]`): with nothing
  typed it lists each Claude window once with your last prompt, needs-you
  first. Type to search every prompt you have sent in every window, across
  all tmux sessions (from each session's transcript, so older prompts and
  resumed sessions are included). `Enter` goes to that window, `Tab` lists all
  prompts, `Esc` closes.
- **Right-click a window row** for a menu: switch to it, mark it reviewed or
  wrapped up (or all reviewed), move it up, down or to the top (this changes
  the tmux window numbers), rename or close it, and turn the AI check on or
  off. Right-click elsewhere still shows tmux's own menu.
- **Click a window row** to switch to it. **Click a key row** to do the same as
  typing `Ctrl-b` and that key. Clicking the sidebar never moves keyboard focus
  into it.
- **Click `×`** to close that window. tmux asks `(y/n)` first, then closes the
  window and stops everything running in it (Claude Code, the shell, and their
  child processes).
- **Width**: 36 columns by default. Click `[−]` or `[+]` at the top of the
  sidebar to make it 2 columns narrower or wider, or drag its right border.
  The new width applies to every window and is kept after tmux restarts
  (it is saved in `~/.tmux/claude-sidebar-width`).
- **`Ctrl-b S`** hides or shows the sidebar in every window of the session. New
  windows follow the current setting.
- **Window names** come from the title Claude Code gives its session, like
  Terminal tabs do. Until Claude sets a title, the window is named after its
  directory, with a leading `codez-` removed. `Ctrl-b ,` renames a window and
  the name then stays fixed.
- A permission prompt in a background window rings the terminal bell, so its
  tab in the status bar turns yellow with a `!`.

`Ctrl-b F` with nothing typed answers "what was I doing in each window?":

![The search popup listing each Claude window once with its status, how long ago, and your last prompt, needs-you first. The chosen row's full prompt is shown at the bottom.](docs/search.png)

Right-click on a window row:

![The right-click menu for window 6: switch to it, mark as reviewed, mark as wrapped up, mark all as reviewed, move up, move down, move to the top, rename, close, and turn off the AI follow-up check.](docs/menu.png)

Every default tmux key still works. The only new keys are `Ctrl-b S` and
`Ctrl-b F`.

Clicking `×` on the "PR 33067 review" row shows this prompt at the bottom.
`y` closes the window, `n` keeps it:

![tmux asks: Close "PR 33067 review" and stop everything running in it? (y/n)](docs/close-prompt.png)

## Install

Needs macOS or Linux, tmux 3.4 or newer, git, python3, and Claude Code.

```sh
git clone https://github.com/andrsnn/tmux-claude-sidebar.git
cd tmux-claude-sidebar
./install.sh
```

Then start tmux, or if it is already running, `install.sh` reloads it for you.
Restart any Claude Code sessions that were already running, so they load the
new hooks.

Running `./install.sh` again is safe; it replaces what it installed before.

### What install changes

- **Backups first**: `~/.tmux.conf` and `~/.claude/settings.json` are copied to
  `.bak-YYYYMMDD` (with a time added if that name is taken).
- **`~/.tmux/sidebar/`**: the scripts (`render.sh` draws the sidebar,
  `sidebar.sh` handles clicks and tmux hooks, `hook.sh` records Claude Code's
  state, `judge.py` is the wrapped-up check, `search.py` the search popup).
- **`~/.tmux/plugins/`**: [tmux-sensible](https://github.com/tmux-plugins/tmux-sensible)
  v3.0.0 and [catppuccin/tmux](https://github.com/catppuccin/tmux) v2.3.2,
  cloned with git and checked out at a fixed commit (no TPM). The commits are
  listed in `install.sh`.
- **`~/.tmux.conf`**: one block between `# >>> tmux-claude-sidebar >>>` and
  `# <<< tmux-claude-sidebar <<<`, at the top of the file so your own settings
  still win. It turns on `mouse`, `extended-keys` (Shift+Enter in Claude Code),
  `allow-passthrough`, and `base-index 1`. Lines of yours that would override
  the theme or the window names (`status-style`, `window-status-*-style/format`,
  `automatic-rename-format`, and `set -as terminal-features ...extkeys`, which
  the block already covers) are commented out with a `# [tmux-claude-sidebar]`
  prefix, and install lists them.
- **`~/.claude/settings.json`**: hooks that run `~/.tmux/sidebar/hook.sh` to
  record each session's state on its tmux window, merged next to your
  existing hooks. They do nothing outside tmux and never fail. Also sets `preferredNotifChannel` to `terminal_bell`;
  your previous value is saved and put back by uninstall.

If `settings.json` is not valid JSON, or `~/.tmux.conf` has a start marker
without an end marker, install stops without changing either file. Symlinked
config files (stow and similar) are edited in place and stay symlinks.

### Mouse mode

The sidebar needs tmux mouse mode. With it on, a drag selects text in tmux,
or in Claude Code, which handles the mouse itself. To select text with your
terminal instead, hold Option in iTerm2, Fn in Terminal.app, or Shift in most
other terminals while dragging. In Terminal.app you can also turn off
View > Allow Mouse Reporting.

A drag with Ctrl or Option held does the same as a plain drag. tmux 3.8 would
otherwise open a new floating pane with a shell in it (Ctrl) or move the pane
(Option).

### Fonts

No Nerd Font needed. If you switch catppuccin to its icon styles, you will need
one.

## Uninstall

```sh
./uninstall.sh
```

Removes the block, the hooks and the scripts, turns your disabled lines back
on, restores your notification setting and tmux's own right-click menu, and
closes the sidebars in a running tmux server. The plugins in `~/.tmux/plugins` are left in place.

## How it works

- Each window gets a 36-column pane on the left, marked with the pane option
  `@sidebar`. It runs `render.sh`, which reads `tmux list-windows` once a
  second and redraws when something changed.
- tmux hooks add the pane to new windows, keep it at the chosen width when the
  terminal resizes, move focus back to the main pane if you land on the
  sidebar, and close a window once only the sidebar is left in it.
- Claude Code hooks run `hook.sh`, which sets the window option
  `@claude_state` (and keeps the last prompt, the transcript path and Claude
  Code's process id on the window):

  | Claude Code event | state |
  |---|---|
  | SessionStart (startup, resume, clear) | idle |
  | UserPromptSubmit | working |
  | PermissionRequest, Notification (permission_prompt, elicitation_dialog) | needs |
  | PostToolUse, PostToolUseFailure, PermissionDenied | working |
  | Stop | review, or done if you are looking at the window |
  | StopFailure | the same, and always "follow up" |
  | SessionEnd | (cleared) |

  SessionStart after an automatic compaction is ignored, since that can
  happen in the middle of a turn.
- After Stop, `judge.py` runs in the background and sets `@claude_verdict`
  (`wrapped` or `followup`) and `@claude_note`. A verdict for a turn that a
  newer prompt has replaced is dropped.
- tmux hooks (`session-window-changed`, `client-session-changed`,
  `client-focus-in`, `client-attached`) turn `review` into `done` for the
  window a focused client is showing.
- Two checks in `render.sh` keep the status honest when no hook runs:
  - Esc fires no hook, so a `working` window whose screen has not changed for
    20 seconds is shown `idle` (a working Claude redraws its spinner every
    second, even while a long command runs).
  - If Claude Code's process is gone (killed, crashed) without a SessionEnd
    and its pane is back at a shell prompt, the status is cleared.

## Known limits

- **Esc shows "working" for up to 20 seconds.** Claude Code runs no hook when
  you interrupt it with Esc; the sidebar notices once Claude's screen stops
  changing. Only the sidebar of the window you are on does this check.
- **"needs you" lasts until the tool finishes.** After you approve a long
  command, the row says `needs you` until the command ends.
- **One status per window.** Two Claude sessions in one window share it.
- **Long names are cut** to 17 characters at the default width. Widen the
  sidebar with `[+]` to see more.
- **The AI check needs `claude` on the PATH** that Claude Code's hooks see.
  Without it you still get the quick guess (a reply that ends with a
  question is a follow-up).
- **Prompt history comes from Claude Code's transcripts.** Search shows what
  is in them; prompts from sessions started before you installed this
  version are found once those sessions are restarted.
- Shift+Enter also needs your terminal to send it as a distinct key (most do,
  including iTerm2, Ghostty, kitty and WezTerm).

## Tests

```sh
python3 test/test_edit_configs.py          # config editing, in temp dirs
python3 test/acceptance.py                 # tmux behaviour on a separate server
python3 test/acceptance.py --claude        # plus live Claude Code checks (haiku)
```

The acceptance tests use their own tmux server (`tmux -L sidebar-test`) and
attach a real client through a pty, so keys, clicks and resizes go through
tmux the same way they do from a terminal.

## Plugin review

Both plugins were read in full at the pinned commits:

- No network calls (`curl`, `wget` or similar), no `eval` of fetched content,
  and no file writes; everything goes through `tmux` commands.
- tmux-sensible v3.0.0 sets options and adds `Ctrl-b R` (reload config),
  `Ctrl-b C-p` / `C-n` (previous/next window) only if those keys are unbound.
  It also tries the `utf8` options that tmux removed, which only print errors;
  the block hides them.
- catppuccin v2.3.2 runs shell commands only from status modules you would
  add yourself (`uptime`, `whoami`, `gitmux`). This setup adds none.

## License

MIT
