"""
Числа, которые проект говорит о себе вслух, должны сходиться между собой.

Повод: 09.09.2026 на лендинге в девяти местах стояло «171 автотест», а
тестов было 321 — цифра отстала на полторы сотни и жила на витрине,
которую жюри проверяет одной командой. PRODUCT.md при этом был прав,
то есть расходились не знание и реальность, а две копии знания.

Здесь не проверяется, что число верное — это должен сделать человек,
запустив pytest. Здесь проверяется, что оно ОДНО: девять мест лендинга
(русский текст плюс словари UZ и EN) и PRODUCT.md не могут утверждать
разное. Частичная правка — ровно тот способ, которым дефект и появился.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LANDING = ROOT / "landing" / "index.html"
PRODUCT = ROOT / "PRODUCT.md"

# «321 автотест», «<b>321</b> avtotest», «321 tests» — во всех трёх языках
# и с разметкой внутри.
COUNT_RE = re.compile(
    r"(\d{2,5})\s*(?:</b>|</span>)?\s*(?:автотест\w*|avtotest\w*|tests?\b)")


def _counts(text: str) -> list[int]:
    return [int(m.group(1)) for m in COUNT_RE.finditer(text)]


def test_landing_states_one_test_count_not_several():
    found = _counts(LANDING.read_text(encoding="utf-8"))
    assert found, "на лендинге не нашлось ни одного упоминания числа тестов"
    assert len(set(found)) == 1, (
        f"лендинг называет разные числа тестов: {sorted(set(found))}. "
        "Правится вручную во всех местах сразу — разметка в них разная.")


def test_product_md_agrees_with_the_landing():
    landing = set(_counts(LANDING.read_text(encoding="utf-8")))
    product = set(_counts(PRODUCT.read_text(encoding="utf-8")))
    if not product:
        pytest.skip("PRODUCT.md больше не называет число тестов")
    assert landing == product, (
        f"витрина говорит {sorted(landing)}, PRODUCT.md — {sorted(product)}")
