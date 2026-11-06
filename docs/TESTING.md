# Testing desktop apps with locum

locum ships two things for tests:

* a **pytest plugin** (registered automatically when locum is installed) with
  `locum_desktop` and `locum_app` fixtures, a `--locum-backend` switch (use `mock`
  on CI) and a trace saved for every failing test;
* **traces** (`locum.trace`): a step-by-step record of everything a `Desktop`
  did -- method, arguments, resolved target, result or error, log notes and
  screenshots -- saved as one self-contained HTML page.

## The pytest plugin

```python
# test_calculator.py
def test_seven_plus_one(locum_app):
    calc = locum_app("calc.exe", window="Calculator", timeout=15)
    for name in ("Seven", "Plus", "One", "Equals"):
        calc.find(f'button "{name}"').click()
    assert "8" in calc.find("#CalculatorResults").name
```

```
pytest                                  # the platform's backend, a trace for each failure
pytest --locum-backend mock             # in-memory virtual desktop (CI, no display needed)
pytest --locum-trace on                 # save a trace for every test that uses `locum_desktop`
pytest --locum-trace off                # no tracing at all
pytest --locum-trace-dir build/traces   # where traces go (default: .locum-traces)
pytest --locum-screenshots all          # a screenshot after every step (default: errors only)
pytest --locum-aliases                  # also provide the fixtures as `desktop` and `app`
```

### Fixtures

| Fixture | What it gives you |
| --- | --- |
| `locum_desktop` | A fresh `Desktop` per test on the selected backend. Real backends run with the screen-corner fail-safe **on**; `mock` gets a new `MockBackend` per test (`desktop.backend`) and no fail-safe. Closed after the test (held keys/buttons released). |
| `locum_app` | A factory: `locum_app(command, window=None, *, timeout=None, **launch_kw)` launches through `locum_desktop.launch(command, wait_window=window)` and returns the new `Window` (`window=None`: any window that appears after the launch). At teardown it closes the windows **it** returned that are still open -- nothing else. |
| `locum_tracer` | The `Tracer` recording this test's `locum_desktop` (`None` with `--locum-trace off`), e.g. to assert on steps or save extra copies. |

The names are prefixed because the plugin is active in **every** project that
has locum installed. A plugin fixture called `app` would collide with the `app`
fixture of Flask, FastAPI or pytest-flask suites, and which one a test received
would depend on the order the plugins were registered in. With the `locum_`
prefix, locum never takes a name another plugin or your `conftest.py` uses. A
fixture with the same name in your `conftest.py` or test module still overrides
the plugin's (standard pytest rules).

**Short names (opt-in).** `--locum-aliases`, or `locum_fixture_aliases = true`
in the ini file, also provides the fixtures as `desktop` and `app`. Turn this on
only in suites where nothing else defines those names:

```toml
[tool.pytest.ini_options]
locum_fixture_aliases = true
```

**Upgrading.** Older versions named the fixtures `desktop` and `app`. Rename the
test parameters, or turn on the aliases.

### Options

Each option also has an ini key, so a project can set defaults in `pytest.ini`
/ `pyproject.toml` (`[tool.pytest.ini_options]`):

| Command line | ini key | Default |
| --- | --- | --- |
| `--locum-backend NAME` | `locum_backend` | `$LOCUM_BACKEND`, else the ini key, else the platform's backend |
| `--locum-trace {off,on,failed}` | `locum_trace` | `failed` |
| `--locum-trace-dir DIR` | `locum_trace_dir` | `.locum-traces` (relative to where pytest was started) |
| `--locum-screenshots {errors,all,none}` | `locum_screenshots` | `errors` |
| `--locum-aliases` | `locum_fixture_aliases` | `false` |

```toml
[tool.pytest.ini_options]
locum_trace = "failed"
locum_trace_dir = "build/locum-traces"
```

On CI, `LOCUM_BACKEND=mock pytest` (or `--locum-backend mock`) runs everything
against the in-memory desktop.

### Traces of failing tests

With the default `--locum-trace failed`, every test that uses `locum_desktop` is
traced and, when it fails (in setup, the test itself or teardown), the trace is
saved as `<trace dir>/<test id>.html` (characters other than letters, digits,
`.`, `_`, `-` become `_`). The failure report says where:

```
------------------------------- locum trace -------------------------------
trace saved: /work/.locum-traces/tests_test_invoice.py_test_submit.html
```

and the path is added to the test's `user_properties` as `locum_trace`, which
`--junitxml` writes as a `<property>` (CI systems can attach it as an artifact).
Tests that pass leave nothing behind.

**Screenshots.** Every step is recorded, but screenshots are taken only when they
are needed. This is the default, `--locum-screenshots errors`: one screenshot at
the step that failed, plus a final "Screen at failure" screenshot when the test
fails (this also covers an `assert` in the test that no step raised). A screenshot
captures the whole desktop and takes about as long as one
`desktop.screenshot()`. Taking one after every step of every test would slow the
suite down, and would put whatever else was on screen into every trace.
`--locum-screenshots all` adds a screenshot after every step, when you need the
full film strip. `none` takes no screenshots (steps are still recorded). Use
`--locum-trace off` to turn tracing off entirely.

### Testing against the mock backend

The mock backend is a full virtual desktop: windows, widgets, an
accessibility tree, a rendered screen. Build the scene through
`locum_desktop.backend`:

```python
def test_submit_invoice(locum_desktop):
    win = locum_desktop.backend.add_window("Invoice", (100, 100, 500, 400), app="erp")
    win.add_edit("Customer", (20, 50, 250, 28))
    sent = []
    win.add_button("Submit", (20, 340, 80, 30), on_click=lambda w: sent.append(True))

    locum_desktop.find('edit "Customer"').type("ACME")
    locum_desktop.click('button "Submit"')
    assert sent == [True]
```

To test code that uses `locum_app(...)` on the mock backend, make `launch` open a
window (the mock's own `launch` only records the command):

```python
import pytest

@pytest.fixture
def fake_calc(locum_desktop):
    be = locum_desktop.backend
    be.launch = lambda command, **kw: be.add_window("Calculator", (200, 200, 320, 480), app="calc")
    return be

def test_opens(fake_calc, locum_app):
    assert locum_app("calc.exe", window="Calculator").title == "Calculator"
```

## Traces in scripts

```python
from locum import Desktop
from locum.trace import Tracer, trace

d = Desktop()

# 1. Explicit: record, then save.
with Tracer(d) as t:
    d.window("Invoice").activate()
    d.click('button "OK"')
t.save_html("trace.html")       # or t.save_json("trace.json")

# 2. Saved on exit -- also when the block raises.
with trace(d, "traces/login.html", screenshots="errors"):
    d.find('edit "User"').type("ada")
    d.click('button "Sign in"')
```

`Tracer(desktop, *, screenshots=True, before=False, max_width=1280, quality=80,
redact=(), log_level=logging.INFO, title=None)`:

* `screenshots` -- `True` (after every step), `"errors"` (only after a failed
  step) or `False`. `before=True` adds one before each step too. Screenshots are
  downscaled to `max_width` and embedded as JPEG.
* `redact` -- strings that must never appear in the trace.
* `log_level` -- `locum.*` log records at this level or above are attached to the
  step that was running (self-healed selectors from `locum.heal` are marked
  "healed" in the timeline); records outside any step are listed separately.

What gets recorded: every public action and lookup of that `Desktop` instance
(`click`, `double_click`, `right_click`, `move`, `drag`, `scroll`, `type`,
`press`, `hotkey`, `find`, `find_all`, `exists`, `wait` -- including
`state="gone"` --, `window`, `launch`, `locate`, `wait_image`, `find_text`, ...;
see `locum.trace.DESKTOP_METHODS`), and the actions of the elements and windows
those lookups return (`d.find(...).click()`, `win.close()`...). Internal calls
(`click` resolving its selector with `find`) are not separate steps; they fill
in the step's target: the selector, the element's description, its bounds and
the clicked point.

Each step has: index, start time, duration, method, arguments, target, result
or error (type, message, traceback), annotations and screenshots. `t.steps` is
a list of `TraceStep`; `t.to_dict()` / `t.save_json()` give the same data as
JSON (`screenshots=False` leaves the images out).

The tracer wraps the methods on the instance only (the `Desktop` class is not
touched) and `stop()` -- or leaving the `with` block -- restores them exactly.
One tracer at a time per desktop.

### The HTML page

One file, no external assets: inline CSS/JS, screenshots as base64, every piece
of user content HTML-escaped. The left column is the timeline (green = passed,
red = failed, a "healed" chip where a selector was self-healed); clicking a step
(or using the arrow keys) shows its screenshot large with the target's bounds
outlined and the clicked point marked, plus selector, element, arguments,
result or error and annotations. The first failed step is selected on open.
Without JavaScript all steps are shown one after another.

### Secrets

These are recorded as `***`, and are also scrubbed from error and log messages:

* text typed into a password field (`d.type(...)` while one has focus,
  `element.type(...)` / `element.set_text(...)` on one);
* text put on the clipboard (`clipboard_set`), and text read from it
  (`clipboard_get` -- the clipboard often holds a password copied from a
  password manager);
* text that a call on a password field returns, and the text value of a
  `wait_until` predicate. `d.wait_until(lambda: field.value)` returns the field's
  content, so it is never written to the trace.

A password field's value never appears in element descriptions. Screenshots show
the screen as it is. Password fields show bullets, but any other sensitive data
on screen is captured. Use `screenshots="errors"` (the plugin's default) or
`False` where that matters, and `redact=[...]` for known strings in text.

Screenshots are of the **whole screen**, so a trace of a test run on your own
desktop also contains every other window that was visible (a terminal, mail,
a browser). This is why the plugin only takes screenshots on failure by default.
Keep the trace directory out of version control (`.locum-traces/` in
`.gitignore`), and run UI tests on a dedicated machine or VM, or with
`--locum-screenshots none` / `--locum-trace off`, when that matters.
