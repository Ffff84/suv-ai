"""
Опыт «по совету против привычки»: деление поля и парное сравнение половин.

Лекарство от «эталона нет» (фаза 2 карты OneSoil): не абсолютные тонны,
а разница половин одного поля при одной погоде. Тесты держат геометрию
(обе половины живые, вдоль хода воды, площади сходятся), дисциплину
сравнения (только кадры одного дня) и сбросы (перечерченный контур
убивает половины).
"""

import json
from datetime import date

import pytest

from suv.field_shape import from_geojson_ring, area_ha
from suv.indices import IndexReading
from suv.ledger import Ledger
from suv.trial import (MIN_HALF_FRAC, flow_bearing_from_inlet, pair_readings,
                       split, summarize)

# Прямоугольник ~300x150 м, вытянут с запада на восток.
RECT = [[66.996, 39.558], [66.9995, 39.558], [66.9995, 39.5593],
        [66.996, 39.5593], [66.996, 39.558]]


def test_split_halves_are_valid_and_equal_enough():
    ts = split(RECT)                       # ход воды = длинная ось (восток)
    total = area_ha(from_geojson_ring(RECT))
    assert ts.half_a[0] == ts.half_a[-1] and ts.half_b[0] == ts.half_b[-1]
    assert ts.area_a + ts.area_b == pytest.approx(total, rel=0.03)
    assert min(ts.area_a, ts.area_b) >= MIN_HALF_FRAC * total
    assert ts.flow_bearing_deg in (90.0, 270.0)   # длинная ось запад-восток


def test_split_along_flow_keeps_both_halves_on_the_inlet():
    """Ход воды на восток -> раздел тянется восток-запад: половины
    северная и южная, обе касаются западного края (входа)."""
    ts = split(RECT, flow_bearing_deg=90.0)
    lats_a = [p[1] for p in ts.half_a]
    lats_b = [p[1] for p in ts.half_b]
    assert max(lats_a) > max(lats_b) or max(lats_b) > max(lats_a)
    west = min(p[0] for p in RECT)
    assert min(p[0] for p in ts.half_a) == pytest.approx(west, abs=1e-4)
    assert min(p[0] for p in ts.half_b) == pytest.approx(west, abs=1e-4)


def test_split_l_shape_survives():
    L = [[66.996, 39.558], [66.999, 39.558], [66.999, 39.5588],
         [66.9975, 39.5588], [66.9975, 39.5596], [66.996, 39.5596],
         [66.996, 39.558]]
    ts = split(L)
    assert ts.area_a > 0 and ts.area_b > 0


def test_degenerate_contour_is_refused_with_advice():
    # Вырожденный «контур»: точки на одной прямой, площадь ноль.
    line = [[66.996, 39.558], [66.997, 39.558], [66.998, 39.558],
            [66.996, 39.558]]
    with pytest.raises(ValueError):
        split(line, flow_bearing_deg=45.0)
    with pytest.raises(ValueError):
        split([[66.996, 39.558], [66.997, 39.558]])   # меньше трёх вершин


def test_flow_bearing_from_inlet_points_into_the_field():
    # Вход — западное ребро (вершины 3->0 нашего кольца: [66.996,39.5593]
    # и [66.996,39.558]); вода должна течь на восток (~90°).
    b = flow_bearing_from_inlet(RECT, 3, 0)
    assert 60 <= b <= 120, b


# ------------------------------------------------------------- сравнение

def _r(day, ndvi, ndmi, clear=1.0):
    return IndexReading(day=day, ndvi=ndvi, msavi=0.3, ndre=0.3,
                        ndmi=ndmi, valid_fraction=clear)


def test_pairing_compares_only_same_day_frames():
    a = [_r(date(2026, 9, 1), 0.50, 0.15), _r(date(2026, 9, 6), 0.48, 0.10)]
    b = [_r(date(2026, 9, 6), 0.45, 0.02), _r(date(2026, 9, 11), 0.44, 0.01)]
    rows = pair_readings(a, b)
    assert [r.day for r in rows] == [date(2026, 9, 6)]
    assert rows[0].d_ndvi == pytest.approx(0.03)
    assert rows[0].d_ndmi == pytest.approx(0.08)


def test_summary_is_numbers_not_verdict():
    rows = pair_readings(
        [_r(date(2026, 9, 1), 0.5, 0.10), _r(date(2026, 9, 6), 0.5, 0.12)],
        [_r(date(2026, 9, 1), 0.5, 0.02), _r(date(2026, 9, 6), 0.5, 0.00)])
    s = summarize(rows)
    assert s["days"] == 2
    assert s["mean_d_ndmi"] == pytest.approx(0.10, abs=1e-6)
    assert "verdict" not in s and "ok" not in s     # вывод делает человек
    assert summarize([]) == {"days": 0}


# ---------------------------------------------------------------- журнал

def test_trial_round_trip_and_redraw_reset(tmp_path):
    led = Ledger(tmp_path / "t.db")
    led.upsert_field(
        field_id="T-1", name="T", owner_chat_id=1, hectares=4.0,
        lat=39.5586, lon=66.9977, elevation_m=700.0, crop_key="grape",
        soil_key="loam", planting_date="2017-04-01",
        irrigation_method="furrow", water_table_depth_m=0.0)
    ts = split(RECT)
    led.set_trial("T-1", json.dumps(ts.half_a), json.dumps(ts.half_b))

    import sqlite3
    c = sqlite3.connect(led.path)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT * FROM fields").fetchone()
    assert row["trial_started"] is not None
    assert json.loads(row["trial_half_a"])[0] == ts.half_a[0]

    # Перечерченный контур убивает половины: они резались по старой границе.
    led.save_polygon("T-1", RECT, 4.2, "pins")
    row = c.execute("SELECT * FROM fields").fetchone()
    assert row["trial_half_a"] is None and row["trial_started"] is None

    led.set_trial("T-1", "[]", "[]")
    led.clear_trial("T-1")
    row = c.execute("SELECT * FROM fields").fetchone()
    assert row["trial_half_b"] is None
