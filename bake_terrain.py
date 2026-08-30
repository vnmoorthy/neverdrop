"""Bake REAL Everest-region elevation into web/terrain.json.

Source: AWS Open Data terrain tiles (Mapzen 'terrarium' encoding, SRTM-derived),
public S3 bucket, no key required. elevation_m = R*256 + G + B/256 - 32768.

Output: {size, span_m, center: [lat,lon], base_m, peak_m, elev_b64}
where elev_b64 is little-endian uint16 meters, size*size values, row-major
from north-west corner.
"""
import base64
import io
import json
import math
import pathlib
import struct
import urllib.request

from PIL import Image

CENTER_LAT, CENTER_LON = 27.986, 86.918   # Everest / Nuptse / Lhotse massif
ZOOM = 12
TILES = 3            # 3x3 tiles ~ 25 km across at this latitude
GRID = 160           # output grid resolution


def tile_xy(lat, lon, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2.0 * n
    return x, y


def tile_to_latlon(x, y, z):
    n = 2 ** z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def main():
    cx, cy = tile_xy(CENTER_LAT, CENTER_LON, ZOOM)
    x0, y0 = int(cx) - TILES // 2, int(cy) - TILES // 2
    # the mosaic snaps to tile boundaries: its TRUE center is not the
    # requested center — store the actual one or every anchor is ~km off
    true_lat, true_lon = tile_to_latlon(x0 + TILES / 2, y0 + TILES / 2, ZOOM)
    mosaic = Image.new("RGB", (256 * TILES, 256 * TILES))
    for dx in range(TILES):
        for dy in range(TILES):
            url = (f"https://s3.amazonaws.com/elevation-tiles-prod/terrarium/"
                   f"{ZOOM}/{x0+dx}/{y0+dy}.png")
            with urllib.request.urlopen(url, timeout=30) as r:
                img = Image.open(io.BytesIO(r.read())).convert("RGB")
            mosaic.paste(img, (256 * dx, 256 * dy))
            print("fetched", url.rsplit("/", 3)[-3:])

    px = mosaic.load()
    W = 256 * TILES
    elev = [[0.0] * W for _ in range(W)]
    for j in range(W):
        for i in range(W):
            r, g, b = px[i, j]
            elev[j][i] = r * 256 + g + b / 256 - 32768

    # meters per pixel at the mosaic's true latitude/zoom
    mpp = 156543.03392 * math.cos(math.radians(true_lat)) / (2 ** ZOOM)
    span_m = mpp * W

    # downsample to GRID x GRID by box max (keep the summits sharp)
    step = W / GRID
    out = []
    peak = -1e9
    base = 1e9
    for j in range(GRID):
        for i in range(GRID):
            j0, j1 = int(j * step), min(W, int((j + 1) * step) + 1)
            i0, i1 = int(i * step), min(W, int((i + 1) * step) + 1)
            m = max(elev[jj][ii] for jj in range(j0, j1) for ii in range(i0, i1))
            m = max(0.0, m)
            out.append(int(round(m)))
            peak = max(peak, m)
            base = min(base, m)

    blob = struct.pack(f"<{len(out)}H", *out)
    doc = {
        "source": "AWS Open Data terrain tiles (Mapzen terrarium, SRTM-derived)",
        "size": GRID, "span_m": round(span_m),
        "center": [round(true_lat, 6), round(true_lon, 6)],
        "base_m": int(base), "peak_m": int(peak),
        "elev_b64": base64.b64encode(blob).decode(),
    }
    for dest in ("web/terrain.json", "docs/terrain.json"):
        pathlib.Path(dest).write_text(json.dumps(doc))
    kb = len(json.dumps(doc)) // 1024
    print(f"baked {GRID}x{GRID} grid spanning {span_m/1000:.1f} km, "
          f"elev {int(base)}-{int(peak)} m -> web/ + docs/ ({kb} KB)")
    assert peak > 8700, f"Everest missing from patch (peak {peak:.0f} m)"
    print("PEAK CHECK: Everest present")


main()
