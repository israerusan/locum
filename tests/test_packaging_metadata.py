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
    pkg = ROOT / "src" / "locum" / "backends" / "macos"  # a package of modules since the split
    src = "\n".join(p.read_text(encoding="utf-8") for p in sorted(pkg.glob("*.py")))
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


@pytest.mark.parametrize("job_name, runner", [
    ("windows-native-selftest", "windows-latest"),
    ("linux-native-selftest", "ubuntu-24.04"),
    ("macos-native-selftest", "macos-latest"),
])
def test_ci_real_desktop_selftests_block_the_build_and_upload_their_report(job_name, runner):
    # A real-desktop failure must fail CI: no continue-on-error at job or step level,
    # and a skipped capability counts as a failure (--strict).
    job = _workflow()["jobs"][job_name]
    assert job["runs-on"] == runner
    assert "continue-on-error" not in job
    steps = job["steps"]
    selftest = next(s for s in steps if "locum.selftest" in s.get("run", ""))
    run = selftest["run"]
    assert "continue-on-error" not in selftest
    assert "--yes" in run and "--strict" in run and "--out selftest-report" in run and "--backend" not in run
    upload = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@v4"))
    assert upload["if"] == "always()" and upload["with"]["path"] == "selftest-report"


def test_ci_java_bridge_job_runs_a_real_jvm_and_blocks():
    # README and docs/BRIDGES.md say the Java bridge is driven against a real Swing window in CI.
    job = _workflow()["jobs"]["windows-java-bridge"]
    assert job["runs-on"] == "windows-latest" and "continue-on-error" not in job
    assert job["env"]["LOCUM_TEST_JAVA"] == "1"
    steps = job["steps"]
    assert any(s.get("uses", "").startswith("actions/setup-java@") for s in steps)
    assert any("jabswitch" in s.get("run", "") for s in steps)
    run = next(s for s in steps if "test_real_java_bridge.py" in s.get("run", ""))
    assert "continue-on-error" not in run
