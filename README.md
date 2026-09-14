# SUV AI — pilot

Sug'orish bo'yicha qaror qabul qiluvchi tizim. / Система принятия решений о поливе.

Per-field irrigation scheduling for Uzbek farms. Satellite + weather +
FAO-56 soil water balance, delivered as one sentence in Uzbek over Telegram.

**Status: pilot / v0.1.0.** The physics engine is tested. The satellite,
weather and Telegram layers have since run against live credentials — see
the table below, which is the authority on what is proven. What is still
missing is not plumbing but ground truth: no flow meter, no yield in tons,
no on-field moisture measurement. Metered savings are zero. That
distinction is stated here on purpose — do not let it get blurred in a
pitch.

---

## What is actually proven

| Layer | State | Evidence |
|---|---|---|
| ET0 (FAO-56 Penman-Monteith) | **Verified** | 9 tests against the worked examples printed in FAO-56 itself |
| Crop coefficients, growth stages | **Working, uncalibrated** | Curve shape tested; Kc values are FAO table defaults, not Uzbek field data. Established trees/vines keep full root depth year-round (FAO-56); orchard NDVI→Kc goes through fraction cover (Allen & Pereira 2009), vines and annuals through the Campos/Calera line |
| Soil water balance | **Verified** | TAW/RAW/percolation tested against FAO-56 tables; drip uses a wetted-soil fraction (FAO-56 Table 20, 0.40) — without it the orchard got 20+ days between irrigations against the farmer's 4 |
| Capillary rise from shallow water table | **Working** | Added after the model demanded ~2× real water use; season total now lands inside Uzbek agronomic norms |
| Irrigation scheduling | **Working, unvalidated** | Season run = 5,455 m³/ha for cotton in Fergana. The 5,000–7,000 band this was once called “the norm” has no citation in the repo and published Uzbek figures appear to sit higher — the season test is a sanity bound, not a validation (see `tests/test_schedule.py`) |
| Savings ledger | **Working** | Schema + derivation tested; baseline window and no-action rule locked by tests |
| Weather feed (Open-Meteo) | **Verified live** | Real forecast pulled on a Samarkand point, ET0 8.9 mm |
| Sentinel-2 NDVI (Copernicus) | **Verified live** | Real reading: NDVI 0.428, 100% cloud-free pixels, scene date from the catalog |
| NDVI wired into the recommendation | **Working** | `enrich.py`; degrades to calendar Kc on any failure |
| Telegram bot | **In use by a farmer** | Farrukh, the pilot farmer, runs it on his own two fields and confirms the advice arrives and reads correctly |
| Savings actually measured | **Not yet** | Until `/bajardim` confirmations accumulate, every saving figure is a back-test, not a measured result |

502 tests, all passing: `python -m pytest tests/ -q`

**Pilot status, August 2026.** The bot runs 24/7 on a VPS and serves one
real farm in Samarkand province: an apple orchard (2 ha, drip, pumped)
and a vineyard (3 ha, gravity-fed from a canal). The farmer has it on his
own phone and has confirmed it works on his fields. What is still missing
is the other half of the loop — his irrigation confirmations, without
which the ledger has nothing real to compare against.

---

## The finding that matters

The first version of the engine told a Fergana cotton field to use roughly
**twice** the water that farms there actually apply. The cause was not a
coding error — it was a missing physical term.

Across much of the irrigated Fergana and Zarafshan valleys the water table
sits 1–3 m below the surface and feeds the root zone directly. Standard
textbook implementations drop this term because most of the world has a
deep table. Uzbekistan does not.

Adding capillary rise brought seasonal use from ~9,700 to 5,455 m³/ha —
an order of magnitude closer to what the pilot farmer actually applies.
Whether 5,455 is *right* is still an open question: the band we used to
call “the norm” is uncited, and published Uzbek figures appear higher. The same shallow table is the mechanism
behind the salinity damage on half the country's irrigated land, so the
correction also produced the salinity warning the bot now sends.

**Two consequences for the pitch:**

1. Over a 30-day heatwave the engine finds **no** saving, because the crop
   genuinely needs every millimetre. Savings appear across a season, from
   skipped irrigations after rain and in cool spells. Any saving claim
   measured over a peak month is not defensible.
2. The baseline must be **that farmer's own last season**, not a national
   average. The ledger stores it per field for exactly this reason.

---

## Quick start

```bash
pip install -r requirements.txt
python -m pytest tests/ -q            # 502 tests, no network needed

cp .env.example .env                  # then fill it in — .env is gitignored
python -m bot.main
```

`.env` holds `TELEGRAM_TOKEN` (@BotFather), the optional Copernicus pair,
and `ALLOWED_CHAT_IDS` — leave that last one empty and the bot answers
anyone who finds it. Deploying to a server: [ДЕПЛОЙ.md](ДЕПЛОЙ.md).

Run one season offline, no credentials required:

```python
from datetime import date
from suv.climate import STATIONS, season
from suv.crop import CROPS
from suv.soil import SOILS, WaterBalanceState
from suv.schedule import Field, recommend
from suv.messages import recommendation_text

st = STATIONS["fergana"]
f = Field("FRG-001", "Shimoliy dala", 4.0, st.lat, st.lon, st.elevation_m,
          CROPS["cotton"], SOILS["loam"], date(2026, 4, 10), "furrow",
          water_table_depth_m=st.typical_water_table_m)
rec = recommend(f, season(st, date(2026, 7, 12), 14),
                WaterBalanceState(105.0, 1.35), date(2026, 7, 12))
print(recommendation_text(rec, "uz"))
```

```
Shimoliy dala

Payshanba kuni (16.07) sug'oring, gektariga 1 818 m³.
Namlik tez kamaymoqda.

Taxminan 7 273 m³ suv (4.0 ga).
Surat yo'q — hisob kalendar bo'yicha.
```

---

## Architecture

```
weather.py    Open-Meteo, 16-day forecast, no API key
satellite.py  Sentinel-2 L2A -> cloud-masked NDVI per field polygon
   |
et0.py        FAO-56 Penman-Monteith (Hargreaves fallback)
crop.py       Kc from growth stage, blended with NDVI by imagery age (trees: by fraction cover)
soil.py       TAW / RAW / percolation / capillary rise / wetted fraction (drip) / salinity
   |
schedule.py   day-by-day water balance -> one irrigation decision
messages.py   Uzbek first, Russian for agronomists. One instruction.
   |
ledger.py     recommendation -> farmer action -> metered outcome (SQLite)
bot/main.py   Telegram shell
```

`ledger.py` never stores a computed saving. It stores what was said, what
was done, and what the meter read. The saving is derived at read time so it
can always be recomputed and challenged — which is the point, because the
competition pays tranches against a verified KPI.

---

## Before the pilot goes live

1. **Replace Kc defaults** with values from the regional extension service.
   Currently FAO table defaults, flagged in `crop.py`.
2. **Get each pilot farmer's last-season water use.** Done for the apple
   orchard (June: 8 sessions × 24 h at 24 m³/h = 288 m³/ha per session).
   Still missing for the vineyard — until it arrives, that field has no
   honest baseline and therefore no saving figure.
3. ~~**Soil type per field.**~~ Closed 11.09.2026 (`7068224`): the wizard
   asks the farmer directly — «Suv tez singadimi?» maps to sand / loam /
   clay. Elevation comes from Open-Meteo in the same step. Loam remains
   the fallback only for fields seeded by a script.
4. **Water table depth per field.** The single most influential input, and
   currently unset for bot-registered fields (defaults to 0 = no
   contribution, which over-estimates water need).
5. ~~**Give the bot real field boundaries.**~~ Closed 11.09.2026: the
   farmer traces the contour in a Telegram Mini App, and whole farms load
   from a file (`3c13351` — KML, KMZ, GeoJSON, ZIP-shapefile, batched).
   `bbox_polygon`'s 200 m square survives only as the fallback for a field
   with no polygon (`suv/enrich.py`). Known gaps of the import path:
   polygon holes are dropped while their area stays in the field,
   self-intersections are not checked, and WGS84 is the only accepted CRS.

