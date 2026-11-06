"""locum.bridges.remote.ImageSession on the mock desktop."""

from __future__ import annotations

import pytest

from locum import Desktop
from locum.bridges.remote import ImageSession
from locum.errors import PolicyViolation, TargetLost
from locum.llm.safety import GuardedDesktop, Policy
from locum.types import Match, Rect


@pytest.fixture
def setup(mock_backend):
    d = Desktop(mock_backend, failsafe=False, sleep=lambda s: None)
    w = mock_backend.add_window("Citrix - SAP", (100, 100, 600, 400), app="wfica32.exe")
    return d, w, d.window("Citrix - SAP")


def test_searches_are_confined_to_the_window(setup, monkeypatch):
    d, _, win = setup
    seen = []
    monkeypatch.setattr(d, "locate", lambda image, region=None, **kw: seen.append(region))
    monkeypatch.setattr(d, "find_text", lambda text, region=None, **kw: seen.append(region))
    s = ImageSession(win)
    s.locate("x.png")
    s.find_text("Post", region=(10, 20, 50, 30))
    assert seen == [Rect(100, 100, 600, 400), Rect(110, 120, 50, 30)]
    with pytest.raises(ValueError, match="outside the window"):
        s.locate("x.png", region=(700, 0, 10, 10))


def test_click_is_bound_to_the_window(setup, mock_backend):
    d, _, win = setup
    s = ImageSession(win)
    assert s.click(10, 10) == (110, 110)
    assert any(e[0] == "down" and e[2:] == (110, 110) for e in mock_backend.events)
    mock_backend.add_window("Teams", (90, 90, 200, 200), app="teams.exe")
    s.activate = False  # do not bring the remote window back
    n = len([e for e in mock_backend.events if e[0] == "down"])
    with pytest.raises(TargetLost):
        s.click(20, 20)
    assert len([e for e in mock_backend.events if e[0] == "down"]) == n


def test_click_text_goes_through_a_guarded_desktop(setup, monkeypatch):
    d, _, win = setup
    gd = GuardedDesktop(d, Policy(), confirm=lambda desc, det: False)
    monkeypatch.setattr(d, "find_text", lambda text, **kw: Match(Rect(150, 150, 40, 12), 1.0, text=text))
    s = ImageSession(gd.window("Citrix - SAP"))
    assert s.desktop is gd
    with pytest.raises(PolicyViolation, match="declined"):
        s.click_text("Post")  # OCR text "Post" matches the confirm patterns
