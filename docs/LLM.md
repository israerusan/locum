# Driving the desktop with an LLM

locum gives a language model three ways to operate a computer, all behind
the same safety policy and audit log:

| Way | What the model gets | Best for |
|-----|---------------------|----------|
| **Tools** (`DesktopToolkit`, `Agent(mode="tools")`) | numbered elements + annotated screenshot, then `click(element_id=7)`, `set_value`, `type_text`, `hotkey`, `find_elements`, `wait_for`, OCR, windows, apps, clipboard | apps with an accessibility tree (most Windows/macOS business apps, Office, browsers, GTK/Qt). Cheapest and most reliable. |
| **Computer use** (`ComputerUseAdapter`, `Agent(mode="computer_use")`) | Claude's native computer-use toolset: screenshots and pixel actions | canvas apps, games, remote desktops (Citrix/RDP), anything without accessibility |
| **MCP** (`locum mcp`) | the tools above over the Model Context Protocol | Claude Desktop, Claude Code, IDE agents — no Python glue |

The examples below run on the built-in demo desktop
(`d = Desktop(DemoBackend())`, see the README quickstart); swap in `Desktop()`
for the real screen. Blocks marked as needing the API are not executed by the
test-suite.

## 1. Observations: set-of-marks

```python
from locum import Desktop
from locum.cli import DemoBackend
from locum.llm import observe

d = Desktop(DemoBackend())
obs = observe(d, max_long_edge=1280)            # screenshot + numbered interactive elements
print(obs.text().splitlines()[0])              # Screen region 1920x1080 px shown as a 1280x720 screenshot ...
vendor = obs.element(1)                        # ObservedElement: role, name, value, bounds, states, selector
print(vendor.role, vendor.name, vendor.selector)   # edit Vendor window "Invoice Entry - ERP" edit#vendor
x, y = obs.to_image(*vendor.center)            # screen -> screenshot pixels (what the model sees)
assert obs.to_screen(x, y).distance(vendor.center) <= 2
png_b64 = obs.image_base64(annotated=True)     # numbered boxes drawn on every element
```

* Element ids are stable **within one observation**; observe again after the
  screen changes. Elements covered by another window are listed with
  `occluded` and are clicked through their native action instead of the mouse.
* Screenshots are downscaled to the model's limits (long edge ≤ `max_long_edge`
  and ≤ 4784 visual tokens of 28×28 px); every coordinate the model sends back
  is mapped to physical pixels with `Observation.to_screen`.
* `observe(d, window="Invoice Entry*")` restricts the view (and the screenshot)
  to one window; `interactive_only=False` adds labels and static text.

From the shell: `locum observe --screenshot marks.png` prints exactly what
the model will read and saves the annotated image.

## 2. The desktop toolkit

```python
from locum.llm import AuditLog, DesktopToolkit, Policy

audit = AuditLog()                              # AuditLog("audit.jsonl") to persist
kit = DesktopToolkit(d, policy=Policy(), audit=audit)
print(kit.names())          # ['observe', 'screenshot', 'click', 'double_click', 'right_click', 'type_text', ...]
kit.call("observe", {"include_screenshot": False})
res = kit.call("type_text", {"element_id": 1, "text": "Initech"})
print(res.text, res.is_error)
res = kit.call("click", {"text": "Save"})       # by visible label; or selector=..., or x/y
print(kit.call("find_elements", {"selector": "edit"}).text.splitlines()[0])
assert len(audit) >= 4                          # every call: args, policy decision, result
```

Tools: `observe, screenshot, click, double_click, right_click, type_text,
press_keys, hotkey, scroll, move_mouse, drag, set_value, find_elements,
wait_for, list_windows, focus_window, launch_app, read_clipboard,
write_clipboard, read_text (OCR), wait`. Targets accept exactly one of
`element_id` (preferred), `selector`, `text`, or `x`+`y` in screenshot pixels.
Results never raise: failures come back as `is_error` results the model can
read and recover from.

Schemas for any provider: `kit.to_anthropic()` (`name/description/input_schema`),
`kit.to_openai()` (function-calling format) and `kit.to_mcp()`
(`name/description/inputSchema`).

### Your own tools

```python
from locum.llm import ToolRegistry

reg = ToolRegistry()

@reg.register
def lookup_vendor(name: str, country: str = "US") -> str:
    """Find the ERP vendor id for a company.

    Args:
        name: Company name as printed on the invoice.
        country: ISO country code.
    """
    return f"V-{sum(map(ord, name + country)) % 10000:04d}"

schema = reg.to_anthropic()[0]
assert schema["input_schema"]["required"] == ["name"]
print(reg.call("lookup_vendor", {"name": "ACME"}).text)
```

Pass functions or `ToolSpec`s to `Agent(extra_tools=[lookup_vendor])` to mix
business lookups with desktop control. They obey the agent's policy: the tool
name and its string arguments are matched against the confirmation patterns,
and `ToolSpec.from_function(release_batch, confirm=True)` asks a human before
every call. In `mode="tools"` the computer-use toolset is not offered, and a
`computer` call the model makes anyway is answered with an error, not run.

## 3. The agent loop

<!-- norun -->
```python
from locum import Desktop
from locum.llm import Agent, AuditLog, Policy, console_confirm

agent = Agent(
    Desktop(),
    model="claude-opus-5-5",          # default
    mode="tools",                     # or "computer_use"
    policy=Policy(confirm=console_confirm, allowed_apps=["excel", "outlook"]),
    audit=AuditLog("agent-audit.jsonl"),
    max_steps=40,
    effort="high",                    # output_config.effort: low | medium | high | xhigh | max
    on_step=lambda step: print(step.index, step.text, [c["name"] for c in step.tool_calls]),
)
result = agent.run("Copy the totals row of Q3.xlsx into a new Outlook e-mail to dana@example.com")
print(result.success, result.final_text, result.usage)
```

The loop follows the Claude Messages API contract: the full assistant content
(thinking blocks included) is appended unchanged every turn; all tool results
of a turn go back in one user message; `stop_reason` drives the loop
(`tool_use` continues, `end_turn` finishes, `max_tokens`, `pause_turn` and
`refusal` are handled). It sends no sampling parameters, thinking budgets,
forced `tool_choice` or prefill. The model ends a task with the `done` tool
(`summary`, `success`). A `done(success=true)` written in the same turn as a
call that failed, was blocked or was skipped is answered with an error instead
(the model has not seen those results yet), and calls after an accepted `done`
are not executed. A run that ends with `end_turn` (no `done`) only reports
`success=True` when no call of the last tool turn failed, was blocked or was
skipped and no call in the whole run was blocked by the policy or declined by
a human; otherwise `success` is `False` (`stop_reason` stays `end_turn`), so
`if result.success: mark_paid()` never fires after a blocked payment. The first user message carries an observation, so the
model starts with eyes open. However a run ends, mouse buttons the model left
pressed (`left_mouse_down`) and keys still held are released. Configuration
errors on the first request (a `TypeError` from wrong `create_kwargs`, a
missing or invalid API key) are raised instead of being reported as a failed
run. `create_kwargs` are passed to `messages.create` (e.g. `extra_headers`,
`extra_body`); a `betas` list routes the requests through
`client.beta.messages.create`.

`client=` accepts anything with `client.messages.create(**kwargs)` returning
SDK objects or plain dicts — your own gateway, a proxy, or `ScriptedClient`
for **offline evaluations**:

```python
from locum.llm import Agent, Policy, ScriptedClient, scripted_response, tool_use_block

client = ScriptedClient([
    scripted_response("Filling the form.",
                      tool_use_block("set_value", {"selector": "edit#vendor", "value": "Wayne Ent"}),
                      tool_use_block("set_value", {"selector": "edit#invoice_no", "value": "INV-77"}),
                      tool_use_block("set_value", {"selector": "edit#amount", "value": "980"}),
                      tool_use_block("click", {"selector": 'button "Save"'})),
    scripted_response(tool_use_block("done", {"summary": "Saved INV-77", "success": True})),
])
result = Agent(d, client=client, policy=Policy.permissive()).run("Enter invoice INV-77")
assert result.success and d.find("text#status").text == "Saved invoice INV-77"
assert client.requests[0]["output_config"] == {"effort": "high"}
```

Because the demo desktop is deterministic, the same pattern scores real model
runs: record the transcript, assert on `d.backend.invoices` and the audit log.

## 4. Computer use

```python
from locum.llm import ComputerUseAdapter, computer_block

cu = ComputerUseAdapter(d, max_long_edge=1280)
print(cu.tool_definition())                   # {'type': 'computer_toolset_20260801'}
w, h = cu.image_size()                        # the model's coordinate space
shot = cu.execute("screenshot", {})           # ToolResult with a base64 PNG
x, y = cu.to_image(370, 207)                  # the Due Date field, in screenshot pixels
results = cu.run_batch([
    computer_block("left_click", {"coordinate": [x, y]}),
    computer_block("type", {"text": "2027-01-15"}),
    computer_block("key", {"text": "Tab"}),
])
assert all(r["toolset_name"] == "computer" and not r.get("is_error") for r in results)
assert d.find("edit#due_date").value == "2027-01-15"
```

The adapter implements every toolset action (`screenshot, zoom, left_click,
right_click, middle_click, double_click, triple_click, left_click_drag,
mouse_move, left_mouse_down, left_mouse_up, cursor_position, scroll, type,
key, hold_key, wait`) and the legacy single `computer` tool with
`input.action`. Batches run in order and stop at the first failure; the rest
are answered with *"Not executed: an earlier computer action in this turn
failed."* Key names follow xdotool (`Return`, `ctrl+s`, `alt+Tab`, `super`).
Keyboard actions are checked against the focused control, and again before
every key or typed piece that follows a key that may have moved focus: `"Tab
Return"` or typed `"1000\t\n"` that lands on a "Pay" button needs the same
confirmation as clicking it. A single combination that mixes Enter/Space with
another key (`tab+Return`) is rejected.
Screenshots are downscaled by locum (long edge ≤ 2576 px and ≤ 4784 visual
tokens) and every coordinate is scaled back to physical pixels — on HiDPI and
multi-monitor setups too. Pick a monitor with `ComputerUseAdapter(d, monitor=1)`.

`Agent(mode="computer_use")` wires this up; custom tools (`extra_tools`) and
`done` stay available next to the toolset.

## 5. MCP server

`locum mcp` speaks MCP (JSON-RPC 2.0 over stdio, protocol `2025-06-18`) with
no extra dependencies. Claude Desktop (`claude_desktop_config.json`) or Claude
Code (`.mcp.json`):

```json
{
  "mcpServers": {
    "locum": {
      "command": "locum",
      "args": ["mcp", "--policy", "default", "--audit", "~/.locum/mcp-audit.jsonl"]
    }
  }
}
```

or `claude mcp add locum -- locum mcp --policy default`. If `locum` is
not on the client's `PATH`, use the interpreter: `"command": "/path/to/python",
"args": ["-m", "locum.mcp"]` (or the `locum-mcp` executable). Step-by-step
setup, timeouts and cancellation: [MCP.md](MCP.md). Add `--backend mock` for a sandbox the
model can practice on, `--dry-run` to rehearse, `--policy strict` for
unattended use, `--allow-app excel` to fence it in.

MCP stdio has no channel for asking you mid-call, so actions that need
confirmation are **blocked** with an explanatory error (the model sees it and
can ask you in chat); use `--policy permissive` only in sandboxes. The
stdio transport is UTF-8 whatever the console code page, so umlauts and `€`
arrive intact on Windows too.

In-process (tests, custom transports):

```python
from locum.llm import MCPServer

server = MCPServer(d)
init = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18"}})
assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
reply = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                               "params": {"name": "list_windows", "arguments": {}}})
print(reply["result"]["content"][0]["text"].splitlines()[0])
```

## 6. Safety model

Every tool call — toolkit, computer use or MCP — goes through
`Policy.check(tool, args, context=...)` **before** anything touches the
desktop, then the outcome is written to the `AuditLog`. RPA code (`Robot`,
YAML/JSON flows, plain `Desktop` calls) is **not** gated unless you give it a
policy — see [RPA behind the policy](#rpa-behind-the-policy).

A policy decides what needs a human in one of two modes. Be clear about which
one you are relying on:

| | Default: heuristic tripwire (`Policy()`) | Allow-list: authorization (`Policy(allow=[...])`, `Policy.strict(allow=[...])`) |
|---|---|---|
| Needs a human when… | the target's label, selector, command or arguments match `confirm_patterns` (English/German commit verbs), a `confirm_keys` shortcut is pressed, or an activating input reaches a control with no usable identity | **every** consequential action, unless an allow rule you wrote matches its target |
| Misses | icon-only controls that report some unrelated name, labels in other languages, *Continue*/*Apply*/*OK*-style commits, app shortcuts it was not told about | nothing it was not told to allow (keywords still apply on top as a tripwire) |
| Use it for | catching common mistakes while an agent explores | money, data and anything irreversible |

The default mode is keyword inference: a useful seat belt, **not** an
authorization mechanism. For payments, postings, deletions and submissions,
use allow-list mode.

```python
from locum import PolicyViolation
from locum.llm import CARD_NUMBER, DesktopToolkit, Policy

asked = []
policy = Policy(
    allowed_apps=["erp"],                        # globs, ".exe" optional; also limits launch_app
    blocked_apps=["cmd", "powershell", "terminal"],
    deny_text_patterns=[CARD_NUMBER],            # Luhn-valid card numbers (IBANs pass)
    confirm=lambda description, details: asked.append(details["reason"]) or False,  # decline everything
    max_actions=50,
    max_actions_per_minute=20,
)
kit = DesktopToolkit(d, policy=policy)
kit.call("observe", {"include_screenshot": False})
blocked = kit.call("type_text", {"selector": "edit#vendor", "text": "4111 1111 1111 1111"})
assert blocked.is_error and "Blocked" in blocked.text
declined = kit.call("click", {"selector": 'button "Submit Payment"'})
assert declined.is_error and asked                # the human said no
try:
    policy.check("launch_app", {"command": "powershell -c whoami"}, context={})
except PolicyViolation as exc:
    print("blocked:", exc)
```

### Allow-list mode (authorization)

With `allow=[...]` every consequential action needs `confirm` unless an
explicit rule matches it. Consequential = clicks and other activations,
text entry (`type_text`, `set_value`), native `invoke`/`toggle`/`select`,
`launch_app`, `write_clipboard`, drags, closing windows, integration tools,
and any key press other than pure navigation (Tab, arrows, Home/End,
PageUp/PageDown, with Shift/Ctrl at most). Read-only tools (observe,
screenshot, find, list windows, read text/clipboard, wait) are never gated;
moving the pointer, scrolling and focusing a window are not consequential.

```python
from locum.llm import DesktopToolkit, Policy
from locum.llm.safety import AllowRule

decided = []
policy = Policy(
    allow=[
        'window "Invoice Entry - ERP" edit',             # any field of the invoice form
        'window "Invoice Entry - ERP" button "Save"',    # ...and its Save button
        "keys=ctrl+a",                                   # select-all, wherever the focus is
    ],
    confirm=lambda description, details: decided.append(details["reason"]) or False,
)
kit = DesktopToolkit(d, policy=policy)
assert not kit.call("set_value", {"selector": "edit#vendor", "value": "ACME GmbH"}).is_error
refused = kit.call("click", {"selector": 'button "Clear"'})   # no rule -- and no keyword would have caught it
assert refused.is_error and decided[-1] == "no allow rule authorizes 'click' on 'Clear'"
assert policy.allow[0] == AllowRule(selector='window "Invoice Entry - ERP" edit')
```

Rules (`AllowRule`, or the string / dict shorthands) — every field given
must match:

| Rule | Matches |
|------|---------|
| `'button "Save"'` / `"selector=..."` / `AllowRule(selector=...)` | the target element is one the selector finds (ancestry included: `window "Invoice 42" button "Save"`). Coordinates and element ids are hit-tested first, so a click at the Save button's pixels matches too. A click is judged by the control the mouse actually presses: a rule on a toolbar or pane does not cover the "Pay now" button at its centre (a decorative icon inside an allowed button still counts as the button). Enter in a single-line field is judged by the default button it presses, not by the field. Keys: see the note below the table. |
| `"window=Invoice*"` | glob on the title of the window the action lands in (the target's window, the window under the pointer; for keyboard input the focused control's or the active window) |
| `"app=excel"` | every application the action touches (like `allowed_apps`) |
| `"launch=notepad.exe"` | `launch_app` of that command line or program. The rule only means something if the process started *is* that program, so guarded launches (`launch_app`, `GuardedDesktop.launch`, robots and flows with a policy) refuse `shell=True`, `executable=` and `preexec_fn` outright, and `cwd`/`env` (which change what a name resolves to) unless the policy opts in with `Policy(launch_options=["cwd", "env"])`. `DesktopToolkit(launch_kwargs=...)` is checked when the toolkit is built. |
| `"keys=ctrl+s ctrl+p"` | keyboard actions pressing only these combinations (combine with a selector or window: `{"selector": "edit#amount", "keys": "f8"}`) |
| `"tool=write_clipboard"`, `"tool=erp_*"` | any call of those tools (needed for `write_clipboard` and integration tools; a rule without `tool=` never covers them) |
| `{"selector": 'button "Save"', "window": "Invoice*"}` | both |

A rule that does not list `keys` authorizes only keys that act on the
control it matched: navigation, a bare Enter or Space, and, in a text field,
characters, Backspace/Delete and Ctrl/Cmd+A/C/V/X/Y/Z. Application
shortcuts — Escape, F-keys (F8 = Execute in SAP), `alt+f4`,
`alt+<access key>`, `ctrl+enter`, `ctrl+s` — act on other controls or the
whole window, so they need a rule that names them, even while an allowed
field has focus.

`confirm_patterns`/`confirm_keys` still apply on top, so an allowed
`button "Submit"` keeps asking until you pass `confirm_patterns=()`.
`Policy.strict(allow=[...])` adds the strict preset's limits (shells blocked,
card numbers denied, 200 actions, 30/min); a `launch=` or
`tool=write_clipboard` rule lifts that tool from its default `deny_tools`.
An authorized action records the rule in `context["allowed_by"]`.

### RPA behind the policy

`Robot`, `run_flow` and `FlowRunner` take the same policy. They then drive a
`GuardedDesktop` that checks every click, keystroke, text entry, native
invoke/toggle, launch and clipboard write — including actions on elements
and windows reached through `find()`, `find_all()`, `wait()` and `window()`,
and the `mouse`/`keyboard` objects — before it is sent. A refused or declined
action raises `PolicyViolation` and is **never retried** (also when it
happens inside a `for_each`/`repeat`/`try`/subflow step that has `retries`,
or inside a Robot task that wraps it in another error). Without a policy
(the default) RPA is not gated at all, exactly as before.

```python
from locum.llm import AuditLog, Policy
from locum.rpa import run_flow

audit = AuditLog()
report = run_flow(
    {"steps": [{"set_text": {"target": "edit#amount", "text": "99.00"}},
               {"click": 'button "Submit Payment"'}]},
    d,
    policy=Policy(allow=["edit#amount"]),   # no confirm callback: anything else is refused
    audit=audit,
)
assert not report.ok and "PolicyViolation" in report.error
assert [e["status"] for e in audit.events] == ["allowed", "blocked"]
```

`Robot(desktop, policy=Policy.strict(allow=[...]), confirm=console_confirm,
audit=AuditLog("audit.jsonl"))` does the same for code-first robots
(`robot.desktop` is the guarded facade). The guard protects data-driven
automation — flow variables, CSV rows, recorded selectors — from doing what
nobody authorized; it is not a sandbox against code that reaches
`desktop.backend` or keeps the unwrapped desktop.

### What every mode does

* **Confirmations.** By default (`DEFAULT_CONFIRM`), targets that look like
  they commit something — *delete, remove, pay, transfer, wire, submit, send,
  purchase, approve, post, buy, checkout, refund, authorize, release* and their
  plurals (*Payments, Transfers, make_payments*), plus the German SAP labels
  *Buchen, Zahlen, Überweisen, Freigeben, Löschen, Senden* — need
  `confirm(description, details) -> bool`. No callback = blocked. Typed text
  is not matched; the click that submits it is. A click on an element is
  judged by what the mouse really hits: clicking a group or toolbar whose
  centre lies on a "Pay now" button counts as clicking "Pay now".
* **Shortcuts.** A shortcut that presses a control is judged by that
  control's label: `alt+p` on a "&Pay now" button (its access key), or a
  menu item's accelerator (`Ctrl+S` next to "Post"), needs the same
  confirmation as clicking it. Combos in `Policy(confirm_keys=...)` — by
  default `ctrl+enter`/`cmd+enter` (send) and `ctrl+s`/`cmd+s` (save, which
  posts the document in SAP FB60) — always need confirmation, whatever has
  focus. Other application shortcuts (F8 = Execute in SAP) are not gated:
  add them to `confirm_keys`, e.g. `Policy(confirm_keys=DEFAULT_CONFIRM_KEYS + ("f8",))`.
* **Unknown targets.** When input that can press something (a left click,
  Enter/Space, typing into an element, which clicks to focus it) reaches a
  control nobody can identify — the focused element is unreadable (no
  AT-SPI, Java, Citrix), nothing is found under the pointer, or the control
  has **no usable identity** (no name, automation id, text value or named
  child under the click point: an icon-only button, a nameless field reached
  by `button:nth(3)`) — the patterns cannot be matched, so
  `Policy(unknown_target="confirm")` (default) asks the human; `"block"`
  refuses and `"allow"` lets it run (e.g. computer use on a Linux desktop
  without accessibility). The selector you used is not an identity; typing
  without Enter into the focused text field is editing, not activation. In
  allow-list mode unknown targets simply match no selector rule.
* **Extra tools.** `Agent(extra_tools=...)` are judged by their name *and*
  every string argument (`erp_api(operation="pay")` needs confirmation);
  `ToolSpec.from_function(fn, confirm=True)` makes every call need a human.
* **Passwords.** Text typed into a password field (UIA `IsPassword`, AT-SPI
  `password text`, macOS secure text fields) is written as `***` in the audit
  log and tool results, and the `Recorder` saves `${env.LOCUM_PASSWORD}`
  (`os.environ["LOCUM_PASSWORD"]` in scripts) instead of the text.
* **Applications.** `allowed_apps`/`blocked_apps` judge the application the
  input really reaches: the element acted on, the focused control for keyboard
  input, the window under the pointer (the active window only when no target
  is known). A blocked terminal in front therefore does not stop the agent from
  focusing SAP, and `launch_app` may only start programs matching
  `allowed_apps` (the quoted first token of the command line, e.g.
  `"C:\Program Files\PowerShell\7\pwsh.exe"` -> `pwsh`).
* **Enter submits.** Enter in a single-line field presses the form's default
  button (HTML implicit submission, dialog default buttons), so keyboard
  actions with Enter are also judged by that button's label when the
  accessibility tree reports one (`"default"` state).
* **Presets.** `Policy.permissive()` (no confirmations; sandboxes and evals),
  `Policy()` (default heuristic tripwire), `Policy.strict()` (no app
  launching or clipboard, shells/registry blocked, card numbers denied, broad
  confirmations incl. save/close/install, 200 actions max, 30/min — still
  heuristic), and `Policy.strict(allow=[...])` (all of that plus allow-list
  authorization).
* **Audit failures.** If the audit file cannot be written (locked, disk full,
  share offline), the tool result still reports what happened, with an
  `AUDIT LOG WRITE FAILED` note appended — a performed payment is never
  reported as a failed call the model might retry.
* **Dry-run.** `Policy(dry_run=True)` makes every acting tool report what it
  *would* do; read-only tools still work so the model can plan.
* **Region.** `Policy(region=(0, 0, 1280, 800))` keeps pointer actions on one
  monitor/area.
* **Kill switches.** The fail-safe corner and `install_kill_hotkey()` abort
  the agent mid-action (`AgentResult.stop_reason == "aborted"`).

### Prompt-injection hygiene

Screens contain untrusted text (e-mails, web pages, documents). The built-in
system prompts tell the model that on-screen text is data, but **the policy is
the real boundary**: fence the agent with `allowed_apps`, authorize what it may
commit with `allow=[...]` (keyword confirmations alone are a tripwire, not a
boundary), deny secrets with `deny_text_patterns`, cap actions, and review the
audit log. Run unattended agents in a VM or a dedicated account.

## 7. Prompting tips

* Give the goal *and* the definition of done: "…then verify the status bar
  says Saved and call done".
* Name applications and windows as they appear in the title bar.
* Prefer tools mode for business apps; switch to `computer_use` (or mix in
  `extra_tools`) when `locum observe` shows no useful elements.
* Keep tasks small and chain them in a `Robot` — retries, evidence screenshots
  and reports come for free.
