"""The README's claims stay honest: platform validation, Wayland, OCR extra, LLM providers, typing."""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
HEAD = README.split("## Install", 1)[0]  # everything above the first real section


def test_platform_support_table_sits_next_to_the_headline():
    assert "| Platform |" in HEAD
    # Each platform says how it is validated, and CI backs it: the real-desktop
    # self-test jobs are blocking (tests/test_packaging_metadata.py).
    assert re.search(r"macOS.*\*\*blocking\*\* real-desktop self-test on the GitHub macOS runner", HEAD)
    assert re.search(r"Windows 10/11.*\*\*blocking\*\* real-desktop self-test", HEAD)
    assert re.search(r"Xvfb.*AT-SPI.*not a full desktop session", HEAD)
    assert re.search(r"Wayland.*Experimental: input never run on a real compositor", HEAD)


def test_headline_does_not_claim_equal_support_on_all_platforms():
    assert "on Windows, macOS\nand Linux." not in HEAD
    assert "alpha (0.x)" in HEAD and "can and\n> do break APIs" in HEAD
    assert "Pin an exact version" in HEAD and "(#limitations-and-known-gaps)" in HEAD


def test_limitations_section_states_the_known_gaps():
    section = README.split("## Limitations and known gaps", 1)[1].split("\n## ", 1)[0]
    for claim in ("pin the exact version", "never run on a real compositor", "never run against a real SAP GUI",
                  "image matching and OCR only", "No orchestrator", "refuses network shares",
                  "Anything else is typed unverified", "`(NOT verified)` with the reason", "heavy AI assistance"):
        assert claim in section, claim
    assert "real JVM runs are still to come" not in README  # stale since 0.4.0 (blocking real-JVM CI)


def test_ocr_extra_is_described_as_pytesseract_only():
    assert "pluggable (Tesseract, RapidOCR, EasyOCR)" not in README
    assert "`locum[ocr]` installs only `pytesseract`" in README
    from locum import ocr

    assert "installs only `pytesseract`" in ocr.__doc__


def test_llm_bullet_does_not_imply_an_openai_agent_runtime():
    assert "Claude (Anthropic) agent loop" in HEAD
    assert "no OpenAI agent runtime" in HEAD


def test_typing_reliability_is_not_overstated():
    assert "regardless of keyboard layout" not in README
    assert "Element.type" in README and 'mode="clipboard"' in README
    assert "Windows 11 Notepad" in README


def test_bridges_say_what_has_and_has_not_run_against_the_real_system():
    assert re.search(r"SAP GUI.*experimental, never run against a real SAP GUI", HEAD)
    assert re.search(r"Java Swing/AWT.*\*\*blocking\*\* CI job driving a real Swing window", HEAD)
    assert re.search(r"Wayland.*opt-in only", HEAD)
