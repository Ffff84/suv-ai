"""
The savings ledger.

This is the module the competition KPI depends on, and the one that turns
a demo into an auditable claim. Every recommendation, the farmer's actual
response, and the measured outcome are written to one append-only table.

Design rule: we never store a saving we computed. We store what we told
the farmer, what he did, and what the meter said. The saving is derived
at read time, so it can always be recomputed and challenged.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS fields (
    field_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_chat_id INTEGER,
    hectares REAL NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    elevation_m REAL NOT NULL,
    crop_key TEXT NOT NULL,
    soil_key TEXT NOT NULL,
    planting_date TEXT NOT NULL,
    irrigation_method TEXT NOT NULL,
    water_table_depth_m REAL DEFAULT 0,
    baseline_m3_per_ha REAL,
    baseline_interval_days INTEGER,
    -- Контур участка: кольцо GeoJSON [[lon,lat],...]. NULL = фермер
    -- ещё не обвёл поле, и всё, что опирается на границы (снимок по
    -- полигону, равномерность, фото), молчит вместо того, чтобы
    -- домысливать квадрат вокруг точки.
    polygon_geojson TEXT,
    area_ha REAL,              -- считается из контура, не со слов
    polygon_source TEXT,       -- 'miniapp' | 'pins'
    inlet_vertices TEXT,       -- два индекса вершин ребра входа воды
    -- Кэш фото поля (ТЗ §4.5). Храним file_id Telegram, а не файл:
    -- повторная отправка тогда мгновенна и не тратит трафик.
    photo_file_id TEXT,
    photo_scene_date TEXT,     -- дата снимка, по которому фото собрано
    photo_key TEXT,            -- отпечаток контура: перечертили — пересобрать
    photo_built_at TEXT,
    -- Насосная установка хозяйства. NULL = самотёк: у такого поля вода
    -- фермеру ничего не стоит, и денежную экономию по нему показывать
    -- нельзя, сколько бы кубов мы ни сберегли.
    pump_kwh_per_hour REAL,
    pump_m3_per_hour REAL,
    pump_cost_per_hour_uzs REAL,
    pump_lift_m REAL,
    last_irrigation_date TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    field_id TEXT NOT NULL REFERENCES fields(field_id),
    generated_on TEXT NOT NULL,
    action_day TEXT,
    gross_mm REAL NOT NULL,
    gross_m3 REAL NOT NULL,
    reason_key TEXT NOT NULL,
    kc REAL, kc_source TEXT,
    et0_mm REAL, etc_mm REAL,
    depletion_mm REAL, raw_mm REAL,
    ndvi REAL, ndvi_date TEXT,
    engine_version TEXT NOT NULL,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_id INTEGER NOT NULL REFERENCES recommendations(id),
    followed INTEGER NOT NULL,          -- 1 yes, 0 no
    actual_day TEXT,
    actual_m3 REAL,                     -- from the farmer or the meter
    source TEXT NOT NULL,               -- 'farmer' | 'meter' | 'wca'
    note TEXT,
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rec_field ON recommendations(field_id, generated_on);

-- Метрика экрана «Dala holati»: каждая строка — фермер сам открыл
-- состояние поля. Для питча это возвраты между пушами, а не рассылка.
CREATE TABLE IF NOT EXISTS field_status_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    field_id TEXT NOT NULL REFERENCES fields(field_id),
    chat_id INTEGER,
    opened_at TEXT NOT NULL
);
"""


@dataclass
class SavingsSummary:
    field_id: str
    recommendations: int
    followed: int
    metered_m3: float
    baseline_m3: float
    saved_m3: float
    verified: bool  # true only when every actual_m3 came from a meter


# Единственные колонки, которые upsert_field согласен принять. Имена
# kwargs попадают в текст SQL, поэтому список закрыт: конфиг из JSON,
# просочившийся сюда напрямую, не должен уметь дописывать SQL.
_FIELD_COLUMNS = frozenset({
    "field_id", "name", "owner_chat_id", "hectares", "lat", "lon",
    "elevation_m", "crop_key", "soil_key", "planting_date",
    "irrigation_method", "water_table_depth_m", "baseline_m3_per_ha",
    "baseline_interval_days", "pump_kwh_per_hour", "pump_m3_per_hour",
    "pump_cost_per_hour_uzs", "pump_lift_m", "last_irrigation_date",
    "polygon_geojson", "area_ha", "polygon_source", "inlet_vertices",
    "created_at",
})

# Колонки, дописанные после того, как база уже работала на пилоте.
# CREATE TABLE IF NOT EXISTS их в существующую таблицу не добавит, и без
# явного ALTER первый же upsert_field на боевом сервере падает.
_ADDED_COLUMNS = (
    ("last_irrigation_date", "TEXT"),
    ("polygon_geojson", "TEXT"),
    ("area_ha", "REAL"),
    ("polygon_source", "TEXT"),
    ("inlet_vertices", "TEXT"),
    ("photo_file_id", "TEXT"),
    ("photo_scene_date", "TEXT"),
    ("photo_key", "TEXT"),
    ("photo_built_at", "TEXT"),
)


class Ledger:
    def __init__(self, path: str | Path = "suv.db"):
        self.path = str(path)
        with closing(sqlite3.connect(self.path)) as c:
            c.executescript(SCHEMA)
            have = {r[1] for r in c.execute("PRAGMA table_info(fields)")}
            for col, sql_type in _ADDED_COLUMNS:
                if col not in have:
                    c.execute(f"ALTER TABLE fields ADD COLUMN {col} {sql_type}")
            c.commit()

    def _conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def upsert_field(self, **kw) -> None:
        """Завести или обновить поле, тронув ТОЛЬКО переданные колонки.

        Здесь был INSERT OR REPLACE, и это тихо стирало данные: SQLite по
        REPLACE удаляет строку и вставляет новую, поэтому всё, что не
        передали, обнулялось. Пока в таблице лежала только анкета поля,
        это было незаметно — заполняли её всегда целиком из конфига. С
        появлением контура цена выросла: повторный запуск
        scripts/seed_field.py (документированный шаг деплоя) стирал
        границу, которую фермер обошёл ногами, и снимок опять начинал
        усредняться по квадрату вокруг точки.
        """
        unknown = set(kw) - _FIELD_COLUMNS
        if unknown:
            raise ValueError(f"unknown field columns: {sorted(unknown)}")
        kw.setdefault("created_at", datetime.utcnow().isoformat())
        cols = ",".join(kw)
        marks = ",".join("?" * len(kw))
        # created_at обновлять нельзя: поле заведено один раз.
        updates = ",".join(f"{c}=excluded.{c}" for c in kw
                           if c not in ("field_id", "created_at"))
        sql = f"INSERT INTO fields ({cols}) VALUES ({marks})"
        if updates:
            sql += f" ON CONFLICT(field_id) DO UPDATE SET {updates}"
        else:
            sql += " ON CONFLICT(field_id) DO NOTHING"
        with closing(self._conn()) as c:
            c.execute(sql, tuple(kw.values()))
            c.commit()

    def log_recommendation(self, rec, engine_version: str) -> int:
        first = next((p for p in rec.plan), None)
        with closing(self._conn()) as c:
            cur = c.execute(
                """INSERT INTO recommendations
                   (field_id, generated_on, action_day, gross_mm, gross_m3,
                    reason_key, kc, kc_source, et0_mm, etc_mm, depletion_mm,
                    raw_mm, ndvi, ndvi_date, engine_version, sent_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rec.field.field_id, rec.generated_on.isoformat(),
                 rec.action_day.isoformat() if rec.action_day else None,
                 rec.gross_mm, rec.gross_m3, rec.reason_key,
                 first.kc if first else None,
                 first.kc_source if first else None,
                 first.et0_mm if first else None,
                 first.etc_mm if first else None,
                 first.depletion_mm if first else None,
                 first.raw_mm if first else None,
                 rec.field.ndvi,
                 rec.field.ndvi_date.isoformat() if rec.field.ndvi_date else None,
                 engine_version, datetime.utcnow().isoformat()))
            c.commit()
            return cur.lastrowid

    def log_action(self, recommendation_id: int, followed: bool,
                   actual_day: date | None = None, actual_m3: float | None = None,
                   source: str = "farmer", note: str | None = None) -> None:
        with closing(self._conn()) as c:
            c.execute(
                """INSERT INTO actions
                   (recommendation_id, followed, actual_day, actual_m3,
                    source, note, recorded_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (recommendation_id, int(followed),
                 actual_day.isoformat() if actual_day else None,
                 actual_m3, source, note, datetime.utcnow().isoformat()))
            c.commit()

    def save_polygon(self, field_id: str, ring: list[list[float]],
                     area_ha: float, source: str) -> None:
        """Записать контур поля и посчитанную по нему площадь.

        hectares НЕ трогаем: там площадь со слов фермера, и она нужна
        как есть — расхождение с обмеренной по контуру видно только
        пока обе цифры целы. Перезаписать её значит потерять
        единственную проверку того, что фермер обвёл своё поле.
        """
        import json
        with closing(self._conn()) as c:
            # Два знака — предел осмысленного: обход с телефонным GPS даёт
            # угол с ошибкой в метры, третий знак (10 м²) обещал бы
            # точность, которой нет.
            # inlet_vertices сбрасывается вместе с контуром: индексы
            # вершин старой границы на новой указывают куда попало, и
            # «вода заходит с севера» стало бы тихой неправдой.
            # Фото собрано по старой границе — вместе с контуром слетает
            # и оно, иначе фермер увидит заливку не по своему полю.
            c.execute("UPDATE fields SET polygon_geojson=?, area_ha=?, "
                      "polygon_source=?, inlet_vertices=NULL, "
                      "photo_file_id=NULL, photo_key=NULL "
                      "WHERE field_id=?",
                      (json.dumps(ring), round(area_ha, 2), source, field_id))
            c.commit()

    def save_inlet(self, field_id: str, i: int, j: int) -> None:
        """Ребро, с которого вода заходит на поле, — два индекса вершин.

        Индексы, а не координаты: контур могут перечертить, и тогда
        сторона входа обязана слететь вместе с ним, а не указывать на
        межу, которой больше нет. save_polygon это и делает.
        """
        import json
        with closing(self._conn()) as c:
            c.execute("UPDATE fields SET inlet_vertices=? WHERE field_id=?",
                      (json.dumps([int(i), int(j)]), field_id))
            c.commit()

    def cached_photo(self, field_id: str, scene_day: str,
                     key: str) -> str | None:
        """file_id готового фото, если пересобирать нечего (ТЗ §4.5).

        Пересобираем, только когда появился новый снимок или изменился
        контур. Иначе фермер ждёт полминуты ради той же картинки, а мы
        зря тратим квоту Copernicus.
        """
        with closing(self._conn()) as c:
            r = c.execute("SELECT photo_file_id, photo_scene_date, photo_key "
                          "FROM fields WHERE field_id=?",
                          (field_id,)).fetchone()
        if not r or not r["photo_file_id"]:
            return None
        if r["photo_scene_date"] != scene_day or r["photo_key"] != key:
            return None
        return r["photo_file_id"]

    def save_photo(self, field_id: str, file_id: str, scene_day: str,
                   key: str) -> None:
        with closing(self._conn()) as c:
            c.execute("UPDATE fields SET photo_file_id=?, photo_scene_date=?, "
                      "photo_key=?, photo_built_at=? WHERE field_id=?",
                      (file_id, scene_day, key,
                       datetime.utcnow().isoformat(), field_id))
            c.commit()

    def log_field_status_view(self, field_id: str,
                              chat_id: int | None = None) -> None:
        with closing(self._conn()) as c:
            c.execute(
                "INSERT INTO field_status_views (field_id, chat_id, opened_at) "
                "VALUES (?,?,?)",
                (field_id, chat_id, datetime.utcnow().isoformat()))
            c.commit()

    def savings(self, field_id: str) -> SavingsSummary:
        """
        Derive the saving. Two honesty rules, both learned the hard way:

        1. The baseline window starts at the FIRST RECOMMENDATION, not at
           the start of the season. The bot cannot claim credit for weeks
           it was not running — and the June "twice a week" cadence never
           applied in March anyway.
        2. No confirmed action — no claim. Until the farmer has logged at
           least one /bajardim, the saving is exactly zero, because there
           is no actual usage to subtract from the baseline.

        A followed action without a metered volume counts as the
        recommended volume: "he did what we told him" is the defensible
        assumption; zero is not — zero inflates the saving.
        """
        with closing(self._conn()) as c:
            f = c.execute("SELECT * FROM fields WHERE field_id=?",
                          (field_id,)).fetchone()
            if f is None:
                raise KeyError(field_id)
            rows = c.execute(
                """SELECT r.id, r.generated_on, r.gross_m3,
                          a.followed, a.actual_m3, a.source
                   FROM recommendations r LEFT JOIN actions a
                     ON a.recommendation_id = r.id
                   WHERE r.field_id = ?""", (field_id,)).fetchall()

        n = len({r["id"] for r in rows})
        followed = sum(1 for r in rows if r["followed"] == 1)
        metered = sum((r["actual_m3"] if r["actual_m3"] is not None
                       else r["gross_m3"] or 0.0)
                      for r in rows if r["followed"] == 1)
        sources = {r["source"] for r in rows if r["source"]}

        base_per_ha = f["baseline_m3_per_ha"] or 0.0
        interval = f["baseline_interval_days"] or 30

        if rows:
            first_rec = min(datetime.strptime(r["generated_on"], "%Y-%m-%d").date()
                            for r in rows)
            elapsed = (date.today() - first_rec).days
        else:
            elapsed = 0
        baseline = base_per_ha * f["hectares"] * max(0, elapsed // interval)

        saved = (baseline - metered) if followed else 0.0

        return SavingsSummary(
            field_id=field_id, recommendations=n, followed=followed,
            metered_m3=round(metered, 1), baseline_m3=round(baseline, 1),
            saved_m3=round(saved, 1),
            verified=bool(sources) and sources <= {"meter", "wca"},
        )
