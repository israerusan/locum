"""AtspiNode.value on current libatspi (found by the real-desktop CI job).

With GObject introspection, ``get_text_iface()`` returns the Accessible itself, and
``Atspi.Accessible.get_text()`` is a deprecated zero-argument alias, so the
interface method must be called through ``Atspi.Text``.
"""

import types

from locum.backends.x11.atspi import AtspiNode, _read_text


class _Acc:
    def __init__(self, text):
        self.text = text

    def get_role_name(self):
        return "text"

    def get_text_iface(self):
        return self  # what PyGObject really returns

    def get_text(self):  # Atspi.Accessible.get_text: deprecated alias, no arguments
        return self


class _Text:
    @staticmethod
    def get_character_count(acc):
        return len(acc.text)

    @staticmethod
    def get_text(acc, start, end):
        return acc.text[start:end]


ATSPI = types.SimpleNamespace(Text=_Text)


def test_value_reads_through_atspi_text_not_the_shadowing_accessible_method():
    node = AtspiNode(_Acc("hello probe"), ATSPI)
    assert node.value == "hello probe"


def test_empty_field_reads_as_empty_string():
    assert AtspiNode(_Acc(""), ATSPI).value == ""


def test_pyatspi_style_interface_object_still_works():
    class Iface:
        def get_text(self, start, end):
            return "legacy"

    assert _read_text(None, object(), Iface()) == "legacy"


def test_unreadable_text_is_none():
    class Broken:
        def get_text(self, *a):
            raise RuntimeError("defunct")

    assert _read_text(types.SimpleNamespace(), object(), Broken()) is None
