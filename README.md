# SUV AI — pilot

Sug'orish bo'yicha qaror qabul qiluvchi tizim. / Система принятия решений о поливе.

Per-field irrigation scheduling for Uzbek farms. Satellite + weather +
FAO-56 soil water balance, delivered as one sentence in Uzbek over Telegram.

**Status: live pilot / v0.1.** The physics engine is tested. The bot runs
24/7 on a VPS, sends its own morning advice, and serves one real farm.
What is *not* yet real is measured savings: until the farmer's irrigation
confirmations accumulate, every saving figure is a back-test. That
distinction is stated here on purpose — do not let it get blurred in a pitch.

---

## What is actually proven

| Layer | State | Evidence |
|---|---|---|
| ET0 (FAO-56 Penman-Monteith) | **Verified** | 9 tests against the worked examples printed in FAO-56 itself |
| Crop coefficients, growth stages | **Working, uncalibrated** | Curve shape tested; Kc values are FAO table defaults, not Uzbek field data |
| Soil water balance | **Verified** | TAW/RAW/percolation tested against FAO-56 tables |
| Capillary rise from shallow water table | **Working** | Added after the model demanded ~2× real water use; season total now lands inside Uzbek agronomic norms |
| Irrigation scheduling | **Verified** | Season run = 5,455 m³/ha for cotton in Fergana, inside the 5,000–7,000 norm |
| Savings ledger | **Working** | Schema + derivation tested; baseline window and no-action rule locked by tests |
| Weather feed (Open-Meteo) | **Verified live** | Real forecast pulled on a Samarkand point, ET0 8.9 mm |
| Sentinel-2 NDVI (Copernicus) | **Verified live** | Real reading: NDVI 0.428, 100% cloud-free pixels, scene date from the catalog |
| NDVI wired into the recommendation | **Working** | `enrich.py`; degrades to calendar Kc on any failure |
| Telegram bot | **In use by a farmer** | Farrukh, the pilot farmer, runs it on his own two fields and confirms the advice arrives and reads correctly |
| Daily push at 06:00 Tashkent | **Working** | Urgent advice always, calm advice at most every 3 days; only what was actually delivered is journaled |
| Field boundaries (corner walk / Mini App) | **Working, closed demo** | Real outline instead of a 200 m square; NDVI measured strictly inside it |
| Field photo with NDMI moisture wash | **Working, closed demo** | Esri archive basemap + the field's own Sentinel-2 moisture gradient, with honest area/freshness/cloud gates |
| «Dala holati» field screen | **Working, closed demo** | One card per field: irrigation, uniformity, imagery, weather, season costs |
| Savings actually measured | **Not yet** | Until `/bajardim` confirmations accumulate, every saving figure is a back-test, not a measured result |

171 tests, all passing: `python -m pytest tests/ -q`

**Pilot status, August 2026.** The bot runs 24/7 on a VPS and serves one
real farm in Samarkand province: an apple orchard (2 ha, drip, pumped)
and a vineyard (1.5 ha, gravity-fed from a canal). The farmer has it on
his own phone and has confirmed it works on his fields. What is still
missing is the other half of the loop — his irrigation confirmations,
without which the ledger has nothing real to compare against.

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

## What the bot does now

Live for the pilot farmer today:

- **Morning push at 06:00 Tashkent.** The bot writes first. Urgent advice
  ("irrigate today/tomorrow") is always sent; calm advice at most once
  every 3 days, so it never becomes spam. The journal records only what
  was actually delivered.
- **Bugun/Kecha irrigation marking.** On gravity-fed fields water runs for
  a day or more, so `/bajardim` asks for the *day* the water ran — today
  or yesterday — and anchors the water balance on that day, not on the day
  the button was pressed.
- **Honest caveats.** A field with no known last-irrigation date gets
  "расчёт приблизительный" instead of a confident "no irrigation needed";
  an irrigation date more than a week out is marked *taxminiy* — it will
  firm up as it approaches.

Behind the closed-demo gate (`FIELD_STATUS_CHAT_IDS` — nobody else sees
these until the gate opens for their chat_id):

- **«Dala holati» screen.** One card per field, built from sections:
  irrigation, uniformity, satellite imagery, weather, season costs — plus
  an overall status in words. Owner visits are journaled as a
  return-rate KPI.
- **Real field outlines.** Two ways to draw the boundary: walk the corners
  sending live location, or draw it with a finger on a satellite map in a
  Telegram Mini App (served at suv-ai.online). Validated (3–20 vertices,
  0.1–500 ha, self-intersection, GPS drift), area confirmed by the farmer,
  and from then on NDVI is measured strictly inside the outline instead of
  a 200 m square that could catch a neighbour's crop or a road.
- **Water inlet side.** After outlining, the farmer picks the edge where
  water enters the field — the future furrow axis.
- **Field photo.** "Показать карту" sends a picture of the field: a
  high-resolution archive basemap with the field's own Sentinel-2 NDMI
  moisture gradient on top — red drier, green wetter. Honest gates: fields
  from 1.2 ha, scene at most 14 days old, ≥80% cloud-free pixels inside
  the outline; a uniform field is *not* painted with a fake gradient.
- **Observer role.** `OBSERVER_CHAT_IDS` see every field in Russian and
  leave no trace in the KPI journal. An observer may set a field up
  (outline, inlet side) but may not mark an irrigation — nobody signs the
  KPI for the farmer.

Around the bot:

- **suv-ai.online** — landing in Uzbek, Russian and English with the
  pilot's real aerial photos, a video presentation per language, and pitch
  decks in `/docs`. Deployed from this repo together with the bot and the
  Mini App.

---

## Quick start

```bash
pip install -r requirements.txt
python -m pytest tests/ -q            # 171 tests, no network needed

cp .env.example .env                  # then fill it in — .env is gitignored
python -m bot.main
```

`.env` holds `TELEGRAM_TOKEN` (@BotFather), the optional Copernicus pair,
`ALLOWED_CHAT_IDS` (empty = the bot answers anyone who finds it), the
closed-demo gate `FIELD_STATUS_CHAT_IDS`, `MINIAPP_URL` for the outline
page, and `OBSERVER_CHAT_IDS`. Deploying to a server: [ДЕПЛОЙ.md](ДЕПЛОЙ.md).

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
weather.py       Open-Meteo, 16-day forecast, no API key
satellite.py     Sentinel-2 L2A -> cloud-masked NDVI per field polygon
   |
et0.py           FAO-56 Penman-Monteith (Hargreaves fallback)
crop.py          Kc from growth stage, blended with NDVI by imagery age (enrich.py)
soil.py          TAW / RAW / percolation / capillary rise / salinity flag
   |
schedule.py      day-by-day water balance -> one irrigation decision
economics.py     pump cost / water price -> season spend per field
messages.py      Uzbek first, Russian for agronomists. One instruction.
   |
ledger.py        recommendation -> farmer action -> metered outcome (SQLite)
bot/main.py      Telegram shell: commands, morning push, closed-demo gate
   |
field_shape.py   field outline: corner walk / Mini App polygon, area, inlet edge
field_status.py  «Dala holati» card: sections registry, one screen per field
scene.py         Sentinel-2 rasters (true color + NDMI) via Process API
basemap.py       Esri World Imagery archive tiles, Web Mercator math
field_photo.py   photo rules: NDMI stats, percentile scale, area/freshness gates
photo_render.py  the picture itself: moisture wash, outline, legend, caption
   |
miniapp/draw.html  Telegram Mini App: draw the field on a satellite map
landing/           suv-ai.online — uz/ru/en landing, videos, decks in /docs
```

`ledger.py` never stores a computed saving. It stores what was said, what
was done, and what the meter read. The saving is derived at read time so it
can always be recomputed and challenged — which is the point, because the
competition pays tranches against a verified KPI.

---

## Still open

1. **Replace Kc defaults** with values from the regional extension service.
   Currently FAO table defaults, flagged in `crop.py`.
2. **Get each pilot farmer's last-season water use.** Done for the apple
   orchard (June: 8 sessions × 24 h at 24 m³/h = 288 m³/ha per session).
   Still missing for the vineyard — until it arrives, that field has no
   honest baseline and therefore no saving figure.
3. **Soil type per field.** Bot registration still defaults to loam; the
   agronomist-seeded fields already carry their real soil.
4. **Water table depth per field.** The single most influential input.
   Seeded fields have it; bot-registered fields default to 0 = no
   contribution, which over-estimates water need.
5. **Open the closed demo.** Outlines, the field photo and «Dala holati»
   work, but the pilot farmer will not see them until his chat_id enters
   the gate.
