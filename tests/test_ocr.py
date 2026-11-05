import sys
import types

import numpy as np
import pytest
from PIL import Image

from pyoilot import ocr
from pyoilot.errors import NotSupported
from pyoilot.ocr import OCRWord
from pyoilot.types import Match, Rect


def W(text, x, y, w=None, h=12, conf=0.9, line=0):
    return OCRWord(text, Rect(x, y, w if w is not None else 8 * len(text), h), conf, line)


SCREEN_WORDS = [
    W("File", 5, 2, line=0),
    W("Edit", 45, 2, line=0),
    W("View", 85, 2, line=0),
    W("Save", 10, 40, conf=0.95, line=1),
    W("as...", 46, 40, conf=0.85, line=1),
    W("Total:", 10, 80, line=2),
    W("$1,234.50", 70, 80, conf=0.7, line=2),
    W("Log", 10, 120, line=3),
    W("in", 40, 120, line=3),
    W("Login", 200, 120, line=3),
    W("Cancel", 10, 160, line=4),
    W("Canecl", 100, 160, conf=0.6, line=4),
]


class FakeEngine:
    def __init__(self, words=SCREEN_WORDS):
        self.words = list(words)
        self.calls = 0
        self.last_image = None

    def read(self, image):
        self.calls += 1
        self.last_image = image
        return list(reversed(self.words))  # engines may return any order


@pytest.fixture
def fake():
    eng = FakeEngine()
    ocr.register_ocr_engine("fake", lambda: eng)
    yield eng
    ocr.unregister_ocr_engine("fake")
    ocr.set_default_ocr_engine(None)


IMG = Image.new("RGB", (300, 200), "white")


# ------------------------------------------------------------------- registry
def test_registry_and_default(fake, monkeypatch):
    assert ocr.available_ocr_engines()[:3] == ["tesseract", "rapidocr", "easyocr"]
    assert "fake" in ocr.available_ocr_engines()
    assert ocr.get_ocr_engine("fake") is fake
    assert ocr.get_ocr_engine("FAKE") is fake  # case-insensitive, cached
    ocr.set_default_ocr_engine("fake")
    assert ocr.get_ocr_engine() is fake
    ocr.set_default_ocr_engine(None)
    monkeypatch.setenv("PYOILOT_OCR_ENGINE", "fake")
    assert ocr.get_ocr_engine() is fake
    with pytest.raises(NotSupported):
        ocr.get_ocr_engine("nope")
    with pytest.raises(NotSupported):
        ocr.set_default_ocr_engine("nope")


def test_factory_called_once_and_instance_registration():
    created = []

    def factory():
        created.append(1)
        return FakeEngine()

    ocr.register_ocr_engine("counting", factory)
    try:
        a = ocr.get_ocr_engine("counting")
        b = ocr.get_ocr_engine("counting")
        assert a is b and len(created) == 1
        inst = FakeEngine()
        ocr.register_ocr_engine("counting", inst, default=True)  # replace with an instance
        assert ocr.get_ocr_engine() is inst
    finally:
        ocr.unregister_ocr_engine("counting")
    with pytest.raises(TypeError):
        ocr.register_ocr_engine("bad", 42)
    with pytest.raises(ValueError):
        ocr.register_ocr_engine("  ", FakeEngine())


def test_factory_import_error_becomes_not_supported():
    def broken():
        raise ImportError("no module named magic_ocr")

    ocr.register_ocr_engine("broken", broken)
    try:
        with pytest.raises(NotSupported, match="magic_ocr"):
            ocr.get_ocr_engine("broken")
    finally:
        ocr.unregister_ocr_engine("broken")


def test_auto_detection_order_and_install_hint(monkeypatch):
    saved = dict(ocr._factories)
    saved_inst = dict(ocr._instances)
    monkeypatch.delenv("PYOILOT_OCR_ENGINE", raising=False)
    try:
        def unavailable():
            raise NotSupported("missing")

        for name in ("tesseract", "rapidocr", "easyocr"):
            ocr._factories[name] = unavailable
            ocr._instances.pop(name, None)
        with pytest.raises(NotSupported, match="pip install"):
            ocr.get_ocr_engine()
        # a user-registered engine is used when no built-in is installed
        eng = FakeEngine()
        ocr.register_ocr_engine("custom", lambda: eng)
        assert ocr.get_ocr_engine() is eng
        # a working built-in wins over custom engines
        ocr._factories["rapidocr"] = lambda: "rapid"
        with pytest.raises(TypeError):  # "rapid" has no read()
            ocr.get_ocr_engine()
        tess = FakeEngine()
        ocr._factories["tesseract"] = lambda: tess
        assert ocr.get_ocr_engine() is tess
    finally:
        ocr._factories.clear()
        ocr._factories.update(saved)
        ocr._instances.clear()
        ocr._instances.update(saved_inst)


def test_engine_object_passed_directly():
    eng = FakeEngine([W("hi", 0, 0)])
    assert ocr.read_text(IMG, engine=eng) == "hi"
    assert eng.last_image.mode == "RGB"
    with pytest.raises(TypeError):
        ocr.read_text(IMG, engine=123)


# ------------------------------------------------------------------ read_text
def test_read_words_text_lines(fake):
    words = ocr.read_words(IMG, engine="fake")
    assert [w.text for w in words[:3]] == ["File", "Edit", "View"]
    text = ocr.read_text(IMG, engine="fake")
    assert text.splitlines()[0] == "File Edit View"
    assert text.splitlines()[1] == "Save as..."
    assert len(text.splitlines()) == 5
    lines = ocr.read_lines(np.zeros((10, 10, 3), np.uint8), engine="fake")
    assert lines[0][0] == "File Edit View"
    assert lines[0][1] == Rect(5, 2, 112, 12)


def test_read_text_accepts_any_image_source(fake, tmp_path):
    p = tmp_path / "img.png"
    IMG.save(p)
    assert ocr.read_text(str(p), engine="fake").startswith("File")
    assert fake.last_image.size == (300, 200)


def test_ocrword_to_dict():
    d = W("x", 1, 2, conf=0.123456).to_dict()
    assert d == {"text": "x", "rect": {"left": 1, "top": 2, "width": 8, "height": 12}, "confidence": 0.1235, "line": 0}


# ------------------------------------------------------------------ find_text
def test_find_single_word_case_insensitive(fake):
    ms = ocr.find_text(IMG, "edit", engine="fake")
    assert len(ms) == 1
    m = ms[0]
    assert isinstance(m, Match)
    assert m.text == "Edit" and m.rect == Rect(45, 2, 32, 12)
    assert m.confidence == pytest.approx(0.9)
    assert m.extra["similarity"] == 1.0 and m.extra["line"] == 0
    assert ocr.find_text(IMG, "edit", engine="fake", case_sensitive=True) == []
    assert len(ocr.find_text(IMG, "Edit", engine="fake", case_sensitive=True)) == 1


def test_find_phrase_same_line_union_rect_and_offset(fake):
    ms = ocr.find_text(IMG, "save  AS", engine="fake", offset=(1000, 500))
    assert len(ms) == 1
    assert ms[0].text == "Save as..."
    assert ms[0].rect == Rect(1010, 540, 76, 12)  # union of both words + offset
    assert ms[0].confidence == pytest.approx(0.9)
    # words on different lines never form a phrase
    assert ocr.find_text(IMG, "View Save", engine="fake") == []


def test_edge_punctuation_ignored(fake):
    assert ocr.find_text(IMG, "total", engine="fake")[0].text == "Total:"
    assert ocr.find_text(IMG, "$1,234.50", engine="fake")[0].text == "$1,234.50"


def test_split_and_merged_words(fake):
    ms = ocr.find_text(IMG, "login", engine="fake")
    # both the single word "Login" and the split "Log in" are found
    assert {m.text for m in ms} == {"Login", "Log in"}
    ms2 = ocr.find_text(IMG, "log in", engine="fake")
    assert {m.text for m in ms2} == {"Log in", "Login"}


def test_fuzzy(fake):
    exact = ocr.find_text(IMG, "Cancel", engine="fake")
    assert [m.text for m in exact] == ["Cancel"]
    fuzzy = ocr.find_text(IMG, "Cancel", engine="fake", fuzzy=0.8)
    assert [m.text for m in fuzzy] == ["Cancel", "Canecl"]  # exact first
    assert fuzzy[1].extra["similarity"] < 1.0
    assert fuzzy[1].confidence < fuzzy[0].confidence
    assert ocr.find_text(IMG, "Cancel", engine="fake", fuzzy=0.95) == exact
    with pytest.raises(ValueError):
        ocr.find_text(IMG, "x", engine="fake", fuzzy=0)
    with pytest.raises(ValueError):
        ocr.find_text(IMG, "   ", engine="fake")


def test_fuzzy_does_not_return_overlapping_spans(fake):
    ms = ocr.find_text(IMG, "File Edit", engine="fake", fuzzy=0.5)
    line0 = [m for m in ms if m.extra["line"] == 0]
    for i, a in enumerate(line0):
        for b in line0[i + 1 :]:
            assert a.rect.intersect(b.rect) is None
    assert ms[0].text == "File Edit"


def test_regex(fake):
    ms = ocr.find_text(IMG, r"\$[\d,]+\.\d\d", engine="fake", regex=True)
    assert len(ms) == 1 and ms[0].text == "$1,234.50" and ms[0].rect == Rect(70, 80, 72, 12)
    ms2 = ocr.find_text(IMG, r"save\s+as", engine="fake", regex=True)
    assert ms2[0].rect == Rect(10, 40, 76, 12) and ms2[0].text == "Save as"
    assert ocr.find_text(IMG, r"save", engine="fake", regex=True, case_sensitive=True) == []
    many = ocr.find_text(IMG, r"[A-Z]\w+", engine="fake", regex=True, case_sensitive=True)
    assert len(many) >= 8
    assert ocr.find_text(IMG, r"x*", engine="fake", regex=True) == []  # empty matches ignored


def test_precomputed_words_skip_engine():
    eng = FakeEngine()
    ms = ocr.find_text(None, "view", words=SCREEN_WORDS, engine=eng)
    assert ms[0].rect == Rect(85, 2, 32, 12)
    assert eng.calls == 0


def test_results_sorted_in_reading_order():
    words = [W("OK", 200, 100, line=1), W("OK", 10, 100, line=1), W("OK", 50, 5, line=0)]
    ms = ocr.find_text(IMG, "ok", engine=FakeEngine(words))
    assert [(m.rect.left, m.rect.top) for m in ms] == [(50, 5), (10, 100), (200, 100)]


# ------------------------------------------------------------ helpers / engines
def test_split_line_and_assign_lines():
    words = ocr._split_line("Hello big world", Rect(0, 10, 150, 20), 0.8)
    assert [w.text for w in words] == ["Hello", "big", "world"]
    assert words[0].rect == Rect(0, 10, 50, 20)
    assert words[2].rect.right == 150
    assert ocr._split_line("   ", Rect(0, 0, 10, 10), 1.0) == []
    mixed = [
        OCRWord("b", Rect(100, 52, 10, 20), 1.0),
        OCRWord("a", Rect(0, 50, 10, 20), 1.0),
        OCRWord("c", Rect(0, 0, 10, 20), 1.0),
        OCRWord("d", Rect(50, 3, 10, 18), 1.0),
    ]
    out = ocr._assign_lines(mixed)
    assert [(w.text, w.line) for w in out] == [("c", 0), ("d", 0), ("a", 1), ("b", 1)]


def test_quad_to_rect():
    assert ocr._quad_to_rect([[10, 20], [50, 20], [50, 40], [10, 40]]) == Rect(10, 20, 40, 20)
    assert ocr._quad_to_rect(np.array([[1.4, 2.6], [9.6, 3.0], [9.0, 8.0]])) == Rect(1, 3, 9, 5)
    assert ocr._quad_to_rect([0, 0, 5, 6]) == Rect(0, 0, 5, 6)


def test_tesseract_engine_with_fake_pytesseract(monkeypatch):
    fake = types.ModuleType("pytesseract")

    class Output:
        DICT = "dict"

    calls = {}

    def image_to_data(img, lang, config, output_type):
        calls["size"] = img.size
        calls["mode"] = img.mode
        return {
            "text": ["", "Hello", "world", "", "Next"],
            "conf": ["-1", "96.5", 80, -1, "50"],
            "page_num": [1, 1, 1, 1, 1],
            "block_num": [1, 1, 1, 1, 2],
            "par_num": [1, 1, 1, 1, 1],
            "line_num": [1, 1, 1, 1, 1],
            "left": [0, 20, 100, 0, 20],
            "top": [0, 10, 10, 0, 60],
            "width": [0, 60, 70, 0, 40],
            "height": [0, 20, 20, 0, 20],
        }

    fake.Output = Output
    fake.image_to_data = image_to_data
    fake.get_tesseract_version = lambda: "5.3"
    fake.pytesseract = types.SimpleNamespace(tesseract_cmd="tesseract")
    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    eng = ocr.TesseractEngine(upscale=2.0)
    words = eng.read(Image.new("RGB", (100, 50), "white"))
    assert calls["size"] == (200, 100) and calls["mode"] == "L"
    assert [(w.text, w.line) for w in words] == [("Hello", 0), ("world", 0), ("Next", 1)]
    assert words[0].rect == Rect(10, 5, 30, 10)
    assert words[0].confidence == pytest.approx(0.965)
    assert ocr.read_text(Image.new("RGB", (100, 50)), engine=eng) == "Hello world\nNext"


def test_tesseract_missing_binary(monkeypatch):
    fake = types.ModuleType("pytesseract")

    def no_binary():
        raise OSError("tesseract is not installed")

    fake.get_tesseract_version = no_binary
    fake.pytesseract = types.SimpleNamespace(tesseract_cmd="tesseract")
    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    with pytest.raises(NotSupported, match="tesseract"):
        ocr.TesseractEngine()


def test_rapidocr_engine_both_apis(monkeypatch):
    quad = [[10, 10], [110, 10], [110, 30], [10, 30]]

    class OldRapid:
        def __init__(self, **kw):
            pass

        def __call__(self, arr):
            assert arr.ndim == 3 and arr.shape[2] == 3
            return ([[quad, "Open file", 0.9], [[[10, 50], [60, 50], [60, 70], [10, 70]], "Quit", 0.8]], 0.01)

    old = types.ModuleType("rapidocr_onnxruntime")
    old.RapidOCR = OldRapid
    monkeypatch.setitem(sys.modules, "rapidocr", None)  # force ImportError for the new package
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", old)
    words = ocr.RapidOCREngine().read(Image.new("RGB", (200, 100)))
    assert [(w.text, w.line) for w in words] == [("Open", 0), ("file", 0), ("Quit", 1)]
    assert words[0].rect.left == 10 and words[1].rect.right == 110

    class NewRapid:
        def __init__(self, **kw):
            pass

        def __call__(self, arr):
            return types.SimpleNamespace(boxes=np.array([quad]), txts=("Hello",), scores=(0.75,))

    new = types.ModuleType("rapidocr")
    new.RapidOCR = NewRapid
    monkeypatch.setitem(sys.modules, "rapidocr", new)
    words = ocr.RapidOCREngine().read(Image.new("RGB", (200, 100)))
    assert [w.text for w in words] == ["Hello"] and words[0].confidence == 0.75


def test_easyocr_engine(monkeypatch):
    class Reader:
        def __init__(self, langs, gpu, verbose, **kw):
            assert langs == ["en"] and gpu is False

        def readtext(self, arr):
            return [([[0, 0], [40, 0], [40, 10], [0, 10]], "Hi there", 0.5)]

    mod = types.ModuleType("easyocr")
    mod.Reader = Reader
    monkeypatch.setitem(sys.modules, "easyocr", mod)
    words = ocr.EasyOCREngine().read(Image.new("RGB", (50, 20)))
    assert [w.text for w in words] == ["Hi", "there"]


def test_missing_optional_engines_raise_not_supported(monkeypatch):
    monkeypatch.setitem(sys.modules, "pytesseract", None)
    monkeypatch.setitem(sys.modules, "rapidocr", None)
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", None)
    monkeypatch.setitem(sys.modules, "easyocr", None)
    for cls in (ocr.TesseractEngine, ocr.RapidOCREngine, ocr.EasyOCREngine):
        with pytest.raises(NotSupported):
            cls()


def test_ocr_engine_protocol():
    assert isinstance(FakeEngine(), ocr.OCREngine)
    assert not isinstance(object(), ocr.OCREngine)
