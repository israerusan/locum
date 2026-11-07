# Changelog

All notable changes to locum. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/) as described in
[docs/API.md](docs/API.md) (what is public, what a breaking change is, and how
deprecations work). Every release also has a GitHub release with the same notes.

Entries for 0.1.0 to 0.2.0 were reconstructed afterwards from those GitHub
releases, which were the only record at the time. Those releases did not follow
the versioning rules in docs/API.md (0.1.7 broke the CLI in a patch release).
The rules apply from 0.3.0 on.

## [Unreleased]

### Security

- **Confirmation callbacks could receive a password in plain text.** When an
  action needed a human confirmation, `details["args"]` was scrubbed of
  registered secrets only. Text typed into a password field is not a
  registered secret, so it reached the callback verbatim. The raw context was
  also merged into `details` (window title, target, description, the target
  node), and registered secrets in those fields passed through too. Affected:
  browser password entry (`browser_type`), `GuardedDesktop`/`GuardedElement`
  input, `DesktopToolkit` `type_text`/`set_value`, and computer use. Now the
  policy marks password-field values sensitive before it judges the action
  (from the target node, and from each gate's own password check). The whole
  callback payload is scrubbed: sensitive values read `***` in `args`,
  `description`, `reason` and every context field. Text after a Tab or newline
  is masked (its field is not known yet). `target_node` is a read-only view
  whose `value` is `***` on a password field, and `details["sensitive"]` says
  whether anything was masked. An error raised by the callback is no longer
  chained into the `PolicyViolation`, because it could carry the payload. New
  context keys `sensitive_args` and `password_field` let integrations that
  call `policy.check` directly mark their own arguments.

### Documentation

- The README has a new **Limitations and known gaps** section. It covers alpha
  status and pinning for financial or production use, how deeply each platform
  and bridge has been validated, no orchestrator or multi-machine queue, when
  typed text needs verification, and that this is a solo project built with
  heavy AI assistance.
- Corrected two stale README statements: the status block said "0.1 alpha",
  and the bridges section said real JVM runs were "still to come". The
  platform table now has a Citrix/RDP row.

## [0.4.1] - 2026-11-07

A full review of 0.4.0 by seven independent reviewers found the defects below.
Each one was reproduced first and now has a regression test that fails on
0.4.0. The worst: with a policy on (the default for Robot and flows), input
could reach a window other than the target, and `press()` clicked an element
before the policy saw it.

### Breaking

These tighten safety behaviour, which docs/API.md allows in a patch release.

- A flow's `exempt:` rules must name a specific control by an exact name,
  text or automation id (`'button "Save"'`, `[automation_id="..."]`, `#id`).
  `"*"`, a bare role, a wildcard, or a substring/prefix/regex/`:has-text`
  test is rejected as "too broad". **Migration:** name the control exactly.
- The shortcut tripwire counts modifiers that are already held:
  `hold("ctrl")` + `press("s")` is checked as Ctrl+S, and `key_down`/`hold`
  treat every Enter key (keypad Enter included) as Enter.
- Self-healing refuses a candidate whose name negates or cancels the target
  ("Save" → "Don't Save", "Order" → "Cancel order"; English, German, Spanish,
  French), and added or missing words no longer earn a similarity bonus.
  Some heals that used to succeed are now refused. **Migration:** fix the
  selector instead of relying on the heal.
- Credential names are case-insensitive on every store and are reported in
  lower case by `Credential.name` and `list_credentials()`. Existing
  mixed-case keyring entries are still found.
- `locum.compat.pyautogui` no longer changes the fail-safe of a desktop passed
  to `set_desktop()`. `FAILSAFE_POINTS` add to its corners, and its own
  settings come back when it is replaced.
- Java bridge: `set_text()` longer than the bridge's 1023-character limit
  raises `ValueError`, and `JavaBridge.window(title, timeout>0)` raises
  `WindowNotFound` instead of `ActionTimeout`.
- pytest trace filenames always end in a hash of the test id.

### Added

- `ComputerUseAdapter.tool_version(model)`. `tool_definition(model=)` returns
  the computer tool and beta header that the model supports, and raises a
  clear error for models without computer use.
- `Agent.keep_images` (default 3): older screenshots are replaced by a short
  note so long computer-use runs stay under the API's image limits. This is
  off for models whose thinking depends on unedited history (the Claude 5
  family), as Anthropic's docs require.

### Fixed

Safety and input:

- A guarded element's `press()`/`type()` is bound to its window like the
  unguarded call. Keys and text after a Tab no longer reach a window that
  took the focus; `TargetLost` is raised instead.
- A guarded element's `press()` no longer clicks the element to focus it
  before the policy check. That click is checked like any click on it.
- Bound clicks check where the cursor really is, and bound drags check the
  drop before releasing.
- The keyword tripwire sees through soft hyphens, zero-width characters and
  `&` mnemonics ("De&lete").
- Unicode typing sends long text in chunks of 32 characters and re-checks the
  target window and the fail-safe between them.

Secrets and the credential vault:

- Windows: names differing only in case no longer create hidden duplicate
  credentials. Writes are read back and checked, and delete removes every
  variant.
- Verification errors no longer show a near-copy of a typed secret (decided
  by whether the typed text was a `Secret`, not by whether it contains one).
- Observations (what an LLM sees), `Element` repr and tool/MCP results: a
  field value equal to a secret is masked, and secrets of 8+ characters are
  scrubbed from names, titles and tool results (JSON stays valid). Short
  secrets such as PINs never change the numbers, titles or selectors the
  model reads. A field value that is a secret shown with separators
  (`4111 1111 1111 1111`) is masked too, and so is the value `read_text`
  returns. `last_verification` masks secrets.
- Robot log messages and step errors are scrubbed. `scrubbing_errors` scrubs
  arguments and OSError filenames along the cause/context chain without
  changing the exception's type. Traces, run reports, robot logs and CLI
  errors scrub exceptions with a custom `__str__`.
- Input after a policy check in the middle of an action (after a Tab, before
  a focusing click) stops with `TargetLost` if the focus moved meanwhile.
  The desktop input lock stays held through the check, also while a human
  confirms, so other workers wait instead of typing between the steps.
- Traces scrub before truncating, so a cut can't leave part of a secret.
- The env credential fallback also applies when keyring has no usable
  backend (headless CI).
- `${credential:x:-default}` falls back only when the credential is missing,
  not on store errors.
- HTML run reports no longer corrupt embedded screenshots.
- The keyring name index survives concurrent writers. An over-long Windows
  username gives a clear error.

Windows:

- Backend calls from any thread use per-monitor DPI coordinates, also when
  the process awareness was set beforehand: the thread is switched for the
  call and switched back afterwards. DWM frame bounds are converted on
  threads that are not DPI aware.
- `dpi_aware=False` is no longer undone by the first screenshot (mss made the
  process DPI aware); auto mode uses GDI there.
- `Window.move()`/`resize()` on a minimized window keep its normal size and
  position instead of the parked caption rect.
- `Window.exists`/`refresh()`/`wait_closed()` find untitled, tool, cloaked
  and other-virtual-desktop windows by handle instead of reporting them
  closed.
- `capture_window(restore_minimized=True)` keeps a maximized window restoring
  to maximized.
- A PrintWindow capture with a black client area counts as a failure.
- `wait_until(interval=0)` no longer busy-spins.
- Bare-string selectors read an element's value only when its name doesn't
  match, which is much cheaper on UIA.

MCP, agent and browser:

- MCP `wait` (and every `Desktop.sleep`) stops when the call times out or is
  cancelled instead of blocking the server's worker.
- A cancelled MCP request no longer gets a late timeout response.
- Unknown arguments to built-in tools return "Unexpected argument(s) …" with
  the valid ones instead of a Python `TypeError`.
- `Page.navigate` raises `CDPError` when navigation fails, and waits for the
  new document so the next observe sees the new page.
- The DevTools WebSocket handshake brackets IPv6 hosts.

RPA and tooling:

- `WorkQueue.export_csv` neutralises cells starting with `=`, `+`, `-`, `@`,
  tab or CR (CSV formula injection), except plain numbers such as `-12.50`.
- `locum run --dry-run` reports checks of simulated input as skipped instead
  of failing the run.
- pytest traces of parametrized tests no longer overwrite each other.
- `python -m locum.selftest` with `LOCUM_BACKEND=mock` uses the simulated
  editor. The pytest plugin accepts `demo` like the CLI.

Experimental bridges and backends:

- Java bridge: repeated lookups on JTable/JList/JTree no longer pin a new JVM
  reference per cell (Swing creates new cell objects on every read).
  Children there are kept by index, and replaced children are released at
  once.
- Java bridge: held elements stay valid when their panel is rebuilt, and
  children that went away are released unless the caller still holds them.
- Java bridge: JTable cells read their own data. Over the Access Bridge every
  cell is one shared renderer object that holds the data of the cell fetched
  last (found on a real JVM), so each cell is now read as it is fetched, and
  read again by fetching its index.
- Java bridge: `click()` works when the Swing click action name is localised
  ("Klicken", "cliquer"). The bridge DLL is found on `PATH`.
- SAP: table-control rows that need a scroll work (the table is found again
  by its stored id, not through the dead COM object). Reading off-screen rows
  works under dry-run and allow-lists. ALV `rows()` reads the counts once per
  page instead of about 4 COM calls per cell. Columns without a `Name` match
  by title or first-cell name.
- Wayland: pointer moves land correctly on GNOME with scaled monitors, and
  are refused when the scale is unknown.
- Wayland: `install_kill_hotkey()` warns that it only works in XWayland
  windows, and the docs no longer present it as an emergency stop there.
- Wayland: portal responses are accepted only from xdg-desktop-portal.
  Screenshot files are deleted only after decoding and only when they are
  the portal's own.
- Wayland: an open approval dialog no longer blocks `release_all()`/`close()`
  from other threads, and `abort()` ends the wait.
- Wayland: fallback window handles no longer point to a sibling window, a
  failed `AddMatch` no longer leaks a filter, and `release_all()` releases in
  reverse order.

Hardening pass before release:

- Bound scrolls check where the cursor really is, like clicks.
- The keyword tripwire also sees through the combining grapheme joiner and
  variation selectors.
- A guarded mouse release is never refused by the action budget or rate
  limit, which used to leave the button held down.
- Windows: element bounds read on another thread than the one that created
  the backend use per-monitor DPI coordinates, so clicks no longer miss.
- `Window.move()`/`resize()` check their arguments before restoring a
  minimized window, and a window handle that Windows reused for another
  process's window counts as closed.
- Windows credentials named like "Straße" by 0.4.0 are found and deleted
  again (names are lower-cased, matching how Windows compares them).
- A short secret (a 6-digit PIN) no longer masks a value that spells it with
  a separator, such as the amount "4821.50".
- Scrub patterns no longer pile up in the `re` module's cache over a long
  run (memory growth, and copies of the secrets).
- `Tracer(redact=...)` scrubs values that appear quoted or escaped in
  arguments and errors (a password with a backslash).
- Tool results in JSON no longer carry a long secret in a key.
- Java bridge: an element whose Java object was released (its row removed,
  `release()`, `release_all()` or `close()`) never passes the freed
  reference to the JVM; reads return empty and actions raise
  `ElementNotFound`. A held JTable cell of a removed row no longer reads
  another cell's data.
- Self-healing no longer takes the French/Spanish article "un" for a
  negation; "Un-lock" and "De-select" are still refused.
- A custom policy pattern with a combining mark inside a character class
  compiles again (folding had turned it into an invalid regex).

### Tests

- The real-JVM CI job covers a JTable, a JList and a JTree and checks that
  the reference count stays flat.

## [0.4.0] - 2026-11-06

### Breaking

- **The Wayland backend is no longer chosen automatically.** In 0.3.0 a
  Wayland session with no X server got the experimental `wayland` backend. Now
  every Linux session gets `x11`. Without XWayland, `x11` fails with a
  `BackendUnavailable` that names the opt-in. **Migration:** set
  `LOCUM_BACKEND=wayland`.

### Experimental

These components have never been run against the real system they drive. They
are now marked as such in code, not only in docs (docs/API.md, "Experimental").

- `locum.bridges.sap` is experimental. It was written against SAP's
  documented scripting API and tested against a fake COM object graph only.
  No SAP GUI has run it: the acceptance job needs a self-hosted runner with SAP
  GUI, and it has not run. `SapGui()`/`attach()` emit an
  `ExperimentalWarning`.
- The Wayland backend is experimental. It has been tested against a fake D-Bus
  portal only, never on GNOME or KDE. `WaylandBackend()` emits an
  `ExperimentalWarning`.
- `locum.bridges.java` is experimental (no compatibility promise) but is no
  longer fakes-only: see **Fixed** and **Tests**.
- `tests/data/public_api.txt` lists experimental modules under a marked header.
  The snapshot shows changes to them in review, but does not freeze them.

### Added

- `locum run --allow RULE` (repeatable) runs the flow under
  `Policy.strict(allow=[...])`. Only actions a rule matches run without
  `--confirm`. Before, `--policy strict` had no way to authorize anything from
  the command line.
- `locum run` without `--policy` prints a one-line note to stderr. It says the
  default policy is a keyword tripwire that authorizes nothing, and points to
  `--allow` for flows that post, pay or delete. `--policy default`, `--json` and
  `--dry-run` do not print it.
- `locum.errors.ExperimentalWarning`.

### Fixed

- **Java bridge: stale reads.** Found by the first run against a real JVM.
  `JavaNode` cached its context info and child list forever. So after the UI
  changed, `win.find(...)` and properties of elements already held reported
  the state from the first read: a checkbox a click had just checked read as
  unchecked, a label kept its old text, and a disabled button still looked
  enabled to `click()`. Lookups (`find`, `find_all`, `exists`, `tree`,
  `walk`, `children`) and every property read and action now re-read from the
  JVM. Unchanged children keep their node, so held elements stay valid. The
  fake-DLL unit tests passed with the bug because nothing changed under a
  cached node; three regression tests now do that.
- **Java bridge: reference leak in `find(timeout=...)`.** Each poll dropped
  the cached child lists without releasing them, so a wait pinned new Java
  objects in the target JVM on every iteration until `close()`.

### Tests

- New blocking CI job `windows-java-bridge`. It enables the Java Access Bridge
  of a Temurin JDK, compiles a Swing window (`tests/helpers/JabProbe.java`) and
  drives it through `locum.bridges.java`: tree and selectors, `set_text` with
  read-back, AccessibleAction clicks, a window-bound mouse click, checkbox
  state, and reference release (`tests/test_real_java_bridge.py`).

## [0.3.0] - 2026-11-06

### Breaking

- **Safety policy on by default for RPA.** `Robot()`, `run_flow()` and `locum run`
  now gate every action with `Policy.default()` instead of running ungated. The
  default is the keyword and shortcut tripwire. Robots and flows are unattended,
  so an action the tripwire flags is **blocked** unless a `confirm` callback is
  given. Flagged actions are clicks on Delete/Pay/Submit/Post/Supprimer/
  Überweisen…, Ctrl/Cmd+S, Ctrl/Cmd+Enter, and activating a control that
  cannot be identified. Launch options `cwd=`/`env=` are refused unless allowed.
  The `PolicyViolation` message says how to let the action run.
  **Migration:** exempt the intended targets
  (`Robot(policy=Policy.default(exempt=['button "Delete row"']))`, `exempt:` in
  the flow, `locum run --exempt ...`) or pass `confirm=`. To restore the 0.2
  behaviour, opt out explicitly with `policy=Policy.off()` (or `policy=None`)
  or `locum run --no-policy`.
- `RunReport.metadata["policy"]` now holds the preset name (`default` /
  `strict` / `permissive` / `custom` / `off`). The mode moved to
  `metadata["policy_mode"]`.
- `Window.screenshot()` of a partly off-screen window now returns the whole
  window (captured from the window itself) instead of the clipped screen area.
  `mode="screen"` gives the old result.

### Added

- **Selectors: sibling combinators.** `A + B` matches a B right after an A
  under the same parent. `A ~ B` matches any later sibling B. Both compose with
  the other combinators and pseudo-classes
  (`window "Calc" > pane text "Name:" + edit`). `~=` is still the regex
  attribute operator.
- **Covered and minimized windows can be captured.**
  `Window.screenshot(mode="auto"|"screen"|"window", restore_minimized=False)`
  uses `PrintWindow(PW_RENDERFULLCONTENT)` on Windows, `CGWindowListCreateImage`
  on macOS and the Composite pixmap on X11, through the new
  `Backend.capture_window` and `CAP_WINDOW_CAPTURE`. `restore_minimized=True`
  shows a minimized window without activating it, captures it, and minimizes it
  again (Windows). `observe(window=...)` of a covered window shows its real
  content. `locum screenshot --window TITLE [--mode] [--restore-minimized]`.
- **Bridges for UIs that UI Automation can't see into** ([docs/BRIDGES.md](docs/BRIDGES.md)):
  - `locum.bridges.sap` (`pip install locum[sap]`): SAP GUI Scripting over COM.
    `attach()` by system/client/user/connection/session and refuses ambiguous
    matches. `find(id)` returns typed wrappers (text fields, buttons, combo
    boxes, check/radio buttons, tabs, table controls, ALV grids, status bar).
    Also `send_vkey`, `start_transaction`, busy waits, `check_status()` →
    `SapError`, and locum selectors over SAP screens. Clear errors when
    scripting is disabled on the PC or by `sapgui/user_scripting`.
  - `locum.bridges.java`: Java Access Bridge via ctypes
    (`WindowsAccessBridge-64/-32.dll`). Swing/AWT trees work as locum
    accessibility nodes (selectors, `find`/`find_all`). `click()` uses
    AccessibleAction or a window-bound mouse click. `set_text` reads back; Java
    object references are released automatically.
  - `locum.bridges.remote.ImageSession`: template matching and OCR confined to a
    Citrix/RDP/Horizon window, with input bound to it.
  - Every acting bridge call honours the fail-safe and goes through `Policy`
    (confirmation, allow and exempt rules, dry run, audit).
- **Native Wayland backend (experimental, not yet validated on a real
  compositor).** `LOCUM_BACKEND=wayland` (`pip install locum[wayland]`) drives
  Wayland through xdg-desktop-portal. RemoteDesktop + ScreenCast handle keyboard
  and absolute pointer input (one approval; the restore token is kept in
  `$XDG_STATE_HOME/locum/wayland-portal.json`). The Screenshot portal handles
  capture, and AT-SPI handles windows and elements. It is chosen automatically
  only in a Wayland session with no X server. With XWayland available, the
  tested `x11` backend stays the default. Window activate/move/close raise
  `NotSupported`. Input bound to a window is refused when its destination
  cannot be verified. GNOME and KDE portals; xdg-desktop-portal-wlr has no
  RemoteDesktop (screenshots only).
- `Policy.default()`, `Policy.off()`, `Policy(exempt=[...])`, `Policy.preset`.
  Flows can declare a top-level `exempt:` list. `locum run --policy
  default|strict|permissive|off`, `--no-policy`, `--exempt RULE`, `--confirm`,
  `--dry-run`, `--audit FILE`.
- The keyword tripwire covers English, German, French, Spanish, Portuguese,
  Italian and Dutch, matched accent-insensitively.
- `python -m locum.selftest --strict` (a skipped check fails the run) and
  `--retries N`. New self-test check: window capture. The window-list check now
  requires the launched editor to be listed.
- `locum info` lists backend limitations and the `jeepney` optional dependency.
- [docs/API.md](docs/API.md): what is public, what counts as breaking, the
  deprecation rule, and a snapshot test (`tests/data/public_api.txt`) that fails
  CI on any unreviewed public API change. This changelog.

- **Credential vault** `locum.rpa.credentials`: Windows Credential Manager
  (ctypes), `keyring` on macOS/Linux (`pip install locum[credentials]`), and
  an env-var fallback only when explicitly enabled. Passwords are `Secret`
  values: `***` in str/repr, and scrubbed from traces, audit logs, run reports,
  confirmation prompts and CLI output. Flows reference
  `${credential:NAME.password}` / `.username`.
  `locum credential set|delete|list` (list shows names only).
- `locum a11y-audit --window W --tab-walk [--reverse] [--max-tabs N]`
  (opt-in, sends keys). It presses only Tab (and Shift+Tab) in that window,
  bound to it under the desktop input lock and through the policy, and records
  the focus order. It reports `tab-trap`, `tab-escape`, `tab-focus-lost`,
  `tab-focus-invisible`, `tab-focus-unnamed`, `tab-unreachable`,
  `tab-order-asymmetric` and `tab-order-backwards`, then restores the initial
  focus. Python: `audit(..., tab_walk=True)` / `locum.a11y.tab_walk()`.
- `locum.compat.pyautogui.LOG_SCREENSHOTS` and `LOG_SCREENSHOTS_LIMIT` now work
  like PyAutoGUI: a screenshot before each action, the newest 10 kept in the
  current directory. Before, the flag was accepted and ignored.

### Fixed

- **Linux: every text field read as empty.** On current libatspi/PyGObject,
  `Atspi.Accessible.get_text()` is a deprecated zero-argument alias of
  `get_text_iface()`. It shadowed `Atspi.Text.get_text(start, end)`, so
  `Element.value`/`.text` returned `None`. Verified typing, `set_text`
  verification and the self-test all failed on a real AT-SPI desktop. Found by
  the new Linux real-desktop CI job.
- A mouse move on a backend that cannot read the pointer position jumps to the
  target instead of failing.

### CI

- The real-desktop self-tests are **blocking** on all three OSes, run with
  `--strict --retries 2`: Windows (hosted runner desktop), macOS (hosted
  runner: Quartz, AX, TextEdit) and Linux (Xvfb + openbox + real D-Bus/AT-SPI
  registry + GTK mousepad). Before, Windows was `continue-on-error`, and macOS
  and Linux had no real-desktop job.


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
[Unreleased]: https://github.com/israerusan/locum/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/israerusan/locum/releases/tag/v0.4.1
[0.4.0]: https://github.com/israerusan/locum/releases/tag/v0.4.0
[0.3.0]: https://github.com/israerusan/locum/releases/tag/v0.3.0
