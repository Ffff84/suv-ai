# SUV AI — pilot

Sug'orish bo'yicha qaror qabul qiluvchi tizim. / Система принятия решений о поливе.

Per-field irrigation scheduling for Uzbek farms. Satellite + weather +
FAO-56 soil water balance, delivered as one sentence in Uzbek over Telegram.

**Status: pilot / v0.1.0.** The physics engine is tested. The satellite and
Telegram layers are written against real APIs but have not yet run against
live credentials. That distinction is stated here on purpose — do not let
it get blurred in a pitch.

---

## What is actually proven

| Layer | State | Evidence |
|---|---|---|
| ET0 (FAO-56 Penman-Monteith) | **Verified** | 9 tests against the worked examples printed in FAO-56 itself |
| Crop coefficients, growth stages | **Working, uncalibrated** | Curve shape tested; Kc values are FAO table defaults, not Uzbek field data |
| Soil water balance | **Verified** | TAW/RAW/percolation tested against FAO-56 tables |
| Capillary rise from shallow water table | **Working** | Added after the model demanded ~2× real water use; season total now lands inside Uzbek agronomic norms |
| Irrigation scheduling | **Verified** | Season run = 5,455 m³/ha for cotton in Fergana, inside the 5,000–7,000 norm |
| Savings ledger | **Working** | Schema + derivation tested |
| Weather feed (Open-Meteo) | **Verified live** | Real forecast pulled on a Samarkand point, ET0 8.9 mm |
| Sentinel-2 NDVI (Copernicus) | **Verified live** | Real reading: NDVI 0.412, 100% cloud-free pixels |
| NDVI wired into the recommendation | **Working** | `enrich.py`; degrades to calendar Kc on any failure |
| Telegram bot | **Connects** | @suv_ai_bot responds to getMe; conversation flow not yet run with a farmer |

40 tests, all passing: `python -m pytest tests/ -q`

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
inside the real agronomic band. The same shallow table is the mechanism
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
python -m pytest tests/ -q            # 30 tests, no network needed

export TELEGRAM_TOKEN=...             # @BotFather
export CDSE_CLIENT_ID=...             # dataspace.copernicus.eu, optional
export CDSE_CLIENT_SECRET=...
python -m bot.main
```

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

Shanba kuni, 182 mm.
Namlik tez kamaymoqda.

Taxminan 7 273 m³ suv (4.0 ga).
```

---

## Architecture

```
weather.py    Open-Meteo, 16-day forecast, no API key
satellite.py  Sentinel-2 L2A -> cloud-masked NDVI per field polygon
   |
et0.py        FAO-56 Penman-Monteith (Hargreaves fallback)
crop.py       Kc from growth stage, blended with NDVI by imagery age
soil.py       TAW / RAW / percolation / capillary rise / salinity flag
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
2. **Get each pilot farmer's last-season water use** — without it there is
   no honest baseline and no honest saving.
3. **Soil type per field.** Currently defaults to loam at registration.
   One question to the farmer, or a lookup against the soil map.
4. **Water table depth per field.** The single most influential input, and
   currently unset for bot-registered fields (defaults to 0 = no
   contribution, which over-estimates water need).
5. **Give the bot real field boundaries.** `bbox_polygon` draws a 200 m
   square around the point, which can catch a neighbour's crop or a road.
   Four corner points from the farmer beat a square.

