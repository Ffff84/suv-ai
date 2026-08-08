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

from .crop import CROPS, season_start

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
    -- Насосная установка хозяйства. NULL = самотёк: у такого поля вода
    -- фермеру ничего не стоит, и денежную экономию по нему показывать
    -- нельзя, сколько бы кубов мы ни сберегли.
    pump_kwh_per_hour REAL,
    pump_m3_per_hour REAL,
    pump_cost_per_hour_uzs REAL,
    pump_lift_m REAL,
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


class Ledger:
    def __init__(self, path: str | Path = "suv.db"):
        self.path = str(path)
        with closing(sqlite3.connect(self.path)) as c:
            c.executescript(SCHEMA)
            c.commit()

    def _conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def upsert_field(self, **kw) -> None:
        kw.setdefault("created_at", datetime.utcnow().isoformat())
        cols = ",".join(kw)
        marks = ",".join("?" * len(kw))
        with closing(self._conn()) as c:
            c.execute(f"INSERT OR REPLACE INTO fields ({cols}) VALUES ({marks})",
                      tuple(kw.values()))
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

    def savings(self, field_id: str) -> SavingsSummary:
        """
        Derive the saving. Baseline comes from what THIS farmer did last
        season, stored on the field row — never from a national average.
        """
        with closing(self._conn()) as c:
            f = c.execute("SELECT * FROM fields WHERE field_id=?",
                          (field_id,)).fetchone()
            if f is None:
                raise KeyError(field_id)
            rows = c.execute(
                """SELECT r.id, a.followed, a.actual_m3, a.source
                   FROM recommendations r LEFT JOIN actions a
                     ON a.recommendation_id = r.id
                   WHERE r.field_id = ?""", (field_id,)).fetchall()

        n = len({r["id"] for r in rows})
        followed = sum(1 for r in rows if r["followed"] == 1)
        metered = sum(r["actual_m3"] or 0.0 for r in rows)
        sources = {r["source"] for r in rows if r["source"]}

        base_per_ha = f["baseline_m3_per_ha"] or 0.0
        interval = f["baseline_interval_days"] or 30
        planting = datetime.strptime(f["planting_date"], "%Y-%m-%d").date()
        # Ko'p yillik ekin (olma, uzum) har yili qaytadan boshlanadi — bazani
        # 2018-yildagi ekish sanasidan emas, shu yilgi uyg'onishdan hisoblaymiz.
        # Aks holda daraxt necha yoshda bo'lsa, "tejaldi" shuncha oshib ketadi.
        crop = CROPS.get(f["crop_key"])
        origin = season_start(crop, planting, date.today()) if crop else planting
        elapsed = (date.today() - origin).days
        baseline = base_per_ha * f["hectares"] * max(0, elapsed // interval)

        return SavingsSummary(
            field_id=field_id, recommendations=n, followed=followed,
            metered_m3=round(metered, 1), baseline_m3=round(baseline, 1),
            saved_m3=round(baseline - metered, 1),
            verified=bool(sources) and sources <= {"meter", "wca"},
        )
