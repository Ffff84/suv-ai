"""
Запасной источник снимков: Landsat 8/9 через Planetary Computer.

Две группы проверок. Первая — склейка в suv/enrich.py: источник НЕ
МЕНЯЕТ поведение, пока его явно не включили, и не роняет расчёт, когда
включён и сломался. Пилотный фермер живёт на боевом сервере, деплой идёт
на каждый push в main, и новый источник данных не имеет права незаметно
вмешаться в утренний совет.

Вторая — арифметика внутри suv/landsat.py: маска качества и усреднение
NDVI. Это единственное место во всей ветке, где рождается само число,
уходящее фермеру, и проверять его подменой fetch_ndvi бессмысленно —
подмена как раз и выключает то, что надо проверить. Поэтому обе функции
вынесены наружу и считаются на синтетических массивах, без сети.

Все тесты офлайн: сеть в CI недоступна.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from suv.crop import CROPS
from suv.schedule import Field
from suv.soil import SOILS


def _field():
    return Field("T", "T", 1.0, 39.65, 66.96, 705, CROPS["cotton"],
                 SOILS["loam"], date(2026, 4, 15), "furrow")


def _no_sentinel(monkeypatch):
    """Убрать ключи CDSE — Sentinel-2 гарантированно ничего не даст."""
    monkeypatch.delenv("CDSE_CLIENT_ID", raising=False)
    monkeypatch.delenv("CDSE_CLIENT_SECRET", raising=False)


# =====================================================================
# Маска качества: пиксель попадает в среднее или нет
# =====================================================================

def test_quality_bits_are_pinned_to_their_real_numbers():
    """Номера битов зафиксированы ЧИСЛАМИ, а не ссылкой на константы.

    Прежняя версия этого теста проверяла `QA_CLOUD in _QA_BAD_BITS` —
    то есть членство константы в кортеже, собранном из неё же. Такая
    проверка проходит при любом номере бита, включая неверный, и не
    ловит ровно тот класс ошибки, ради которого написана: перепутанную
    нумерацию DFCB.
    """
    from suv.landsat import bad_quality

    assert bad_quality(np.array([[0]], dtype="uint16"))[0][0] == False  # noqa: E712
    for bit, name in ((0, "fill"), (1, "dilated"), (2, "cirrus"),
                      (3, "cloud"), (4, "shadow"), (5, "snow")):
        qa = np.array([[1 << bit]], dtype="uint16")
        assert bad_quality(qa)[0][0], f"бит {bit} ({name}) должен быть браком"


def test_water_pixel_is_not_a_defect():
    """Бит 7 (вода) в брак не идёт: у поля на самотёке залитая борозда —
    это норма, а не помеха."""
    from suv.landsat import bad_quality

    qa = np.array([[1 << 7]], dtype="uint16")
    assert not bad_quality(qa)[0][0]


def test_medium_confidence_cloud_is_rejected():
    """Тонкое облако и летняя пыль над Самаркандом.

    CFMask поднимает однобитный флаг «облако» ТОЛЬКО при высокой
    уверенности. Пиксель со средней уверенностью проходил бы как чистый,
    занижал NDVI, и движок откладывал бы полив на сохнущем саду — при
    высокой доле «чистых» пикселей, то есть порог годности кадра такой
    кадр бы не отсеял.
    """
    from suv.landsat import bad_quality

    low = np.array([[1 << 8]], dtype="uint16")        # уверенность 1
    medium = np.array([[2 << 8]], dtype="uint16")     # уверенность 2
    high = np.array([[3 << 8]], dtype="uint16")       # уверенность 3
    assert not bad_quality(low)[0][0]
    assert bad_quality(medium)[0][0]
    assert bad_quality(high)[0][0]
    # То же для уверенности в тени облака (биты 10-11).
    assert bad_quality(np.array([[2 << 10]], dtype="uint16"))[0][0]


# =====================================================================
# Усреднение NDVI
# =====================================================================

def test_dark_pixel_does_not_blow_up_the_mean():
    """Регрессия на деление малого на малое.

    После сдвига -0.2 отражение Landsat легально бывает отрицательным.
    Пара DN 7000 и 7546 даёт red -0.0075, nir +0.0075 и знаменатель
    1.5e-05 — то есть NDVI ровно 1001. Проверкой «знаменатель не ноль»
    такой пиксель не отсекается, а зажим в единицу объявил бы тень
    максимумом растительности. Один такой пиксель из двадцати пяти
    уводил бы kc_from_ndvi на границу конверта культуры, и фермер
    получал бы команду лить в полтора раза больше воды.
    """
    from suv.landsat import mean_ndvi

    red = np.array([[12000, 7000]], dtype="uint16")
    nir = np.array([[19300, 7546]], dtype="uint16")
    usable = np.array([[True, True]])

    got = mean_ndvi(red, nir, usable)
    # Тёмный пиксель выброшен, остался только нормальный.
    assert got == pytest.approx(0.4357, abs=1e-3)
    assert -1.0 <= got <= 1.0


def test_mean_matches_hand_calculation():
    """Обычный пиксель считается ровно по паспортной формуле."""
    from suv.landsat import mean_ndvi

    red = np.array([[12000]], dtype="uint16")
    nir = np.array([[19300]], dtype="uint16")
    got = mean_ndvi(red, nir, np.array([[True]]))
    assert got == pytest.approx((0.33075 - 0.13) / 0.46075, abs=1e-6)


def test_unusable_pixels_are_excluded_from_the_mean():
    """Маска решает, что попадает в среднее: закрытый облаком пиксель с
    совсем другим NDVI не должен на него влиять."""
    from suv.landsat import mean_ndvi

    red = np.array([[12000, 20000]], dtype="uint16")
    nir = np.array([[19300, 21000]], dtype="uint16")
    both = mean_ndvi(red, nir, np.array([[True, True]]))
    only_first = mean_ndvi(red, nir, np.array([[True, False]]))
    assert only_first == pytest.approx(0.4357, abs=1e-3)
    assert both != pytest.approx(only_first, abs=1e-3)


def test_nothing_usable_returns_none():
    """Не из чего считать — молчим, а не возвращаем ноль."""
    from suv.landsat import mean_ndvi

    red = np.array([[12000]], dtype="uint16")
    nir = np.array([[19300]], dtype="uint16")
    assert mean_ndvi(red, nir, np.array([[False]])) is None


def test_custom_scale_from_stac_is_honoured():
    """Если каталог отдал свои scale/offset, считаем по ним."""
    from suv.landsat import mean_ndvi

    red = np.array([[12000]], dtype="uint16")
    nir = np.array([[19300]], dtype="uint16")
    a = mean_ndvi(red, nir, np.array([[True]]))
    b = mean_ndvi(red, nir, np.array([[True]]),
                  red_scale=3.0e-05, red_offset=-0.25,
                  nir_scale=3.0e-05, nir_offset=-0.25)
    assert a != pytest.approx(b, abs=1e-4)


# =====================================================================
# Пересчёт сырых значений
# =====================================================================

def test_passport_scale_values_are_pinned():
    """Множитель и сдвиг зафиксированы ЧИСЛАМИ.

    Прежняя версия сравнивала результат _scale_of с теми же константами
    модуля, которые функция и возвращает, — то есть проходила бы при
    любых значениях, включая испорченные. Инцидент, ради которого
    константы вообще заведены: у части гранул scale и offset не
    прописаны в GeoTIFF, rasterio возвращает 1.0/0.0, и без явного
    пересчёта NDVI выходил 0.239 вместо 0.472.
    """
    from suv import landsat

    assert landsat.SR_SCALE == 2.75e-05
    assert landsat.SR_OFFSET == -0.2
    assert landsat._scale_of({}) == (2.75e-05, -0.2)
    assert landsat._scale_of({"raster:bands": []}) == (2.75e-05, -0.2)
    # Неполные метаданные уводят на паспортные, а не роняют разбор.
    assert landsat._scale_of({"raster:bands": [{"scale": 1e-5}]}) == (
        2.75e-05, -0.2)


def test_scale_prefers_stac_metadata():
    """Когда каталог значения отдал — верим ему, а не константам."""
    from suv import landsat

    got = landsat._scale_of({"raster:bands": [{"scale": 3.0e-05,
                                               "offset": -0.25}]})
    assert got == (3.0e-05, -0.25)


# =====================================================================
# Гейт: выключен по умолчанию
# =====================================================================

def test_disabled_by_default(monkeypatch):
    """Без LANDSAT_FALLBACK в окружении выкатка ничего не меняет.

    Это условие деплоя, а не вкусовщина: бот перезапускается на каждый
    push в main, и появление второго источника данных не должно менять
    совет пилотному фермеру само по себе.
    """
    from suv import landsat

    monkeypatch.delenv("LANDSAT_FALLBACK", raising=False)
    assert landsat.enabled() is False


@pytest.mark.parametrize("raw,want", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True),
    ("on", True), (" 1 ", True),
    ("", False), ("0", False), ("no", False), ("false", False),
    ("нет", False),
])
def test_enabled_parses_env(monkeypatch, raw, want):
    from suv import landsat

    monkeypatch.setenv("LANDSAT_FALLBACK", raw)
    assert landsat.enabled() is want


def test_disabled_landsat_is_never_called(monkeypatch):
    """Выключенный источник не должен даже ходить в сеть."""
    import suv.enrich as enrich

    _no_sentinel(monkeypatch)
    monkeypatch.delenv("LANDSAT_FALLBACK", raising=False)

    def boom(*a, **k):  # pragma: no cover — вызов означает провал теста
        raise AssertionError("Landsat вызван, хотя выключен")

    monkeypatch.setattr(enrich.landsat, "fetch_ndvi", boom)

    f = _field()
    status = enrich.attach_ndvi(f)
    assert f.ndvi is None
    assert "календар" in status


# =====================================================================
# Гейт включён
# =====================================================================

def test_fills_the_gap_when_sentinel_is_clouded(monkeypatch):
    """Ровно та дыра, ради которой источник и заводился.

    За сезон 2026 у Sentinel-2 был провал 28.04 → 07.06 (40 дней), в
    который Landsat снимал поле четырежды. Здесь проверяется, что такой
    кадр действительно доезжает до поля вместе со своей датой.
    """
    import suv.enrich as enrich
    from suv.satellite import NdviReading

    _no_sentinel(monkeypatch)
    monkeypatch.setenv("LANDSAT_FALLBACK", "1")
    monkeypatch.setattr(
        enrich.landsat, "fetch_ndvi",
        lambda *a, **k: NdviReading(value=0.478,
                                    observed_on=date(2026, 5, 16),
                                    valid_fraction=0.92))

    f = _field()
    status = enrich.attach_ndvi(f)
    assert f.ndvi == pytest.approx(0.478)
    assert f.ndvi_date == date(2026, 5, 16)
    assert "Landsat" in status
    # Дата кадра обязана быть в строке: у Landsat отставание около девяти
    # дней, и «спутник сработал» без даты читается как «снимок свежий».
    assert "2026-05-16" in status


def test_sentinel_wins_when_it_has_a_frame(monkeypatch):
    """Sentinel-2 всегда первый: 10 м против 30 и сутки против девяти.

    Landsat при живом Sentinel-2 не должен вызываться вовсе — иначе на
    каждое поле уходил бы лишний обход сети, а счётчик запросов и есть
    настоящее узкое место квоты.
    """
    import suv.enrich as enrich
    from suv.satellite import NdviReading

    monkeypatch.setenv("CDSE_CLIENT_ID", "x")
    monkeypatch.setenv("CDSE_CLIENT_SECRET", "y")
    monkeypatch.setenv("LANDSAT_FALLBACK", "1")
    monkeypatch.setattr(enrich, "get_token", lambda: "tok")
    monkeypatch.setattr(
        enrich, "fetch_ndvi",
        lambda *a, **k: NdviReading(value=0.611,
                                    observed_on=date(2026, 8, 20),
                                    valid_fraction=1.0))

    def boom(*a, **k):  # pragma: no cover — вызов означает провал теста
        raise AssertionError("Landsat вызван при живом Sentinel-2")

    monkeypatch.setattr(enrich.landsat, "fetch_ndvi", boom)

    f = _field()
    status = enrich.attach_ndvi(f)
    assert f.ndvi == pytest.approx(0.611)
    assert "Sentinel-2" in status
    assert "Landsat" not in status


# =====================================================================
# Включён и сломался: расчёт обязан выжить
# =====================================================================

def test_landsat_failure_never_breaks_the_engine(monkeypatch):
    """Правило модуля enrich: обогащение не роняет расчёт. Второй
    источник обязан подчиняться ему так же, как первый."""
    import suv.enrich as enrich

    _no_sentinel(monkeypatch)
    monkeypatch.setenv("LANDSAT_FALLBACK", "1")
    monkeypatch.setattr(
        enrich.landsat, "fetch_ndvi",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    f = _field()
    status = enrich.attach_ndvi(f)
    assert f.ndvi is None
    assert "календар" in status


def test_no_usable_landsat_frame_falls_back_to_calendar(monkeypatch):
    """Нет годного кадра — считаем по календарю, а не подставляем
    последнее известное значение."""
    import suv.enrich as enrich

    _no_sentinel(monkeypatch)
    monkeypatch.setenv("LANDSAT_FALLBACK", "1")
    monkeypatch.setattr(enrich.landsat, "fetch_ndvi", lambda *a, **k: None)

    f = _field()
    status = enrich.attach_ndvi(f)
    assert f.ndvi is None
    assert "календар" in status


def test_calendar_phrase_is_not_duplicated(monkeypatch):
    """Строку статуса читает человек в логе деплоя — «считаю по
    календарю» дважды в одной строке читать невозможно."""
    import suv.enrich as enrich

    _no_sentinel(monkeypatch)
    monkeypatch.delenv("LANDSAT_FALLBACK", raising=False)

    status = enrich.attach_ndvi(_field())
    assert status.count("календар") == 1
