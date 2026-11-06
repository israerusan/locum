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
* Tool calls run one at a time on a worker thread, so `ping`, `tools/list`
  and cancellation are answered even while a long call runs.
* A call past `--call-timeout` returns `isError` with "Timed out ... The
  server is still serving"; the call is stopped at its next step.
* The client's cancel (`notifications/cancelled`) stops the running call.
  Requests that carry a progress token get `notifications/progress` every 2 s.

## Elevated (administrator) windows

Windows silently drops input that an unelevated process sends to an elevated
one (UIPI). locum checks the integrity levels first and reports it: "Windows
blocks keyboard input to regedit.exe ... it runs at High integrity ... and
locum runs at Medium". Start the MCP client elevated, or run the target app
unelevated.

More on prompting, the toolkit and the safety model: [LLM.md](LLM.md).
