#!/usr/bin/env python3
"""
Сверить конфиги полей с базой бота.

    python3 scripts/check_fields.py                  # все fields/*.json
    python3 scripts/check_fields.py fields/uzum.json  # только это поле

Зачем. Анкета поля лежит в двух местах: конфиг в git и строка в suv.db.
Деплой везёт только код, поэтому исправленная в конфиге площадь в базу
не попадает, пока руками не запустят seed_field.py. Молчаливое
расхождение уже стоило дорого: 16.08.2026 виноградник FAR-UZUM в
конфиге стал 1,5 га вместо 3, а бот продолжал считать по трём — вдвое
больше воды, чем нужно, и никакой ошибки в логах.

Печатает ТОЛЬКО расхождения. Ничего не чинит: пересев базы — решение
человека. Автоматический пересев затирал бы правки, сделанные с той
стороны (площадь, уточнённую фермером на месте), и сам стал бы новым
способом потерять данные.

Код возврата:
    0 — всё сходится
    1 — есть расхождение или битый конфиг
    2 — базу не прочитать (нет файла, нет таблицы)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from suv.config import load_env

load_env()

from suv.field_config import area_mismatch, diff_row


def read_fields(db: Path) -> dict[str, sqlite3.Row]:
    """Строки таблицы fields ТОЛЬКО НА ЧТЕНИЕ.

    mode=ro не для безопасности, а по существу: проверка не имеет права
    менять базу. Обычный Ledger(path) создал бы пустую базу там, где её
    нет, и прогнал миграции — после чего «расхождений не найдено»
    означало бы всего лишь, что мы сами только что сделали пустой файл.
    """
    uri = db.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT * FROM fields").fetchall()
    return {r["field_id"]: r for r in rows}


def short(path: Path) -> str:
    """fields/uzum.json вместо простыни абсолютного пути."""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def check(configs: list[Path], db: Path) -> int:
    print()
    print(f"  SUV AI — сверка полей с базой {db}")

    if not db.exists():
        print(f"  Базы {db} нет.")
        print("  Либо запущено не из папки проекта, либо поля ещё ни разу")
        print("  не заводили: python3 scripts/seed_field.py fields/olma.json"
              " --chat ID")
        print()
        return 2
    try:
        rows = read_fields(db)
    except sqlite3.DatabaseError as exc:   # нет таблицы, файл не база
        print(f"  Базу {db} не прочитать: {exc}")
        print("  Если это «no such table: fields» — база пустая, поля не")
        print("  заводили. Заведите их scripts/seed_field.py.")
        print()
        return 2

    diverged = 0
    broken = 0
    report: list[str] = []
    notes: list[str] = []
    for path in configs:
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            fid = cfg["field_id"]
            diffs = diff_row(cfg, rows.get(fid))
            row = rows.get(fid)
            spor = area_mismatch(cfg, dict(row) if row else None)
            if spor:
                notes.append(f"  {fid} · {spor}")
        # Конфиг пишут руками, и сломать его можно по-разному: пропал
        # ключ, вместо числа строка, вместо блока pump — строка. Любая
        # такая беда это одна новость «поле не завести», а не трассировка
        # на пол-лога деплоя.
        except (ValueError, KeyError, OSError, AttributeError,
                TypeError) as exc:
            broken += 1
            report.append(f"  ! {short(path)}: конфиг не читается — {exc}")
            continue

        if not diffs:
            continue
        diverged += 1
        report.append(f"  {fid} · {short(path)}")
        if fid not in rows:
            report.append("      поля нет в базе — его ни разу не заводили")
            continue
        report += [f"      {d}" for d in diffs]

    def print_area_notes() -> None:
        """Спор заявленной и обмеренной площади — отдельно от расхождений.

        Код возврата он не меняет: пересевом это не лечится, и валить им
        деплой значит приучить смотреть мимо. Но и молчать нельзя —
        объём полива считается по заявленной цифре.
        """
        if not notes:
            return
        print("  " + "-" * 58)
        print("  Площади спорят (пересев не поможет, решает человек):")
        print("\n".join(notes))

    if not diverged and not broken:
        print(f"  Сходится: полей проверено {len(configs)}.")
        print_area_notes()
        print()
        return 0

    print("  " + "-" * 58)
    print("\n".join(report))
    print_area_notes()
    print("  " + "-" * 58)
    if diverged:
        print(f"  Расходится полей: {diverged} из {len(configs)}.")
        print()
        print("  Само это не починится. Решать человеку:")
        print("  • прав конфиг (правку внесли в git) — пересейте базу руками:")
        print("        python3 scripts/seed_field.py ФАЙЛ.json --chat CHAT_ID")
        print("  • права база (площадь уточнил фермер на месте) — поправьте")
        print("    конфиг и закоммитьте, иначе следующий пересев вернёт старое.")
    if broken:
        print(f"  Конфигов не прочитано: {broken}. Такое поле не завести:")
        print("  seed_field.py упрётся в ту же ошибку. Поправьте конфиг.")
    print()
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Сверить fields/*.json со строками в базе бота")
    ap.add_argument("configs", nargs="*",
                    help="по умолчанию — все fields/*.json")
    ap.add_argument("--db", default=os.environ.get("SUV_DB", "suv.db"))
    args = ap.parse_args()

    configs = ([Path(c) for c in args.configs]
               or sorted((ROOT / "fields").glob("*.json")))
    if not configs:
        print("Не нашёл ни одного конфига поля в fields/*.json")
        return 2
    return check(configs, Path(args.db))


if __name__ == "__main__":
    raise SystemExit(main())
