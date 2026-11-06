# Changelog

All notable changes to locum. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/) as described in
[docs/API.md](docs/API.md) (what is public, what a breaking change is, and how
deprecations work). Every release also has a GitHub release with the same notes.

Entries for 0.1.0 to 0.2.0 were reconstructed from those GitHub releases, which
were the only record at the time.

## [Unreleased]

<!-- UNRELEASED -->


## [0.2.0] - 2026-11-06

### Financial-grade verification
- `set_text` / `type` take `verify="exact"` (document numbers: `0001234` is not `1234`) and `verify="amount"` (one decimal separator via `decimal=` or `Desktop(decimal_separator=...)`, compared as `Decimal`; SAP `1.250,00-` understood).
- **Behaviour change:** the default (`verify=True`) no longer treats locale-ambiguous numbers as equal — `"1,000"` no longer matches `"1"` or `"1.00"` without a known separator.
- Every call records `Element.last_verification` (`verified` / `unverified` / `skipped`). An unreadable or password field is never reported verified; the strict modes raise `VerificationUnavailable` (`type()` before sending a key).
- Flows: `set_text` accepts `verify` and `decimal`.

### Desktop input lock
- One owner of mouse / keyboard / focus per session, across processes (named mutex on Windows, `flock` on POSIX; released if the owner dies).
- Whole `Element` actions and every `Keyboard`/`Mouse` call hold it; native set-value/invoke and key/button releases do not.
- `DesktopBusy` after `Desktop(input_lock_timeout=30)`; `locum.desktop_input(d)`, `locum.try_desktop_input(d)`; `LOCUM_DESKTOP_LOCK=0` disables.

### Work queues
- `WorkQueue` refuses paths in OneDrive / Dropbox / Google Drive / iCloud / Box folders and network shares (`allow_synced=True` overrides).
- `locum.rpa.default_queue_dir()` and `WorkQueue.export(path)` to archive finished state into a synced folder.

### Real-application acceptance
- `tests/acceptance/`: opt-in Excel dialog scenarios (save prompt, overwrite prompt, Save As exact file name — native invoke only, private Excel instance) and a read-only SAP GUI SE16N export. See docs/TESTING.md.
- CI: focus-steal and input-lock tests are a blocking real-desktop job; on-demand self-hosted acceptance job.

## [0.1.7] - 2026-11-06

### Breaking: CLI input needs a target

- `type`, `press` and `hotkey` need `--window TITLE` (or `--into SELECTOR` for `type`), or an explicit `--focused`.
- Clicks on coordinates or images need `--window TITLE` or `--anywhere`; `locate --click` needs `--anywhere`.
- pytest plugin fixtures are now `locum_desktop` / `locum_app` (`--locum-aliases` restores `desktop` / `app`).

### Input target binding

- Every key press, typed text chunk, click and scroll is checked right before it is sent.
  If another window took the focus (or covers the click point), nothing more is sent: `TargetLost`.
- Element actions are bound to the element's window and focus; the LLM tools (MCP, agent, computer use) bind each input to the window the policy judged.
- Real focus-steal tests run on the CI Windows desktop.

### Privacy and output

- `observe --window` shows that window only: no other titles; covering windows are blanked out of the screenshot.
- `--json`: exactly one JSON document on stdout, also on failure (`ok: false` plus the error); non-ASCII escaped unless stdout is UTF-8.
- The CLI no longer crashes on legacy console code pages (e.g. a braille spinner in a window title).

### Review fixes

- browser_type submit: an Enter target it can't identify (cross-origin iframe card fields, no button) now asks for confirmation; the page script follows focus into shadow roots and same-origin frames.
- Recorder: a secret typing burst is never downgraded; lagging live capture fails closed.
- Cancel checks before all input, element-id reuse, websocket stalls, local-only DevTools attach, Store-app observe reuse, ~25x faster selector generation with stable window titles, trace secret masking, and more.

## [0.1.6] - 2026-11-06

The rest of the 2026-11-05 live-session list (P2 and P3).

### Trace viewer (`locum.trace`)
- `with trace(desktop, "run.html"):` records every action and lookup: selector, element, bounds, arguments (passwords masked), result or error, a screenshot with the target highlighted, and log notes such as self-heals.
- Output is one self-contained HTML timeline, or JSON.

### pytest plugin (installed automatically)
- `desktop` and `app("calc.exe", window="Calculator")` fixtures.
- `--locum-backend mock` for CI.
- `--locum-trace failed` (the default) saves the trace of a failing test and puts its path in the report. See docs/TESTING.md.

### Self-repairing selectors (`locum.heal`, opt-in)
- Turn on with `Desktop(heal_selectors=True)`, `find(..., heal=True)` or `heal:` in flows.
- When a selector stops matching, it accepts one clear winner by role, name similarity, automation id, window and position. Ambiguous matches fail.
- Every heal is logged with what changed.
- LLM tools never heal. See docs/HEALING.md.

### Recorder
- `locum record` builds selectors with a session-wide cache: about 0.1–0.5 s per click after the first, instead of 3–6 s on a busy desktop.
- `--help` is documented, plus new `--no-selectors` and `--name` options.

### Accessibility audit
- `locum a11y-audit [WINDOW]` flags unnamed controls, unlabeled inputs, controls the keyboard can't focus, small targets (WCAG 2.5.8) and duplicate names.
- `--json` and `--fail-on` options. See docs/A11Y.md.

### Demos
- `examples/calculator_selectors.py` and `examples/paint_drawing.py` run on the real Windows 11 apps, or with `--backend mock`.
- The README GIF is recorded on a real desktop with `tools/make_demo_gif.py --real`.

## [0.1.5] - 2026-11-06

The P1 items from the 2026-11-05 live MCP session report.

**Changed-only observe**: `observe(changes_only=true)`
- After an action, returns only new, changed (with the old value) and gone elements, plus windows opened or closed.
- Elements that are still there keep their ids, and new elements never reuse an id.
- The screenshot is cropped to the visible change. No image is sent when nothing visible changed, and the full one when most of the screen did.
- Measured on a busy 1080p desktop: ~1–2 KB of text and a ~160 KB crop, instead of ~24 KB and a 1.5 MB screenshot.

### Event-driven waits (Windows)
- Waits for elements and windows wake on UI Automation focus, structure and property events, instead of re-searching the tree every 0.1 s. They fall back to a 0.5 s poll and are never faster than `poll_interval`.
- `observe` reuses the elements of windows whose app raised no event since the previous call.
- `LOCUM_UIA_EVENTS=0` turns events off.

### Browser pages over DevTools (Edge / Chrome)
- New `browser_launch`, `browser_pages`, `browser_observe`, `browser_click`, `browser_type`, `browser_navigate` and `browser_read` tools, and `locum.browser.Browser` in Python. Standard library only.
- Two ways to connect: attach to a browser that already exposes a DevTools port, or start Edge or Chrome with a separate, locum-managed profile. Your everyday profile is never touched.
- The browser tools go through the safety policy: app allow-lists, confirmations, dry-run and password redaction.
- While a browser is connected, `observe` skips that window's page content and points to `browser_observe`.

Setup and details: docs/MCP.md.

## [0.1.4] - 2026-11-06

Correctness release: two independent bug-hunt rounds (78 reproduced bugs fixed, each with a regression test) plus the P0 items from the 2026-11-05 live MCP session.

### MCP
- `python -m locum.mcp` and a `locum-mcp` command start the server (same options as `locum mcp`). Setup guide: docs/MCP.md.
- Tool calls run on a worker thread: `ping`, `tools/list` and cancellation are answered while a long call runs.
- Per-call time limit (`--call-timeout`, default 60 s): an overrunning call returns a clear timeout error and stops; the server keeps serving. `notifications/cancelled` and `notifications/progress` are supported.
- `observe` / `screenshot` are time-bounded (8 s) and return partial results naming the windows they could not finish; one huge app can no longer stall them.

### Safety
- Guarded clicks with an offset are judged at the point actually pressed.
- Passwords are redacted from `read_text` and from text typed after a Tab, in results and audit logs.
- `launch_app` policy checks the program that will actually run (unquoted paths with spaces).
- Action budgets are enforced atomically.

### Windows
- Input to an elevated window raises a clear error (integrity levels, UIPI) instead of vanishing.
- Unquoted program paths with spaces, shifted dead keys and `ShellProcess.wait(0)` fixed.

### Also fixed
- Selectors: no cross-window collisions; stale caches after window renames.
- Windows: a closed window never resolves to a same-titled other window; `wait_closed` error handling; `move`/`resize` with a stale rect.
- RPA: retry/abort/teardown semantics; `${var}` in step options; CSV export columns.
- Keys: key names like "Num Add" / "Left Shift"; incomplete combos rejected.
- Self-test: an aborted run no longer reports success.
- Fixes in the pyautogui compatibility layer, the macOS / X11 / mock backends, and vision.

macOS changes from 0.1.2 and 0.1.4 are tested against fakes; please confirm with `python -m locum selftest` on a real Mac.

## [0.1.3] - 2026-11-06

- `find_elements` and `wait_for` now describe a re-found element as it is now, not as the last `observe` saw it (e.g. Calculator's display no longer printed as "Display is 0" after it changed). The element keeps its id; a selector built from a changed name is rebuilt on next use.

## [0.1.2] - 2026-11-06

- MCP: `screenshot` / `observe` no longer hang on busy desktops (lazy selectors, window-scoped uniqueness; ~2-4 s instead of minutes). `find_elements` caps selector work at a 3 s budget.
- Self-test saves and restores the full clipboard on every platform; skips the clipboard check if it cannot save it.
- macOS: fail-closed clipboard snapshot for clipboard-mode typing, cursor cache that yields to real moves, app names with arguments/documents launch correctly.
- Launch-option gate (`shell`, `executable`, `preexec_fn` forbidden; `cwd`/`env` opt-in via Policy); X11 launch keeps DISPLAY/XAUTHORITY; finite-timeout validation.

## [0.1.1] - 2026-11-05

```
pip install --upgrade locum
```

### Security
- **Launch allow-lists could be bypassed.** `Policy.strict(allow=["launch=notepad.exe"])` combined with `DesktopToolkit(launch_kwargs={"shell": True})` let `launch_app("notepad.exe & powershell ...")` run PowerShell; `GuardedDesktop.launch` also forwarded `shell=` / `executable=` unchecked. Guarded launches now accept only options that cannot change which program runs. `shell`, `executable` and `preexec_fn` are always refused, and `cwd`/`env` need `Policy(launch_options=[...])`. **Behaviour change:** integrations that passed `cwd`/`env` through `launch_kwargs` or a guarded launch must now opt in.

### macOS (first run on a real Mac: Apple Silicon, Retina, macOS 26; the self-test now passes 16/16)
- Clicks right after a move no longer land at the old cursor position.
- Minimized windows can be restored, moved and closed.
- `launch("Mail")`, `"Calendar"` and others open the app, not the same-named command-line tool.
- Retina screenshots are captured at full resolution instead of 1x upscaled.
- Clipboard-mode typing restores every pasteboard type (images, files, rich text), not only text.
- Reading text-range attributes no longer raises; capslock works; unstable Interface Builder ids are no longer used as automation ids.

### Linux / X11
- `launch(..., env=...)` no longer drops `DISPLAY` (the app could not open a window); `DISPLAY` and `XAUTHORITY` are added when missing.

### Reliability
- `timeout=inf` (and infinite poll intervals) are rejected instead of waiting forever; `None` still means "wait forever".

### Internals
- The Windows, macOS and X11 backends are now packages of focused modules (pure move, verified byte-for-byte; every existing import still works).
- The version is derived from the git tag: a build that is not exactly this release can no longer report itself as one.

## [0.1.0] - 2026-11-05

First public release (alpha — APIs may still change).

```
pip install locum            # core
pip install "locum[all]"     # + Claude agent, OCR, recorder, YAML flows
```

**What it is:** desktop automation for Python — mouse/keyboard input, screenshots and template matching, accessibility selectors (UI Automation / AX / AT-SPI), RPA robots, work queues and YAML flows, and LLM-agent control (Claude agent loop, computer-use adapter, MCP server) behind a safety policy.

### Platform support
- **Windows** — UI Automation backend; validated on a real Windows 11 machine and on GitHub's Windows runner by `python -m locum.selftest`.
- **macOS** — Quartz/AX backend; unit-tested on macOS against a fake API, not yet validated against real apps.
- **Linux** — X11/XWayland (XTEST, AT-SPI), tested under Xvfb. Native Wayland input/capture is not supported.

### Highlights
- Verified typing: `Desktop.type` / `Element.type` read text fields back and repair keystrokes an app dropped or garbled; autocomplete and input masks are left alone.
- Typing is refused unless keyboard focus is proven.
- Safety: allow-list (default-deny) authorization for consequential actions, scoped to the control actually pressed and to on-target keys; robots and flows are gated when given a policy.
- Work queues safe for several worker processes on one machine (file lock, fsync, leases).
- Non-transient errors (bugs, permissions) fail fast instead of waiting out a timeout.

Check it on your machine: `python -m locum.selftest` (opens its own Notepad tab, never types into your documents).

[0.2.0]: https://github.com/israerusan/locum/releases/tag/v0.2.0
[0.1.7]: https://github.com/israerusan/locum/releases/tag/v0.1.7
[0.1.6]: https://github.com/israerusan/locum/releases/tag/v0.1.6
[0.1.5]: https://github.com/israerusan/locum/releases/tag/v0.1.5
[0.1.4]: https://github.com/israerusan/locum/releases/tag/v0.1.4
[0.1.3]: https://github.com/israerusan/locum/releases/tag/v0.1.3
[0.1.2]: https://github.com/israerusan/locum/releases/tag/v0.1.2
[0.1.1]: https://github.com/israerusan/locum/releases/tag/v0.1.1
[0.1.0]: https://github.com/israerusan/locum/releases/tag/v0.1.0
[Unreleased]: https://github.com/israerusan/locum/compare/v0.2.0...HEAD
