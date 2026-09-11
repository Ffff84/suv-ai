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
    -- Подпись хранится рядом с file_id: без неё повторная отправка
    -- уходила бы БЕЗ ДАТЫ СНИМКА, а это прямой запрет ТЗ §1.2.
    photo_caption TEXT,
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

-- Полевые заметки фермера: фото (file_id Телеграма — сам файл живёт у
-- Телеграма), подпись, дата и, если фермер прислал локацию следом, —
-- точка. Свидетельства для журнала и разборов: «вода дошла до края»
-- фотографией убедительнее слов.
CREATE TABLE IF NOT EXISTS field_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    field_id TEXT NOT NULL REFERENCES fields(field_id),
    chat_id INTEGER,
    taken_on TEXT NOT NULL,
    file_id TEXT,
    caption TEXT,
    lat REAL, lon REAL,
    created_at TEXT NOT NULL
);

-- Оценка совета одним тапом — «Rate zones» OneSoil, перенесённый на
-- наш жанр. Один голос на рекомендацию и чат; переголосовать можно —
-- последнее слово за фермером. Это НЕ метрика качества расчёта
-- (эталона по-прежнему нет) — это сырьё для разговора с фермером:
-- «вы трижды ставили 👎 по средам — что не так по средам?»
CREATE TABLE IF NOT EXISTS advice_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_id INTEGER NOT NULL REFERENCES recommendations(id),
    field_id TEXT NOT NULL,
    chat_id INTEGER,
    verdict TEXT NOT NULL,              -- 'up' | 'down'
    created_at TEXT NOT NULL,
    UNIQUE(recommendation_id, chat_id)
);

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
    # Ложь без этого флага: при незаданном baseline_m3_per_ha экономия
    # не «ноль», её просто не с чем сравнить — текст обязан сказать это.
    has_baseline: bool = True
    # Дни между подтверждёнными поливами, когда журнал молчал дольше
    # SILENT_GAP_INTERVALS прежних интервалов: в счёт не вошли ни базой,
    # ни расходом. Текст обязан назвать их — иначе «экономия» читается
    # как посчитанная за весь сезон.
    silent_days: int = 0


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
    "created_at", "wetted_fraction", "harvest_start", "harvest_end",
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
    ("photo_caption", "TEXT"),
    ("photo_built_at", "TEXT"),
    ("photo_latest_seen", "TEXT"),
    # Доля смачивания почвы поливом (капля 0,3-0,4). NULL = по способу
    # полива, см. suv/soil.py WETTED_FRACTION.
    ("wetted_fraction", "REAL"),
    # Окно съёма урожая (ISO-даты, NULL = не задано): за
    # crop.preharvest_hold_days до начала и до конца окна движок
    # не назначает поливов — режим терима.
    ("harvest_start", "TEXT"),
    ("harvest_end", "TEXT"),
    # Архив вместо удаления: DELETE стёр бы поле, на которое ссылаются
    # рекомендации и отметки, — а журнал у нас не стирается. NULL =
    # поле живое; дата = когда сняли с ро'йхата.
    ("archived_at", "TEXT"),
    # Опыт «по совету против привычки»: кольца половин (A — по совету)
    # и дата старта. Сбрасываются при перечерчивании контура — половины
    # старой границы на новой лежат криво.
    ("trial_half_a", "TEXT"),
    ("trial_half_b", "TEXT"),
    ("trial_started", "TEXT"),
)


# Сколько прежних интервалов полива подряд журнал может молчать, чтобы
# отрезок между двумя подтверждениями ещё считался покрытым. Два: совет
# бота честно растягивает интервал (дождь, прохлада) — это и есть
# экономия, и она должна войти; но дольше двух привычных интервалов без
# единой отметки — это не «сад три недели не поливали», это «фермер не
# нажимал кнопку», и такие дни из счёта выпадают (см. savings()).
SILENT_GAP_INTERVALS = 2


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

    def has_action_on_day(self, field_id: str, day: date) -> bool:
        """Есть ли у поля уже подтверждённый полив за этот день.

        Утренний пуш и дневная кнопка дают две рекомендации с разными id,
        и has_action() по id пропускал второй «✅ Suv berdim» за тот же
        полив: расход задваивался, /tejaldi показывал антиэкономию
        фермеру, который выполнил совет ровно один раз (аудит 18.08.2026,
        ledger.py:363). savings() дедуплицирует по дню и сам, но запись
        лучше не плодить: по ней же встаёт якорь водного баланса.
        """
        with closing(self._conn()) as c:
            r = c.execute(
                """SELECT 1 FROM actions a
                   JOIN recommendations r ON r.id = a.recommendation_id
                   WHERE r.field_id=? AND a.followed=1 AND a.actual_day=?
                   LIMIT 1""", (field_id, day.isoformat())).fetchone()
        return r is not None

    def has_action(self, recommendation_id: int) -> bool:
        """Есть ли уже отметка по этой рекомендации.

        Нужна хендлерам /bajardim: двойной тап по кнопке часов давал два
        INSERT, metered задваивался, и /tejaldi занижал экономию на весь
        объём лишней строки.
        """
        with closing(self._conn()) as c:
            r = c.execute("SELECT 1 FROM actions WHERE recommendation_id=? "
                          "LIMIT 1", (recommendation_id,)).fetchone()
        return r is not None

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
            # Половины опыта тоже слетают: они резались по старой границе.
            c.execute("UPDATE fields SET polygon_geojson=?, area_ha=?, "
                      "polygon_source=?, inlet_vertices=NULL, "
                      "trial_half_a=NULL, trial_half_b=NULL, "
                      "trial_started=NULL, "
                      "photo_file_id=NULL, photo_key=NULL, photo_caption=NULL "
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

    def cached_photo(self, field_id: str, latest_day: str,
                     key: str) -> tuple[str, str, str] | None:
        """(file_id, подпись, дата кадра) готового фото, если пересобирать
        нечего. Пересобираем, только когда появился новый снимок или
        изменился контур: иначе фермер ждёт полминуты ради той же
        картинки, а мы зря тратим квоту Copernicus.

        latest_day — самый свежий проход из каталога. Сравнивается и с
        датой кадра, и с photo_latest_seen — датой каталога на момент
        сборки. Без второго сравнения кэш не срабатывал НИКОГДА, пока
        свежайший проход облачный: фото законно собрано по предыдущему
        чистому дню, его дата не равна каталожной, и каждое нажатие
        уходило в полную пересборку с той же картинкой на выходе.
        """
        with closing(self._conn()) as c:
            r = c.execute("SELECT photo_file_id, photo_scene_date, photo_key, "
                          "photo_caption, photo_latest_seen FROM fields "
                          "WHERE field_id=?", (field_id,)).fetchone()
        if not r or not r["photo_file_id"]:
            return None
        if r["photo_key"] != key:
            return None
        if latest_day not in (r["photo_scene_date"], r["photo_latest_seen"]):
            return None
        return (r["photo_file_id"], r["photo_caption"] or "",
                r["photo_scene_date"] or "")

    def save_photo(self, field_id: str, file_id: str, scene_day: str,
                   key: str, caption: str = "",
                   latest_seen: str | None = None) -> None:
        with closing(self._conn()) as c:
            c.execute("UPDATE fields SET photo_file_id=?, photo_scene_date=?, "
                      "photo_key=?, photo_caption=?, photo_built_at=?, "
                      "photo_latest_seen=? WHERE field_id=?",
                      (file_id, scene_day, key, caption,
                       datetime.utcnow().isoformat(),
                       latest_seen or scene_day, field_id))
            c.commit()

    def set_trial(self, field_id: str, half_a_json: str,
                  half_b_json: str) -> None:
        """Начать опыт: половины A (по совету) и B (привычка)."""
        with closing(self._conn()) as c:
            c.execute(
                "UPDATE fields SET trial_half_a=?, trial_half_b=?, "
                "trial_started=? WHERE field_id=?",
                (half_a_json, half_b_json, datetime.utcnow().isoformat(),
                 field_id))
            c.commit()

    def clear_trial(self, field_id: str) -> None:
        with closing(self._conn()) as c:
            c.execute("UPDATE fields SET trial_half_a=NULL, "
                      "trial_half_b=NULL, trial_started=NULL "
                      "WHERE field_id=?", (field_id,))
            c.commit()

    def add_feedback(self, recommendation_id: int, field_id: str,
                     chat_id: int | None, verdict: str) -> None:
        """Голос по совету. REPLACE — переголосовать можно."""
        if verdict not in ("up", "down"):
            raise ValueError(f"verdict up|down, не {verdict!r}")
        with closing(self._conn()) as c:
            c.execute(
                """INSERT OR REPLACE INTO advice_feedback
                   (recommendation_id, field_id, chat_id, verdict, created_at)
                   VALUES (?,?,?,?,?)""",
                (recommendation_id, field_id, chat_id, verdict,
                 datetime.utcnow().isoformat()))
            c.commit()

    def feedback_counts(self, field_id: str) -> tuple[int, int]:
        """(👍, 👎) по полю за всё время."""
        with closing(self._conn()) as c:
            row = c.execute(
                """SELECT SUM(verdict='up'), SUM(verdict='down')
                   FROM advice_feedback WHERE field_id=?""",
                (field_id,)).fetchone()
        return int(row[0] or 0), int(row[1] or 0)

    def recommendation_field(self, recommendation_id: int) -> str | None:
        with closing(self._conn()) as c:
            row = c.execute(
                "SELECT field_id FROM recommendations WHERE id=?",
                (recommendation_id,)).fetchone()
        return row[0] if row else None

    def rename_field(self, field_id: str, name: str) -> bool:
        """Новое имя поля. Право владения проверяет вызывающий слой."""
        with closing(self._conn()) as c:
            cur = c.execute("UPDATE fields SET name=? WHERE field_id=?",
                            (name, field_id))
            c.commit()
            return cur.rowcount > 0

    def archive_field(self, field_id: str) -> bool:
        """Снять поле с ро'йхата, ничего не стирая: история рекомендаций
        и отметок остаётся, id никогда не переиспользуется."""
        with closing(self._conn()) as c:
            cur = c.execute(
                "UPDATE fields SET archived_at=? "
                "WHERE field_id=? AND archived_at IS NULL",
                (datetime.utcnow().isoformat(), field_id))
            c.commit()
            return cur.rowcount > 0

    def add_note(self, field_id: str, chat_id: int | None, taken_on: date,
                 file_id: str | None = None,
                 caption: str | None = None) -> int:
        """Полевая заметка. Возвращает id — к нему может приехать локация."""
        with closing(self._conn()) as c:
            cur = c.execute(
                """INSERT INTO field_notes
                   (field_id, chat_id, taken_on, file_id, caption, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (field_id, chat_id, taken_on.isoformat(), file_id, caption,
                 datetime.utcnow().isoformat()))
            c.commit()
            return cur.lastrowid

    def attach_note_location(self, note_id: int, lat: float,
                             lon: float) -> bool:
        """Приложить точку к заметке — один раз: локация, присланная
        позже по другому поводу, не должна тихо переписать место."""
        with closing(self._conn()) as c:
            cur = c.execute(
                "UPDATE field_notes SET lat=?, lon=? "
                "WHERE id=? AND lat IS NULL",
                (lat, lon, note_id))
            c.commit()
            return cur.rowcount > 0

    def notes(self, field_id: str, limit: int = 20) -> list:
        """Свежие заметки поля, новые сверху."""
        with closing(self._conn()) as c:
            return c.execute(
                """SELECT id, taken_on, file_id, caption, lat, lon
                   FROM field_notes WHERE field_id=?
                   ORDER BY id DESC LIMIT ?""",
                (field_id, limit)).fetchall()

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
        Derive the saving. Honesty rules, every one learned the hard way:

        1. The baseline window starts at the FIRST RECOMMENDATION, not at
           the start of the season. The bot cannot claim credit for weeks
           it was not running — and the June "twice a week" cadence never
           applied in March anyway.
        2. No confirmed action — no claim. Until the farmer has logged at
           least one /bajardim, the saving is exactly zero, because there
           is no actual usage to subtract from the baseline.
        3. Silence is not saving. The baseline is accrued only over
           stretches the journal actually covers: from one confirmed
           irrigation to the next, and only while that stretch is no
           longer than SILENT_GAP_INTERVALS old-habit intervals. A longer
           gap means the farmer irrigated without pressing the button (or
           the season was over) — either way we do not know, and «do not
           know» enters the ledger as nothing, not as free water. This one
           rule closes three audit findings at once (18.08.2026): phantom
           savings over unmarked irrigations, the winter gap between
           seasons, and the baseline outrunning the journal.
        4. One irrigation is one irrigation. A farmer who confirms the
           same day twice (morning push + afternoon button give two
           recommendation ids) has NOT irrigated twice: actions are
           de-duplicated by actual_day, keeping the LARGER volume — the
           assumption that costs the claim, not the farmer.

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
                          a.followed, a.actual_day, a.actual_m3, a.source
                   FROM recommendations r LEFT JOIN actions a
                     ON a.recommendation_id = r.id
                   WHERE r.field_id = ?
                   ORDER BY r.generated_on, r.id, a.id""", (field_id,)).fetchall()

        n = len({r["id"] for r in rows})
        followed_rows = [r for r in rows if r["followed"] == 1]
        sources = {r["source"] for r in rows if r["source"]}

        # Правило 4: один день — один полив. Из дублей остаётся больший
        # объём; без даты дедуплицировать не по чему — такая отметка
        # считается отдельным поливом.
        by_day: dict = {}
        undated = []
        for r in followed_rows:
            m3 = r["actual_m3"] if r["actual_m3"] is not None else (r["gross_m3"] or 0.0)
            if r["actual_day"]:
                d = datetime.strptime(r["actual_day"], "%Y-%m-%d").date()
                by_day[d] = max(by_day.get(d, 0.0), m3)
            else:
                undated.append(m3)
        followed = len(by_day) + len(undated)
        metered = sum(by_day.values()) + sum(undated)

        base_per_ha = f["baseline_m3_per_ha"] or 0.0
        interval = f["baseline_interval_days"] or 30
        max_gap = SILENT_GAP_INTERVALS * interval

        # Правило 3: база — только по покрытым окнам. Подтверждённые
        # поливы идут по порядку от первой рекомендации (правило 1);
        # пока соседние отметки не дальше max_gap друг от друга, они
        # лежат в одном окне; разрыв длиннее — молчание: старое окно
        # закрывается, новое начинается с подтверждения после разрыва.
        # Окно длиной L дней даёт L // interval поливов по старой
        # привычке ПЛЮС один — полив в самом начале окна (в первом окне
        # это привычный полив в день старта, в последующих — тот
        # подтверждённый, что открыл окно). Окно в 8 дней при интервале
        # 4 держит три полива, а не два — иначе база отставала от расхода
        # на один полив весь сезон, и у фермера, льющего ровно прежнюю
        # норму, /tejaldi показывал минус вместо нуля. Считаем окном
        # целиком, а не суммой отрезков: floor по каждому отрезку терял
        # бы дробные интервалы у фермера, который льёт чаще привычки.
        events = 0
        silent_days = 0
        if rows and by_day:
            first_rec = min(datetime.strptime(r["generated_on"], "%Y-%m-%d").date()
                            for r in rows)
            window_start = first_rec
            prev = first_rec
            in_window = 0          # подтверждений в текущем окне
            for d in sorted(by_day):
                gap = (d - prev).days
                if gap > max_gap:
                    # Окно без единого подтверждения (первая отметка
                    # пришла позже max_gap от первой рекомендации) не
                    # даёт ничего — правило 2.
                    if in_window:
                        events += max(0, (prev - window_start).days) // interval + 1
                    silent_days += gap
                    window_start = d
                    in_window = 0
                prev = d
                in_window += 1
            if in_window:
                events += max(0, (prev - window_start).days) // interval + 1
        events += len(undated)
        baseline = base_per_ha * f["hectares"] * events

        # baseline == 0 значит «сравнивать не с чем» (не задан, или ни
        # одного полного интервала): выпускать отсюда минус — значит
        # показать фермеру «анти-экономию» на пустом месте.
        saved = (baseline - metered) if followed and baseline > 0 else 0.0

        return SavingsSummary(
            field_id=field_id, recommendations=n, followed=followed,
            metered_m3=round(metered, 1), baseline_m3=round(baseline, 1),
            saved_m3=round(saved, 1),
            verified=bool(sources) and sources <= {"meter", "wca"},
            has_baseline=base_per_ha > 0,
            silent_days=silent_days,
        )
