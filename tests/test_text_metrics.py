import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from utils.text_metrics import contrast, fit, wrap  # noqa: E402


def test_fit_wraps_latin_by_words_and_fits_box():
    content, size, w, h = fit("Comment in Chinese ↓", None, 9.5, 420, 400)
    assert all(ln.strip() for ln in content.split("\n"))
    assert w <= 420 and content.count("\n") <= 1


def test_no_line_starts_with_closing_or_ends_with_opening_bracket():
    for t in ("今天的「人」 rén", "甲骨文里的「人」"):
        for ln in wrap(t, None, 12, 330):
            assert not ln.startswith("」") and not ln.endswith("「")


def test_short_text_stays_on_one_line_when_slightly_too_wide():
    content, size, _w, _h = fit("rén · person", None, 10.5, 330, 300)
    assert size >= 10.5 * 0.88 - 1e-6


def test_contrast_ratio():
    assert contrast((0, 0, 0), (255, 255, 255)) > 20
    assert contrast((200, 55, 45), (26, 26, 26)) < 4.5
