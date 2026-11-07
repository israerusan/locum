"""Review 12: Java bridge reference lifetime, heal negation false refusals."""

from __future__ import annotations

from typing import List

import pytest

from locum import Desktop
from locum.backends.mock import MockBackend
from locum.bridges import java
from locum.errors import ElementNotFound
from locum.heal import name_similarity

from test_bridge_java import HWND, FakeDll, Obj, bridge, make_tree


@pytest.fixture
def fake(monkeypatch):
    dll = FakeDll(make_tree())
    monkeypatch.setattr(java, "_dll_loader", lambda path=None: dll)
    return dll


def _rows(n: int) -> List[Obj]:
    return [Obj(f"cell {i}", "text", "enabled,visible,showing,editable",
                (0, 0, 1, 1), text="", editable_text=True) for i in range(n)]


def _watch_released(fake: FakeDll) -> List[int]:
    """Record every bridge call made with a reference the JVM no longer has."""
    bad: List[int] = []
    orig = fake._obj

    def check(ac):
        if ac not in fake.refs:
            bad.append(ac)
        return orig(ac)

    fake._obj = check
    return bad


# ---------------------------------------------------------------- Java: deleted references never reach the JVM
def test_held_cell_of_a_removed_row_is_never_used_after_its_release(fake):
    table = Obj("Orders", "table", "enabled,visible,showing", (0, 0, 300, 300), kids=_rows(10), fresh_kids=True)
    fake.roots[HWND].kids[0].kids.append(table)
    jab = bridge()
    win = jab.from_hwnd(HWND)
    cell = win.find('table edit "cell 7"')
    del table.kids[5:]
    win.find_all("table edit")  # the re-read releases the cells past the end, the held one included
    bad = _watch_released(fake)
    for act in (cell.focus, lambda: cell.set_text("x"), lambda: cell.do_action("click"), cell.click):
        with pytest.raises(ElementNotFound):
            act()
    assert cell.name == "" and cell.text == ""
    assert bad == []  # a deleted JNI global reference is undefined behaviour in the JVM
    jab.close()


def test_nothing_reaches_the_jvm_after_close(fake):
    jab = bridge()
    win = jab.from_hwnd(HWND)
    save = win.find('button "Save"')
    jab.close()
    bad = _watch_released(fake)
    assert save.name == "" and win.name == ""
    win.node.refresh()
    assert win.node.children() == []
    with pytest.raises(ElementNotFound):
        save.click()
    with pytest.raises(ElementNotFound):
        save.focus()
    assert bad == [] and fake.refs == {} and jab.live_references == 0


def test_held_shared_renderer_cell_of_a_removed_row_reads_as_gone_not_as_another_cell(fake):
    table = Obj("Orders", "table", "enabled,visible,showing", (0, 0, 300, 300),
                kids=[Obj(f"cell {i}", "label", "enabled,visible,showing", (0, 0, 1, 1)) for i in range(10)],
                shared_renderer=True)
    fake.roots[HWND].kids[0].kids.append(table)
    with bridge() as jab:
        win = jab.from_hwnd(HWND)
        held = win.find('table text "cell 7"')
        assert win.find('table text "cell 9"').name == "cell 9"  # the renderer now shows cell 9
        del table.kids[5:]
        assert held.name == ""  # was "cell 9": the shared renderer's last stamp
        with pytest.raises(ElementNotFound):
            held.click()


# ---------------------------------------------------------------- heal: "un" is an article, "Un-lock" a prefix
@pytest.mark.parametrize("old, new", [
    ("Ajouter un fichier", "Ajouter le fichier"),
    ("Añadir un archivo", "Añadir archivo"),
])
def test_heal_does_not_take_the_french_spanish_article_un_for_a_negation(old, new):
    be = MockBackend(800, 600)
    w = be.add_window("Editor", (0, 0, 600, 400))
    w.add_button(new, (10, 300, 160, 30))
    w.add_button("Aide", (200, 300, 100, 30))
    d = Desktop(be, sleep=lambda s: None, default_timeout=0)
    assert d.find(f'button "{old}"', heal=True).name == new


@pytest.mark.parametrize("old, new", [("Lock", "Un-lock"), ("Select", "De-select"), ("Lock screen", "Un-lock screen")])
def test_hyphenated_negating_prefix_is_still_refused(old, new):
    assert name_similarity(old, new) == 0.0
