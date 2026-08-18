"""
Анкета поля: конфиг fields/*.json → строка таблицы fields.

Анкета живёт в двух местах — конфиг в git и строка в базе бота. Деплой
обновляет только код, поэтому правка конфига в базу сама не попадает,
пока руками не запустят scripts/seed_field.py. Расхождение получается
тихим, а стоит дорого: 16.08.2026 площадь виноградника FAR-UZUM в
конфиге исправили с 3 га на 1,5, в базе осталось 3 — а объём считается
как мм × 10 × га (suv/schedule.py), и бот советовал бы вдвое больше
воды, чем нужно. Обводка контура это не лечит намеренно: save_polygon
пишет area_ha и hectares не трогает (suv/ledger.py).

Поэтому мэппинг «конфиг → колонки базы» лежит здесь ОДИН, и работают по
нему оба: seed_field.py по нему пишет, check_fields.py по нему же
сравнивает. Знай чекер свой отдельный список колонок — он бы молчал
ровно о той колонке, которую забыли добавить в оба места.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from suv.crop import CROPS
from suv.soil import SOILS

# Без этих ключей поле не завести: бот не умеет догадываться, где оно
# и что на нём растёт.
REQUIRED = ("field_id", "name", "hectares", "lat", "lon", "elevation_m",
            "crop", "soil", "planting_date", "irrigation_method")

# Колонки, которых здесь сознательно НЕТ, и почему:
#
#   owner_chat_id  — приходит ключом --chat, а не из конфига: chat_id
#                    фермера не место в git.
#   polygon_geojson, area_ha, polygon_source, inlet_vertices
#                  — контур обвёл фермер. В конфиге он null у обоих
#                    полей, и сравнивать его с обмером значило бы
#                    объявлять расхождением нормальную работу.
#   photo_*        — кэш картинки, пересобирается сам.
#
# Из конфига в базу не едут и station / forecast_days / field_size_m:
# таких колонок в таблице просто нет, они нужны расчётным скриптам.


@dataclass(frozen=True)
class Diff:
    """Одно расхождение: колонка, что в конфиге, что в базе."""

    column: str
    config: object
    db: object

    def __str__(self) -> str:
        return (f"{self.column}: конфиг {show(self.config)} · "
                f"база {show(self.db)}")


class MissingColumn:
    """Колонки нет в таблице — база старше кода.

    Отдельный тип, а не None: «в базе NULL» и «в базе негде хранить» —
    разные диагнозы, и лечатся они по-разному.
    """

    def __repr__(self) -> str:
        return "нет такой колонки"


MISSING = MissingColumn()


def show(value: object) -> str:
    """Значение так, как его читает человек в логе деплоя."""
    if value is None:
        return "пусто"
    if isinstance(value, MissingColumn):
        return repr(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def validate(cfg: Mapping) -> None:
    """Ругнуться на конфиг, который база не примет. Молча — ничего."""
    missing = [k for k in REQUIRED if cfg.get(k) is None]
    if missing:
        raise ValueError("не хватает полей: " + ", ".join(missing))
    if cfg["crop"] not in CROPS:
        raise ValueError(f"культура '{cfg['crop']}' неизвестна; "
                         f"доступны: {', '.join(CROPS)}")
    if cfg["soil"] not in SOILS:
        raise ValueError(f"почва '{cfg['soil']}' неизвестна; "
                         f"доступны: {', '.join(SOILS)}")
    date.fromisoformat(cfg["planting_date"])
    if cfg.get("last_irrigation_date"):
        date.fromisoformat(cfg["last_irrigation_date"])
    pump = cfg.get("pump")
    if pump is not None and not isinstance(pump, Mapping):
        raise ValueError("pump должен быть блоком {…} или null; "
                         f"сейчас там {type(pump).__name__}")
    declared_inlet(cfg)   # румб входа воды: либо известный, либо ошибка


def to_row(cfg: Mapping) -> dict:
    """Колонки таблицы fields ровно так, как их запишет seed_field.py.

    Приведение типов здесь не косметика: конфиг пишут руками, и 700 без
    точки должно давать те же 700.0, что уже лежат в базе, — иначе
    чекер ловил бы расхождение там, где его нет.
    """
    validate(cfg)
    pump = cfg.get("pump") or {}
    return {
        "field_id": str(cfg["field_id"]),
        "name": str(cfg["name"]),
        "hectares": float(cfg["hectares"]),
        "lat": float(cfg["lat"]),
        "lon": float(cfg["lon"]),
        "elevation_m": float(cfg["elevation_m"]),
        "crop_key": cfg["crop"],
        "soil_key": cfg["soil"],
        "planting_date": cfg["planting_date"],
        "irrigation_method": cfg["irrigation_method"],
        # Пустая глубина грунтовых вод = «их нет»: так поле заводили с
        # самого начала, и менять это значит менять расчёт.
        "water_table_depth_m": float(cfg.get("water_table_depth_m") or 0.0),
        "baseline_m3_per_ha": _opt_float(cfg.get("baseline_m3_per_ha")),
        "baseline_interval_days": int(cfg.get("baseline_interval_days") or 30),
        "pump_kwh_per_hour": _opt_float(pump.get("kwh_per_hour")),
        "pump_m3_per_hour": _opt_float(pump.get("m3_per_hour")),
        "pump_cost_per_hour_uzs": _opt_float(pump.get("cost_per_hour_uzs")),
        "pump_lift_m": _opt_float(pump.get("lift_m")),
        # Точка отсчёта водного баланса: без неё бот считает поле
        # «только что политым» и откладывает первый совет.
        "last_irrigation_date": cfg.get("last_irrigation_date"),
    }


def diff_row(cfg: Mapping, row: Mapping | None) -> list[Diff]:
    """Чем строка базы отличается от конфига. Пустой список — сходится.

    row — sqlite3.Row или словарь; None означает, что поля в базе нет
    вовсе, и тогда расхождением становится всё, что конфиг задаёт.
    """
    want = to_row(cfg)
    have = dict(row) if row is not None else {}
    out = []
    for col, expected in want.items():
        if col == "field_id":
            continue                       # ключ поиска, отличаться не может
        actual = have.get(col, MISSING)
        if not _same(expected, actual):
            out.append(Diff(col, expected, actual))
    out += _inlet_diff(cfg, have)
    return out


def _inlet_diff(cfg: Mapping, have: Mapping) -> list[Diff]:
    """Сторона входа воды: конфиг знает румб, база — индексы вершин.

    Сравниваем по НАЗВАНИЮ стороны на нынешнем контуре. Перечерченная
    граница обнуляет индексы (save_polygon), и это ровно тот случай,
    который чекер обязан показать: знание не устарело, а из базы
    пропало — нужен пересев.
    """
    want = declared_inlet(cfg)
    if not want:
        return []
    ring_raw = have.get("polygon_geojson")
    if not ring_raw:
        return []          # контура нет — записывать вход и не на что
    import json
    try:
        ring = json.loads(ring_raw)
        pts = __import__("suv.field_shape", fromlist=["x"]).from_geojson_ring(ring)
    except Exception:      # noqa: BLE001 — битый контур ловит своя проверка
        return []
    raw = have.get("inlet_vertices")
    actual = None
    if raw:
        try:
            i, j = (int(v) for v in json.loads(raw))
            from suv.field_shape import inlet_edge
            edge = inlet_edge(pts, (i, j))
            actual = edge.name("ru") if edge else None
        except Exception:  # noqa: BLE001
            actual = None
    return [] if actual == want else [Diff("вход воды", want, actual)]


# ------------------------------------------------- сторона входа воды
#
# В таблице она хранится индексами вершин контура — и намеренно слетает
# при каждом перечерчивании границы (save_polygon), потому что на новой
# границе старые индексы указывают куда попало. Цена: знание «вода
# заходит с северо-востока» живёт только в базе и теряется вместе с
# индексами. Поэтому в конфиге лежит РУМБ, а ребро под него находится
# заново по нынешнему контуру.


def declared_inlet(cfg: Mapping) -> str | None:
    """Румб входа воды из конфига, приведённый к русскому названию."""
    raw = (cfg.get("water_inlet") or "").strip().lower()
    if not raw:
        return None
    from suv.field_shape import COMPASS_RU, COMPASS_UZ
    for uz, ru in zip(COMPASS_UZ, COMPASS_RU):
        if raw in (uz, ru):
            return ru
    raise ValueError(f"water_inlet '{raw}' — не румб; ожидается один из: "
                     + ", ".join(COMPASS_RU))


def resolve_inlet(ring: list, want_ru: str) -> tuple[int, int] | None:
    """(i, j) ребра, смотрящего в заданную сторону. None — такого нет.

    Ищем среди сторон И ворот: вход с канала часто и есть короткий
    торец. Совпадений может быть несколько — берём самое длинное ребро:
    ворота шире межи в полметра. Соседний румб НЕ берём: «северо-восток»
    и «восток» на вытянутом поле это разные межи, и подменять одну
    другой значит тихо соврать про сторону полива.
    """
    from suv.field_shape import from_geojson_ring, inlet_options
    pts = from_geojson_ring(ring)
    hits = [e for e, _lbl in inlet_options(pts, "ru")
            if e.name("ru") == want_ru]
    if not hits:
        return None
    best = max(hits, key=lambda e: e.length_m)
    return best.i, best.j


# Насколько обмер контура вправе разойтись с заявленной площадью, пока
# это списывается на неточность пальца на карте. Тот же порог, при
# котором бот переспрашивает фермера прямо при сохранении контура.
AREA_MISMATCH_FRAC = 0.10


def area_mismatch(cfg: Mapping, row: Mapping | None) -> str | None:
    """Заявленная площадь против обмеренной по контуру, если они спорят.

    Это НЕ расхождение конфига с базой: hectares — со слов фермера,
    area_ha — по обводке, и держать обе цифры целыми задумано (см.
    save_polygon). Но объём полива считается по hectares — мм × 10 × га,
    — поэтому спор двух цифр это спор о том, сколько воды лить. На
    виноградника такой спор уже стоил половины нормы, и заметили его
    случайно. Пересевом не лечится: кто прав, решает человек.
    """
    if not row:
        return None
    declared = _opt_float(cfg.get("hectares"))
    measured = _opt_float(row.get("area_ha"))
    if not declared or not measured:
        return None
    off = (measured - declared) / declared
    if abs(off) < AREA_MISMATCH_FRAC:
        return None
    return (f"заявлено {show(declared)} га, обмер контура {show(measured)} га "
            f"({off:+.0%}) — объём полива считается по заявленной")


def _opt_float(value) -> float | None:
    return None if value is None else float(value)


def _same(want, have) -> bool:
    if isinstance(have, MissingColumn):
        # Колонки нет, но и писать в неё нечего — на рекомендацию это
        # не влияет. Миграция в Ledger.__init__ добавит её сама.
        return want is None
    if want is None or have is None:
        return want is None and have is None
    if isinstance(want, (int, float)) and isinstance(have, (int, float)):
        # Через SQLite число проходит без потерь, допуск здесь — только
        # чтобы 1.5 не разошлось с 1.4999999999 после ручной правки.
        return math.isclose(float(want), float(have),
                            rel_tol=1e-9, abs_tol=1e-9)
    return want == have
