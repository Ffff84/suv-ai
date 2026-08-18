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

Что именно из конфига едет в базу — решает suv/field_config.to_row, и
тот же мэппинг читает scripts/check_fields.py, когда сверяет базу с
конфигом. Один список на запись и на проверку: иначе колонка,
добавленная только сюда, для чекера навсегда осталась бы невидимой.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from suv.config import load_env

load_env()

from suv.crop import CROPS
from suv.field_config import declared_inlet, resolve_inlet, to_row
from suv.ledger import Ledger
from suv.soil import SOILS


def _restore_inlet(ledger: Ledger, cfg: dict) -> str:
    """Вернуть на место сторону входа воды, если она задана в конфиге.

    Индексы вершин в базе слетают при каждом перечерчивании контура — так
    задумано, на новой границе они указывают куда попало. Но само знание
    «вода заходит с северо-востока» от этого не устаревает, поэтому оно
    лежит румбом в конфиге, а ребро под него ищется по нынешнему контуру.
    """
    want = declared_inlet(cfg)
    if not want:
        return ""
    import json
    import sqlite3
    with sqlite3.connect(ledger.path) as c:
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT polygon_geojson FROM fields WHERE field_id=?",
                      (cfg["field_id"],)).fetchone()
    ring = json.loads(r["polygon_geojson"]) if r and r["polygon_geojson"] else None
    if not ring:
        return (f"  ! вход воды «{want}» записать не на что: контур не "
                f"обведён.\n    Обведите поле в боте и пересейте — встанет.")
    pair = resolve_inlet(ring, want)
    if pair is None:
        return (f"  ! стороны «{want}» на контуре нет — вход НЕ записан.\n"
                f"    Проверьте румб в конфиге или обводку поля.")
    ledger.save_inlet(cfg["field_id"], *pair)
    return f"  вход воды: с {want}а (вершины {pair[0]},{pair[1]})"


def _known_owner(ledger: Ledger, field_id: str) -> int | None:
    """Владелец уже заведённого поля."""
    import sqlite3
    with sqlite3.connect(ledger.path) as c:
        r = c.execute("SELECT owner_chat_id FROM fields WHERE field_id=?",
                      (field_id,)).fetchone()
    return r[0] if r and r[0] else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Завести поле в базе бота")
    ap.add_argument("config", help="например fields/olma.json")
    # Не required: пересев — рутинный шаг деплоя, а chat_id владельца в
    # базе уже лежит. Требовать его каждый раз значит гонять человека за
    # числом, которое система знает сама, — и он либо вставит команду с
    # заглушкой (проверено на живом сервере 17.08.2026), либо не
    # пересеет вовсе, и конфиг с базой снова разъедутся.
    ap.add_argument("--chat", type=int,
                    help="Telegram chat_id владельца; для уже заведённого "
                         "поля не нужен — берётся из базы")
    ap.add_argument("--db", default=os.environ.get("SUV_DB", "suv.db"))
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))

    try:
        row = to_row(cfg)          # он же проверит культуру, почву и дату
    except ValueError as exc:
        raise SystemExit(f"{args.config}: {exc}")

    ledger = Ledger(args.db)
    # owner_chat_id единственный приходит не из конфига: chat_id фермера
    # в git не место.
    chat = args.chat or _known_owner(ledger, cfg["field_id"])
    if chat is None:
        raise SystemExit(
            f"Поле {cfg['field_id']} в базе {args.db} не заведено, и владелец "
            f"неизвестен.\nУкажите его числом: --chat 123456789 (chat_id "
            f"фермера видно в логах бота после его /start,\nлибо @userinfobot "
            f"в Telegram пришлёт свой).")
    ledger.upsert_field(owner_chat_id=chat, **row)
    inlet_note = _restore_inlet(ledger, cfg)

    src = "насос" if cfg.get("pump") else "самотёк"
    print(f"Поле записано в {args.db}")
    print(f"  {cfg['field_id']} · {cfg['name']} · {cfg['hectares']} га · "
          f"{CROPS[cfg['crop']].name_ru}")
    print(f"  {SOILS[cfg['soil']].name_ru} · {cfg['irrigation_method']} · {src}")
    print(f"  грунтовые воды {cfg.get('water_table_depth_m') or 0} м")
    print(f"  владелец: chat_id {chat}"
          f"{'' if args.chat else ' (из базы)'}")
    if inlet_note:
        print(inlet_note)
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
