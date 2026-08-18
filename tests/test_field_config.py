"""
Тесты сверки конфигов полей с базой бота.

Проверка появилась после конкретной потери. 16.08.2026 площадь
виноградника FAR-UZUM в fields/uzum.json исправили с 3 га на 1,5, но в
базе на сервере осталось 3 — деплой везёт код, а не анкету поля. Объём
считается как мм × 10 × га, и бот советовал вдвое больше воды, не
жалуясь ни строчкой в логах.

Поэтому здесь два рода тестов: что чекер ловит именно такое расхождение
и что он НЕ считает расхождением нормальную работу бота — обведённый
контур, кэш фото. Ложная тревога в деплое хуже молчания: на неё
перестают смотреть.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from suv.field_config import MISSING, diff_row, to_row, validate
from suv.ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent
CONFIGS = sorted((ROOT / "fields").glob("*.json"))
CHECKER = ROOT / "scripts" / "check_fields.py"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _seed(tmp_path, cfg: dict, name: str = "t.db") -> Ledger:
    """Завести поле ровно так, как это делает scripts/seed_field.py."""
    led = Ledger(tmp_path / name)
    led.upsert_field(owner_chat_id=111, **to_row(cfg))
    return led


def _row(led: Ledger, field_id: str):
    with sqlite3.connect(led.path) as c:
        c.row_factory = sqlite3.Row
        return c.execute("SELECT * FROM fields WHERE field_id=?",
                         (field_id,)).fetchone()


# ------------------------------------------------- конфиги самого проекта

@pytest.mark.parametrize("path", CONFIGS, ids=lambda p: p.name)
def test_seeded_field_matches_its_config(tmp_path, path):
    """Только что засеянное поле обязано сходиться с конфигом.

    Главный тест этой пары файлов. Он ловит рассинхрон мэппинга: колонку
    добавили в to_row, но база её не принимает; тип поехал и число
    вернулось из SQLite другим. Если этот тест зелёный, то расхождение,
    найденное на сервере, — всегда настоящее, а не выдумка чекера.
    """
    cfg = _load(path)
    led = _seed(tmp_path, cfg)
    assert diff_row(cfg, _row(led, cfg["field_id"])) == []


@pytest.mark.parametrize("path", CONFIGS, ids=lambda p: p.name)
def test_repo_configs_are_valid(path):
    """Культура, почва и даты в конфигах проекта — известные движку."""
    validate(_load(path))


# ------------------------------------------------------- ловит расхождение

def test_stale_hectares_is_caught(tmp_path):
    """Тот самый случай: в базе 3 га, в конфиге уже 1,5."""
    cfg = _load(ROOT / "fields" / "uzum.json")
    stale = dict(cfg, hectares=3.0)
    led = _seed(tmp_path, stale)              # база отстала от git

    diffs = diff_row(cfg, _row(led, cfg["field_id"]))
    assert [d.column for d in diffs] == ["hectares"]
    assert diffs[0].config == 1.5 and diffs[0].db == 3.0
    assert "1.5" in str(diffs[0]) and "3" in str(diffs[0])


def _something_else(value):
    """Заведомо другое значение той же колонки."""
    if value is None:
        return 999.0
    if isinstance(value, (int, float)):
        return value + 1
    return str(value) + "!"


@pytest.mark.parametrize("column", sorted(set(to_row(_load(CONFIGS[0]))) -
                                          {"field_id"}))
def test_every_column_is_compared(column):
    """Каждая колонка проверяется отдельно.

    Молчание про ОДНУ колонку и есть тот способ потерять цифру, ради
    которого всё писалось: площадь разошлась, а чекер зелёный. Портим по
    одной и требуем, чтобы назвали ровно её.
    """
    cfg = _load(CONFIGS[0])
    row = to_row(cfg)
    row[column] = _something_else(row[column])

    assert [d.column for d in diff_row(cfg, row)] == [column]


def test_pump_replaced_by_gravity_is_caught(tmp_path):
    """Насос убрали из конфига — в базе он остался и считает деньги."""
    cfg = _load(ROOT / "fields" / "olma.json")
    led = _seed(tmp_path, cfg)

    gravity = dict(cfg, pump=None)
    changed = {d.column for d in diff_row(gravity, _row(led, cfg["field_id"]))}
    assert changed == {"pump_kwh_per_hour", "pump_m3_per_hour",
                       "pump_cost_per_hour_uzs", "pump_lift_m"}


def test_field_missing_from_db_is_a_divergence():
    """Поля в базе нет вовсе — конфиг ни разу не заводили."""
    cfg = _load(ROOT / "fields" / "olma.json")
    assert diff_row(cfg, None), "отсутствие поля в базе прошло молча"


def test_unknown_crop_is_rejected():
    """Битый конфиг должен падать до записи, а не после."""
    cfg = dict(_load(ROOT / "fields" / "olma.json"), crop="banana")
    with pytest.raises(ValueError):
        to_row(cfg)


# --------------------------------------------- не считает расхождением своё

def _vineyard_ring() -> list:
    """Контур виноградника: вытянут с юга на север, северо-восточный
    угол срезан межой ~33 м — теми самыми воротами с канала."""
    from suv.field_shape import meters_per_degree, to_geojson_ring
    lat0, lon0 = 39.558516, 66.996956
    m_lat, m_lon = meters_per_degree(lat0)
    def P(x, y): return (lat0 + y / m_lat, lon0 + x / m_lon)
    return to_geojson_ring([P(0, 0), P(60, 0), P(60, 200),
                            P(37, 224), P(0, 224)])


def test_drawn_contour_is_not_a_divergence(tmp_path):
    """Фермер обвёл поле, обмер дал 1,41 га против заявленных 1,5.

    Это нормальная работа, а не рассинхрон: hectares — со слов фермера,
    area_ha — по контуру, и save_polygon первую не трогает намеренно.
    Начни чекер ругаться здесь — его выключат в первый же день.
    """
    from suv.field_config import declared_inlet, resolve_inlet
    cfg = _load(ROOT / "fields" / "uzum.json")
    led = _seed(tmp_path, cfg)
    ring = _vineyard_ring()
    led.save_polygon(cfg["field_id"], ring, 1.41, "miniapp")
    # Как это делает seed_field: вход воды находится по румбу из конфига.
    led.save_inlet(cfg["field_id"],
                   *resolve_inlet(ring, declared_inlet(cfg)))
    led.save_photo(cfg["field_id"], "AgAC-file-id", "2026-08-14", "key")

    assert diff_row(cfg, _row(led, cfg["field_id"])) == []


def test_declared_and_measured_area_are_compared(tmp_path):
    """Боевые данные 17.08.2026: у сада заявлено 2,0 га, а обмер контура
    дал 2,52 — четверть воды мимо, и заметили случайно. Обе цифры целы
    намеренно, но спорить молча они не должны.

    Виноградник (1,46 против 1,5) обязан молчать: палец на карте точнее
    и не бывает, а ложная тревога отучает смотреть.
    """
    from suv.field_config import area_mismatch
    apple = _load(ROOT / "fields" / "olma.json")
    assert area_mismatch(apple, {"area_ha": 2.52})
    assert "2.52" in area_mismatch(apple, {"area_ha": 2.52})
    assert area_mismatch(apple, {"area_ha": 2.1}) is None

    vine = _load(ROOT / "fields" / "uzum.json")
    assert area_mismatch(vine, {"area_ha": 1.46}) is None
    assert area_mismatch(vine, {"area_ha": 3.0})     # старая ошибка с 3 га

    # Нет контура или нет строки — сравнивать нечего, не выдумываем.
    assert area_mismatch(vine, {"area_ha": None}) is None
    assert area_mismatch(vine, None) is None


def test_area_mismatch_is_not_a_divergence(tmp_path):
    """Спор площадей пересевом не лечится, поэтому в расхождения он не
    попадает и кода возврата не меняет — иначе деплой краснел бы вечно."""
    cfg = _load(ROOT / "fields" / "olma.json")
    led = _seed(tmp_path, cfg)
    led.save_polygon(cfg["field_id"], _vineyard_ring(), 2.52, "miniapp")
    assert diff_row(cfg, _row(led, cfg["field_id"])) == []


def test_redrawn_contour_loses_the_inlet_and_checker_says_so(tmp_path):
    """Перечерченная граница обнуляет вход воды — так задумано (индексы
    старых вершин на новой границе врут). Но знание «вода заходит с
    северо-востока» от этого не устарело: чекер обязан показать пропажу,
    иначе она молча доживёт до сезона полива."""
    cfg = _load(ROOT / "fields" / "uzum.json")
    led = _seed(tmp_path, cfg)
    led.save_polygon(cfg["field_id"], _vineyard_ring(), 1.41, "miniapp")

    diffs = diff_row(cfg, _row(led, cfg["field_id"]))
    assert [d.column for d in diffs] == ["вход воды"]
    assert diffs[0].config == "северо-восток" and diffs[0].db is None


def test_int_in_config_matches_real_in_db(tmp_path):
    """700 в конфиге и 700.0 в базе — одна и та же высота."""
    cfg = _load(ROOT / "fields" / "uzum.json")
    led = _seed(tmp_path, cfg)
    assert isinstance(cfg["elevation_m"], int)
    assert _row(led, cfg["field_id"])["elevation_m"] == 700.0
    assert diff_row(cfg, _row(led, cfg["field_id"])) == []


def test_absent_column_with_empty_config_value_is_quiet():
    """База старше кода: колонки нет, но и писать в неё нечего.

    Миграция в Ledger.__init__ добавит колонку сама, на рекомендацию это
    не влияет — поднимать тревогу не о чем.
    """
    cfg = _load(ROOT / "fields" / "uzum.json")
    assert cfg["last_irrigation_date"] is None
    row = dict(to_row(cfg))
    del row["last_irrigation_date"]
    assert diff_row(cfg, row) == []

    # А вот когда значение в конфиге есть — молчать нельзя.
    filled = dict(cfg, last_irrigation_date="2026-08-06")
    diffs = diff_row(filled, row)
    assert [d.column for d in diffs] == ["last_irrigation_date"]
    assert diffs[0].db is MISSING


# ------------------------------------------------------------ сам скрипт

def _run(args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(CHECKER), *args],
                          cwd=ROOT, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def test_script_exits_zero_when_everything_matches(tmp_path):
    """Ноль — и деплой молчит."""
    db = tmp_path / "ok.db"
    led = Ledger(db)
    for path in CONFIGS:
        led.upsert_field(owner_chat_id=111, **to_row(_load(path)))

    r = _run(["--db", str(db)])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Сходится" in r.stdout


def test_script_exits_one_and_names_the_column(tmp_path):
    """Ненулевой код и колонка в выводе — иначе шаг деплоя бесполезен."""
    db = tmp_path / "stale.db"
    led = Ledger(db)
    cfg = _load(ROOT / "fields" / "uzum.json")
    led.upsert_field(owner_chat_id=111, **to_row(dict(cfg, hectares=3.0)))

    r = _run(["fields/uzum.json", "--db", str(db)])
    assert r.returncode == 1, r.stdout + r.stderr
    assert "hectares" in r.stdout
    assert "FAR-UZUM" in r.stdout


def test_script_does_not_create_the_database(tmp_path):
    """Проверка не имеет права создавать базу.

    Ledger(path) создал бы пустой файл, и «расхождений нет» означало бы
    лишь то, что мы сами его только что сделали.
    """
    db = tmp_path / "нет-такой.db"
    r = _run(["--db", str(db)])
    assert r.returncode == 2, r.stdout + r.stderr
    assert not db.exists()


def test_script_leaves_the_database_untouched(tmp_path):
    """Пересев — решение человека: чекер не чинит ничего сам."""
    db = tmp_path / "stale.db"
    led = Ledger(db)
    cfg = _load(ROOT / "fields" / "uzum.json")
    led.upsert_field(owner_chat_id=111, **to_row(dict(cfg, hectares=3.0)))
    before = db.read_bytes()

    assert _run(["fields/uzum.json", "--db", str(db)]).returncode == 1
    assert _row(led, cfg["field_id"])["hectares"] == 3.0
    assert db.read_bytes() == before, "чекер поменял базу"
