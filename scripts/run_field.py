#!/usr/bin/env python3
"""
Расчёт по конкретному полю.

    python scripts/run_field.py field.json

Берёт живую погоду, если интернет есть. Если нет — считает по климатическим
нормам и ГОВОРИТ ОБ ЭТОМ ПРЯМО. Молча подменять источник данных нельзя:
рекомендация по норме и рекомендация по факту выглядят одинаково, а стоят
по-разному.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from suv.config import load_env

load_env()

from suv import __version__
from suv.climate import STATIONS, season
from suv.crop import CROPS
from suv.messages import recommendation_text, salinity_warning
from suv.schedule import Field, recommend
from suv.soil import SOILS, WaterBalanceState


def load_field(path: str) -> tuple[Field, dict]:
    cfg = json.load(open(path, encoding="utf-8"))
    for key in ("field_id", "name", "hectares", "lat", "lon", "elevation_m",
                "crop", "soil", "planting_date", "irrigation_method"):
        if key not in cfg:
            raise SystemExit(f"В {path} не хватает поля: {key}")
    if cfg["crop"] not in CROPS:
        raise SystemExit(f"Культура '{cfg['crop']}' неизвестна. "
                         f"Доступны: {', '.join(CROPS)}")
    if cfg["soil"] not in SOILS:
        raise SystemExit(f"Почва '{cfg['soil']}' неизвестна. "
                         f"Доступны: {', '.join(SOILS)}")

    wt = cfg.get("water_table_depth_m")
    if wt is None:
        print("! Глубина грунтовых вод не указана — расчёт будет ЗАВЫШЕН.")
        print("  Для Ферганы это ошибка почти вдвое. Спросите у фермера или")
        print("  у местного водхоза и впишите water_table_depth_m.\n")
        wt = 0.0

    f = Field(
        field_id=cfg["field_id"], name=cfg["name"], hectares=float(cfg["hectares"]),
        lat=float(cfg["lat"]), lon=float(cfg["lon"]),
        elevation_m=float(cfg["elevation_m"]),
        crop=CROPS[cfg["crop"]], soil=SOILS[cfg["soil"]],
        planting_date=date.fromisoformat(cfg["planting_date"]),
        irrigation_method=cfg["irrigation_method"],
        water_table_depth_m=float(wt),
    )
    return f, cfg


_WARNED = False


def get_weather(f: Field, cfg: dict, days: int, past_days: int = 0):
    """Возвращает (ряд, подпись, сколько дней в ряду — прошлое)."""
    try:
        from suv.weather import fetch_forecast
        wx = fetch_forecast(f.lat, f.lon, days=min(days, 16), past_days=past_days)
        return wx, "живой прогноз Open-Meteo", past_days
    except Exception as exc:  # noqa: BLE001
        st = STATIONS.get(cfg.get("station", "samarkand"), STATIONS["samarkand"])
        global _WARNED
        if not _WARNED:
            _WARNED = True
            print(f"! Живая погода недоступна ({type(exc).__name__}).")
            print(f"  Считаю по климатическим нормам станции «{st.name_ru}».")
            print("  Для демонстрации это допустимо. Для заявления об экономии — нет.\n")
        start = date.today() - timedelta(days=past_days)
        return season(st, start, days + past_days), f"нормы: {st.name_ru}", past_days


def warm_start(f: Field, cfg: dict) -> tuple[WaterBalanceState, str]:
    """
    Определить сегодняшний дефицит влаги.

    В середине сезона нельзя считать, что поле полное — это самая частая
    ошибка при подключении нового хозяйства, и она даёт рекомендацию
    "поливать не надо" там, где поливать пора.

    Если фермер назвал дату последнего полива, отматываем баланс от неё:
    в тот день поле было полным, дальше считаем испарение и дожди.
    """
    from suv.schedule import simulate

    raw_date = cfg.get("last_irrigation_date")
    if not raw_date:
        d = float(cfg.get("current_depletion_mm", 0.0))
        if d == 0:
            print("! Дата последнего полива не указана, дефицит принят за 0.")
            print("  Это значит «поле только что полито». Если это не так,")
            print("  впишите last_irrigation_date — расчёт станет верным.\n")
        return WaterBalanceState(d, 0.20), "задан вручную"

    last = date.fromisoformat(raw_date)
    gap = (date.today() - last).days
    if gap <= 0:
        return WaterBalanceState(0.0, 0.20), "полив сегодня"

    gap = min(gap, 92)  # предел архива Open-Meteo
    hist, src, _ = get_weather(f, cfg, days=1, past_days=gap)
    hist = hist[:gap]
    if not hist:
        return WaterBalanceState(0.0, 0.20), "архив погоды пуст"
    # Стартуем с первого дня ИСТОРИИ, а не с last: при разрыве дольше
    # 92 дней история покрывает последние 92 дня, и старые метки
    # сдвигали фазы культуры относительно погоды реальных дат.
    plan = simulate(f, hist, WaterBalanceState(0.0, 0.20),
                    date.today() - timedelta(days=len(hist)),
                    apply_irrigation=False)
    st = WaterBalanceState(plan[-1].depletion_mm, plan[-1].taw_mm and
                           plan[-1].taw_mm / f.soil.available_water_mm_per_m)
    return st, f"отмотано {len(hist)} дн. от полива {last} ({src})"


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "field.json"
    if not os.path.exists(path):
        raise SystemExit(f"Файл {path} не найден. Возьмите за основу "
                         f"scripts/field.example.json")

    f, cfg = load_field(path)
    days = int(cfg.get("forecast_days", 14))
    from suv.economics import PumpProfile, describe as pump_describe
    pump = None
    if cfg.get("pump"):
        pd = cfg["pump"]
        pump = PumpProfile(kwh_per_hour=float(pd["kwh_per_hour"]),
                           m3_per_hour=float(pd["m3_per_hour"]),
                           cost_per_hour_uzs=float(pd["cost_per_hour_uzs"]),
                           lift_m=float(pd.get("lift_m", 0)))

    from suv.enrich import attach_ndvi
    sat = attach_ndvi(f, field_size_m=float(cfg.get("field_size_m", 200)),
                      polygon=cfg.get("polygon"))

    state, how = warm_start(f, cfg)
    wx, source, _ = get_weather(f, cfg, days)

    rec = recommend(
        f, wx, state, date.today(),
        baseline_interval_days=int(cfg.get("baseline_interval_days", 30)),
        # None означает "фермер ещё не сказал" — это не то же самое, что 0.
        baseline_application_m3_per_ha=float(cfg.get("baseline_m3_per_ha") or 0.0),
    )

    from suv.crop import season_start
    origin = season_start(f.crop, f.planting_date, date.today())
    dap = (date.today() - origin).days
    cycle = ("вегетация с " + origin.isoformat()) if f.crop.perennial else "после сева"
    print("=" * 62)
    print(f"  {f.name} · {f.hectares} га · {f.crop.name_ru}")
    print(f"  {f.soil.name_ru} · {f.irrigation_method} · "
          f"грунтовые воды {f.water_table_depth_m:.1f} м")
    print(f"  {dap} дн. — {cycle} · источник погоды: {source}")
    print(f"  дефицит на сегодня: {state.depletion_mm:.0f} мм ({how})")
    print(f"  {sat}")
    if rec.plan:
        print(f"  источник Kc: {rec.plan[0].kc_source}")
    print(f"  вода: {pump_describe(pump)}")
    print(f"  движок v{__version__}")
    print("=" * 62)
    print()
    print("СООБЩЕНИЕ ФЕРМЕРУ (узбекский):")
    print("-" * 62)
    print(recommendation_text(rec, "uz", pump=pump))
    w = salinity_warning(rec.plan[0].salinity if rec.plan else "unknown", "uz")
    if w:
        print("\n" + w)
    print("-" * 62)
    print()
    print("ТО ЖЕ ПО-РУССКИ:")
    print(recommendation_text(rec, "ru", pump=pump))
    print()

    print(f"{'дата':<12}{'ET0':>6}{'Kc':>6}{'ETc':>7}{'дождь':>7}"
          f"{'дефицит':>9}{'порог':>7}  полив")
    for p in rec.plan:
        mark = f"  <-- {p.gross_mm:.0f} мм / {p.gross_m3:.0f} м³" if p.irrigate else ""
        print(f"{p.day.isoformat():<12}{p.et0_mm:>6.1f}{p.kc:>6.2f}{p.etc_mm:>7.1f}"
              f"{p.rain_mm:>7.1f}{p.depletion_mm:>9.0f}{p.raw_mm:>7.0f}{mark}")

    total = sum(p.gross_m3 for p in rec.plan)
    print()
    print(f"За {len(rec.plan)} дней по расчёту: {total:.0f} м³ "
          f"({total / f.hectares:.0f} м³/га)")
    if cfg.get("baseline_m3_per_ha"):
        print(f"По старому графику за тот же срок: {rec.baseline_m3:.0f} м³")
        if rec.baseline_m3 > 0:
            diff = rec.baseline_m3 - total
            sign = "экономия" if diff > 0 else "перерасход старого графика не подтверждён"
            print(f"Разница: {diff:+.0f} м³  ({sign})")
        print()
        print("Помните: сравнение честно только если baseline_m3_per_ha —")
        print("это реальный прошлогодний расход ЭТОГО фермера.")
    else:
        print("baseline_m3_per_ha не задан — экономию считать не с чем.")
        print("Спросите у фермера: сколько поливов за сезон и по сколько кубов.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
