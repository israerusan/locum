# locum vs PyAutoGUI vs UiPath

An honest comparison. locum aims to be strictly better than PyAutoGUI for
writing automation in Python, and to bring the robustness ideas of
enterprise RPA (selectors, queues, retries, evidence) to code — plus native
LLM control. It is **not** a replacement for the UiPath platform as a whole.

## At a glance

| | PyAutoGUI | UiPath | locum |
|---|---|---|---|
| What it is | Python library: synthetic input + screenshots | Commercial RPA platform (Studio, Robots, Orchestrator, …) | Python library: input, vision, accessibility, RPA, LLM control |
| Authoring | Python | Visual designer (Studio), low-code; C#/VB expressions | Python, YAML/JSON flows, recorder output |
| Price / license | Free, BSD-3 | Commercial; free Community tier for individuals/small teams | Free, MIT |
| Platforms | Windows, macOS, Linux (X11) | Windows-first (Studio, most activities); cross-platform robots for some workloads | Windows, macOS, Linux (X11) |

## Finding things on screen

| | PyAutoGUI | UiPath | locum |
|---|---|---|---|
| Coordinates | ✓ | ✓ | ✓ (any "target": point, rect, element, selector, image) |
| Accessibility tree | — | ✓ UIA / MSAA / browser DOM / SAP / Java bridges | ✓ UIA (Windows), AX (macOS), AT-SPI (Linux), browser DOM over DevTools (`locum.browser`), SAP GUI Scripting, Java Access Bridge (`locum.bridges`) |
| Selectors | — | XML selectors, fuzzy & anchor-based targeting | CSS-like (`window "ERP" button:has-text("Save")`), **UiPath-XML compatible subset** (`<wnd app='x' title='*'/><ctrl name='OK' role='push button'/>`), dict form |
| Selector generation | — | Studio "Indicate element" | `locum inspect`, `selector_for(node)`, the recorder |
| Native actions (invoke / set value / toggle / expand) | — | ✓ ("simulate" input) | ✓ with automatic mouse fallback |
| Template matching | ✓ (confidence needs OpenCV), no masks, single scale | ✓ image activities | NCC via FFT in numpy (OpenCV optional), **alpha masks**, multi-scale, multi-monitor, NMS for `locate_all`, color search |
| OCR | — | ✓ (several engines, some paid) | pluggable: Tesseract (`locum[ocr]` installs pytesseract), RapidOCR and EasyOCR (separate optional installs); `find_text` / `click_text` with fuzzy matching |
| Waiting | `time.sleep` | element-exists / wait activities | everything waits with timeouts: `find`, `wait(state=...)`, `wait_image`, `wait_until_stable`, `wait_for_change` |
| Error messages | generic | good, Studio debugging | "No element matching `button "Sav"`; did you mean: `button "Save" #save`" |

## Input

| | PyAutoGUI | UiPath | locum |
|---|---|---|---|
| Mouse paths | tweens | instant / simulated | tweens + humanized Bézier paths with jitter/overshoot and Fitts's-law timing |
| Unicode typing | limited (keymap-dependent) | ✓ | ✓ layout-independent injection (SendInput Unicode, XTEST spare keycodes, Quartz), paced per character; some apps can still drop keystrokes under load, so `Element.type` reads text fields back and repairs them; clipboard mode pastes |
| Hotkeys | ✓ | ✓ | ✓, portable `mod+s` (ctrl/cmd), modifiers always released in `finally` |
| HiDPI / multi-monitor | partial | ✓ | ✓ physical-pixel coordinate space on every backend |
| Fail-safe | top-left corner | — (Orchestrator stop) | all corners + global abort flag + kill hotkey |

## Automation at scale

| | PyAutoGUI | UiPath | locum |
|---|---|---|---|
| Retries, steps, logs | DIY | ✓ (Try/Catch, Retry Scope) | `Robot` tasks/steps with retries + backoff, structured logs |
| Evidence & reports | — | Orchestrator logs, screenshots | error screenshots, `report.json` and self-contained `report.html` per run |
| Work queues | — | Orchestrator queues (central, SLA, multi-robot) | `WorkQueue`: local JSON, resumable, business vs system exceptions, CSV/JSON/XLSX loading — single machine |
| Declarative workflows | — | XAML workflows from Studio | YAML/JSON flows: loops over CSV, try/catch, sub-flows, interpolation, validation before run |
| Recorder | — | ✓ (rich) | ✓ emits Python or YAML **with selectors**, coalesces typing/hotkeys/drags |
| Credential vault | — | ✓ Orchestrator assets | local: `locum.rpa.credentials` (Credential Manager / keyring), `${credential:NAME.password}` in flows, scrubbed from logs |
| Scheduling, robot fleet management | — | ✓ Orchestrator | — by design: use cron/Task Scheduler/CI, and a real queue for several machines |
| Governance, RBAC, audit, support | — | ✓ enterprise | audit log of LLM tool calls only; community support |
| Testing without a desktop | — | — | `MockBackend` / demo desktop: deterministic, runs in CI |

## LLM control

| | PyAutoGUI | UiPath | locum |
|---|---|---|---|
| LLM tool schemas | — | via UiPath AI products | `DesktopToolkit` → Anthropic, OpenAI and MCP schemas |
| Set-of-marks observation | — | — | numbered elements + annotated, token-budgeted screenshot |
| Claude computer-use toolset | — | — | `ComputerUseAdapter` (batches, scaling, legacy shape) |
| MCP server | — | — | `locum mcp`, zero extra dependencies |
| Agent loop | — | agentic products (commercial) | `Agent` for Claude, any compatible client, offline `ScriptedClient` evals |
| Guardrails | — | platform governance | `Policy`: allow/deny tools and apps, confirmations for consequential verbs, deny patterns, rate limits, screen region, dry-run |

## When to choose what

* **PyAutoGUI** — you need a tiny dependency to move the mouse in a script
  that will only ever run on one known screen. (locum's compat layer runs
  those scripts unchanged when they outgrow it.)
* **UiPath** — you are an organization that wants citizen developers building
  automations in a visual designer, central orchestration of many robots,
  credential vaults, SLAs, governance and vendor support, deep SAP/Citrix/
  mainframe integrations, and a budget for it.
* **locum** — you write code; you want selector-based robustness, vision,
  queues, reports and CI-testable automations in plain Python; you want LLM
  agents (Claude, MCP clients) to operate desktop apps with guardrails; and
  you want it free and open source.

## Honest gaps (today)

* Alpha (0.x): breaking changes only in minor releases, listed in
  [CHANGELOG.md](../CHANGELOG.md); the public API is frozen by a snapshot test
  ([API.md](API.md)).
* Validation depth differs by platform, and CI says which. Every push runs
  the built-in self-test against a real desktop on all three OSes, and a
  failure (or a skipped capability) fails the build. Windows runs on the
  GitHub Windows runner and a real Windows 11 machine. macOS runs on the
  GitHub macOS runner, and 0.1.1 passed it on a real Apple Silicon Mac.
  Linux runs Xvfb with a window manager, a real AT-SPI registry and a GTK
  editor. That is real X11 and AT-SPI, but not a full desktop session.
* No visual designer and no orchestrator. locum is a library that runs on one
  machine. It has a local credential vault (`locum.rpa.credentials`: Windows
  Credential Manager, keyring elsewhere). It has no scheduler and no
  multi-machine queue: the work queue deliberately refuses network shares and
  synced folders, because file locks across machines are not reliable. Schedule
  with Task Scheduler, cron or CI. For a fleet, put a real queue (a database, a
  message broker) in front of several locum workers. If central orchestration
  is the requirement, UiPath is the right tool.
* Native Wayland: an experimental portal-based backend (input and capture via
  xdg-desktop-portal, AT-SPI for elements, no window control). It has not yet
  been validated on a real compositor. XWayland apps and X11 sessions are the
  tested path.
* SAP GUI Scripting, Java Access Bridge and an image-only remote-session
  helper (`locum.bridges`, since 0.3) have been tested only against fakes of
  the COM and DLL APIs, not yet against real SAP GUI, JVMs or Citrix
  sessions. Inside remote sessions everything is pixels: vision and OCR,
  confined to the session window.
* Browser DOM automation (`locum.browser`, since 0.1.5) speaks the DevTools
  protocol to Edge/Chrome: launch with a separate profile or attach, navigate,
  evaluate, click, type, shadow roots and same-origin frames. It goes
  through the same safety policy. It is not a Playwright replacement: no
  Firefox/WebKit, no network interception, no auto-waiting test runner. Pair
  with Playwright for heavy web testing.
* SAP GUI: UI Automation sees part of it. `locum.bridges.sap` drives the
  rest through SAP GUI Scripting (it needs scripting enabled on the server and
  the client).
