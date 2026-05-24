# Publishing Vector Tiles with PostGIS + Python

A step-by-step guide for serving Mapbox Vector Tiles (MVT) from PostGIS using a **Python** tile server (FastAPI + asyncpg), and consuming them in a MapLibre GL JS web map.

PostGIS still does the heavy lifting via `ST_AsMVT`; Python is the thin HTTP layer that turns `/{z}/{x}/{y}.pbf` requests into SQL.

---

## 1. Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| PostgreSQL | 14+ | Database engine |
| PostGIS | 3.2+ | Spatial extension (provides `ST_AsMVT`) |
| Python | 3.11+ | Tile server runtime |
| FastAPI / Uvicorn | latest | Async web framework |
| asyncpg | latest | Async PostgreSQL driver |
| QGIS *(optional)* | 3.28+ | For loading/inspecting data |
| Web browser | any modern | For the MapLibre viewer |

Verify PostGIS has MVT support:

```sql
SELECT PostGIS_Full_Version();
-- ST_AsMVT requires PostGIS >= 2.4
```

---

## 2. Prepare the PostGIS Database

### 2.1 Create database and enable PostGIS

```sql
CREATE DATABASE tiles_db;
\c tiles_db
CREATE EXTENSION postgis;
```

### 2.2 Load spatial data

Example: load a shapefile with `ogr2ogr`.

```powershell
ogr2ogr -f "PostgreSQL" PG:"host=localhost dbname=tiles_db user=postgres" `
        -nln public.parcels -nlt PROMOTE_TO_MULTI -lco GEOMETRY_NAME=geom `
        -t_srs EPSG:3857 parcels.shp
```

> Reproject data to **EPSG:3857** (Web Mercator) — this is what MVT expects.

### 2.3 Create a spatial index

```sql
CREATE INDEX parcels_geom_idx ON public.parcels USING GIST (geom);
ANALYZE public.parcels;
```

### 2.4 Create the MVT-producing SQL function

This function is called by the Python server with `(z, x, y)` and returns a tile as `bytea`.

```sql
CREATE OR REPLACE FUNCTION public.parcels_mvt(
    z integer, x integer, y integer
) RETURNS bytea AS $$
DECLARE
    mvt bytea;
BEGIN
    SELECT INTO mvt ST_AsMVT(tile, 'parcels', 4096, 'geom')
    FROM (
        SELECT
            ST_AsMVTGeom(
                ST_Transform(p.geom, 3857),
                ST_TileEnvelope(z, x, y),
                4096, 64, true
            ) AS geom,
            p.gid,
            p.parcel_no,
            p.owner
        FROM public.parcels p
        WHERE p.geom && ST_Transform(ST_TileEnvelope(z, x, y), ST_SRID(p.geom))
          AND (z >= 12 OR p.area_m2 > 5000)
    ) AS tile
    WHERE tile.geom IS NOT NULL;

    RETURN mvt;
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE;
```

---

## 3. Set Up the Python Project

### 3.1 Create a virtual environment

PowerShell:

```powershell
cd C:\_burhan\GIS\vector-tiles-postgis
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3.2 Install dependencies

```powershell
pip install "fastapi[standard]" uvicorn asyncpg python-dotenv
```

Pin them in `requirements.txt`:

```
fastapi[standard]>=0.115
uvicorn>=0.30
asyncpg>=0.29
python-dotenv>=1.0
```

### 3.3 Configuration file (`.env`)

```env
DATABASE_URL=postgresql://postgres:PASSWORD@localhost:5432/tiles_db
TILE_HOST=0.0.0.0
TILE_PORT=7800
CORS_ORIGINS=*
```

---

## 4. The Python Tile Server (`server.py`)

Save the following as `server.py` in this folder:

```python
import os
from contextlib import asynccontextmanager

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")

# Whitelist of MVT-returning SQL functions the server is allowed to call.
# Keys are URL slugs, values are fully-qualified function names.
LAYERS: dict[str, str] = {
    "parcels": "public.parcels_mvt",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=1, max_size=10, command_timeout=30
    )
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(title="PostGIS Vector Tile Server", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/")
async def index():
    return {
        "layers": {
            name: f"/tiles/{name}/{{z}}/{{x}}/{{y}}.pbf"
            for name in LAYERS
        }
    }


@app.get("/tiles/{layer}/{z}/{x}/{y}.pbf")
async def get_tile(layer: str, z: int, x: int, y: int):
    if layer not in LAYERS:
        raise HTTPException(status_code=404, detail="Unknown layer")
    if not (0 <= z <= 22):
        raise HTTPException(status_code=400, detail="Zoom out of range")
    max_xy = (1 << z) - 1
    if not (0 <= x <= max_xy and 0 <= y <= max_xy):
        raise HTTPException(status_code=400, detail="Tile coords out of range")

    fn = LAYERS[layer]  # safe — value comes from server-side whitelist
    sql = f"SELECT {fn}($1, $2, $3)"

    async with app.state.pool.acquire() as conn:
        mvt: bytes | None = await conn.fetchval(sql, z, x, y)

    if not mvt:
        # 204 keeps MapLibre quiet for empty tiles
        return Response(status_code=204)

    return Response(
        content=mvt,
        media_type="application/vnd.mapbox-vector-tile",
        headers={"Cache-Control": "public, max-age=3600"},
    )
```

### 4.1 Run it

```powershell
uvicorn server:app --host 0.0.0.0 --port 7800 --reload
```

Test:

```powershell
curl.exe -o tile.pbf "http://localhost:7800/tiles/parcels/12/2456/1583.pbf"
```

Expected: a non-empty `.pbf` file (a few KB to a few hundred KB).

---

## 5. Adding More Layers

For each new dataset:

1. Write a `public.<name>_mvt(z, x, y)` function in PostGIS (template from §2.4).
2. Add an entry to the `LAYERS` dict in `server.py`:
   ```python
   LAYERS = {
       "parcels": "public.parcels_mvt",
       "roads":   "public.roads_mvt",
       "poi":     "public.poi_mvt",
   }
   ```
3. Restart Uvicorn (auto-reloads with `--reload`).

This whitelist pattern prevents SQL injection — only pre-approved function names are ever interpolated into the query.

---

## 6. Consume the Tiles in MapLibre GL JS

Create `viewer.html` alongside `server.py`:

```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>PostGIS Vector Tiles (Python)</title>
  <link href="https://unpkg.com/maplibre-gl@4.5.0/dist/maplibre-gl.css" rel="stylesheet" />
  <script src="https://unpkg.com/maplibre-gl@4.5.0/dist/maplibre-gl.js"></script>
  <style>body,html,#map{margin:0;height:100%;}</style>
</head>
<body>
<div id="map"></div>
<script>
const map = new maplibregl.Map({
  container: 'map',
  style: 'https://demotiles.maplibre.org/style.json',
  center: [29.0, 41.0],
  zoom: 10
});

map.on('load', () => {
  map.addSource('parcels', {
    type: 'vector',
    tiles: ['http://localhost:7800/tiles/parcels/{z}/{x}/{y}.pbf'],
    minzoom: 0,
    maxzoom: 22
  });

  map.addLayer({
    id: 'parcels-fill',
    type: 'fill',
    source: 'parcels',
    'source-layer': 'parcels',   // matches the layer name passed to ST_AsMVT
    paint: {
      'fill-color': '#3388ff',
      'fill-opacity': 0.4,
      'fill-outline-color': '#1f4e8a'
    }
  });
});
</script>
</body>
</html>
```

Serve it:

```powershell
python -m http.server 8080
```

Open <http://localhost:8080/viewer.html>.

---

## 7. Performance Tuning

- **Spatial index** is mandatory — verify with `EXPLAIN ANALYZE`.
- **`ST_AsMVTGeom` clip buffer**: 64 px is typical; raise to 256 for thick strokes.
- **Simplify by zoom** inside the SQL function:
  ```sql
  ST_SimplifyPreserveTopology(p.geom, CASE WHEN z < 10 THEN 50 ELSE 0 END)
  ```
- **Materialized views** for heavy aggregations:
  ```sql
  CREATE MATERIALIZED VIEW parcels_z8 AS
  SELECT gid, ST_SimplifyPreserveTopology(geom, 50) AS geom FROM parcels;
  CREATE INDEX ON parcels_z8 USING GIST (geom);
  ```
- **asyncpg pool**: tune `min_size` / `max_size` to match Postgres `max_connections`.
- **HTTP caching**: the `Cache-Control` header above lets browsers/proxies cache tiles.
- **Reverse proxy**: put nginx in front for TLS, gzip, and disk-based tile caching.
- **Parallel safety**: keep MVT functions `PARALLEL SAFE` so Postgres can parallelize them.

---

## 8. Optional: Use a Ready-Made Python Tile Server

If you don't want to maintain `server.py`, drop-in alternatives exist:

| Project | Notes |
|---------|-------|
| **TiPg** (`pip install tipg`) | OGC API Tiles + Features over PostGIS. Auto-discovers tables. |
| **TiMVT** | Lean MVT-only FastAPI server (predecessor of TiPg). |

Quick start with TiPg:

```powershell
pip install tipg
$env:DATABASE_URL = "postgresql://postgres:PASSWORD@localhost:5432/tiles_db"
uvicorn tipg.main:app --port 7800
```

Trade-off: less code to own, but harder to add custom zoom-dependent SQL.

---

## 9. Common Issues

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Empty tiles (204 everywhere) | SRID mismatch | Ensure `ST_Transform(geom, 3857)` inside the function |
| CORS error in browser | Wrong `CORS_ORIGINS` | Set `CORS_ORIGINS=*` (dev) or your domain (prod) |
| 404 on `.pbf` | Layer not in `LAYERS` dict | Add it and restart Uvicorn |
| `asyncpg.exceptions.UndefinedFunctionError` | SQL function not created | Re-run §2.4 |
| Slow tiles at low zoom | No simplification | Add `ST_SimplifyPreserveTopology` |
| `function ST_AsMVT does not exist` | PostGIS < 2.4 | Upgrade PostGIS |

---

## 10. Production Checklist

- [ ] Use a **read-only** Postgres role for the asyncpg pool
- [ ] Run Uvicorn behind nginx with HTTPS
- [ ] Restrict `CORS_ORIGINS` to known frontend domains
- [ ] Enable tile caching (HTTP headers + nginx `proxy_cache`)
- [ ] Monitor query times via `pg_stat_statements`
- [ ] Run Uvicorn with `--workers N` (or via Gunicorn) in production
- [ ] Back up the database (geometry + function definitions)

---

## File Layout

```
vector-tiles-postgis/
├── README.md          ← this file
├── server.py          ← FastAPI tile server (§4)
├── viewer.html        ← MapLibre client (§6)
├── requirements.txt   ← Python deps (§3.2)
├── .env               ← DB connection (§3.3)
└── .venv/             ← virtual environment (gitignored)
```

---

## References

- PostGIS `ST_AsMVT`: <https://postgis.net/docs/ST_AsMVT.html>
- PostGIS `ST_AsMVTGeom`: <https://postgis.net/docs/ST_AsMVTGeom.html>
- FastAPI: <https://fastapi.tiangolo.com/>
- asyncpg: <https://magicstack.github.io/asyncpg/current/>
- TiPg: <https://developmentseed.org/tipg/>
- MapLibre GL JS: <https://maplibre.org/maplibre-gl-js/docs/>
- MVT spec: <https://github.com/mapbox/vector-tile-spec>
