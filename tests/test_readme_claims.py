"""The README's claims stay honest: platform validation, Wayland, OCR extra, LLM providers, typing."""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
HEAD = README.split("## Install", 1)[0]  # everything above the first real section


def test_platform_support_table_sits_next_to_the_headline():
    assert "| Platform |" in HEAD
    assert re.search(r"macOS.*not yet validated on a real Mac", HEAD)
    assert re.search(r"Windows 11 machine by the built-in self-test", HEAD)
    assert "Xvfb" in HEAD
    assert re.search(r"Native Wayland is not supported", HEAD)


def test_headline_does_not_claim_equal_support_on_all_platforms():
    assert "on Windows, macOS\nand Linux." not in HEAD
    assert "alpha" in HEAD and "APIs may still change" in HEAD


def test_ocr_extra_is_described_as_pytesseract_only():
    assert "pluggable (Tesseract, RapidOCR, EasyOCR)" not in README
    assert "`pypilot[ocr]` installs only `pytesseract`" in README
    from pypilot import ocr

    assert "installs only `pytesseract`" in ocr.__doc__


def test_llm_bullet_does_not_imply_an_openai_agent_runtime():
    assert "Claude (Anthropic) agent loop" in HEAD
    assert "no OpenAI agent runtime" in HEAD


def test_typing_reliability_is_not_overstated():
    assert "regardless of keyboard layout" not in README
    assert "Element.type" in README and 'mode="clipboard"' in README
    assert "Windows 11 Notepad" in README
