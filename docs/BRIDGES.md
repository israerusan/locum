# Bridges: SAP GUI, Java, and remote desktops

Windows UI Automation is blind inside three kinds of enterprise UI: SAP GUI for
Windows (a few opaque panes), Java Swing/AWT applications (one pane per
window), and remote sessions such as Citrix, RDP and VMware Horizon (one
bitmap). `locum.bridges` adds a way in for each:

| UI | Module | How | Needs |
|---|---|---|---|
| SAP GUI for Windows | `locum.bridges.sap` | SAP GUI Scripting API over COM | `pip install locum[sap]` (pywin32); scripting enabled on the PC and the server |
| Java Swing/AWT | `locum.bridges.java` | Java Access Bridge (`WindowsAccessBridge-64.dll`) via ctypes | a JRE/JDK with the bridge enabled (`jabswitch -enable`) |
| Citrix / RDP / Horizon | `locum.bridges.remote` | Image matching and OCR confined to the session window | `pip install locum[ocr]` for text |

`import locum` never imports a bridge. Each one loads its platform pieces only
when used, and raises `NotSupported` with the fix when they are missing.

## Safety: the same policy as the rest of locum

A bridge action sends no mouse or keyboard input. An SAP `press()` is a COM
call, and a Java `click()` is an `AccessibleAction`. So neither
`GuardedDesktop` nor the screen-corner fail-safe would ever see them. Every
acting bridge call therefore goes through the same checks itself:

* **Policy.** Pass `policy=` (plus optional `confirm=` and `audit=`), or pass a
  `GuardedDesktop` as `desktop=`; its policy, confirm callback and audit log are
  used. The context is the one `GuardedDesktop` builds. The control's label is
  the `target` matched by `confirm_patterns`, so a *Post* button needs
  confirmation. The control is also the `target_node`, so allow rules with
  selectors work, for example `Policy(allow=['button[name*="Save"]'])`. The
  window title is the `target_window` (for `window=` rules), and the
  application is the `target_app` (for `allowed_apps`/`blocked_apps`). A
  control with no usable label (an SAP icon button without a tooltip) is an
  unknown target and follows `unknown_target`.
* **Dry run.** Actions are skipped and audited as `dry_run`.
* **Audit.** Each decision is recorded as one `rpa_action` event with
  `"bridge": "sap"` or `"java"`. Text for password fields is logged as `***`.
* **Fail-safe.** `locum.failsafe.abort()` and the kill hotkey stop bridge
  actions too, and so does a desktop's `FailSafe` when you pass a `desktop`.

| Action | Policy tool name |
|---|---|
| SAP `press()`, grid `press_toolbar_button()` / Java `click()` | `click` |
| SAP text field / cell write, Java `set_text()` | `set_value` |
| SAP combo `key`, radio/tab `select()`, grid `select_rows()` | `select` |
| SAP check box `check()`/`uncheck()` | `toggle` |
| SAP `set_focus()`, grid `set_current_cell()`, Java `focus()` | `focus` |
| SAP table `scroll_to()` (also before a cell write on an off-screen row) | `scroll` |
| SAP `send_vkey(n)` | `press_keys`, with `keys` set to the key combination (`11` is `ctrl+s`, so the default `confirm_keys` apply) |
| SAP `start_transaction(tcode)` | `sap_start_transaction` (target `transaction <tcode>`) |
| SAP window `close()` | `close_window` |
| Java `do_action(name)` | `invoke` |

Reads (`text`, `find`, cell values, the status bar) are never gated.

## SAP GUI Scripting (`locum.bridges.sap`)

```python
from locum.bridges import sap
from locum.llm.safety import Policy, console_confirm

s = sap.attach(system="PRD", client="100", policy=Policy(), confirm=console_confirm)
s.start_transaction("SE16N")
s.find("wnd[0]/usr/ctxtGD-TAB").text = "T001"
s.send_vkey(0)                                    # Enter
s.find("wnd[0]/usr/txtGD-MAX_LINES").text = "50"
s.find("wnd[0]/tbar[1]/btn[8]").press()           # F8 Execute
s.check_status()                                  # SapError on an E or A message

grid = s.find("wnd[0]/usr/cntlRESULT_LIST/shellcont/shell")
for row in grid.rows(["BUKRS", "BUTXT"]):
    print(row)
```

### Enabling scripting

Scripting has to be on in **two** places, and the error tells you which one is off:

1. **On the PC (client side):** *SAP GUI Options > Accessibility & Scripting >
   Scripting > Enable scripting*. If the two *Notify when a script…* boxes are
   ticked, SAP GUI shows a prompt each time a script attaches or opens a
   connection. Untick them for unattended runs, or answer the prompt once. When
   scripting is off here, `attach()` raises `NotSupported` ("SAP GUI Scripting
   is disabled on this PC").
2. **On the server:** set the profile parameter `sapgui/user_scripting = TRUE`
   (transaction RZ11 changes it dynamically, but only new logons see the
   change). With `sapgui/user_scripting_per_user = TRUE`, the user also needs
   authorization object `S_SCR`. When the server refuses, the connection
   reports `DisabledByServer`, and `attach()` raises `NotSupported` naming
   `sapgui/user_scripting`.
   `sapgui/user_scripting_set_readonly = TRUE` allows reading only. Every write
   then fails with a `BackendError` that mentions this parameter.

If SAP Logon is not running, `attach()` raises `BackendUnavailable`. Without
pywin32, it raises `NotSupported` (`pip install locum[sap]`).

### Choosing a session

`sap.sessions()` lists every open session (connection index, session index,
system, client, user, current transaction, busy flag) without attaching.
`attach()` filters by `system=` (the system id or the SAP Logon entry
description), `client=`, `user=`, `connection=` (an index or a description
glob) and `session=` (an index). If **more than one** session matches, it
raises `AmbiguousElement` instead of picking one, because the other session may
be a person's unsaved work. Narrow the filters with `connection=` and
`session=`.

### API

* `session.find(id, timeout=0)` calls `findById` and returns a typed wrapper.
  Ids are relative to the session (`wnd[0]/usr/txtX`), or full ids. Use
  `timeout` for a popup that is about to appear (`wnd[1]/...`). `exists(id)`
  returns a bool.
* `SapTextField` (`GuiTextField`, `GuiCTextField`, `GuiPasswordField`,
  `GuiOkCodeField`) has `.text` (get and set) and `set_text()`. Text longer
  than the field's `MaxLength` raises `ValueError` before anything is written
  (SAP would cut it off silently). A field that is not changeable raises
  `ElementNotFound`.
* `SapButton.press()`.
* `SapComboBox`: `.key` (get and set; it must be one of `.entries`), `.value`
  (the shown text), and `.entries` as `[(key, text)]`.
* `SapCheckBox`: `.selected`, `check()`, `uncheck()`. `SapRadioButton.select()`
  and `SapTab.select()`.
* `SapTableControl` (classic table control) has `row_count`,
  `visible_row_count`, `columns`, `cell(row, col)`, `read(row, col)` and
  `write(row, col, text)`. Rows are **absolute**. When a row is off-screen, the
  table is scrolled to it first. That is a server round trip, and the screen's
  PAI logic runs just as when you scroll by hand. A scroll that only positions
  the table for `cell()`/`read()` is a read and is not gated (it also runs under
  `dry_run`); the scroll before a `write()` is gated as `scroll`. The table is
  found again by its id after each scroll, because SAP GUI rebuilds it. Columns
  can be given as an index, a technical name or a title; a column without a
  `Name` takes the name of its first cell.
* `SapGridView` (ALV grid, a `GuiShell` with subtype `GridView`) has
  `row_count`, `columns`, `cell(row, col)`/`read`, and `rows(columns,
  limit=)`. The grid loads rows on demand, so `FirstVisibleRow` is moved before
  a read of a row outside the visible page (`rows()` moves it once per page).
  It also has `write()` (`ModifyCell`), `set_current_cell()`,
  `select_rows()`, `double_click_cell()` and `press_toolbar_button("&MB_EXPORT")`.
* `SapStatusbar` (`session.statusbar`) has `.text`, `.message_type`
  (`S`/`W`/`E`/`A`/`I`), `.message_id`, `.message_number` and `.is_error`.
  `session.check_status(raise_on="EA")` raises `SapError` for those types.
  Information popups arrive as `wnd[1]`, not in the status bar.
* `session.send_vkey(n, window="wnd[0]")` sends an SAP virtual key: `0` Enter,
  `3` F3 Back, `8` F8 Execute, `11` Ctrl+S Save, `12` F12 Cancel; `13`–`24` are
  Shift+F1–F12, `25`–`36` Ctrl+F1–F12, and `37`–`48` Ctrl+Shift+F1–F12 (see
  `sap.VKEYS`). `session.start_transaction(tcode)` works like `/n<tcode>`
  (`StartTransaction`, with the OK-code field as a fallback). Both return the
  status bar.
* `session.wait_ready(timeout)` polls `session.Busy`. Every acting call waits
  for the session to become idle afterwards (up to `default_timeout`, 30 s) and
  raises `ActionTimeout` if it stays busy.
* `session.find_all(selector)` and `session.tree()` run locum selectors over the
  screen. `SapNode` maps `GuiButton` to `button`, text fields to `edit`,
  `GuiComboBox` to `combobox`, `GuiLabel` to `text`, table controls and ALV
  grids to `table`, and windows to `window`/`dialog`. A node's
  `automation_id` is its session-relative id. Example:
  `s.find_all('button[name*="Save"]')`.
* For anything not wrapped, the raw COM object is available as `.com` on every
  wrapper, and as `sap.engine()` for the whole application.

### Caveats

* COM objects belong to the thread that created them. Use a session on the
  thread that attached it, and call `pythoncom.CoInitialize()` first in a
  worker thread.
* A **frozen** session (an endless loop on the server, a hanging RFC) blocks
  every COM call into it, including `Busy`. locum cannot time such a call out.
  The acceptance scenario (`tests/acceptance/test_sap_export.py`) probes sessions
  from a separate thread with a deadline for this reason.
* Downloads and uploads trigger SAP GUI's own *SAP GUI Security* prompt. It is a
  normal Windows dialog, so answer it with the desktop side of locum (the
  acceptance scenario does), or set the security rules in SAP GUI Options.

## Java Access Bridge (`locum.bridges.java`)

```python
from locum import Desktop
from locum.bridges.java import JavaBridge

with JavaBridge(Desktop()) as jab:
    win = jab.window("Order Entry*")                 # title: exact, glob or substring
    win.find('edit[name="Customer"]').set_text("ACME")
    win.find('checkbox "Rush order"').click()
    win.find('button "Save"').click()
    print(win.find('text[name="Status"]').text)
```

### Enabling the bridge

* Run `jabswitch -enable` (it is in the JRE or JDK `bin` folder), or tick
  *Control Panel > Ease of Access Center > Use the computer without a display
  > Enable Java Access Bridge*. Then **restart the Java application**: a JVM
  loads the bridge only at startup.
* locum loads `WindowsAccessBridge-64.dll` (64-bit Python) or
  `WindowsAccessBridge-32.dll` (32-bit Python). It looks for the DLL in this
  order: `JavaBridge(dll_path=...)`, `LOCUM_JAB_DLL`, System32, every
  directory on `PATH` (searched explicitly: Python 3.8+ no longer loads DLLs
  from `PATH`), `%JAVA_HOME%\bin`, `%JRE_HOME%\bin`, then next to `java.exe` on `PATH`. The
  bitness of the DLL must match Python. It does not have to match the Java
  application. The legacy `WindowsAccessBridge.dll` (JRE 6 and older, 32-bit
  object handles) is not supported.
* If the DLL cannot be loaded, or a window is not visible to the bridge,
  `NotSupported` says so and gives these steps.

### How it works

* `Windows_run()` runs on a dedicated daemon thread that keeps pumping Windows
  messages. The bridge talks to the JVMs through a hidden window on that thread:
  nothing answers until it pumps, and JVMs started later are only discovered
  while it keeps running. `pump=False` calls `Windows_run` on your own thread
  instead, which then needs its own message loop (for example a Tk or Qt GUI
  thread). `settle` (0.3 s by default) gives the JVMs time to announce
  themselves, and `from_hwnd()` keeps polling `isJavaWindow` for up to
  `timeout`.
* The struct layouts follow `AccessBridgePackages.h` exactly. `MAX_STRING_SIZE`
  is 1024 and `SHORT_STRING_SIZE` is 256. `wchar_t` is UTF-16, so
  `AccessibleContextInfo` is 6188 bytes. `JOBJECT64` is `jlong` (64-bit) in both
  the `-32` and the `-64` DLL, and `vmID` is a 32-bit `long`.
  `tests/test_bridge_java.py` checks every field offset.
* Every node is a `JavaNode`, a real locum `AccessibleNode`, so `find`,
  `find_all`, `tree()` and the policy's allow-rule selectors work on Java trees.
  Roles come from `role_en_US`: `push button` becomes `button`, `text` becomes
  `edit`, `label` becomes `text`, `frame` becomes `window`, `page tab` becomes
  `tab`, and so on. `native_role` keeps the Java role. States come from
  `states_en_US`: `showing` means visible (otherwise offscreen), no `enabled`
  means disabled, `indeterminate` becomes `mixed`, a `text` without `editable`
  is readonly, and `password text` gets the `password` state.
* `click(native="auto")` runs the element's `click` AccessibleAction (Swing
  buttons, check boxes and menu items have one) through `doAccessibleActions`.
  Swing localises that action's name (`AbstractButton.clickText`: "Klicken",
  "cliquer", "hacer clic"...), so for a push button, toggle button, check box,
  radio button or menu item without an English click name, its first action is
  used.
  If the element has no click action, locum clicks with the mouse at the
  element's center. That click goes through `Desktop.mouse` with a **strict
  input-target binding** to the Java window. If another window covers the
  point and the Java window cannot be brought to the front, `TargetLost` is
  raised and nothing is clicked. `native=True` uses only the action, and
  `native=False` uses only the mouse.
* `set_text(text)` calls `setTextContents` and then reads the text back with
  `getAccessibleTextInfo` and `getAccessibleTextRange`. If the read-back
  differs, it raises `ActionTimeout`. The bridge copies the text into a
  1024-`wchar_t` buffer, so text longer than 1023 UTF-16 code units is refused
  with `ValueError` before anything is sent. A component the bridge cannot edit raises
  `NotSupported`; for those, click the field and type instead.
* `.text` reads the accessible text in chunks of 1000 characters, or the name
  when the component has no text interface. `do_action(name)` runs any named
  action, and `focus()` calls `requestFocus`.
* **References.** Every context the bridge hands out pins an object in the JVM.
  `JavaBridge` records each one, and `close()` releases them all (or use
  `with JavaBridge() as jab:`). `element.release()` frees one subtree early.
  Lookups re-read the tree: unchanged children keep their node, and children
  that went away are released at once. Below a `JTable`, `JList` or `JTree`
  Swing returns a new proxy object (`AccessibleJTableCell`...) on every read,
  so those children are kept by index: the node stays (a held cell element
  stays valid and reads the cell now at its position) and its old reference is
  released, so repeated lookups keep the reference count flat. A long-running
  robot should still open one bridge per unit of work rather than keep one
  open forever.
  `max_children` (2000 by default) caps the children read per node, because a
  `JTable` can report millions of cells.

## Citrix, RDP and Horizon (`locum.bridges.remote`)

There is no new runtime here. A remote session is pixels plus key and mouse
events, and locum already has the image and OCR tools. `ImageSession` only
**scopes** those tools to the session window and **binds** the input to it:

```python
from locum import Desktop
from locum.bridges.remote import ImageSession

d = Desktop()
citrix = ImageSession(d.window(app="wfica32"), default_timeout=20)
citrix.click_text("Purchase Orders")           # OCR inside the Citrix window only
citrix.click_image("icons/new_po.png")         # template match inside the window only
citrix.type("4500012345", mode="keys")
citrix.press("enter")
print(citrix.read_text((0, 0, 400, 60)))       # window-relative region
```

* `locate`, `locate_all`, `wait_image`, `find_text` and `read_text` search only
  the window's current rectangle. `region=` is relative to the window.
* `click(x, y)` (a window-relative point), `click_image`, `click_text`, `type`,
  `press` and `hotkey` run inside a strict `InputTarget` binding. If another
  window takes the foreground or covers the point, `TargetLost` is raised before
  anything is sent. The window is activated first unless `activate=False`.
* Pass a window from a `GuardedDesktop`, or `desktop=GuardedDesktop(...)`, to
  have every action policy-checked. OCR clicks are judged by their text, so
  *Post* needs confirmation.
* Remote sessions often drop injected Unicode, so prefer `mode="keys"` for
  typing. Scale the remote desktop to 100 % (or capture the needle images at the
  scale you run at), and give `wait_image`/`click_text` generous timeouts,
  because remote screens repaint late.

For a Citrix-published SAP GUI or Java application, there is also a second
route: SAP GUI Scripting and the Java Access Bridge both work *inside* the
remote session. You can run locum on the Citrix server, or on the VDI, next to
the application, and drive it there with the bridges above.

## What is tested

The unit tests use fakes. `tests/test_bridge_sap.py` builds a fake SAP GUI
Scripting COM object graph, `tests/test_bridge_java.py` checks the header
layouts and uses a fake `WindowsAccessBridge` DLL object, and
`tests/test_bridge_remote.py` runs on the mock desktop. They cover attach and
selection errors, the typed wrappers, busy waits, the policy, dry runs, audit,
the fail-safe, reference release and the bound mouse fallback.

Against the real thing:

| Bridge | Real-system test | Status |
| --- | --- | --- |
| `locum.bridges.java` | CI job `windows-java-bridge` on every push: a Temurin JDK with the Access Bridge enabled, a Swing window (`tests/helpers/JabProbe.java`), and `tests/test_real_java_bridge.py` (tree, selectors, `set_text` read-back, AccessibleAction and mouse clicks, checkbox state, reference release, `JTable`/`JList`/`JTree` items with a flat reference count) | **experimental**: verified on one small Swing window, not on a business application |
| `locum.bridges.sap` | none. The on-demand acceptance job needs a self-hosted runner with SAP GUI and has never run; its SAP scenario does not use this module yet | **experimental, unverified**: emits `ExperimentalWarning` |
| `locum.bridges.remote` | none against Citrix/RDP; it is template matching and OCR over a window, the same code paths as the rest of locum's vision | tested on the mock desktop |

Do not use `locum.bridges.sap` in production until you have run it against
your own SAP GUI test system.
