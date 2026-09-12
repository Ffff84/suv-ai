"""
Режим съёма урожая: полив останавливается перед теримом и на время
съёма, после — возвращается.

Причина настоящая, с поля: 30.08.2026 Фаррух сказал, что не будет
поливать яблоню перед сбором — боится, что налитый водой плод заплесневеет
в холодильнике. Садоводческая практика подтверждает сухую паузу в 1-2
недели перед съёмом, но НЕ «без воды с сентября»: без даты съёма движок
работает как раньше, с датой — держит паузу ровно
crop.preharvest_hold_days и отпускает после конца окна.
"""

from datetime import date, timedelta

import pytest

from suv.crop import CROPS
from suv.et0 import DailyWeather
from suv.messages import recommendation_text
from suv.schedule import Field, harvest_hold_window, recommend
from suv.soil import SOILS, WaterBalanceState


def _wx(days=14):
    return [DailyWeather(doy=230 + i, t_max=34.0, t_min=20.0, rh_mean=30.0,
                         wind_2m=2.0, solar_rad=23.0, rainfall=0.0)
            for i in range(days)]


def _orchard(**kw):
    base = dict(field_id="T-OLMA", name="Olmazor", hectares=2.52,
                lat=39.558, lon=66.996, elevation_m=700.0,
                crop=CROPS["apple"], soil=SOILS["sandy_loam"],
                planting_date=date(2018, 3, 20), irrigation_method="drip",
                water_table_depth_m=40.0)
    base.update(kw)
    return Field(**base)


TODAY = date(2026, 8, 30)
# Дефицит у порога: без окна съёма движок назначил бы полив в первые дни.
NEAR_THRESHOLD = WaterBalanceState(40.0, 1.5)


def test_no_harvest_date_means_no_change():
    rec = recommend(_orchard(), _wx(), NEAR_THRESHOLD, TODAY)
    assert rec.action_day is not None
    assert rec.reason_key != "harvest_hold"


def test_irrigation_inside_the_hold_window_is_suppressed():
    """Съём 10.09: полив, который выпал бы на первые дни сентября,
    попадает в 14-дневную сухую паузу яблони и не назначается."""
    f = _orchard(harvest_start=date(2026, 9, 10), harvest_end=date(2026, 9, 25))
    rec = recommend(f, _wx(), NEAR_THRESHOLD, TODAY)
    assert rec.action_day is None
    assert rec.reason_key == "harvest_hold"
    assert rec.gross_m3 == 0.0
    # План при этом честный: дефицит в нём виден, кабинет его покажет.
    assert rec.plan


def test_window_is_hold_days_wide_not_the_whole_autumn():
    """Съём далеко (октябрь): август поливается как обычно — сухая пауза
    начинается за preharvest_hold_days, а не «с сентября не поливаем»."""
    apple = CROPS["apple"]
    assert apple.preharvest_hold_days == 14
    f = _orchard(harvest_start=date(2026, 10, 20), harvest_end=date(2026, 10, 30))
    rec = recommend(f, _wx(), NEAR_THRESHOLD, TODAY)
    assert rec.action_day is not None, "полив за месяц до терима законен"
    lo, hi = harvest_hold_window(f)
    assert lo == date(2026, 10, 6) and hi == date(2026, 10, 30)


def test_advice_returns_after_harvest_end():
    """Съём закончился 2 дня назад — первый послеуборочный полив
    назначается, движок не «застревает» в режиме терима."""
    f = _orchard(harvest_start=date(2026, 8, 14), harvest_end=date(2026, 8, 28))
    rec = recommend(f, _wx(), NEAR_THRESHOLD, TODAY)
    assert rec.action_day is not None
    assert rec.reason_key != "harvest_hold"


def test_open_ended_harvest_does_not_release_on_the_first_day():
    """Конец не назван — пауза не кончается в первый день съёма.

    Прежнее правило приравнивало конец к началу, и бот отпускал полив
    ровно тогда, когда фермер выходил снимать: у сада Фарруха окно
    закрывалось 14.09, а 15-го приходило «полейте сегодня, насос 12 ч».
    Съём яблони идёт две-три недели.
    """
    from suv.schedule import OPEN_HARVEST_MAX_DAYS

    f = _orchard(harvest_start=date(2026, 9, 1))
    lo, hi = harvest_hold_window(f)
    assert lo == date(2026, 8, 18)
    assert hi == date(2026, 9, 1) + timedelta(days=OPEN_HARVEST_MAX_DAYS)
    # Но не бессрочно: дата из прошлого сезона не смеет остановить полив
    # навсегда — её просто некому обновить.
    old = _orchard(harvest_start=date(2025, 9, 1))
    assert harvest_hold_window(old)[1] < date(2026, 1, 1)


def test_open_ended_harvest_still_expires():
    """Обратная сторона: через разумный срок пауза отпускает."""
    from suv.schedule import OPEN_HARVEST_MAX_DAYS

    f = _orchard(harvest_start=date(2026, 9, 1))
    _lo, hi = harvest_hold_window(f)
    assert (hi - date(2026, 9, 1)).days == OPEN_HARVEST_MAX_DAYS


def test_last_years_dates_do_not_fire():
    f = _orchard(harvest_start=date(2025, 9, 10), harvest_end=date(2025, 9, 25))
    rec = recommend(f, _wx(), NEAR_THRESHOLD, TODAY)
    assert rec.reason_key != "harvest_hold"


def test_message_says_stopped_not_unneeded():
    """«Полив приостановлен» — не «не требуется»: влага у порога, и
    фермер должен видеть, что это сознательная пауза на съём.

    Две разные фразы по обе стороны даты съёма. До неё бот не имеет
    права говорить «идёт съём»: дата лежит в том же объекте и
    опровергает фразу — у сада Фарруха 12.09.2026 съём был назначен на
    14.09, то есть шла сухая пауза, а не терим.
    """
    # ДО начала съёма: сухая пауза, и дата названа вслух.
    f = _orchard(harvest_start=date(2026, 9, 5), harvest_end=date(2026, 9, 20))
    rec = recommend(f, _wx(), NEAR_THRESHOLD, TODAY)
    assert rec.reason_key == "harvest_hold"
    assert TODAY < f.harvest_start
    uz, ru = recommendation_text(rec, "uz"), recommendation_text(rec, "ru")
    assert "to'xtatildi" in uz and "05.09" in uz
    assert "остановлен перед съёмом" in ru and "05.09" in ru
    assert "Идёт съём" not in ru, "съём ещё не начался"
    # Конец съёма назван — за него не просим.
    assert "хуже лежит" in ru or "лучше лежит" in ru

    # ВО ВРЕМЯ съёма: та самая фраза про терим.
    started = _orchard(harvest_start=TODAY - timedelta(days=1),
                       harvest_end=TODAY + timedelta(days=10))
    rec2 = recommend(started, _wx(), NEAR_THRESHOLD, TODAY)
    assert rec2.reason_key == "harvest_hold"
    uz2, ru2 = recommendation_text(rec2, "uz"), recommendation_text(rec2, "ru")
    assert "Terim davri" in uz2 and "to'xtatilgan" in uz2
    assert "Съём урожая — полив приостановлен" in ru2
    assert "хуже лежит" in ru2
    assert "не требуется" not in ru and "shart emas" not in uz
    # После съёма советы вернутся — обещание про послеуборочный полив.
    assert "qaytadi" in uz and "вернутся" in ru


def test_field_status_line_matches():
    """Карточка говорит ровно то же, что совет, — по обе стороны даты.

    Три экрана про одно поле обязаны сходиться: 12.09.2026 совет по саду
    Фарруха говорил «остановлен перед съёмом (съём с 14.09)», а карточка
    в ту же секунду — «идёт съём».
    """
    from suv.field_status import Status, water_section

    # ДО съёма — сухая пауза и дата.
    f = _orchard(harvest_start=date(2026, 9, 5), harvest_end=date(2026, 9, 20))
    rec = recommend(f, _wx(), NEAR_THRESHOLD, TODAY)
    s = water_section(rec, TODAY - timedelta(days=4), TODAY, None, "ru")
    assert s.status == Status.OK
    assert "перед съёмом" in s.line and "05.09" in s.line
    assert "Съём урожая" not in s.line
    assert "перед съёмом" in recommendation_text(rec, "ru")

    # ВО ВРЕМЯ съёма — обе строки про терим.
    g = _orchard(harvest_start=TODAY - timedelta(days=1),
                 harvest_end=TODAY + timedelta(days=10))
    rec2 = recommend(g, _wx(), NEAR_THRESHOLD, TODAY)
    s2 = water_section(rec2, TODAY - timedelta(days=4), TODAY, None, "ru")
    assert "приостановлен" in s2.line
    assert "приостановлен" in recommendation_text(rec2, "ru")


def test_config_and_db_round_trip(tmp_path):
    from suv.field_config import to_row, validate
    from suv.ledger import Ledger
    import bot.main as B
    cfg = {
        "field_id": "T-H", "name": "T", "hectares": 1.0, "lat": 39.5,
        "lon": 67.0, "elevation_m": 700, "crop": "apple", "soil": "sandy_loam",
        "planting_date": "2018-03-20", "irrigation_method": "drip",
        "water_table_depth_m": 40.0,
        "harvest_start": "2026-09-10", "harvest_end": "2026-09-25",
    }
    row = to_row(cfg)
    assert row["harvest_start"] == "2026-09-10"
    led = Ledger(tmp_path / "t.db")
    led.upsert_field(owner_chat_id=1, **row)
    import sqlite3
    c = sqlite3.connect(led.path)
    c.row_factory = sqlite3.Row
    fld = B._build_field(c.execute("SELECT * FROM fields WHERE field_id='T-H'").fetchone())
    assert fld.harvest_start == date(2026, 9, 10)
    assert fld.harvest_end == date(2026, 9, 25)

    with pytest.raises(ValueError):
        validate(dict(cfg, harvest_end="2026-09-01"))       # конец раньше начала
    with pytest.raises(ValueError):
        validate(dict(cfg, harvest_start=None))             # конец без начала
    with pytest.raises(ValueError):
        validate(dict(cfg, harvest_start="10.09.2026"))     # не ISO
    validate(dict(cfg, harvest_start=None, harvest_end=None))


def test_harvest_hold_is_never_urgent_for_the_push():
    """Автопуш: режим терима не будит «поливай сегодня» — action_day
    пуст, и push_due считает такое утро спокойным."""
    from bot.main import push_due
    f = _orchard(harvest_start=date(2026, 9, 5), harvest_end=date(2026, 9, 20))
    rec = recommend(f, _wx(), NEAR_THRESHOLD, TODAY)
    urgent = rec.action_day is not None and rec.days_until <= 1
    assert not urgent
    assert push_due(urgent, TODAY, TODAY) is False
