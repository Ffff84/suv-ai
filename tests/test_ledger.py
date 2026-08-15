"""
Тесты журнала экономии — правила честности savings().

Каждый тест здесь соответствует способу СЛУЧАЙНО завысить экономию,
который уже был найден в коде. Ломаются — значит, завышение вернулось.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import date, timedelta

import pytest

from suv.ledger import Ledger


@dataclass
class _Plan:
    kc: float = 0.9
    kc_source: str = "calendar"
    et0_mm: float = 6.0
    etc_mm: float = 5.4
    depletion_mm: float = 40.0
    raw_mm: float = 80.0
    salinity: str = "low"


@dataclass
class _Field:
    field_id: str = "T-1"
    ndvi: float | None = None
    ndvi_date: date | None = None


@dataclass
class _Rec:
    field: _Field = dc_field(default_factory=_Field)
    generated_on: date = dc_field(default_factory=date.today)
    action_day: date | None = None
    gross_mm: float = 30.0
    gross_m3: float = 600.0
    reason_key: str = "threshold_reached"
    plan: list = dc_field(default_factory=lambda: [_Plan()])


def _make_ledger(tmp_path, baseline_per_ha=288.0, interval=4):
    led = Ledger(tmp_path / "t.db")
    led.upsert_field(
        field_id="T-1", name="Test", owner_chat_id=1, hectares=2.0,
        lat=39.0, lon=67.0, elevation_m=700.0, crop_key="apple",
        soil_key="sandy_loam", planting_date="2018-03-20",
        irrigation_method="drip", water_table_depth_m=40.0,
        baseline_m3_per_ha=baseline_per_ha, baseline_interval_days=interval)
    return led


def test_no_actions_means_no_claim(tmp_path):
    """Рекомендации были, фермер молчит — экономия ровно 0."""
    led = _make_ledger(tmp_path)
    led.log_recommendation(_Rec(generated_on=date.today() - timedelta(days=20)),
                           "test")
    s = led.savings("T-1")
    assert s.recommendations == 1
    assert s.followed == 0
    assert s.saved_m3 == 0.0


def test_baseline_starts_at_first_recommendation(tmp_path):
    """База считается с первой рекомендации, не с начала вегетации.

    Поле яблони с вегетацией с 20 марта: считай мы базу от неё, к августу
    набежало бы ~35 интервалов. От первой рекомендации 8 дней назад при
    интервале 4 дня — ровно 2.
    """
    led = _make_ledger(tmp_path)
    rid = led.log_recommendation(
        _Rec(generated_on=date.today() - timedelta(days=8)), "test")
    led.log_action(rid, followed=True, actual_day=date.today(),
                   actual_m3=500.0, source="farmer")
    s = led.savings("T-1")
    # 288 м3/га * 2 га * (8 дней // 4) = 1152
    assert s.baseline_m3 == pytest.approx(1152.0)
    assert s.saved_m3 == pytest.approx(1152.0 - 500.0)


def test_followed_without_volume_counts_recommended(tmp_path):
    """followed без замера — считаем, что вылил сколько сказали (gross_m3),
    а не ноль: ноль завышает экономию."""
    led = _make_ledger(tmp_path)
    rid = led.log_recommendation(
        _Rec(generated_on=date.today() - timedelta(days=4), gross_m3=600.0),
        "test")
    led.log_action(rid, followed=True, actual_day=date.today(),
                   actual_m3=None, source="farmer")
    s = led.savings("T-1")
    assert s.metered_m3 == pytest.approx(600.0)
    # база: 288*2*(4//4)=576; экономия отрицательная — и это правда
    assert s.saved_m3 == pytest.approx(576.0 - 600.0)


def test_unknown_column_rejected(tmp_path):
    """Имена kwargs попадают в SQL — незнакомая колонка должна падать."""
    led = _make_ledger(tmp_path)
    with pytest.raises(ValueError):
        led.upsert_field(field_id="X", **{"name": "n", "evil) VALUES(1); --": 1})


def test_old_db_gets_last_irrigation_column(tmp_path):
    """Миграция: база, созданная до колонки last_irrigation_date,
    должна открыться и принять запись с этой колонкой."""
    import sqlite3
    p = tmp_path / "old.db"
    with sqlite3.connect(p) as c:
        c.execute("""CREATE TABLE fields (
            field_id TEXT PRIMARY KEY, name TEXT NOT NULL,
            owner_chat_id INTEGER, hectares REAL NOT NULL,
            lat REAL NOT NULL, lon REAL NOT NULL,
            elevation_m REAL NOT NULL, crop_key TEXT NOT NULL,
            soil_key TEXT NOT NULL, planting_date TEXT NOT NULL,
            irrigation_method TEXT NOT NULL,
            water_table_depth_m REAL DEFAULT 0,
            baseline_m3_per_ha REAL, baseline_interval_days INTEGER,
            pump_kwh_per_hour REAL, pump_m3_per_hour REAL,
            pump_cost_per_hour_uzs REAL, pump_lift_m REAL,
            created_at TEXT NOT NULL)""")
    led = Ledger(p)
    led.upsert_field(
        field_id="T-2", name="Old", owner_chat_id=1, hectares=1.0,
        lat=39.0, lon=67.0, elevation_m=700.0, crop_key="apple",
        soil_key="loam", planting_date="2020-03-20",
        irrigation_method="drip", water_table_depth_m=0.0,
        baseline_m3_per_ha=None, baseline_interval_days=30,
        last_irrigation_date="2026-08-01")


# ---------------------------------------------------------------- контур

def _seeded(tmp_path, **extra):
    """Поле в свежей базе — как его заводит агроном из конфига."""
    led = Ledger(tmp_path / "shape.db")
    led.upsert_field(
        field_id="F-1", name="Guliston", owner_chat_id=1, hectares=10.0,
        lat=39.6, lon=66.9, elevation_m=700.0, crop_key="cotton",
        soil_key="loam", planting_date="2026-04-10",
        irrigation_method="furrow", water_table_depth_m=0.0,
        baseline_m3_per_ha=1100.0, baseline_interval_days=30, **extra)
    return led


RING = [[66.9, 39.6], [66.91, 39.6], [66.91, 39.61], [66.9, 39.6]]


def _field(led):
    import sqlite3
    with sqlite3.connect(led.path) as c:
        c.row_factory = sqlite3.Row
        return c.execute("SELECT * FROM fields WHERE field_id='F-1'").fetchone()


def test_reseeding_a_field_keeps_the_drawn_contour(tmp_path):
    """Повторный запуск scripts/seed_field.py — документированный шаг
    деплоя. Он не смеет стирать границу, которую фермер обошёл ногами:
    INSERT OR REPLACE обнулял все не переданные колонки."""
    led = _seeded(tmp_path)
    led.save_polygon("F-1", RING, 8.4, "pins")
    led.save_inlet("F-1", 0, 1)

    _seeded(tmp_path)          # тот же конфиг заливают ещё раз
    row = _field(led)
    assert row["polygon_geojson"], "контур стёрт повторным seed"
    assert row["area_ha"] == 8.4
    assert row["polygon_source"] == "pins"
    assert row["inlet_vertices"] == "[0, 1]"


def test_redrawing_the_contour_forgets_the_inlet_side(tmp_path):
    """Сторона входа хранится индексами вершин. На новой границе они
    указывают куда попало, и «вода заходит с севера» стало бы тихой
    неправдой — поэтому сбрасывается вместе с контуром."""
    led = _seeded(tmp_path)
    led.save_polygon("F-1", RING, 8.4, "pins")
    led.save_inlet("F-1", 0, 1)
    assert _field(led)["inlet_vertices"] == "[0, 1]"

    led.save_polygon("F-1", RING, 9.1, "miniapp")
    assert _field(led)["inlet_vertices"] is None


def test_area_from_the_contour_never_overwrites_the_declared_one(tmp_path):
    """Заявленная площадь остаётся как есть: расхождение с обмеренной
    видно только пока целы обе цифры."""
    led = _seeded(tmp_path)
    led.save_polygon("F-1", RING, 8.4, "pins")
    row = _field(led)
    assert row["hectares"] == 10.0 and row["area_ha"] == 8.4
