# pypilot architecture & module contracts

This file is the **source of truth for cross-module APIs**. Every module must
implement exactly the public names/signatures listed for it, so modules written
independently fit together. Adding extra helpers is fine; renaming is not.

Package root: `src/pypilot/`. Python >= 3.9 (use `from __future__ import annotations`,
`typing.Optional/List/...`, no `match`, no PEP 604 unions at runtime).
Hard deps: `pillow`, `numpy`, `mss` (+ `python-xlib` on Linux, `comtypes` on Windows,
`pyobjc` on macOS). Everything else is optional and imported lazily inside functions
(raise `NotSupported`/`ImportError` with an install hint like `pip install pypilot[llm]`).
**Importing any module must work on every OS with only hard deps installed**
(platform imports go inside functions / try-blocks).

## Layering

```
cli.py ─────────────────────────────────────────────────────────────┐
llm/ (tools, observe, safety, agent, computer_use, mcp_server)      │
rpa/ (robot, queue, flow, recorder, report)                         │
desktop.py  (Desktop facade)  +  compat/pyautogui.py                 │
element.py  window.py  selectors.py  wait.py                         │
mouse.py  keyboard.py  motion.py  failsafe.py  screen.py  vision.py  ocr.py
backends/base.py (Backend, AccessibleNode)   types.py  keys.py  errors.py
backends/{mock,x11,windows,macos}.py
```

Lower layers never import higher ones (except lazily inside a function).

## Foundation (already written — do not change signatures)

* `pypilot.types`: `Point(x,y)`, `Rect(left,top,width,height)` (`.center .right .bottom .contains .intersect .union .iou .offset .inflate .as_tuple .as_ltrb .to_dict`, `Rect.coerce`, `Rect.from_ltrb`), `MouseButton` (`.coerce`), `Monitor`, `WindowInfo`, `Match(rect, confidence, text="", scale=1.0, extra={})` with `.center`, `coerce_point(any)`.
* `pypilot.errors`: `PypilotError, BackendError, BackendUnavailable, NotSupported, FailSafeTriggered, ActionTimeout, ElementNotFound, AmbiguousElement, ImageNotFound, WindowNotFound, SelectorSyntaxError, PolicyViolation, WorkflowError`.
* `pypilot.keys`: `KEYS, MODIFIERS, normalize_key, is_modifier, parse_hotkey, SHIFTED_CHARS, char_needs_shift, split_text_tokens`.
* `pypilot.backends.base`: `Backend`, `AccessibleNode`, `ROLES`, `CAP_*` constants.
* `pypilot.backends`: `get_backend, set_backend, create_backend, register_backend`.
* `pypilot.backends.mock`: `MockBackend` (simulated desktop with widgets, a11y tree, rendering, event log `be.events`), `MockWindow`, `MockWidget`, `MockNode`. Use it in tests: `be = MockBackend(); w = be.add_window("T", (100,100,400,300)); b = w.add_button("OK", (10,10,80,30)); e = w.add_edit("Name", (10,50,200,28))`. Widget rects are window-relative; `widget.screen_rect` is absolute. Events: `("move",x,y)`, `("down",btn,x,y)`, `("up",btn,x,y)`, `("scroll",dx,dy,x,y)`, `("key_down",k)`, `("key_up",k)`, `("type",text)`, `("screenshot",region)`, ...

## Input layer

### `motion.py`
* `TWEENS: dict[str, Callable[[float], float]]` — `linear, ease_in_quad, ease_out_quad, ease_in_out_quad, ease_in_cubic, ease_out_cubic, ease_in_out_cubic, ease_out_back, ease_in_out_sine`.
* `@dataclass Humanize(speed: float = 1.0, jitter: float = 1.0, overshoot: bool = True, curvature: float = 1.0, seed: Optional[int] = None)` — `rng` property.
* `def plan_path(start: PointLike, end: PointLike, *, duration: float, tween: str|Callable = "ease_in_out_quad", humanize: Optional[Humanize] = None, rate: int = 120) -> List[Tuple[Point, float]]` — waypoints `(point, t_offset_seconds)`, last point == end exactly; humanized = randomized cubic Bézier + slight overshoot/correction + jitter, deterministic with seed.
* `def fitts_duration(distance: float, target_width: float = 20, speed: float = 1.0) -> float` — Fitts's-law natural movement time (clamped ~0.08–1.2s).

### `failsafe.py`
* `class FailSafe(backend, enabled: bool = True, corners: Optional[List[Point]] = None, margin: int = 0)` — `.check()` raises `FailSafeTriggered` if cursor in a corner (default: the 4 corners of the virtual screen) or if `abort()` was called. `.enabled` attr.
* Module-level: `abort(reason="")` (sets global abort event), `reset_abort()`, `is_aborted() -> bool`, `install_kill_hotkey(combo="ctrl+alt+shift+q") -> bool` (pynput, optional; returns False if unavailable).

### `mouse.py`
`class Mouse(backend, failsafe: Optional[FailSafe] = None, pause: float = 0.0, humanize: Optional[Humanize] = None, sleep=time.sleep)`
* `position() -> Point`
* `move(x, y=None, *, duration: Optional[float] = None, tween="ease_in_out_quad", humanize: Optional[Humanize|bool] = None) -> Point` — `x` may be any point-like (then `y` None). `duration=None` → instant unless humanize active (then Fitts). Calls `failsafe.check()` before and during moves.
* `move_rel(dx, dy, **kw)`
* `down(button="left")`, `up(button="left")`
* `click(x=None, y=None, *, button="left", clicks=1, interval=0.0, duration=None, humanize=None) -> Point` (x may be point-like or None = current position)
* `double_click(...)`, `triple_click(...)`, `right_click(...)`, `middle_click(...)`
* `drag(start, end, *, button="left", duration=0.5, humanize=None, hold=0.05)` (start None = current position)
* `scroll(dy=0, dx=0, *, at=None)` — notches, dy>0 = up.

### `keyboard.py`
`class Keyboard(backend, failsafe: Optional[FailSafe] = None, pause: float = 0.0, sleep=time.sleep)`
* `down(key)`, `up(key)` (accept aliases; normalize via `keys.normalize_key`)
* `press(*keys, presses=1, interval=0.0)` — taps each key in sequence (`press("tab", "tab", "enter")`)
* `hotkey(*keys)` — `hotkey("ctrl+shift+s")` or `hotkey("ctrl","c")`; press in order, release in reverse **in a finally** (never leave stuck modifiers).
* `hold(*keys)` — context manager.
* `type(text, *, interval=0.0, mode="auto")` — modes: `"unicode"` (backend.type_text), `"keys"` (per-char key taps, shift for SHIFTED_CHARS/uppercase, `\n`→enter, `\t`→tab), `"clipboard"` (save clipboard → set → paste via `mod+v` → restore), `"auto"` (unicode if backend has CAP_UNICODE_TYPING; else keys for ASCII, clipboard for non-ASCII).
* `release_all()` — releases every key this instance pressed.

## Screen & vision

### `screen.py`
`class Screen(backend)`
* `capture(region: Optional[RectLike] = None, *, monitor: Optional[int] = None) -> PIL.Image` (RGB)
* `monitors() -> List[Monitor]`, `size() -> Tuple[int,int]` (primary), `virtual_rect() -> Rect`
* `pixel(x, y) -> Tuple[int,int,int]`, `pixel_matches(x, y, color, tolerance=0) -> bool`
* `wait_for_change(region=None, *, timeout=10, interval=0.2, threshold=0.01) -> bool`
* `wait_until_stable(region=None, *, stable_for=0.5, timeout=10, interval=0.1, threshold=0.001) -> bool`
* `save(path, region=None) -> str`

### `vision.py` (pure functions; numpy; use cv2 only if importable)
* `load_image(src) -> PIL.Image` — path / `pathlib.Path` / PIL / numpy array / bytes / base64 str / data URL.
* `locate(needle, haystack, *, confidence=0.9, grayscale=False, scales: Sequence[float] = (1.0,), region: Optional[RectLike] = None, offset=(0,0)) -> Optional[Match]` — normalized cross-correlation (FFT); **alpha channel of the needle acts as a mask**; result rects are in haystack coords (+offset).
* `locate_all(needle, haystack, *, confidence=0.9, grayscale=False, scales=(1.0,), limit=100, overlap=0.3, region=None, offset=(0,0)) -> List[Match]` — non-max suppression, sorted by confidence desc.
* `find_color(haystack, color, *, tolerance=0, region=None, offset=(0,0)) -> List[Rect]` — connected regions of matching pixels.
* `image_diff(a, b) -> float` — 0.0 identical .. 1.0 totally different (mean abs diff normalized).
* `annotate(image, boxes: Sequence[Tuple[RectLike, str]], *, color=(255,0,0), offset=(0,0)) -> PIL.Image` — draw labeled boxes (used by set-of-marks).
* `to_base64_png(image, *, max_size: Optional[int] = None) -> str`, `downscale(image, max_long_edge) -> Tuple[PIL.Image, float]` (returns scale factor applied, ≤1).

### `ocr.py`
* `@dataclass OCRWord(text: str, rect: Rect, confidence: float, line: int = 0)`
* `class OCREngine(Protocol)`: `read(image) -> List[OCRWord]`
* `register_ocr_engine(name, factory)`, `get_ocr_engine(name: Optional[str] = None) -> OCREngine` (auto: tesseract via pytesseract → rapidocr → easyocr; raise `NotSupported` with install hint).
* `read_text(image, *, engine=None) -> str` (lines joined by `\n`)
* `find_text(image, query, *, engine=None, regex=False, case_sensitive=False, fuzzy: float = 1.0, offset=(0,0)) -> List[Match]` — matches single words or multi-word phrases on the same line; `fuzzy<1` uses difflib ratio.

## UI layer

### `wait.py`
* `wait_until(predicate, timeout=10.0, interval=0.1, *, message=None, backoff=1.0, max_interval=1.0, ignore_exceptions=True, sleep=time.sleep, clock=time.monotonic)` → returns the first truthy result; raises `ActionTimeout(message)`; `timeout=0` = single check.
* `retry(fn=None, *, attempts=3, delay=0.5, backoff=2.0, max_delay=30.0, exceptions=(Exception,), on_retry=None, sleep=time.sleep)` — decorator (with/without args) **and** `retry(fn, attempts=...)()`-style use via `retry_call(fn, *args, attempts=..., **kwargs)`.
* `class Deadline(timeout)` — `.remaining`, `.expired`, `.sleep(interval)`.

### `selectors.py`
Selector language (CSS-like, plus UiPath-XML and dict forms), matching `AccessibleNode` trees:
* role / `*`; `#id` → automation_id; `.cls` → class_name
* attributes `[name="OK"]`, ops: `=` exact, `!=`, `*=` contains, `^=` prefix, `$=` suffix, `~=` regex search, `%=` glob (`*`/`?` wildcards); trailing ` i` flag = case-insensitive: `[name="ok" i]`. Bare-word values allowed: `[name=OK]`.
* attribute aliases: `title`→name, `id`→automation_id, `class`/`cls`→class_name, `text`→name-or-value, `app`, `pid`, `role`, `value`, `description`, `handle`; anything else → `node.attributes()`.
* pseudo-classes: `:enabled :disabled :visible :focused :checked :selected :editable`, `:has-text("x")` (name or value contains, case-insensitive), `:has(sel)`, `:not(sel)`, `:nth(n)` (0-based index among matches of the compound so far; negative from end), `:first`, `:last`.
* shorthand: `button "OK"` ≡ `button[name="OK"]`; a bare quoted string `"OK"` ≡ `*[text="OK"]`.
* combinators: descendant (space), child `>`; union `,`.
* UiPath XML: `<wnd app='excel.exe' title='*Book1*' /><ctrl name='OK' role='push button' />` — attribute values with `*`/`?` are globs; `wnd`=top-level window, `ctrl`= descendant; `aaname`→name, `cls`→class_name, `automationid`→automation_id, UiPath roles mapped (`push button`→button, `editable text`→edit, ...).
* dict: `{"role": "button", "name": "OK", "within": {...}}`.
* API: `parse(selector: str|dict|Selector) -> Selector` (cached), `Selector.find_all(roots: Sequence[AccessibleNode], *, max_depth=64, limit=None) -> List[AccessibleNode]`, `Selector.find_first(roots, max_depth=64) -> Optional[AccessibleNode]`, `Selector.matches(node) -> bool` (compound only, ignores ancestry), `str(Selector)` round-trips to the CSS form.
* `def selector_for(node: AccessibleNode, *, root: Optional[AccessibleNode] = None) -> str` — generate a robust selector for a node (prefers automation_id, then role+name, adds window ancestor and `:nth` if ambiguous). Used by the recorder & LLM layer.
* `def tree_text(roots, *, max_depth=8, interactive_only=False, max_nodes=2000, include_bounds=True) -> str` — indented human/LLM readable dump: `button "OK" #okBtn (120,340 80x30) [focused]`.

### `element.py`
`class Element(node: AccessibleNode, desktop: "Desktop", selector: Optional[str] = None)`
* properties: `node, desktop, selector, role, name, value, automation_id, class_name, bounds (Optional[Rect]), center (Point; raises ElementNotFound if no bounds), states, is_enabled, is_visible, is_focused, is_checked`
* tree: `parent -> Optional[Element]`, `children() -> List[Element]`, `find(selector, timeout=None) -> Element`, `find_all(selector) -> List[Element]`, `exists(selector, timeout=0) -> bool`
* actions (all return `self` for chaining):
  `click(button="left", clicks=1, *, offset=(0,0), native: bool|str = "auto")` — "auto": mouse click at center if visible with bounds, else native `invoke`; `native=True` → invoke only (raise if unsupported); `native=False` → mouse only.
  `double_click()`, `right_click()`, `hover()`, `invoke()`, `focus()`,
  `set_text(text, *, native="auto", clear=True)` — native `set_value`, else focus/click + `mod+a` + type (mode via desktop keyboard),
  `type(text, **kw)`, `clear()`, `press(*keys)`, `toggle()`, `check()`, `uncheck()`, `select()`, `expand()`, `collapse()`, `scroll_into_view()`, `scroll(dy)`,
  `screenshot() -> PIL.Image`, `wait_until(state="visible"|"enabled"|"focused"|"gone"|callable, timeout=None) -> Element`, `refresh() -> Element` (re-resolve via selector), `highlight(duration=1.0)` (optional, may no-op), `to_dict() -> dict`, `__repr__`.

### `window.py`
`class Window(info: WindowInfo, desktop: "Desktop")`
* properties: `info, handle, title, rect, app, pid, is_active, is_minimized, is_maximized`
* `activate()`, `minimize()`, `maximize()`, `restore()`, `close()`, `move(x, y)`, `resize(w, h)`, `set_rect(rect)` (all return self), `refresh() -> Window`, `screenshot()`, `element -> Element` (the window's a11y root: accessibility root whose `handle` == window handle, else whose name == title), `find(selector, timeout=None) -> Element`, `find_all(selector) -> List[Element]`, `wait_closed(timeout=10)`, `to_dict()`.
* `def match_window(info: WindowInfo, title=None, app=None, class_name=None, pid=None) -> bool` — title: exact string match first, else if str contains glob chars → glob, `re.Pattern` → regex search; plain str also matches as case-insensitive substring. app: case-insensitive, `.exe` optional.

## Facade: `desktop.py` — `class Desktop`

```python
Desktop(backend: Backend|str|None = None, *, failsafe: bool = True, pause: float = 0.0,
        humanize: bool|Humanize = False, default_timeout: float = 10.0, typing_mode: str = "auto")
```
Attributes: `backend, mouse (Mouse), keyboard (Keyboard), screen (Screen), failsafe (FailSafe), default_timeout, humanize (Optional[Humanize])`.

Mouse: `position()`, `move(target, y=None, *, duration=None, humanize=None)`, `click(target=None, y=None, *, button="left", clicks=1, interval=0.0, offset=(0,0), duration=None)`, `double_click`, `right_click`, `middle_click`, `triple_click`, `mouse_down(button)`, `mouse_up(button)`, `drag(start, end, *, duration=0.5, button="left")`, `scroll(dy=0, dx=0, *, at=None)`.
`target` resolution (shared helper `Desktop.resolve_point(target, y=None) -> Point`): `None` → current pos; point-like / Rect / Match / Element → coerce; `str` → treated as a **selector** (find element, use its center); `pathlib.Path` or `PIL.Image` → template locate on screen (raise ImageNotFound).

Keyboard: `type(text, *, interval=0.0, mode=None)`, `press(*keys, presses=1, interval=0.0)`, `hotkey(*keys)`, `key_down(key)`, `key_up(key)`, `hold(*keys)`.

Screen/vision: `screenshot(region=None, *, monitor=None, path=None) -> Image`, `monitors()`, `size()`, `pixel(x, y)`, `locate(image, *, region=None, confidence=0.9, grayscale=False, scales=(1.0,), timeout=0.0) -> Optional[Match]`, `locate_all(image, **kw) -> List[Match]`, `wait_image(image, *, timeout=None, **kw) -> Match` (raises ImageNotFound), `click_image(image, *, timeout=None, button="left", clicks=1, **kw) -> Match`, `find_text(text, *, region=None, timeout=0.0, **kw) -> Optional[Match]`, `click_text(text, *, timeout=None, **kw) -> Match`, `read_text(region=None) -> str`, `wait_for_change(...)`, `wait_until_stable(...)`.

Elements: `find(selector, *, timeout=None, root: Optional[Element|Window] = None) -> Element` (raises ElementNotFound after timeout; `timeout=None` → `default_timeout`), `find_all(selector, *, root=None) -> List[Element]`, `exists(selector, *, timeout=0.0) -> bool`, `wait(selector, *, state="visible", timeout=None) -> Optional[Element]` (state `"gone"` returns None), `element_at(x, y) -> Optional[Element]`, `focused() -> Optional[Element]`, `tree(*, window=None, max_depth=8, interactive_only=False) -> str`, `roots() -> List[AccessibleNode]`.

Windows: `windows(title=None, *, app=None) -> List[Window]`, `window(title=None, *, app=None, timeout=None) -> Window` (raises WindowNotFound), `active_window() -> Optional[Window]`, `launch(command, *, wait_window: Optional[str] = None, timeout=None, **kw)` (returns process or Window when wait_window given).

Clipboard: `clipboard_get() -> str`, `clipboard_set(text)`. Misc: `sleep(s)`, `wait_until(predicate, timeout=None, interval=0.1)`, `close()`, context manager (`with Desktop() as d:`).

Module `pypilot/__init__.py` exposes `Desktop`, the types/errors, `__version__`, `get_desktop()` (lazy default instance) and module-level convenience functions bound to the default desktop: `click, double_click, right_click, move, drag, scroll, type, press, hotkey, screenshot, locate, find, find_all, window, windows, wait, launch, position, size` (implemented lazily so `import pypilot` never touches the display).

`compat/pyautogui.py` — drop-in subset of the PyAutoGUI API on top of Desktop: `FAILSAFE, PAUSE, size, position, onScreen, moveTo, moveRel/move, dragTo, dragRel/drag, click, doubleClick, tripleClick, rightClick, middleClick, mouseDown, mouseUp, scroll, hscroll, write/typewrite, press, keyDown, keyUp, hotkey, hold, screenshot, locateOnScreen, locateAllOnScreen, locateCenterOnScreen, pixel, pixelMatchesColor, getActiveWindow/getAllWindows/getWindowsWithTitle (minimal), FailSafeException, ImageNotFoundException, KEYBOARD_KEYS, easeInOutQuad etc tweens, sleep`.

## RPA layer: `rpa/`
* `rpa/robot.py`: `Robot(desktop=None, name="robot", run_dir="pypilot_runs", screenshot_on_error=True, logger=None)`; `@robot.task(name=None, retries=0, retry_delay=1.0, timeout=None, continue_on_error=False)`; `with robot.step("name"):` (single attempt; `retries>0` with a plain `with` raises `TypeError`), retried steps as `for attempt in robot.step("name", retries=n, retry_delay=1.0): with attempt: ...` or `robot.step("name", retries=n).run(fn, *args)`; `robot.run(*task_names) -> RunReport`; per-step timing, error capture with screenshot; `robot.log(msg)`; `robot.vars` dict.
* `rpa/report.py`: `StepResult`, `RunReport` (`.ok`, `.steps`, `.to_dict()`, `.save_json(path)`, `.save_html(path)`).
* `rpa/queue.py`: `WorkQueue` (UiPath-Orchestrator-style transaction queue): add items (dict payloads), load from CSV/JSON/XLSX (openpyxl optional), `process(handler, *, retries=1)` with per-item status `new/in_progress/success/failed/business_exception`, `BusinessException` (no retry), persistence to JSON file so runs resume. Items a crashed run left `in_progress` are marked failed (`Abandoned`) on resume unless `on_abandoned="requeue"`; attempts are persisted and enforced across runs; file rows without `reference_field` get content-hash references (`<abs path>#<sha256[:16]>`).
* `rpa/flow.py`: declarative YAML/JSON workflows: `run_flow(path_or_dict, desktop=None, variables=None) -> RunReport`. Steps: `click, double_click, right_click, type, press, hotkey, set_text, invoke, toggle, select, expand, collapse, check, uncheck, wait, wait_gone, wait_image, click_image, click_text, screenshot, launch, activate_window, sleep, assert_exists, set_var, read_value, clipboard_set, log, if_exists {then, else}, for_each {items|csv, as, steps}, try {steps, catch}, call (sub-flow), repeat {times, steps}`. `${var}` interpolation (incl. `${item.field}`, `${env.NAME}`).
* `rpa/recorder.py`: `Recorder(desktop=None)` — `.start()`, `.stop() -> List[RecordedAction]`, uses pynput if available; enriches clicks with `selectors.selector_for(backend.element_at(x,y))` (UiPath-style selector recording, not raw coords); `.to_python() -> str`, `.to_flow() -> dict` (YAML-ready). Also `Recorder.feed(event)` for programmatic/test use without pynput.

## LLM layer: `llm/`
* `llm/observe.py`: `@dataclass ObservedElement(id: int, role, name, value, bounds: Rect, states, selector: str, node)`; `@dataclass Observation(screenshot: Optional[Image], elements: List[ObservedElement], windows: List[WindowInfo], active_window, cursor: Point, screen_size, scale: float, annotated: Optional[Image])` with `.text(max_elements=300) -> str`, `.element(id) -> ObservedElement`, `.to_screen(x, y) -> Point` (map LLM image coords → screen using `scale`), `.to_image(x, y)`, `.image_base64(annotated=True) -> str`. `observe(desktop, *, screenshot=True, annotate=True, max_long_edge=1568, interactive_only=True, window=None, max_elements=300) -> Observation` — set-of-marks: numbered boxes on the screenshot matching element ids.
* `llm/safety.py`: `Policy(allow_tools=None, deny_tools=(), allowed_apps=None, blocked_apps=(), deny_text_patterns=(), confirm_patterns=DEFAULT_CONFIRM (e.g. delete|remove|pay|transfer|wire|submit|send|purchase|approve|post), confirm: Optional[Callable[[str, dict], bool]] = None, dry_run=False, max_actions=None, max_actions_per_minute=None, region: Optional[Rect]=None)`; `Policy.check(tool_name, args, *, context: dict) -> None` raises PolicyViolation; `AuditLog(path=None)` JSONL `record(event: dict)`; `Policy.permissive()`, `Policy.strict()` presets.
* `llm/tools.py`: `@dataclass ToolResult(text: str = "", image_base64: Optional[str] = None, is_error: bool = False, data: Any = None)`; `ToolSpec(name, description, parameters: dict JSON schema, fn)`; `ToolRegistry` with `.register(fn=None, *, name=None, description=None)` decorator (schema from type hints + docstring Args section), `.call(name, args) -> ToolResult` (never raises; errors → is_error), `.to_anthropic() -> List[dict]` (`name, description, input_schema`), `.to_openai()`, `.to_mcp()` (`name, description, inputSchema`), `.names()`. `DesktopToolkit(desktop, policy=None, audit=None, observe_kwargs=None)` builds a registry with tools: `observe, screenshot, click (element_id | x,y | selector | text), double_click, right_click, type_text (optional element_id/selector first), press_keys, hotkey, scroll, move_mouse, drag, set_value, find_elements, wait_for, list_windows, focus_window, launch_app, read_clipboard, write_clipboard, read_text (OCR), wait (seconds)`. Element ids refer to the last observation; coordinates in tool args are **screen coordinates of the last observation's image space** (converted via `Observation.to_screen`). Every call goes through `policy.check` and `audit.record`.
* `llm/computer_use.py`: `ComputerUseAdapter(desktop, *, max_long_edge=1568|auto, policy=None)` executes Anthropic computer-use actions: toolset members (`screenshot, zoom, left_click, right_click, middle_click, double_click, triple_click, left_click_drag, mouse_move, left_mouse_down, left_mouse_up, cursor_position, scroll, type, key, hold_key, wait`) with params `coordinate, start_coordinate, text (modifiers for clicks/scroll; key combo for key/hold_key; xdotool-style names like Return, ctrl+s, alt+Tab, super), scroll_direction, scroll_amount, repeat, duration, region` and legacy `input.action` form. `.tool_definition()` → `{"type": "computer_toolset_20260801"}` (+ optional configs), `.execute(name, input) -> ToolResult`, `.run_batch(tool_use_blocks) -> List[dict tool_result]` (sequential, stop at first failure, `"Not executed: an earlier computer action in this turn failed."`, every result includes `"toolset_name": "computer"`). Screenshots downscaled so long edge ≤ 2576 and ceil(w/28)*ceil(h/28) ≤ 4784; coordinates scaled back.
* `llm/agent.py`: `Agent(desktop=None, *, model="claude-opus-5-5", client=None, mode="tools"|"computer_use", policy=None, audit=None, max_steps=50, system_prompt=None, on_step=None, effort="high")`; `.run(task: str) -> AgentResult(success, final_text, steps: List[AgentStep], usage)`. Anthropic SDK (`anthropic` optional import): `client.messages.create(model, max_tokens=16000, system, tools, messages, output_config={"effort": ...})` — no `temperature`, no `budget_tokens`, no forced `tool_choice`, no prefill; loop while `stop_reason == "tool_use"`; append full `response.content`; return all tool_results in one user message; handle `refusal`/`max_tokens` stop reasons. A `done`/`finish` tool lets the model declare completion with a summary. `LLMClient` protocol for model-agnostic use + `ScriptedClient` (replays canned responses; for tests/evals).
* `llm/mcp_server.py`: dependency-free MCP server over stdio (JSON-RPC 2.0, newline-delimited): `initialize` (protocolVersion echo / "2025-06-18", capabilities.tools), `notifications/initialized`, `tools/list`, `tools/call` (content: text and `{"type":"image","data":b64,"mimeType":"image/png"}`, `isError`), `ping`. `serve(desktop=None, policy=None, stdin=sys.stdin, stdout=sys.stdout)`; `handle_message(dict) -> Optional[dict]` for tests.

## CLI: `cli.py` (`pypilot ...`, argparse)
`info`, `screenshot [-o out.png] [--region L,T,W,H] [--monitor N]`, `windows`, `tree [--window TITLE] [--depth N] [--all]`, `find SELECTOR`, `click TARGET` (selector, or `x,y`), `type TEXT`, `hotkey COMBO`, `locate IMAGE [--confidence]`, `observe [--json]`, `mcp [--policy strict|default|permissive] [--dry-run]`, `agent TASK [--model] [--max-steps] [--dry-run] [--mode tools|computer_use]`, `run FLOW.yaml [--var k=v]`, `record [-o out.py|out.yaml]`. Global `--backend NAME`.

## Testing conventions
* `pytest`, tests in `tests/test_<module>.py`. Use `MockBackend` everywhere; never require a display except tests marked `@pytest.mark.x11` which must `pytest.skip` when `$DISPLAY` / Xvfb is unavailable.
* Inject `sleep=lambda s: None` / tiny durations to keep tests fast (< 2s per file).
* Run: `cd /home/user/pypilot && python -m pytest -q tests/test_<module>.py`.
