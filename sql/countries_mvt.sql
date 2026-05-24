-- MVT function for the `countries` layer.
--
-- This supersedes the gid-only function created inline by workflow.ipynb: it
-- exposes the country name as a tile property (so the viewer can label/popup
-- features) and applies coarse, zoom-dependent simplification so low-zoom world
-- tiles stay small.
--
-- Run once against the target database, e.g.:
--   psql "$env:DATABASE_URL" -f sql/countries_mvt.sql
--
-- Notes:
--   * Data is already stored in EPSG:3857 (see notebook step 5), so no
--     ST_Transform is needed inside the tile loop.
--   * GeoPandas->PostGIS preserves the shapefile's mixed-case column names, so
--     "NAME" / "ADMIN" must be double-quoted. Adjust if your columns differ.

CREATE OR REPLACE FUNCTION public.countries_mvt(z integer, x integer, y integer)
RETURNS bytea AS $$
DECLARE
    mvt bytea;
    tol double precision;
BEGIN
    -- Simplification tolerance in Web Mercator metres; 0 = full detail.
    tol := CASE
        WHEN z <= 2 THEN 10000
        WHEN z <= 4 THEN 2000
        WHEN z <= 6 THEN 500
        ELSE 0
    END;

    SELECT INTO mvt ST_AsMVT(tile, 'countries', 4096, 'geom')
    FROM (
        SELECT
            ST_AsMVTGeom(
                CASE WHEN tol > 0
                     THEN ST_SimplifyPreserveTopology(t.geometry, tol)
                     ELSE t.geometry
                END,
                ST_TileEnvelope(z, x, y),
                4096, 64, true
            ) AS geom,
            t.gid,
            t."NAME"  AS name,
            t."ADMIN" AS admin
        FROM public.countries t
        WHERE t.geometry && ST_TileEnvelope(z, x, y)
    ) AS tile
    WHERE tile.geom IS NOT NULL;

    RETURN mvt;
END;
$$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
