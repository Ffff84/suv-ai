"""
Регрессионные тесты на баги, найденные проверкой 17.08.2026.

Каждый тест — это способ, которым бот УЖЕ врал или молчал: неверный вес
NDVI из будущего, вечно растущая «экономия», съеденный торец поля,
замороженное «(вчера)» в подписи фото. Ломаются — значит, враньё
вернулось.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from suv.crop import CROPS, blended_kc
from suv.ledger import Ledger
from suv.messages import recommendation_text, savings_text


# ------------------------------------------------------------- NDVI / Kc

def test_future_imagery_never_outweighs_both_sources():
    """Warm-start гонит симуляцию по дням ДО даты снимка — возраст
    отрицательный. Вес был без верхней границы: blended_kc(0.95, 0.55,
    -11) давал 0.45 — ниже И календаря, И спутника разом."""
    kc, src = blended_kc(0.95, 0.55, -11)
    assert 0.55 <= kc <= 0.95, f"Kc {kc} вне обоих источников"
    assert "124%" not in src

    # Снимок из будущего весит как свежий, не больше.
    fresh, _ = blended_kc(0.95, 0.55, 0)
    negative, _ = blended_kc(0.95, 0.55, -30)
    assert negative == pytest.approx(fresh)


def test_bbox_polygon_side_matches_requested_size():
    """«Квадрат 200 м» обязан быть 200x200 м (4 га), а не 400x400 (16):
    NDVI двухгектарного сада на 7/8 состоял из соседей и дороги."""
    import math
    from suv.satellite import bbox_polygon
    ring = bbox_polygon(39.558, 66.996, 200.0)
    lats = [p[1] for p in ring]
    lons = [p[0] for p in ring]
    height_m = (max(lats) - min(lats)) * 111_320.0
    width_m = ((max(lons) - min(lons)) * 111_320.0
               * math.cos(math.radians(39.558)))
    assert height_m == pytest.approx(200.0, rel=0.01)
    assert width_m == pytest.approx(200.0, rel=0.05)


# ------------------------------------------------------- стороны поля

def _vineyard():
    """Виноградник Фарруха: вытянут с юга на север, северо-восточный
    угол срезан межой ~33 м. Это и есть ворота с канала."""
    import math
    from suv.field_shape import meters_per_degree
    lat0, lon0 = 39.558516, 66.996956
    m_lat, m_lon = meters_per_degree(lat0)
    def P(x, y): return (lat0 + y / m_lat, lon0 + x / m_lon)
    return [P(0, 0), P(60, 0), P(60, 200), P(37, 224), P(0, 224)]


def test_the_gate_from_the_canal_is_offered_to_the_farmer():
    """Вода в виноградник заходит с СЕВЕРО-ВОСТОКА (со слов Амира
    17.08.2026), а склейка прячет 33-метровые ворота внутри длинной
    «восточной» межи. В списке кнопок северо-востока не было вовсе —
    нажать было нечего, и вход записывали руками прямо в базу."""
    from suv.field_shape import inlet_options, side_labels
    vine = _vineyard()

    sides = {e.name("ru") for e, _l in side_labels(vine, "ru")}
    assert "северо-восток" not in sides, "склейка изменилась — проверьте тест"

    options = inlet_options(vine, "ru")
    gate = [e for e, _l in options if e.name("ru") == "северо-восток"]
    assert gate, "ворот с канала нет в списке — выбрать нечего"
    assert 30 <= gate[0].length_m <= 36
    # Склейка не сломана: длинные межи по-прежнему цельные, а не по куску
    # на каждую точку обхода.
    assert len(options) == len(sides) + 1


def test_gates_never_become_a_button_per_point():
    """Ворота — срезанный угол, а не любой излом. Зубец виляющей межи
    поворачивает туда и сразу обратно, дуга поворачивает понемногу — ни
    то, ни другое воротами не считается. Иначе вернулась бы та самая
    беда, ради которой склейка и написана: поле, обведённое двадцатью
    точками, давало двадцать почти одинаковых кнопок."""
    import math
    from suv.field_shape import inlet_options, meters_per_degree, side_labels
    lat0, lon0 = 39.5585, 66.9960
    m_lat, m_lon = meters_per_degree(lat0)
    def P(x, y): return (lat0 + y / m_lat, lon0 + x / m_lon)

    jagged = ([P(0, 0), P(400, 0), P(400, 250)]
              + [P(400 * k / 8, 250 + (35 if k % 2 else -35))
                 for k in range(7, 0, -1)]
              + [P(0, 250)])
    sides = side_labels(jagged, "ru")
    options = inlet_options(jagged, "ru")
    assert len(options) < len(jagged), "кнопка на каждую точку обхода"
    # Склейка цела: все настоящие стороны на месте.
    assert {(e.i, e.j) for e, _l in sides} <= {(e.i, e.j) for e, _l in options}

    # Плавная дуга — вообще без ворот: поворот на каждой вершине мелкий.
    circle = [P(200 * math.cos(math.radians(a * 18)),
                200 * math.sin(math.radians(a * 18))) for a in range(20)]
    assert len(inlet_options(circle, "ru")) == len(side_labels(circle, "ru"))


def test_declared_inlet_survives_a_redrawn_contour():
    """Индексы вершин слетают при перечерчивании — румб в конфиге нет.
    По нему ворота находятся на новом контуре заново."""
    from suv.field_config import resolve_inlet
    from suv.field_shape import inlet_edge, to_geojson_ring
    vine = _vineyard()
    pair = resolve_inlet(to_geojson_ring(vine), "северо-восток")
    assert pair is not None
    assert inlet_edge(vine, pair).name("ru") == "северо-восток"

    # Соседний румб не подсовываем: восток и северо-восток на вытянутом
    # поле — разные межи, и подмена была бы тихой неправдой.
    from suv.field_shape import meters_per_degree
    m_lat, m_lon = meters_per_degree(39.5585)
    square = to_geojson_ring([(39.5585, 66.996),
                              (39.5585, 66.996 + 100 / m_lon),
                              (39.5585 + 100 / m_lat, 66.996 + 100 / m_lon),
                              (39.5585 + 100 / m_lat, 66.996)])
    assert resolve_inlet(square, "северо-восток") is None


def test_narrow_field_keeps_its_short_ends():
    """Полоса 400x35 м вдоль канала: допуск склейки превышал ширину, оба
    торца исчезали, и сторону входа воды с канала нельзя было выбрать."""
    from suv.field_shape import edges
    lat0, lon0 = 39.5585, 66.9960
    import math
    dlat = 35 / 110_540.0
    dlon = 400 / (111_320.0 * math.cos(math.radians(lat0)))
    pts = [(lat0, lon0), (lat0, lon0 + dlon),
           (lat0 + dlat, lon0 + dlon), (lat0 + dlat, lon0)]
    got = sorted(e.name("uz") for e in edges(pts))
    assert got == ["g'arb", "janub", "sharq", "shimol"], \
        f"торцы съедены склейкой: {got}"


# ------------------------------------------------------------- экономия

def _make_ledger(tmp_path, baseline_per_ha=288.0):
    led = Ledger(tmp_path / "t.db")
    led.upsert_field(
        field_id="T-1", name="Test", owner_chat_id=1, hectares=2.0,
        lat=39.0, lon=67.0, elevation_m=700.0, crop_key="apple",
        soil_key="sandy_loam", planting_date="2018-03-20",
        irrigation_method="drip", water_table_depth_m=40.0,
        baseline_m3_per_ha=baseline_per_ha, baseline_interval_days=4)
    return led


def _rec(generated_on):
    from dataclasses import dataclass, field as dc_field

    @dataclass
    class _F:
        field_id: str = "T-1"
        ndvi: float | None = None
        ndvi_date: date | None = None

    @dataclass
    class _R:
        field: _F = dc_field(default_factory=_F)
        generated_on: date = date.today()
        action_day: date | None = None
        gross_mm: float = 30.0
        gross_m3: float = 600.0
        reason_key: str = "threshold_reached"
        plan: list = dc_field(default_factory=list)

    r = _R()
    r.generated_on = generated_on
    return r


def test_savings_freeze_when_farmer_stops_confirming(tmp_path):
    """Одно подтверждение за сезон — и база росла по календарю вечно:
    +576 м³ «экономии» каждые 4 дня без единого нового факта. База
    обязана замирать на последнем подтверждённом поливе."""
    led = _make_ledger(tmp_path)
    rid = led.log_recommendation(_rec(date.today() - timedelta(days=40)),
                                 "test")
    led.log_action(rid, followed=True,
                   actual_day=date.today() - timedelta(days=32),
                   actual_m3=600.0, source="farmer")
    s = led.savings("T-1")
    # окно = 8 дней (первая рекомендация -> последнее подтверждение):
    # три полива по старой привычке, а не десять календарных дней
    assert s.baseline_m3 == pytest.approx(288.0 * 2.0 * 3)
    assert s.saved_m3 == pytest.approx(1728.0 - 600.0)


def test_null_baseline_never_shows_negative_savings(tmp_path):
    """FAR-UZUM: baseline не задан. После первой отметки /tejaldi
    показывал «-2 445 m³ сэкономлено». Без базы заявки нет вообще."""
    led = _make_ledger(tmp_path, baseline_per_ha=None)
    rid = led.log_recommendation(_rec(date.today() - timedelta(days=10)),
                                 "test")
    led.log_action(rid, followed=True, actual_day=date.today(),
                   actual_m3=None, source="farmer")
    s = led.savings("T-1")
    assert s.saved_m3 == 0.0
    assert not s.has_baseline
    for lang in ("uz", "ru"):
        text = savings_text(s, lang)
        assert "-" not in text.split("\n")[0]


def test_double_tap_detected_by_has_action(tmp_path):
    """Двойной тап по «10 soat» задваивал metered — хендлер обязан уметь
    спросить, была ли уже отметка."""
    led = _make_ledger(tmp_path)
    rid = led.log_recommendation(_rec(date.today()), "test")
    assert not led.has_action(rid)
    led.log_action(rid, followed=True, actual_day=date.today(),
                   actual_m3=600.0, source="farmer")
    assert led.has_action(rid)


# ------------------------------------------------------------- сообщения

def test_cost_matches_the_hours_shown():
    """«Включите насос на 10 ч» и деньги за 9.6 ч в одном сообщении:
    Фаррух знает цену часа наизусть и делит — бот выглядел ошибающимся."""
    from dataclasses import dataclass, field as dc_field

    @dataclass
    class _F:
        name: str = "Olmazor"
        hectares: float = 2.0

    @dataclass
    class _R:
        field: _F = dc_field(default_factory=_F)
        action_day: date | None = date.today()
        gross_mm: float = 28.8
        gross_m3: float = 576.0
        reason_key: str = "threshold_reached"
        days_until: int = 0
        plan: list = dc_field(default_factory=list)

    @dataclass
    class _Pump:
        m3_per_hour: float = 60.0
        cost_per_hour_uzs: float = 45000.0

    msg = recommendation_text(_R(), "uz", pump=_Pump())
    # 576/60 = 9.6 -> показываем 10 soat, деньги тоже за 10
    assert "10 soat" in msg
    assert "450 000" in msg, msg


# ------------------------------------------------------------- кэш фото

def test_photo_cache_survives_a_cloudy_newer_pass(tmp_path):
    """Свежий проход облачный, фото собрано по предыдущему чистому дню.
    Кэш сравнивал дату кадра с датой каталога и промахивался ВСЕГДА:
    каждое нажатие — 30-60 секунд пересборки той же картинки."""
    led = _make_ledger(tmp_path)
    led.save_photo("T-1", "file-abc", "2026-08-13", "v2:uz:fp", "cap",
                   latest_seen="2026-08-16")
    # каталог по-прежнему показывает облачный 16.08 — кэш обязан отдать
    hit = led.cached_photo("T-1", "2026-08-16", "v2:uz:fp")
    assert hit is not None
    file_id, caption, scene = hit
    assert file_id == "file-abc" and scene == "2026-08-13"
    # чистый кадр за 18.08 появился — кэш обязан промахнуться
    assert led.cached_photo("T-1", "2026-08-18", "v2:uz:fp") is None
    # другой язык — другой ключ: подпись не достаётся не на том языке
    assert led.cached_photo("T-1", "2026-08-16", "v2:ru:fp") is None


def test_when_word_is_computed_from_today():
    """«(kecha)» в кэшированной подписи через три дня — неправда."""
    from suv.photo_render import when_word
    d = date(2026, 8, 13)
    assert when_word(d, date(2026, 8, 13), True) == "bugun"
    assert when_word(d, date(2026, 8, 14), True) == "kecha"
    assert when_word(d, date(2026, 8, 17), True) == "4 kun oldin"
    assert when_word(d, date(2026, 8, 17), False) == "4 дн. назад"


# ------------------------------------------------------- watermark-старт

def test_rewind_starts_from_a_full_profile_after_irrigation():
    """В день полива профиль полон — и это НЕ описка.

    Однажды это уже «чинили»: движок ограничивает капельный сеанс 25 мм
    net, а порог RAW у яблони под 90 мм, откуда следовал остаток ~72 мм
    долга. Посылка оказалась ложной — Фаррух поливает по расписанию раз
    в четыре дня, а не по порогу, и накопленные за них ~23 мм сеанс
    перекрывает целиком. Подстановка остатка давала 2778 м³ на две
    недели против 1808 м³ настоящей потребности: перелив в полтора раза,
    а лишняя перколяция на засоленной земле поднимает соль к корням.

    Тест держит именно ноль, чтобы «починка» не вернулась третий раз.
    """
    from datetime import timedelta

    from bot.main import _rewind
    from suv.climate import STATIONS, season
    from suv.schedule import Field, recommend
    from suv.soil import SOILS

    today = date(2026, 8, 17)
    last_irr = date(2026, 8, 6)
    gap = (today - last_irr).days
    fld = Field(
        field_id="FAR-OLMA", name="Olmazor", hectares=2.0,
        lat=39.558108, lon=66.996099, elevation_m=700.0,
        crop=CROPS["apple"], soil=SOILS["sandy_loam"],
        planting_date=date(2018, 3, 20), irrigation_method="drip",
        water_table_depth_m=40.0)
    series = season(STATIONS["samarkand"], today - timedelta(days=gap),
                    gap + 14)

    state, future = _rewind(fld, last_irr, series, gap, today)
    rec = recommend(fld, future, state, today)
    # 11 дней августа без полива — дефицит есть, но не запредельный,
    # и бот не назначает полив четыре дня подряд.
    assert 30.0 < state.depletion_mm < 100.0, state.depletion_mm
    events = sum(1 for p in rec.plan if p.irrigate)
    assert events <= 3, f"{events} поливов за 14 дней — это перелив"


def test_rewind_without_anchor_keeps_the_old_behaviour():
    """Поле без даты последнего полива стартует с полного профиля и
    честного предупреждения — задокументированный компромисс."""
    from bot.main import _rewind
    from suv.climate import STATIONS, season
    from suv.schedule import Field
    from suv.soil import SOILS

    today = date(2026, 8, 17)
    grape = Field(
        field_id="FAR-UZUM", name="Uzumzor", hectares=1.5,
        lat=39.558516, lon=66.996956, elevation_m=700.0,
        crop=CROPS["grape"], soil=SOILS["sandy_loam"],
        planting_date=date(2017, 4, 1), irrigation_method="furrow",
        water_table_depth_m=40.0)
    state, _future = _rewind(grape, None, season(STATIONS["samarkand"],
                                                 today, 14), 0, today)
    assert state.depletion_mm == 0.0


# --------------------------------------------- настоящий контур с сервера

# Контур виноградника Фарруха, снят с боевой базы 18.08.2026. Площадь по
# нему 1.46 га — ровно то, что показывает бот. Держим его здесь целиком:
# все прежние проверки ворот делались на реконструкции по описанию из
# коммита, и один раз это уже вышло боком — фикс, проверенный на
# выдуманной форме, на настоящей мог и не сработать.
REAL_UZUM = [
    [66.996732, 39.559532], [66.997665, 39.560061], [66.997966, 39.559937],
    [66.996582, 39.557199], [66.996185, 39.557307], [66.997107, 39.559292],
    [66.996732, 39.559532],
]


def test_real_vineyard_offers_the_northeast_gate():
    """На НАСТОЯЩЕМ контуре виноградника вода заходит с северо-востока
    через межу 29 м, и она обязана быть в списке кнопок.

    Что показывал бот до правки (снято с сервера 18.08.2026): «с
    юго-востока · 330 м», «с запада · 251 м», «с северо-запада · 115 м».
    Северо-востока нет вовсе — Фаррух не мог ответить на вопрос бота, и
    вход воды записывали руками прямо в базу.
    """
    from suv.field_shape import area_ha, from_geojson_ring, inlet_options
    pts = from_geojson_ring(REAL_UZUM)
    assert area_ha(pts) == pytest.approx(1.46, abs=0.01)

    options = inlet_options(pts, "ru")
    gates = [e for e, _l in options if e.name("ru") == "северо-восток"]
    assert gates, [l for _e, l in options]
    assert 25 <= gates[0].length_m <= 35

    # Склейка не рассыпалась: длинные межи остались цельными.
    assert len(options) <= 6, [l for _e, l in options]


def test_real_vineyard_gate_survives_a_stray_vertex():
    """Двойной тап пальцем в Mini App оставляет у ворот отрезок в
    полметра. Поворот считался от сырых соседей, шип делил его надвое, и
    ворота пропадали в половине случаев — на живом поле это значило
    «вход воды снова не выбрать»."""
    from suv.field_shape import from_geojson_ring, inlet_options
    pts = from_geojson_ring(REAL_UZUM)
    m_lat, m_lon = __import__("suv.field_shape", fromlist=["x"]).meters_per_degree(pts[0][0])

    lost = 0
    for cm in range(50, 300, 25):          # шип 0.5 … 3 м за воротами
        a, b = pts[1], pts[2]
        ang = math.atan2((b[1] - a[1]) * m_lon, (b[0] - a[0]) * m_lat)
        spur = (b[0] + (cm / 100) * math.cos(ang) / m_lat,
                b[1] + (cm / 100) * math.sin(ang) / m_lon)
        jagged = pts[:2] + [spur] + pts[2:]
        if not any(e.name("ru") == "северо-восток"
                   for e, _l in inlet_options(jagged, "ru")):
            lost += 1
    assert lost == 0, f"ворота потеряны на {lost} вариантах шипа"
