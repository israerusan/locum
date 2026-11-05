"""Packaging metadata and CI configuration: what the project promises to install and run."""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _dependencies() -> list:
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    with open(ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)["project"]["dependencies"]


def _darwin_packages() -> set:
    return {re.split(r"[<>=;\s]", d, maxsplit=1)[0] for d in _dependencies() if "sys_platform == 'darwin'" in d}


def test_macos_dependencies_cover_every_pyobjc_framework_the_backend_imports():
    # The macOS backend imports Quartz, ApplicationServices and AppKit; AppKit
    # (clipboard, media keys) ships in pyobjc-framework-Cocoa. Without it the
    # backend silently loses those features.
    src = (ROOT / "src" / "locum" / "backends" / "macos.py").read_text(encoding="utf-8")
    assert "import AppKit" in src
    assert {"pyobjc-framework-Quartz", "pyobjc-framework-ApplicationServices",
            "pyobjc-framework-Cocoa"} <= _darwin_packages()


def test_core_dependencies_stay_light():
    names = {re.split(r"[<>=;\s]", d, maxsplit=1)[0] for d in _dependencies()}
    assert not names & {"anthropic", "pytesseract", "pynput", "pyyaml", "opencv-python"}


def _workflow() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def test_ci_matrix_does_not_fail_fast():
    test = _workflow()["jobs"]["test"]
    assert test["strategy"]["fail-fast"] is False
    assert {"ubuntu-latest", "windows-latest", "macos-latest"} <= set(test["strategy"]["matrix"]["os"])


def test_ci_native_selftest_job_is_non_blocking_and_uploads_its_report():
    job = _workflow()["jobs"]["windows-native-selftest"]
    assert job["runs-on"] == "windows-latest"
    assert job["continue-on-error"] is True
    steps = job["steps"]
    run = next(s["run"] for s in steps if "locum.selftest" in s.get("run", ""))
    assert "--yes" in run and "--out selftest-report" in run and "--backend" not in run
    upload = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@v4"))
    assert upload["if"] == "always()" and upload["with"]["path"] == "selftest-report"


def test_ci_native_selftest_failure_does_not_turn_the_pr_check_red():
    # Job-level continue-on-error still records the job's check run as failed;
    # the self-test step itself must swallow the failure and surface a warning.
    steps = _workflow()["jobs"]["windows-native-selftest"]["steps"]
    selftest = next(s for s in steps if "locum.selftest" in s.get("run", ""))
    assert selftest["continue-on-error"] is True
    sid = selftest["id"]
    warn = next(s for s in steps if s.get("if") == f"steps.{sid}.outcome == 'failure'")
    assert "::warning" in warn["run"]
    names = [s.get("name") for s in steps]
    assert names.index(warn["name"]) < next(i for i, s in enumerate(steps)
                                            if s.get("uses", "").startswith("actions/upload-artifact"))
