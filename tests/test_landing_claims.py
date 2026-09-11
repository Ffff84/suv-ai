"""
Числа, которые проект говорит о себе вслух, должны сходиться между собой.

Повод: 09.09.2026 на лендинге в девяти местах стояло «171 автотест», а
тестов было 321 — цифра отстала на полторы сотни и жила на витрине,
которую жюри проверяет одной командой. PRODUCT.md при этом был прав,
то есть расходились не знание и реальность, а две копии знания.

Повторилось 12.09.2026 и хуже: лендинг и PRODUCT.md говорили 421, а
README — 321, потому что замок охранял ровно два файла из четырёх.
README открывают первым. Теперь под замком все витрины разом.

Здесь не проверяется, что число верное — это должен сделать человек,
запустив pytest. Здесь проверяется, что оно ОДНО: девять мест лендинга
(русский текст плюс словари UZ и EN), PRODUCT.md, README.md и разбор
конкурента не могут утверждать разное. Частичная правка — ровно тот
способ, которым дефект и появился, причём дважды.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LANDING = ROOT / "landing" / "index.html"
PRODUCT = ROOT / "PRODUCT.md"
SHOWCASES = (LANDING, PRODUCT, ROOT / "README.md",
             ROOT / "RAZVEDKA-ONESOIL.md")

# «321 автотест», «<b>321</b> avtotest», «321 tests», «**421 тест**» —
# во всех трёх языках и с разметкой внутри.
COUNT_RE = re.compile(
    r"(\d{2,5})\s*(?:</b>|</span>|\*\*)?\s*"
    r"(?:автотест\w*|avtotest\w*|тест\w*|tests?\b)")


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


@pytest.mark.parametrize("path", SHOWCASES, ids=lambda p: p.name)
def test_every_showcase_agrees_with_the_landing(path: Path):
    """Все витрины называют актуальное число и ни одного устаревшего.

    Не равенство множеств: разбор конкурента цитирует старую цифру в
    стоп-листе («было 171 → стало N»), и это правильный текст. Поэтому
    требуется вхождение актуального числа и отсутствие любого ДРУГОГО
    трёхзначного рядом с ним — кроме цифр, уже объявленных устаревшими.
    """
    if not path.exists():
        pytest.skip(f"{path.name} больше нет в репозитории")
    landing = set(_counts(LANDING.read_text(encoding="utf-8")))
    found = _counts(path.read_text(encoding="utf-8"))
    if not found:
        pytest.skip(f"{path.name} больше не называет число тестов")

    current = landing.pop()
    assert current in found, (
        f"{path.name} не называет актуальное число {current}: "
        f"{sorted(set(found))}")

    # «171» живёт в стоп-листе как то, что говорить больше нельзя.
    stale = {n for n in found if n != current and n != 171}
    assert not stale, (
        f"{path.name} всё ещё говорит {sorted(stale)} рядом с {current}. "
        "Цифра правится во всех витринах сразу — частичная правка и есть "
        "тот способ, которым дефект появился дважды.")
