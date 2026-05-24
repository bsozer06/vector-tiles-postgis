# Getting Started — PostGIS Vector Tiles MVP

Operational runbook for the working app in this repo. Where `README.md` is the
conceptual guide, this documents **what was actually built and how to run it
locally**, with the exact config and verified outputs from the last run.

---

## What this is

A thin async HTTP layer that serves Mapbox Vector Tiles (MVT) straight out of
PostGIS, plus a browser map that consumes them.

```
shapefile ──GeoPandas──▶ PostGIS (countries, EPSG:3857)
                              │
                       countries_mvt(z,x,y)        ← PostGIS does the MVT work
                              │  ST_AsMVT / ST_AsMVTGeom
                    FastAPI + asyncpg (server.py)  ← thin routing + pooling
                              │  /tiles/{z}/{x}/{y}.pbf
                       MapLibre GL JS (viewer.html)
```

PostGIS builds the tiles; Python only turns `/{z}/{x}/{y}.pbf` requests into a
call to a pre-approved SQL function and streams back the bytes.

---

## Components

| File | Role |
|------|------|
| `workflow.ipynb` | Ingestion pipeline: read shapefile → reproject to 3857 → load to PostGIS → create index/PK → create the MVT function → smoke-test a tile. |
| `sql/countries_mvt.sql` | The MVT function `public.countries_mvt(z,x,y)`. Exposes `name`/`admin` as tile properties and applies zoom-based simplification. Supersedes the gid-only version the notebook defines inline. |
| `server.py` | FastAPI tile server (asyncpg pool). Endpoints below. |
| `viewer.html` | MapLibre map (OSM basemap), loads the layer via TileJSON, click-popup of feature properties. |
| `requirements.txt` | Server runtime deps. |
| `.env` | Connection string + dataset settings. |
| `data/` | Natural Earth `ne_10m_admin_0_countries` shapefile (258 countries). |

---

## Configuration (`.env`)

```env
DATABASE_URL=postgresql://geouser:geopassword@localhost:5433/geodata
SHAPEFILE_PATH=data/ne_10m_admin_0_countries.shp
TABLE_NAME=countries
```

`server.py` also reads (optional): `DB_SCHEMA` (default `public`),
`CORS_ORIGINS` (default `*`).

---

## Prerequisites

- **Docker** — the database runs as the `postgis_db` container
  (`postgis/postgis:15-3.3`), mapping host **`5433`** → container `5432`.
- **Python 3.11+** with the project virtualenv at `.venv/`.

---

## Run it end-to-end

### 1. Start the database

```powershell
docker start postgis_db
docker ps --filter name=postgis_db        # expect: Up (healthy), 0.0.0.0:5433->5432
```

> Fresh machine without the container? Create an equivalent one:
> ```powershell
> docker run -d --name postgis_db -p 5433:5432 `
>   -e POSTGRES_USER=geouser -e POSTGRES_PASSWORD=geopassword `
>   -e POSTGRES_DB=geodata postgis/postgis:15-3.3
> ```

### 2. Load the data (once)

Run `workflow.ipynb` top to bottom (it reads the shapefile, reprojects to
EPSG:3857, and loads `public.countries`). To execute it headless:

```powershell
.\.venv\Scripts\python.exe -m nbconvert --to notebook --execute --inplace `
  --ExecutePreprocessor.kernel_name=python3 workflow.ipynb
```

### 3. Install/upgrade the MVT function

The notebook creates a gid-only function; this version adds country names and
simplification:

```powershell
docker exec -i postgis_db psql -U geouser -d geodata < sql/countries_mvt.sql
```

### 4. Start the tile server

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt          # first time only
uvicorn server:app --host 0.0.0.0 --port 7800 --reload
```

### 5. Verify

```powershell
curl.exe http://localhost:7800/healthz                         # {"status":"ok"}
curl.exe http://localhost:7800/                                # lists layers
curl.exe -o tile.pbf "http://localhost:7800/tiles/countries/2/2/3.pbf"
```

### 6. Open the map

```powershell
python -m http.server 8080
# browse to http://localhost:8080/viewer.html
```

---

## HTTP API

| Method & path | Returns |
|---|---|
| `GET /` | JSON listing each layer's tile + TileJSON URLs. |
| `GET /healthz` | `{"status":"ok"}`, or `503` if the DB is unreachable. |
| `GET /tiles/{layer}.json` | TileJSON 3.0 doc (what MapLibre's `addSource({url})` loads). |
| `GET /tiles/{layer}/{z}/{x}/{y}.pbf` | MVT bytes (`application/vnd.mapbox-vector-tile`), or `204` for an empty tile. |

Layers are a server-side whitelist (`LAYERS` in `server.py`); only those names
are ever interpolated into SQL, which is what keeps the query injection-safe.
Add a layer by writing a `<name>_mvt(z,x,y)` function and adding an entry.

---

## Verified run (last execution)

| Step | Output |
|---|---|
| PostGIS version | `POSTGIS="3.3.4" ... PGSQL="150"` |
| Load | `Loaded 258 rows into public.countries (SRID=3857)` |
| Smoke-test tile | `z=2 x=2 y=3 → 5,827 bytes` (non-empty) |
| Decode | `Layer 'countries': 1 features, extent=4096` |

Notebook ran all cells with **0 errors**.

---

## Notebook smoke-test fix

The original preview cell picked a tile from the data's **union centroid** at a
hard-coded **`z=14`**. For a global layer that centroid back-projects onto the
pole (lat −90, pulled there by Antarctica), and `z=14` was far too zoomed, so
the coords came out as `y=55185` — outside the valid `0..16383` range — and the
tile was always empty. The fix:

1. Pick the tile from the **bounding-box centre**, not a union centroid.
2. **Clamp latitude** to ±85.0511 (Web Mercator's limit) before the tile math,
   so a polar centre maps to a valid edge tile instead of overflowing.
3. **Scan zoom levels** `[2,3,4,5,6,8,10]` and stop at the first tile that
   returns features, instead of guessing one zoom.

Result: the centre still resolves to lat −90 (Antarctica dominates the dataset),
but it now yields the valid tile `z=2 x=2 y=3` with real MVT bytes.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `connection refused` on 5433 | Container stopped | `docker start postgis_db` |
| `/healthz` → 503 | DB down / wrong `DATABASE_URL` | Check container + `.env` |
| Tiles all `204` | SRID mismatch or empty area | Data must be EPSG:3857; try `z=2 x=2 y=3` |
| Popups show only `gid` | Notebook's gid-only function in use | Run `sql/countries_mvt.sql` (step 3) |
| `UndefinedColumn "NAME"` | Columns loaded lowercase | Adjust the quoted column names in `sql/countries_mvt.sql` |
| CORS error in browser | `CORS_ORIGINS` too narrow | Set `CORS_ORIGINS=*` for dev |
| 404 on `.pbf` | Layer not whitelisted | Add it to `LAYERS` in `server.py`, restart |
