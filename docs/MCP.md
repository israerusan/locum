# Using locum with Claude Code and Claude Desktop

locum ships an MCP server (JSON-RPC over stdio, no extra dependencies) that
gives Claude the desktop tools: `observe`, `screenshot`, `click`, `type_text`,
`find_elements`, `wait_for`, `list_windows`, `launch_app` and the rest.

```
pip install -U locum
```

## The server command

Any of these start the same server; every option below works with all of them:

| Command | Use when |
|---|---|
| `locum mcp` | `locum` is on the client's `PATH` (recommended) |
| `locum-mcp` | same, as a single executable (handy in JSON configs) |
| `python -m locum.mcp` | you want to pin a specific interpreter / virtualenv |
| `python -m locum mcp` | same as above (older spelling) |

If the client reports `CONNECTION_CLOSED` right after start, the command
failed before speaking MCP: run it in a terminal (`locum mcp --help`) to see
the error. The usual causes are a different Python than the one locum is
installed in, or a module name typo.

## Claude Code

```
claude mcp add locum -- locum mcp --policy default
```

With a specific interpreter (a virtualenv, or when `locum` is not on `PATH`):

```
claude mcp add locum -- /path/to/venv/bin/python -m locum.mcp --policy default
# Windows
claude mcp add locum -- C:\path\to\venv\Scripts\python.exe -m locum.mcp --policy default
```

Add `--scope project` to write it into the repository's `.mcp.json` instead of
your user settings. Check it with `claude mcp list`.

## Claude Desktop

`claude_desktop_config.json` (Settings > Developer > Edit Config):

```json
{
  "mcpServers": {
    "locum": {
      "command": "locum-mcp",
      "args": ["--policy", "default", "--audit", "~/.locum/mcp-audit.jsonl"]
    }
  }
}
```

or with an interpreter: `"command": "C:\\path\\to\\python.exe", "args": ["-m", "locum.mcp", "--policy", "default"]`.
Restart Claude Desktop after editing.

## Options

| Option | Meaning |
|---|---|
| `--policy strict\|default\|permissive` | safety preset. `default` asks before pay/submit/delete-style actions; over MCP (no mid-call prompt) those are blocked with an explanation the model can relay to you |
| `--allow-app APP` | only drive these apps (repeatable, globs) |
| `--dry-run` | simulate every action; nothing touches the desktop |
| `--audit FILE` | append a JSON-lines log of every tool call |
| `--max-actions N` | stop after N actions |
| `--call-timeout SEC` | a tool call that runs longer gets a timeout error and is stopped (default 60; `wait_for` with a longer `timeout` gets that plus 15 s; `0` = no limit) |
| `--backend mock` | an in-memory practice desktop (put it anywhere on the command line) |

## Responsiveness on busy desktops

* `observe` and `screenshot` spend at most 8 s walking the accessibility
  trees. Windows are read front-most first, and no single application (a
  browser tab with thousands of nodes) may use more than its share; anything
  cut short is named in a `Note:` line of the result. Observe one window
  (`observe(window="Calculator")`) to see the rest.
* `observe(changes_only=true)` after an earlier observe returns only what
  changed: new, changed and gone elements (elements still there keep their
  ids, new ones never reuse an id) and a screenshot cropped to the visible
  change, or no image at all when nothing visible changed. On a busy 1080p
  desktop that is ~1-2 KB of text and ~160 KB of image instead of ~24 KB and
  ~1.5 MB. Changes hidden behind other windows add no image; tiny isolated
  pixel changes (a caret, the clock) are ignored.
* On Windows, waits for elements and windows wake on UI Automation change
  events instead of re-searching the tree every 0.1 s, and `observe` reuses the
  elements of windows whose app raised no event since the previous call
  (`LOCUM_UIA_EVENTS=0` turns events off).
* Tool calls run one at a time on a worker thread, so `ping`, `tools/list`
  and cancellation are answered even while a long call runs.
* A call past `--call-timeout` returns `isError` with "Timed out ... The
  server is still serving"; the call is stopped at its next step.
* The client's cancel (`notifications/cancelled`) stops the running call.
  Requests that carry a progress token get `notifications/progress` every 2 s.

## Web pages (Edge / Chrome over DevTools)

A browser tab's accessibility tree can hold thousands of nodes. For page
content locum talks to the browser directly over the Chrome DevTools Protocol
(standard library only) with the `browser_*` tools:

| Tool | Does |
|---|---|
| `browser_launch(browser="edge", url=None, headless=False)` | start Edge/Chrome with a **separate, locum-managed profile** (`~/.locum/browser/<name>`, override with `LOCUM_BROWSER_PROFILES`) and a DevTools port; reuses it if running |
| `browser_pages()` | list the tabs |
| `browser_observe(tab=None)` | the page's interactive elements with ids, title, URL, visible text (optional screenshot) |
| `browser_click(element=… \| selector=…)` | click (real mouse events inside the page; nothing on your screen moves) |
| `browser_type(text, element=…, clear=, submit=)` | type into a field, optionally press Enter |
| `browser_navigate(url)` / `browser_read(selector=None)` | open a URL / read text |

Two ways to connect, both opt-in:

* **locum-managed profile** (`browser_launch`): the recommended way. Your
  everyday profile is never touched; sign in once in the managed profile and
  it stays signed in there.
* **attach** to a browser you started yourself with a DevTools port, e.g.
  `msedge --remote-debugging-port=9222 --user-data-dir=C:\automation-profile`
  (Chrome 136+ ignores the port on the default profile), and tell locum the
  port with `LOCUM_CDP_PORT=9222` (Python: `Browser.attach(port=9222)`).

Which browser locum attaches to when no port is given, in order:
`$LOCUM_CDP_PORT`, then a running browser with a locum-managed profile. It does
**not** pick the `DevToolsActivePort` of your everyday Edge/Chrome profile or
whatever answers on port 9222 (that may be your signed-in browser or some other
program) unless you opt in with `LOCUM_CDP_ATTACH_ANY=1` (Python:
`Browser.attach(allow_any=True)`). Every endpoint must answer `/json/version`
like a Chromium DevTools server, and locum only connects to DevTools
WebSockets on this machine (`127.0.0.1`, `localhost`, `::1`); tabs whose
WebSocket points elsewhere are ignored.

While a browser is connected, `observe` skips that browser window's page
content (its tabs and toolbar stay listed) and says to use `browser_observe`.
The browser tools go through the same safety policy as everything else
(app allow-lists use `msedge.exe` / `chrome.exe`; a "Pay now" click asks for
confirmation; passwords are redacted, and a field's value -- e.g. an autofilled
password -- is never used as its name). `browser_type(..., submit=True)` is
judged by what Enter activates: the field's form's default button (its first
submit button, e.g. "Pay now") and the form's action URL, or, outside a form,
the field's nearby button (a chat's "Send") -- so typing a card number and
pressing Enter needs the same confirmation as clicking "Pay now". When what Enter
activates cannot be seen -- the focus is inside an embedded cross-origin frame
(Stripe-style card fields), the form has no submit button, or the button has no
name -- the submit is treated like a click on an unidentified control (asks). A cancelled
or timed-out tool call sends no further input to the page. DevTools gives full
control of that browser, so only connect to browsers you started for automation.

## Elevated (administrator) windows

Windows silently drops input that an unelevated process sends to an elevated
one (UIPI). locum checks the integrity levels first and reports it: "Windows
blocks keyboard input to regedit.exe ... it runs at High integrity ... and
locum runs at Medium". Start the MCP client elevated, or run the target app
unelevated.

More on prompting, the toolkit and the safety model: [LLM.md](LLM.md).
