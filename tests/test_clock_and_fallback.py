"""
«Сегодня» по Ташкенту и честный резервный генератор погоды.

Три находки аудита 18.08.2026: date.today() по зоне сервера
(bot/main.py:654), дождь ×2 в synth_day (climate.py:103) и часы солнца
× 12 вместо × долготы дня (climate.py:96).
"""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from suv import clock
from suv.climate import STATIONS, season, synth_day
from suv.et0 import daylight_hours, et0

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ clock

def test_today_is_tashkent_not_server_zone():
    utc_now = datetime.now(timezone.utc)
    expected = (utc_now + timedelta(hours=5)).date()
    assert clock.today() == expected
    assert clock.now().utcoffset() == timedelta(hours=5)


def test_bot_and_cabinet_never_call_date_today_directly():
    """Одно «сегодня» на проект: любое date.today() в боте, кабинете или
    расчётных модулях — это снова вчерашний день для ночного фермера."""
    for rel in ("bot/main.py", "web/app.py", "suv/enrich.py", "suv/scene.py",
                "suv/photo_render.py", "scripts/run_field.py",
                "scripts/why_today.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "date.today()" not in src, f"{rel}: date.today() вместо suv.clock.today()"


def test_engine_accepts_tashkent_today(monkeypatch):
    """bot._log_one ставит якорь на today_tashkent(): в 00:30 по Ташкенту
    «Bugun» — это ташкентское сегодня, а не вчерашний день UTC."""
    import bot.main as B
    assert B.today_tashkent is clock.today
    src = inspect.getsource(B._log_one)
    assert "today_tashkent()" in src


# ------------------------------------------------------- synthetic weather

@pytest.mark.parametrize("month", [3, 4, 5, 10, 11])
def test_synthetic_rain_matches_the_monthly_normal(month):
    """Раньше генератор давал ~1,96 нормы (март Самарканда: 70 -> 151 мм),
    и в деградированном режиме фантомный дождь стирал реальный дефицит."""
    st = STATIONS["samarkand"]
    days = season(st, date(2026, month, 1), 30)
    got = sum(d.rainfall for d in days)
    norm = st.rain_mm[month - 1]
    assert 0.75 * norm <= got <= 1.25 * norm, (month, norm, got)


def test_dry_summer_stays_dry():
    st = STATIONS["samarkand"]
    days = season(st, date(2026, 7, 1), 31)
    assert sum(d.rainfall for d in days) < 6.0


def test_sunshine_hours_scale_with_day_length():
    """Часы солнца = n/N × N, а не × 12: в июле (N ≈ 14,6 ч) прежняя
    константа занижала Rs на ~11% и резервный ET0 на 5-6% в пик сезона."""
    st = STATIONS["fergana"]
    jul = synth_day(st, date(2026, 7, 15))
    n_jul = daylight_hours(date(2026, 7, 15).timetuple().tm_yday, st.lat)
    assert n_jul > 14.0
    # Rs июля выше, чем дал бы тот же день при 12-часовом дне.
    from suv.et0 import solar_radiation_from_sunshine
    frac = jul.solar_rad  # уже посчитанный Rs
    rs_12h = solar_radiation_from_sunshine(0.8 * 12.0, 196, st.lat)
    assert frac > rs_12h
    ref, method = et0(jul, st.lat, st.elevation_m)
    assert method == "penman-monteith"
    assert 6.0 < ref < 9.0, ref        # FAO-56 диапазон для сухого жаркого лета


def test_ndvi_path_never_filters_by_scene_cloudiness():
    """Годность кадра решает поле, а не квадрат 110 километров.

    scene.candidate_days отказалась от фильтра по облачности сцены
    сознательно и с комментарием: облако в сорока километрах от участка
    выбраковывает совершенно годный кадр. suv/satellite.py — путь, по
    которому NDVI попадает в РЕКОМЕНДАЦИЮ — при этом просил
    maxCloudCoverage: 60.

    Как это выглядело в проде 09.09.2026: Sentinel-2 двенадцать дней
    подряд «не давал годного кадра», а Landsat в тот же день брал кадр
    от 06.09 со 100% чистых пикселей внутри контура. Небо было ясным;
    отбраковывал фильтр.
    """
    for rel in ("suv/satellite.py", "suv/scene.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        offending = [ln for ln in src.splitlines()
                     if "maxCloudCoverage" in ln and not ln.strip().startswith("#")]
        assert not offending, (
            f"{rel}: фильтр по облачности сцены вернулся — {offending}")
