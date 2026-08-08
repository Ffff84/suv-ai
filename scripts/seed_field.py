#!/usr/bin/env python3
"""
Загрузить настоящее поле в базу бота.

    python scripts/seed_field.py fields/olma.json --chat 123456789

Зачем это нужно. Регистрация в боте задаёт фермеру четыре вопроса и
подставляет остальное по умолчанию: суглинок, высота 500 м, грунтовых
вод нет, насоса нет. Для быстрого знакомства это нормально, но для поля
Фарруха неверно почти всё — супесь вместо суглинка, скважина 40 м,
насос с известным расходом. Бот молча считал бы по чужим параметрам.

Поэтому на пилоте поле заводится из конфига, а не через диалог: те же
цифры, что в run_field.py, попадают в базу бота один в один.

Chat ID узнать просто: напишите боту /start, он появится в логах.
Или напишите @userinfobot в Telegram — он пришлёт ваш ID.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from suv.config import load_env

load_env()

from suv.crop import CROPS
from suv.ledger import Ledger
from suv.soil import SOILS


def main() -> int:
    ap = argparse.ArgumentParser(description="Завести поле в базе бота")
    ap.add_argument("config", help="например fields/olma.json")
    ap.add_argument("--chat", type=int, required=True,
                    help="Telegram chat_id владельца поля")
    ap.add_argument("--db", default=os.environ.get("SUV_DB", "suv.db"))
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))

    if cfg["crop"] not in CROPS:
        raise SystemExit(f"Культура '{cfg['crop']}' неизвестна")
    if cfg["soil"] not in SOILS:
        raise SystemExit(f"Почва '{cfg['soil']}' неизвестна")
    date.fromisoformat(cfg["planting_date"])  # проверка формата

    pump = cfg.get("pump") or {}
    ledger = Ledger(args.db)
    ledger.upsert_field(
        field_id=cfg["field_id"],
        name=cfg["name"],
        owner_chat_id=args.chat,
        hectares=float(cfg["hectares"]),
        lat=float(cfg["lat"]),
        lon=float(cfg["lon"]),
        elevation_m=float(cfg["elevation_m"]),
        crop_key=cfg["crop"],
        soil_key=cfg["soil"],
        planting_date=cfg["planting_date"],
        irrigation_method=cfg["irrigation_method"],
        water_table_depth_m=float(cfg.get("water_table_depth_m") or 0.0),
        baseline_m3_per_ha=cfg.get("baseline_m3_per_ha"),
        baseline_interval_days=cfg.get("baseline_interval_days") or 30,
        pump_kwh_per_hour=pump.get("kwh_per_hour"),
        pump_m3_per_hour=pump.get("m3_per_hour"),
        pump_cost_per_hour_uzs=pump.get("cost_per_hour_uzs"),
        pump_lift_m=pump.get("lift_m"),
        # Точка отсчёта водного баланса: без неё бот считает поле
        # «только что политым» и откладывает первый совет.
        last_irrigation_date=cfg.get("last_irrigation_date"),
    )

    src = "насос" if pump else "самотёк"
    print(f"Поле записано в {args.db}")
    print(f"  {cfg['field_id']} · {cfg['name']} · {cfg['hectares']} га · "
          f"{CROPS[cfg['crop']].name_ru}")
    print(f"  {SOILS[cfg['soil']].name_ru} · {cfg['irrigation_method']} · {src}")
    print(f"  грунтовые воды {cfg.get('water_table_depth_m') or 0} м")
    print(f"  владелец: chat_id {args.chat}")
    if cfg.get("last_irrigation_date"):
        print(f"  последний полив: {cfg['last_irrigation_date']}")
    else:
        print("  ! last_irrigation_date не задан — первый расчёт будет")
        print("    считать поле только что политым.")
    if not cfg.get("baseline_m3_per_ha"):
        print()
        print("  ! baseline_m3_per_ha не задан — экономию считать не с чем.")
    print()
    print("Теперь отправьте боту /suv — он ответит по ЭТОМУ полю.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
