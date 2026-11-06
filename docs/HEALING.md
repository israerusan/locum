# Self-repairing selectors

When a selector stops matching (a button renamed from `Save` to `Save as`,
moved to another pane, or rebuilt with a new name but the same automation id),
locum can act on the element the selector most likely meant instead of
failing. It logs what changed so you can fix the script.

**It is off by default.** Acting on the wrong element is dangerous. You turn it
on explicitly:

```python
from locum import Desktop
from locum.heal import Fingerprint, HealOptions

d = Desktop(heal_selectors=True)                  # every find()/wait() on this desktop
d = Desktop(heal_selectors=HealOptions(threshold=0.8, margin=0.15))

el = d.find('window "Editor" button "Save"', heal=True)   # this call only
el.healed            # HealInfo or None
el.healed.diff       # "name 'Save' -> 'Save as'; moved 40px"
el.healed.new_selector
```

`heal=False` on a call turns it off even when the desktop default is on.
`locum.heal.suppressed()` (a context manager) turns it off on the current
thread, whatever the desktop or the call says.

## How a heal decides

Healing runs only after the normal lookup has timed out, so the normal
(matching) path is not slowed down. Then one scan of the candidates runs:

* **Scope.** For a multi-step selector, the candidates are the top-level windows
  that its first compound (`window "Editor"`) still matches. For a one-step
  selector, they are all roots. For `Element.find`/`root=`, they are inside
  that element or window. If the window part matches nothing, nothing heals.
* **Signals.** The selector's last compound is parsed for role, name
  (`"Save"`, `[name^=…]`, `:has-text`), automation id and class. A candidate
  is scored on:
  * role: the same role, or a close one such as `button` and `togglebutton`.
    An unrelated role rejects the candidate.
  * name similarity: difflib ratio, token overlap or whole-word containment,
    case-insensitive.
  * automation id: a match counts strongly (score at least 0.9) and a
    different id counts against the candidate.
  * class name.
  * with a fingerprint: distance to the previous position, size and window title.
* **Acceptance.** The best candidate's score must reach `threshold` (0.7 by
  default). It must also beat the runner-up by `margin` (0.1). Two plausible
  candidates fail with an `ambiguous:` reason, and the heal never guesses
  between them. A selector without a name or automation id never heals.

When a heal fails, the usual `ElementNotFound` message gets
` (self-healing: <reason>)` appended. When healing is off, the message is
unchanged.

The scan calls `locum._cancel.check()` for every node and visits at most
`HealOptions.max_nodes` nodes (20,000 by default). **A truncated scan does not
heal.** A "clear winner" among the nodes scanned so far may not be the best
match overall, and the real target may be among the nodes that were never
scanned. The lookup fails with the reason `scan truncated after N nodes`, and
`HealOutcome.truncated` is `True`. To fix it, raise `max_nodes` or narrow the
selector's scope (a `window "…"` prefix, `Element.find`). Use
`HealOptions(allow_partial=True)` only if you accept a decision based on part
of the tree.

## Logging

Every accepted heal is logged once at `WARNING` on the `locum.heal` logger:

```
healed selector 'button "Save"' -> 'window "Editor" button "Save as"' (score 0.90): name 'Save' -> 'Save as'
```

The log record carries `record.locum_heal`, which is `HealInfo.to_dict()`:
the old and new selectors, score, runner-up, diff, and old and new fingerprints.

## Fingerprints

A fingerprint is what the element looked like before: role, name,
automation_id, class_name, bounds, window and app. Store one next to a
selector and pass it back. A heal can then also use the old position, which
can break a tie between two look-alikes:

```python
fp = d.find('button "Save"').fingerprint()        # == Fingerprint.from_element(el)
saved = fp.to_dict()                              # {"role": "button", "name": "Save", "bounds": [l, t, w, h], ...}
d.find('button "Save"', heal=True, fingerprint=saved)
```

## Flows

```yaml
settings:
  heal: true                 # every step of this flow (and of sub-flows without their own setting)
steps:
  - click: 'button "Save"'
  - set_text: {target: 'edit "Customer"', text: ACME}
    heal: true               # or per step; heal: false switches it off again
    fingerprint: {role: edit, name: Customer, bounds: [120, 140, 200, 28]}
```

* A `heal:` on a control step (`repeat`, `for_each`, `try`...) applies to the
  steps inside it. The nearest explicit `heal:` wins over `settings.heal`, and
  `heal: false` also wins over `Desktop(heal_selectors=True)`.
* `fingerprint:` goes on the step that acts on the element. The validator
  rejects unknown keys and a fingerprint on a control step.
* Each heal is added to the step's report data (`healed: [HealInfo.to_dict()]`)
  and logged as a flow warning.
* Existence checks never heal: `if_exists`, `assert_exists`, `wait_gone`,
  `Desktop.exists` and `Element.exists`. "Is the old element still there?"
  must not become true because a look-alike exists.

## Where healing never happens

The LLM tool layer should not heal: a model should re-observe the screen
instead. Code that drives a `Desktop` on a model's behalf should run its
lookups inside `locum.heal.suppressed()` or pass `heal=False`.
