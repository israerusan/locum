# API stability

What locum promises not to break, how a break is announced, and how that
promise is enforced. Changes are listed in [CHANGELOG.md](../CHANGELOG.md).

## What is public

The public API is exactly:

1. **Python names** in the `__all__` of these modules:
   `locum`, `locum.errors`, `locum.selectors`, `locum.rpa`, `locum.llm`,
   `locum.browser`, `locum.trace`, `locum.heal`, `locum.a11y`,
   `locum.compat.pyautogui`, and the bridge and credential modules
   (`locum.bridges.*`, `locum.rpa.credentials`) once they are released.
   For each name, the public part is its signature: parameter names, order,
   keyword-only markers and defaults. For a class, it is also the public
   methods and properties the class defines.
2. **The selector language** (docs in `locum.selectors` and the README).
3. **The CLI**: documented commands, flags and exit codes, and the
   `--json` output shape (one JSON document, `ok` plus fields; new fields may be
   added).
4. **The YAML/JSON flow format** (`locum run`): step names, their arguments,
   and variable syntax.
5. **MCP and LLM tool names and arguments** (`locum mcp`, `DesktopToolkit`).
6. **Documented environment variables** (`LOCUM_BACKEND`, `LOCUM_DESKTOP_LOCK`,
   `LOCUM_UIA_EVENTS`, ...).

Everything else is internal and may change in any release: modules and names
that start with `_`, everything under `locum.backends.*` except
`register_backend`/`create_backend`, the HTML layout of reports and traces,
log messages, and the wording of exception messages (the exception **types**
are public).

## Versioning

locum follows [Semantic Versioning](https://semver.org/). Before 1.0:

| Release | May contain |
| --- | --- |
| `0.x.Y` (patch) | Fixes and additions only. **No breaking change.** |
| `0.X.0` (minor) | Breaking changes, each listed under **Breaking** in the changelog with a migration line. |

From 1.0, a breaking change needs a major version.

A **breaking change** removes or renames a public name or parameter,
reorders positional parameters, changes a default so the same code behaves
differently, narrows accepted input, or changes a CLI flag, flow step or tool
argument in a way that breaks existing scripts. A safety fix may tighten
behaviour in a patch release when the old behaviour was a vulnerability. It is
still marked **Breaking** in the changelog.

> 0.1.7 broke the CLI (`--window`/`--focused` became required) in a patch
> release. Under these rules that would have needed 0.2.0. It is the last time.

## Deprecations

Before a public name or parameter is removed, it keeps working for at least
**one minor release** (pre-1.0) and emits a `DeprecationWarning` that names the
replacement. The changelog lists it under **Deprecated** when the warning is
introduced and under **Removed** when it goes.

## How it is enforced

`tests/data/public_api.txt` is a snapshot of every public Python name with its
signature. `tests/test_public_api.py` runs on every CI job and fails if the
snapshot no longer matches the code. An API change is therefore always a
deliberate, reviewable edit:

```
python tools/api_snapshot.py --check   # what changed
python tools/api_snapshot.py --write   # accept it, then add the CHANGELOG entry
```

Long default tables (keyword lists, sets of tool names) appear as their type
only, so tuning them is not an API change. Changing behaviour through them can
still be a breaking change and goes in the changelog.

## Release discipline

* Changes accumulate under **Unreleased** in the changelog. A release moves
  them under the new version and date.
* Every release has a changelog section. The GitHub release notes are copied
  from it, not written separately.
* Only fixes go into a patch release. Features wait for the next minor release.
