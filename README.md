# locum

**Desktop automation for Python that understands the screen.** One library for
mouse/keyboard input, screenshots and template matching, accessibility
selectors, RPA robots, and LLM-agent control. Windows is the most-validated
platform; macOS and Linux (X11) backends exist but have been tested less (see
below). **Native Wayland is not supported**: on Linux, locum drives X11
sessions and XWayland windows only.

| Platform | Backend | How it is tested today |
|----------|---------|------------------------|
| Windows 10/11 | SendInput + **UI Automation** | unit tests against a fake Win32/UIA API; validated on a real Windows 11 machine by the built-in self-test |
| macOS 12+ | Quartz events + **AX API** | unit tests against a fake Quartz/AX API only — **not yet validated on a real Mac** |
| Linux (X11 / XWayland) | XTEST + **AT-SPI** | unit tests plus X11 tests under Xvfb in CI; native Wayland input/capture **not supported** |

* **Selectors, not coordinates.** `d.click('button "Save"')` finds the element
  through UI Automation / AX / AT-SPI, survives window moves, DPI and theme
  changes, and falls back to the native *invoke* action when the control is
  covered. UiPath-style XML selectors work too.
* **Vision when there is no accessibility tree.** FFT-based normalized
  cross-correlation, multi-scale, multi-monitor, **alpha channel = mask**, OCR
  hooks, wait-for-change / wait-until-stable.
* **Input that behaves like a person** (optional): Fitts's-law timing,
  Bézier mouse paths with overshoot, Unicode keystroke injection that does not
  depend on the keyboard layout (with read-back verification for text fields,
  see [below](#humanized-layout-independent-input)), clipboard paste,
  stuck-modifier-proof hotkeys, a corner fail-safe and a kill hotkey.
* **RPA built in.** Robots with steps/retries/evidence screenshots and HTML
  reports, resumable file-backed work queues with business exceptions that
  several worker processes on one machine can drain together (locked, leased,
  fsynced), YAML flows, and a recorder that writes selectors instead of raw
  coordinates.
* **LLM-native.** Set-of-marks observations; a Claude (Anthropic) agent loop
  and computer-use adapter; tool schemas exportable in Anthropic, OpenAI and
  MCP formats (locum ships no OpenAI agent runtime — bring your own loop);
  a zero-dependency MCP server; and a safety policy with human confirmation,
  dry-run and audit logs.
* **A drop-in PyAutoGUI layer** (`import locum.compat.pyautogui as pyautogui`)
  so existing scripts move over with one line.

> Status: **0.1 alpha** — usable, tested, but APIs may still change between
> releases. The design is in [`docs/ARCHITECTURE.md`](https://github.com/israerusan/locum/blob/main/docs/ARCHITECTURE.md).
> See [`docs/COMPARISON.md`](https://github.com/israerusan/locum/blob/main/docs/COMPARISON.md) and [`docs/LLM.md`](https://github.com/israerusan/locum/blob/main/docs/LLM.md).

---

## Install

```bash
pip install locum                 # core: pillow, numpy, mss (+ python-xlib / comtypes / pyobjc per OS)
pip install "locum[llm]"          # + anthropic SDK for the Agent
pip install "locum[ocr]"          # + pytesseract only (install the Tesseract binary too)
pip install "locum[record]"       # + pynput: live recorder and global kill hotkey
pip install "locum[yaml]"         # + PyYAML for YAML flows (JSON flows need nothing)
pip install "locum[all]"          # everything above
```

From a checkout: `pip install -e ".[dev]"` and `python -m pytest -q`.

Platform notes: on **Linux** the clipboard works out of the box (native X11
selection handling; `xclip`/`xsel` are used if present) and, for
accessibility selectors, install PyGObject with AT-SPI (`python3-gi gir1.2-atspi-2.0`
on Debian/Ubuntu). On **macOS** grant your terminal/IDE *Accessibility* and
*Screen Recording* permission (System Settings → Privacy & Security).

## Check it on your machine

Before automating real work, run the self-test. It opens a fresh, empty
editor tab (Notepad / TextEdit / gedit), types into it, uses the clipboard and
moves that window, then cleans up and restores your clipboard and mouse. It
refuses to type into an editor that already contains text:

```bash
python -m locum.selftest          # report in ./locum_selftest_<timestamp>/
```

Don't touch the mouse or keyboard while it runs (about a minute); slam the
mouse into a screen corner to abort.

## 60-second quickstart

locum ships with a simulated desktop (an ERP form, a spreadsheet and a text
editor), so you can try everything without touching your real screen:

```python
from locum import Desktop
from locum.cli import DemoBackend

d = Desktop(DemoBackend())          # Desktop() drives your real screen instead

form = d.window("Invoice Entry*")   # exact title, substring, glob or regex
form.find("edit#vendor").set_text("ACME Corp")       # native set-value, falls back to typing;
                                                     # read back: refused/truncated input raises
form.find('edit "Invoice No"').set_text("INV-42")
form.find("edit#amount").set_text("1250.00")
d.click('button "Save"')                             # selector -> element -> click
print(d.find("text#status").text)                    # Saved invoice INV-42
```

The same from the shell (`--backend mock` = the demo desktop):

```bash
locum --backend mock windows
locum --backend mock tree --window "Invoice Entry"
locum --backend mock click 'button "Save"'
locum --backend mock observe                       # what an LLM agent sees
locum --backend mock run examples/excel_report_flow.yaml
```

On your real desktop the same calls work against real applications — e.g.
`locum inspect` prints a robust selector for the element under the mouse.

## Feature tour

The snippets below continue the quickstart (`d` is the demo desktop) and are
executed by the test-suite, so they stay correct.

### Selectors: CSS-like, UiPath XML or dicts

```python
d.find('window[name%="Invoice*"] button:has-text("save")')    # glob, case-insensitive text
d.find("<wnd app='erp' title='Invoice Entry*' /><ctrl automationid='amount' role='editable text' />")
d.find({"role": "button", "name": "Clear", "within": {"role": "window", "name": "Invoice Entry - ERP"}})
d.find('window "Book1 - Excel" > edit "Name Box"')             # child combinator
cells = d.find_all('edit[name~="^[A-E]1$"]')                    # regex attribute match
assert [c.name for c in cells] == ["A1", "B1", "C1", "D1", "E1"]

from locum import selector_for
el = d.element_at(370, 171)                                     # what is under this point?
print(selector_for(el.node, roots=d.roots()))                   # window "Invoice Entry - ERP" edit#amount
print(d.tree(window="Invoice Entry*", interactive_only=True))   # readable tree dump
```

Operators: `=` `!=` `*=` `^=` `$=` `~=` (regex) `%=` (glob), `i` flag for
case-insensitive; pseudo-classes `:enabled :visible :focused :checked :unchecked :mixed
:has-text() :has() :not() :nth() :first :last`. Every lookup waits up to
`default_timeout` and failures say what *was* there:
`No element matching 'button "Sav"'; did you mean: button "Save" #save`.

### Elements and windows

```python
amount = d.find("edit#amount")
amount.set_text("99.90").press("tab")                # actions chain
d.find("checkbox#urgent").check()
assert d.find("checkbox#urgent").is_checked
d.wait('button "Save"', state="enabled", timeout=5)

excel = d.window("Book1*")
excel.activate()
print(excel.rect, excel.app, excel.is_active)
d.window("Invoice Entry*").activate()
```

### Vision: template matching with alpha masks

```python
needle = d.screenshot((180, 268, 100, 32))           # a crop of the Save button
match = d.locate(needle, confidence=0.95)
print(match.rect, round(match.confidence, 3))         # Rect(left=180, top=268, ...) 1.0

masked = needle.convert("RGBA")
masked.paste((255, 0, 255, 0), (0, 0, 100, 8))         # transparent pixels are ignored when matching
assert d.locate(masked, region=(0, 0, 700, 600)).center == match.center
d.click_image(needle, timeout=2)                      # wait for it, then click its center
```

`locate_all` (non-max suppression), `scales=(0.8, 1.0, 1.25)` for DPI
differences, `grayscale=True`, `region=`, `d.wait_until_stable()` and
`d.wait_for_change()` are all there. With an OCR engine installed,
`d.find_text("Total")`, `d.click_text("Submit")` and `d.read_text(region)` work
on any pixels. `locum[ocr]` installs only `pytesseract` (plus the Tesseract
binary from your OS); RapidOCR (`pip install rapidocr-onnxruntime`) and EasyOCR
(`pip install easyocr`) are optional, separate installs that locum picks up
when present.

### Humanized, layout-independent input

```python
d.move('button "Clear"', humanize=True, duration=0.2)    # Bezier path with jitter and overshoot
d.click("edit#vendor")
d.type("Café Zoë ✓")                                     # Unicode injection, no keymap needed
d.hotkey("mod+a")                                        # ctrl+a, or cmd+a on macOS
d.press("backspace")
with d.hold("shift"):
    d.press("tab")
```

How reliable is typing? `d.type()` injects Unicode keystrokes (no keymap
lookup, so the keyboard layout does not matter), paced per character. That
does not make it infallible: some applications drop or garble synthesized
keystrokes while busy — seen intermittently with Windows 11 Notepad. For text
fields, `d.find("edit#vendor").type("…")` (`Element.type`, `verify=True` by
default) reads the field back afterwards and repairs a garbled result, or
raises instead of reporting success; `set_text()` sets the value natively and
verifies it; `d.type(text, mode="clipboard")` pastes instead of typing. Use one
of those when the exact text matters.

`Desktop(humanize=True)` (or `Humanize(speed=..., jitter=..., seed=...)`) makes
every move human-like; `pause=` mirrors PyAutoGUI's `PAUSE`. Moving the mouse
into a screen corner raises `FailSafeTriggered`; `locum.install_kill_hotkey()`
(needs `pynput`) aborts from anywhere.

### RPA: robots, work queues, flows, recorder

```python
import re
from locum import FailSafeTriggered
from locum.rpa import BusinessException, Robot, WorkQueue

robot = Robot(d, name="invoices", run_dir="locum_runs")
queue = WorkQueue("invoices", path="locum_runs/invoices.queue.json")   # persistent: runs resume
queue.add({"vendor": "Initech", "invoice_no": "INV-43", "amount": "75"}, reference="Initech|INV-43")
queue.add({"vendor": "Umbrella", "invoice_no": "INV-44", "amount": "-1"}, reference="Umbrella|INV-44")

def enter_invoice(item):
    if not re.fullmatch(r"\d+(\.\d{1,2})?", item["amount"]) or float(item["amount"]) <= 0:
        raise BusinessException(f"invalid amount {item['amount']!r}")   # recorded, never retried
    d.click('button "Clear"')                               # known state: status "Cleared"
    d.find("edit#vendor").set_text(item["vendor"])          # errors up to here are retried
    d.find("edit#invoice_no").set_text(item["invoice_no"])
    d.find("edit#amount").set_text(item["amount"])

    def answered():                                         # the old status is still on screen
        status = d.find("text#status").text
        return status if status not in ("", "Cleared") else None

    d.click('button "Save"')
    try:                                                    # after Save, a retry could post twice
        status = d.wait_until(answered, timeout=10)
    except FailSafeTriggered:
        raise                                               # the operator stopped the robot
    except Exception as exc:
        raise BusinessException(f"needs review: no answer after Save ({exc})") from exc
    if status.lower().startswith("error"):
        raise BusinessException(status)                     # rejected by the app: never "success"
    return {"status": status}

@robot.task(retries=1)
def process():
    with robot.step("enter invoices"):
        robot.vars["stats"] = queue.process(enter_invoice, retries=2)

report = robot.run()          # timings, logs, error screenshots, report.json + report.html
print(report.summary(), robot.vars["stats"]["success"], robot.vars["stats"]["business_exception"])
```

Declarative flows (YAML/JSON) cover the same ground without Python —
`click, type, set_text, wait, if_exists, for_each (csv/json/items), try/catch,
repeat, call, read_value, assert_exists, ...` with `${var}` interpolation; see
[`examples/excel_report_flow.yaml`](https://github.com/israerusan/locum/blob/main/examples/excel_report_flow.yaml):

```python
from locum.rpa import run_flow

report = run_flow({"steps": [
    {"set_text": {"target": "edit#vendor", "text": "${vendor}"}},
    {"assert_exists": {"target": "edit#vendor", "equals": "Hooli"}},
]}, d, variables={"vendor": "Hooli"})
assert report.ok
```

The `Recorder` turns live input (or anything locum itself does) into flows
or Python **with selectors instead of coordinates**:

```python
from locum.rpa import Recorder

rec = Recorder(d).start(source="backend")       # "auto" = global hooks via pynput
d.click("edit#due_date")
d.type("2026-12-31")
rec.stop()
print(rec.to_python())                          # d.click('window "Invoice Entry - ERP" edit#due_date') ...
```

### LLM control: tools, computer use, MCP, agents

```python
from locum.llm import DesktopToolkit, Policy

kit = DesktopToolkit(d, policy=Policy())        # every call is policy-checked and audited
tools = kit.to_anthropic()                      # also .to_openai() / .to_mcp()
obs = kit.call("observe", {"include_screenshot": False})
print(obs.text.splitlines()[-1])                # [N] ... numbered elements (set-of-marks)
print(kit.call("set_value", {"selector": "edit#vendor", "value": "Pied Piper"}).text)
print(kit.call("click", {"selector": 'button "Submit Payment"'}).is_error)   # True: needs confirmation
```

<!-- norun -->
```python
from locum import Desktop
from locum.llm import Agent, AuditLog, Policy, console_confirm

agent = Agent(Desktop(), policy=Policy(confirm=console_confirm), audit=AuditLog("audit.jsonl"))
result = agent.run("Open the Q3 report in Excel and e-mail the totals to Dana")
print(result.success, result.final_text)
```

* `mode="tools"` (default) gives Claude the toolkit above: element ids,
  selectors, OCR, keyboard/mouse, windows, apps, clipboard, and a `done` tool.
* `mode="computer_use"` uses Claude's computer-use toolset, executed by
  `ComputerUseAdapter` (screenshot scaling and coordinate mapping included).
* `locum mcp` exposes the same tools to Claude Desktop, Claude Code or any
  MCP client — config in [`examples/mcp_config.json`](https://github.com/israerusan/locum/blob/main/examples/mcp_config.json):

```json
{"mcpServers": {"locum": {"command": "locum", "args": ["mcp", "--policy", "default"]}}}
```

or `claude mcp add locum -- locum mcp --policy default` for Claude Code
(`locum-mcp` and `python -m locum.mcp` start the same server). Setup for
Claude Code / Claude Desktop, options and timeouts:
[`docs/MCP.md`](https://github.com/israerusan/locum/blob/main/docs/MCP.md).
Prompting tips and the safety model: [`docs/LLM.md`](https://github.com/israerusan/locum/blob/main/docs/LLM.md).

Web pages in Edge / Chrome are read and driven over the DevTools protocol
(`browser_launch`, `browser_observe`, `browser_click`, `browser_type`, ... or
`locum.browser.Browser` in Python), with a separate locum-managed browser
profile, instead of the browser's huge accessibility tree. After an action,
`observe(changes_only=true)` returns only what changed, with a cropped
screenshot.

### Safety policy

```python
from locum.llm import CARD_NUMBER, Policy

policy = Policy(
    allowed_apps=["erp", "excel"],              # act (and launch) only inside these apps
    deny_text_patterns=[CARD_NUMBER],           # never type card numbers (Luhn-checked; IBANs pass)
    confirm=lambda description, details: input(f"Allow {description}? [y/N] ") == "y",
    max_actions=200, max_actions_per_minute=30,
    region=(0, 0, 1920, 1080),                  # pointer must stay in this rectangle
    dry_run=False,                              # True: describe instead of act
)
strict = Policy.strict()                        # no launching/clipboard, shells blocked, broad confirmations
authorized = Policy.strict(allow=[              # default-deny: only these run without a human
    'window "Invoice Entry - ERP" edit',
    'window "Invoice Entry - ERP" button "Save"',
])
```

The default policy is a **heuristic tripwire**, not authorization. A human
`confirm` is needed (and without a callback the action is **blocked**) when the
target looks like a commit — *pay, transfer, wire, submit, send, delete, approve,
post, purchase, release, …*, their plurals and German SAP labels such as *Buchen*
and *Überweisen* — when Ctrl/Cmd+Enter or Ctrl/Cmd+S is pressed, or when a click,
Enter or field entry reaches a control that cannot be identified (nothing under
the pointer, or no name, automation id, value or named child: an icon-only
button). Keyword matching misses other languages, "Continue"/"Apply"-style
commits and shortcuts it was not told about.

For money and data actions use **allow-list mode**: with `Policy(allow=[...])` /
`Policy.strict(allow=[...])` every consequential action (clicks, text entry,
invoke, launching, clipboard writes, non-navigation keys) needs a human unless one
of your rules — a selector, `"window=..."`, `"app=..."`, `"launch=..."`,
`"keys=..."` or `"tool=..."` — matches what really receives it: the control the
mouse actually presses (not the container around it), or the default button that
Enter presses. A rule without `keys=` covers only typing, editing and navigation
keys and Enter/Space on the control it names; application shortcuts such as
Escape, F-keys or Alt+F4 need a `keys=` rule. Robots and flows are gated only when
you pass a policy (`Robot(policy=...)`, `run_flow(..., policy=...)`); without one
they run ungated as before. Text typed into password fields is masked in audit
logs and recordings. Details: [`docs/LLM.md`](https://github.com/israerusan/locum/blob/main/docs/LLM.md#6-safety-model).

### PyAutoGUI compatibility

<!-- norun -->
```python
import locum.compat.pyautogui as pyautogui    # was: import pyautogui

pyautogui.moveTo(100, 200, duration=0.5, tween=pyautogui.easeInOutQuad)
pyautogui.write("Hello!\n", interval=0.05)
box = pyautogui.locateOnScreen("button.png", confidence=0.9)
pyautogui.click(pyautogui.center(box))
```

See [`examples/pyautogui_migration.py`](https://github.com/israerusan/locum/blob/main/examples/pyautogui_migration.py) for
the before/after.

## How it compares

|                                   | PyAutoGUI | UiPath | **locum** |
|-----------------------------------|:---------:|:------:|:-----------:|
| Mouse/keyboard/screenshots        | ✓ | ✓ | ✓ |
| Accessibility selectors (UIA/AX/AT-SPI) | — | ✓ (Windows-first) | ✓ |
| UiPath-style XML selectors        | — | ✓ | ✓ (compatible subset) |
| Template matching w/ alpha masks, multi-scale | confidence needs OpenCV | ✓ | ✓ (numpy; OpenCV optional) |
| OCR                               | — | ✓ | pluggable: Tesseract via `locum[ocr]`; RapidOCR / EasyOCR as separate installs |
| Native invoke / set-value (works when covered) | — | ✓ | ✓ |
| Humanized motion, Unicode typing  | partial | — | ✓ |
| Robots, retries, evidence reports | — | ✓ | ✓ (code-first) |
| Transaction queues                | — | ✓ (Orchestrator: central, multi-machine) | ✓ (local JSON file: resumable, multi-process on one machine, leases; no network shares or multi-machine) |
| Recorder                          | — | ✓ (Studio) | ✓ (emits Python/YAML with selectors) |
| Visual designer, Orchestrator, enterprise governance, support | — | ✓ | — |
| LLM tools, MCP server, computer-use adapter, agent loop | — | add-ons | ✓ built in |
| Price / license                   | free, BSD-3 | commercial (free Community tier) | free, MIT |

UiPath is a full enterprise platform — Studio's visual designer, Orchestrator
scheduling and credential vaults, Action Center, governance and vendor
support. locum does not replace that; it is a **code-first, free,
LLM-native** library for developers who want UiPath-grade robustness inside
plain Python. Full write-up: [`docs/COMPARISON.md`](https://github.com/israerusan/locum/blob/main/docs/COMPARISON.md).

## Platform details

How each platform is tested is in the table at the top; this is what each
backend uses.

| Platform | Input | Screenshots | Windows | Accessibility | Notes |
|----------|-------|-------------|---------|---------------|-------|
| Windows 10/11 | SendInput (Unicode, DPI-aware, multi-monitor) | mss / GDI | Win32 | **UI Automation** (comtypes) | run as the same integrity level as the target app (UAC); text locum puts on the clipboard is kept out of Clipboard History (Win+V) and cloud sync (`WindowsBackend(clipboard_history=True)` allows it) |
| macOS 12+ | Quartz events | Quartz (Retina-aware) | Quartz / AppKit | **AX API** | needs Accessibility + Screen Recording permission; not yet validated on a real Mac |
| Linux X11 | XTEST (Unicode via spare keycodes) | mss / XGetImage | EWMH | **AT-SPI** (PyGObject) | native X11 clipboard (xclip/xsel optional) |
| Linux Wayland | XWayland apps only | XWayland only | XWayland only | AT-SPI works | see below |
| Any (tests, CI, evals) | `MockBackend` / `DemoBackend` | ✓ | ✓ | ✓ | deterministic, no display |

**Wayland:** Wayland deliberately prevents applications from
injecting input into, or capturing, other applications. The X11 backend works
inside an X11 session and with XWayland windows; native Wayland windows cannot
be driven or captured through it. Use an X11 session (“GNOME on Xorg”) for
full-desktop automation; portal-based (`xdg-desktop-portal` RemoteDesktop /
ScreenCast) support is not implemented yet.

## Safety notes for LLM control

* An agent with desktop access can do anything **you** can: send e-mail, move
  money, delete files. Run unattended agents in a VM or a dedicated user
  account, never on a machine with privileged sessions open.
* Keep the default policy (or `Policy.strict()`): consequential actions need
  a human `confirm`; without one they are blocked. Use `allowed_apps` to fence
  the agent into the applications the task needs, and `dry_run=True` to
  rehearse.
* Keep the fail-safe on (mouse to a corner aborts) and arm the kill hotkey
  (`locum --kill-hotkey agent ...` or `install_kill_hotkey()`).
* Treat on-screen text as untrusted: a web page or document can contain
  instructions aimed at the model (prompt injection). The policy enforces
  limits regardless of what the model was told.
* Keep the audit log (`AuditLog(path)` / `--audit FILE`): every tool call, its
  arguments, the policy decision and the result are recorded as JSON lines.

## CLI

```text
locum [--backend NAME] [--timeout S] [--json] [--no-failsafe] [--humanize] [--kill-hotkey] COMMAND
  info | screenshot [-o PNG] [--region L,T,W,H] [--monitor N] | windows | tree [--window T] [--depth N] [--all]
  find SELECTOR [--all] | click TARGET | move TARGET | type TEXT [-e] [--into SEL] | hotkey COMBO | press KEY...
  scroll DY [--at TARGET] | wait SELECTOR [--state S] | locate IMAGE [--confidence C] [--click]
  observe [--json] [--screenshot PNG] | inspect [X,Y]
  mcp [--policy strict|default|permissive] [--dry-run] [--audit FILE]
  agent TASK [--model M] [--max-steps N] [--dry-run] [--mode tools|computer_use] [--replay FILE]
  run FLOW.yaml [--var k=v] [--report-dir DIR] [--validate] | record [-o out.py|out.yaml] [--duration S]
```

Exit codes: 0 ok · 1 failed (not found, timeout, policy, failed flow/agent) ·
2 usage/invalid input · 3 unavailable (backend, capability, missing extra) ·
4 fail-safe abort · 130 interrupted.

## Examples

| File | What it shows |
|------|---------------|
| [`basic_automation.py`](https://github.com/israerusan/locum/blob/main/examples/basic_automation.py) | windows, selectors, typing, screenshots, template matching |
| [`sap_style_data_entry.py`](https://github.com/israerusan/locum/blob/main/examples/sap_style_data_entry.py) | robot + CSV work queue entering invoices via selectors, business exceptions, reports |
| [`excel_report_flow.yaml`](https://github.com/israerusan/locum/blob/main/examples/excel_report_flow.yaml) | YAML flow filling a spreadsheet from CSV |
| [`llm_agent.py`](https://github.com/israerusan/locum/blob/main/examples/llm_agent.py) | Claude agent with a policy, confirmation callback and audit log |
| [`mcp_config.json`](https://github.com/israerusan/locum/blob/main/examples/mcp_config.json) | Claude Desktop / Claude Code MCP configuration |
| [`pyautogui_migration.py`](https://github.com/israerusan/locum/blob/main/examples/pyautogui_migration.py) | one-line PyAutoGUI switch, then the upgrade |

All of them run on the demo desktop: `LOCUM_BACKEND=mock python examples/<file>`.

## License

MIT.
