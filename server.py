"""PostGIS Vector Tile Server (MVP).

A thin async HTTP layer over PostGIS. Each request for /{z}/{x}/{y}.pbf is turned
into a call to a pre-approved SQL function that returns Mapbox Vector Tile bytes
via ST_AsMVT. PostGIS does all the spatial work; this server just routes and
pools connections.

Run:
    uvicorn server:app --host 0.0.0.0 --port 7800 --reload
"""

import os
from contextlib import asynccontextmanager

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
SCHEMA = os.getenv("DB_SCHEMA", "public")
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")

# Whitelist of MVT-returning SQL functions the server is allowed to call.
# Keys are URL slugs (and the ST_AsMVT layer name); values are the
# fully-qualified SQL functions created by workflow.ipynb / sql/countries_mvt.sql.
# Only names that appear here are ever interpolated into a query, which is what
# keeps the f-string below safe from SQL injection.
_DEFAULT_TABLE = os.getenv("TABLE_NAME", "countries")
LAYERS: dict[str, str] = {
    _DEFAULT_TABLE: f"{SCHEMA}.{_DEFAULT_TABLE}_mvt",
}

MIN_ZOOM = 0
MAX_ZOOM = 22
MVT_MEDIA_TYPE = "application/vnd.mapbox-vector-tile"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=1, max_size=10, command_timeout=30
    )
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(title="PostGIS Vector Tile Server", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@app.get("/")
async def index(request: Request):
    """List available layers and their tile + TileJSON endpoints."""
    base = _base_url(request)
    return {
        "layers": {
            name: {
                "tiles": f"{base}/tiles/{name}/{{z}}/{{x}}/{{y}}.pbf",
                "tilejson": f"{base}/tiles/{name}.json",
            }
            for name in LAYERS
        }
    }


@app.get("/healthz")
async def healthz():
    """Liveness + DB connectivity check."""
    try:
        async with app.state.pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as exc:  # pragma: no cover - surfaced as 503
        raise HTTPException(status_code=503, detail=f"db unavailable: {exc}")
    return {"status": "ok"}


@app.get("/tiles/{layer}.json")
async def tilejson(layer: str, request: Request):
    """Minimal TileJSON 3.0 document so MapLibre can consume a layer by URL."""
    if layer not in LAYERS:
        raise HTTPException(status_code=404, detail="Unknown layer")
    base = _base_url(request)
    return {
        "tilejson": "3.0.0",
        "name": layer,
        "scheme": "xyz",
        "tiles": [f"{base}/tiles/{layer}/{{z}}/{{x}}/{{y}}.pbf"],
        "minzoom": MIN_ZOOM,
        "maxzoom": MAX_ZOOM,
        "vector_layers": [{"id": layer, "fields": {}}],
    }


@app.get("/tiles/{layer}/{z}/{x}/{y}.pbf")
async def get_tile(layer: str, z: int, x: int, y: int):
    if layer not in LAYERS:
        raise HTTPException(status_code=404, detail="Unknown layer")
    if not (MIN_ZOOM <= z <= MAX_ZOOM):
        raise HTTPException(status_code=400, detail="Zoom out of range")
    max_xy = (1 << z) - 1
    if not (0 <= x <= max_xy and 0 <= y <= max_xy):
        raise HTTPException(status_code=400, detail="Tile coords out of range")

    fn = LAYERS[layer]  # safe — value comes from the server-side whitelist
    sql = f"SELECT {fn}($1, $2, $3)"

    async with app.state.pool.acquire() as conn:
        mvt: bytes | None = await conn.fetchval(sql, z, x, y)

    if not mvt:
        # 204 keeps MapLibre quiet for empty tiles (ocean, no features, etc.)
        return Response(status_code=204)

    return Response(
        content=mvt,
        media_type=MVT_MEDIA_TYPE,
        headers={"Cache-Control": "public, max-age=3600"},
    )
