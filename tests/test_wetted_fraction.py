"""
Доля смачивания почвы поливом — пропущенный член водного баланса капли.

До 23.08.2026 движок считал запас влаги сада на капле как 1,5 м корней
на ВСЮ площадь: ~100 мм до порога, 20+ дней между поливами. Капля
смачивает влажный конус под капельницей, а не межрядье, и FAO-56
(таблица 20) даёт для неё долю 0,3-0,4; Keller & Bliesner считают запас
под каплей через неё же: d_x = MAD · Wa · Z · Pw. Фаррух льёт раз в
четыре дня — с долей 0,40 движок сходится с его практикой, без неё
расходится впятеро.

Тесты держат три вещи: борозда не изменилась (доля 1,0), капля получила
долю, и warm-start не путает глубину корней с урезанным TAW.
"""

from datetime import date

import pytest

from suv.crop import CROPS
from suv.et0 import DailyWeather
from suv.schedule import Field, recommend, simulate
from suv.soil import (SOILS, WETTED_FRACTION, WaterBalanceState, step,
                      total_available_water, wetted_fraction)


def _orchard(**kw):
    base = dict(
        field_id="T-OLMA", name="Olmazor", hectares=2.52,
        lat=39.558, lon=66.996, elevation_m=700.0,
        crop=CROPS["apple"], soil=SOILS["sandy_loam"],
        planting_date=date(2018, 3, 20), irrigation_method="drip",
        water_table_depth_m=40.0,
    )
    base.update(kw)
    return Field(**base)


def _august(days=14):
    # Жаркий сухой август Самарканда: ET0 около 6 мм в день.
    return [DailyWeather(doy=229 + i, t_max=35.0, t_min=20.0, rh_mean=30.0,
                         wind_2m=2.0, solar_rad=24.0, rainfall=0.0)
            for i in range(days)]


# ------------------------------------------------------------ умолчания

def test_defaults_only_drip_is_partially_wetted():
    assert WETTED_FRACTION["drip"] == pytest.approx(0.40)
    for method in ("furrow", "furrow_improved", "sprinkler"):
        assert wetted_fraction(method) == 1.0
    assert wetted_fraction("unknown_method") == 1.0


def test_override_wins_and_is_bounded():
    assert wetted_fraction("drip", 0.6) == pytest.approx(0.6)
    assert wetted_fraction("furrow", 0.5) == pytest.approx(0.5)
    with pytest.raises(ValueError):
        wetted_fraction("drip", 0.0)
    with pytest.raises(ValueError):
        wetted_fraction("drip", 1.5)


def test_taw_scales_with_wetted_fraction():
    soil = SOILS["sandy_loam"]
    full = total_available_water(soil, 1.5)
    assert full == pytest.approx(195.0)
    assert total_available_water(soil, 1.5, 0.40) == pytest.approx(78.0)
    # Подпись без доли — прежняя формула FAO-56 eq. 82, ничего не поехало.
    assert total_available_water(soil, 1.5) == full


def test_step_caps_depletion_at_the_wetted_reservoir():
    soil = SOILS["sandy_loam"]
    st = WaterBalanceState(0.0, 1.5)
    # 40 дней по 5 мм без воды: сухой сад не может «задолжать» больше,
    # чем держит влажный объём.
    for _ in range(40):
        st, _ = step(st, soil, 1.5, 5.0, 0.0, wetted_fraction=0.40)
    assert st.depletion_mm == pytest.approx(78.0)


# ------------------------------------------------------------ движок

def test_drip_orchard_interval_matches_farm_practice():
    """После полива сад на капле просит воду через неделю, не через три."""
    f = _orchard()
    rec = recommend(f, _august(), WaterBalanceState(0.0, 1.5), date(2026, 8, 17))
    assert rec.action_day is not None, "за 14 дней августа капля обязана попросить воду"
    assert 4 <= rec.days_until <= 10, rec.days_until
    # Запас и порог в плане — уже с долей смачивания.
    assert rec.plan[0].taw_mm == pytest.approx(78.0, abs=0.5)
    assert rec.plan[0].raw_mm < 50.0


def test_drip_without_the_fraction_would_wait_three_weeks():
    """Контроль: та же ситуация с долей 1,0 — старое поведение (20+ дней).
    Это и есть величина ошибки, которую закрыл член смачивания."""
    f = _orchard(wetted_fraction=1.0)
    rec = recommend(f, _august(), WaterBalanceState(0.0, 1.5), date(2026, 8, 17))
    assert rec.action_day is None or rec.days_until >= 12


def test_furrow_vineyard_unchanged_by_the_new_term():
    """У борозды доля 1,0: explicit 1.0 и умолчание дают один и тот же план."""
    a = _orchard(field_id="T-UZUM", crop=CROPS["grape"], hectares=1.5,
                 irrigation_method="furrow", planting_date=date(2017, 4, 1))
    b = _orchard(field_id="T-UZUM", crop=CROPS["grape"], hectares=1.5,
                 irrigation_method="furrow", planting_date=date(2017, 4, 1),
                 wetted_fraction=1.0)
    st = WaterBalanceState(40.0, 1.5)
    pa = simulate(a, _august(), st, date(2026, 8, 17))
    pb = simulate(b, _august(), st, date(2026, 8, 17))
    assert [(p.depletion_mm, p.taw_mm, p.irrigate) for p in pa] == \
           [(p.depletion_mm, p.taw_mm, p.irrigate) for p in pb]
    assert pa[0].taw_mm == pytest.approx(195.0, abs=0.5)


def test_plan_carries_root_depth_separately_from_taw():
    """taw_mm у капли урезан долей — глубину корней из него не вывести.
    Warm-start (bot._rewind, run_field.warm_start) читает root_depth_m."""
    plan = simulate(_orchard(), _august(3), WaterBalanceState(0.0, 1.5),
                    date(2026, 8, 17), apply_irrigation=False)
    assert plan[-1].root_depth_m == pytest.approx(1.5)
    assert plan[-1].taw_mm == pytest.approx(78.0, abs=0.5)


def test_rewind_keeps_mature_roots_for_drip():
    """Отмотка по истории не должна «укорачивать» корни сада до 0,6 м."""
    from datetime import timedelta

    from bot.main import _rewind
    today = date(2026, 8, 17)
    series = _august(20)
    state, future = _rewind(_orchard(), today - timedelta(days=6), series, 6, today)
    assert state.last_root_depth_m == pytest.approx(1.5)
    assert len(future) == 14


# ------------------------------------------------------------ конфиг/база

def test_config_and_ledger_carry_wetted_fraction(tmp_path):
    from suv.field_config import to_row, validate
    from suv.ledger import Ledger
    cfg = {
        "field_id": "T-1", "name": "T", "hectares": 1.0, "lat": 39.5,
        "lon": 67.0, "elevation_m": 700, "crop": "apple", "soil": "sandy_loam",
        "planting_date": "2018-03-20", "irrigation_method": "drip",
        "water_table_depth_m": 40.0, "wetted_fraction": 0.55,
    }
    row = to_row(cfg)
    assert row["wetted_fraction"] == pytest.approx(0.55)
    assert to_row(dict(cfg, wetted_fraction=None))["wetted_fraction"] is None

    led = Ledger(tmp_path / "t.db")
    led.upsert_field(owner_chat_id=1, **row)
    import sqlite3
    with sqlite3.connect(led.path) as c:
        got = c.execute("SELECT wetted_fraction FROM fields WHERE field_id='T-1'").fetchone()[0]
    assert got == pytest.approx(0.55)

    with pytest.raises(ValueError):
        validate(dict(cfg, wetted_fraction=0))
    with pytest.raises(ValueError):
        validate(dict(cfg, wetted_fraction=1.2))


def test_build_field_reads_fraction_from_row(tmp_path):
    """Строка базы -> Field: значение доходит до движка, NULL = умолчание."""
    from suv.field_config import to_row
    from suv.ledger import Ledger
    import bot.main as B
    cfg = {
        "field_id": "T-2", "name": "T", "hectares": 1.0, "lat": 39.5,
        "lon": 67.0, "elevation_m": 700, "crop": "apple", "soil": "sandy_loam",
        "planting_date": "2018-03-20", "irrigation_method": "drip",
        "water_table_depth_m": 40.0, "wetted_fraction": 0.55,
    }
    led = Ledger(tmp_path / "t.db")
    led.upsert_field(owner_chat_id=1, **to_row(cfg))
    led.upsert_field(owner_chat_id=1, **to_row(dict(cfg, field_id="T-3",
                                                    wetted_fraction=None)))
    import sqlite3
    c = sqlite3.connect(led.path)
    c.row_factory = sqlite3.Row
    r2 = c.execute("SELECT * FROM fields WHERE field_id='T-2'").fetchone()
    r3 = c.execute("SELECT * FROM fields WHERE field_id='T-3'").fetchone()
    assert B._build_field(r2).wetted_fraction == pytest.approx(0.55)
    assert B._build_field(r3).wetted_fraction is None
