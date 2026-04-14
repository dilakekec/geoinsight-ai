"""
OpenStreetMap Overpass API — yeşil alan verisi çekici.

Sorgu kapsamı (5 km varsayılan):
  - leisure=park       → parklar
  - landuse=forest     → orman
  - natural=wood       → doğal orman
  - landuse=grass      → çimen alan
  - landuse=meadow     → çayır
  - natural=scrub      → çalılık

Skor formülü:
  coverage = green_area_m2 / circle_area_m2
  score    = min(coverage / 0.40 * 100, 100)   ← %40 örtü = mükemmel
"""

import math
import httpx

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Cache: (round(lat,2), round(lng,2), radius_m) → result dict
_cache: dict = {}

# OSM yeşil alan etiketleri
_GREEN_TAGS = [
    '["leisure"="park"]',
    '["landuse"="forest"]',
    '["natural"="wood"]',
    '["landuse"="grass"]',
    '["landuse"="meadow"]',
    '["natural"="scrub"]',
]


def _cache_key(lat: float, lng: float, radius_m: int) -> tuple:
    # ~1.1 km hassasiyet (0.01°)
    return (round(lat, 2), round(lng, 2), radius_m)


def _polygon_area_m2(nodes: list[dict]) -> float:
    """
    Shoelace (Gauss) formülü ile poligon alanı (m²).
    nodes: [{"lat": ..., "lon": ...}, ...]
    """
    if len(nodes) < 3:
        return 0.0

    lat0 = nodes[0]["lat"]
    lng0 = nodes[0]["lon"]
    lat_m = 111_320.0
    lng_m = 111_320.0 * math.cos(math.radians(lat0))

    xs = [(n["lon"] - lng0) * lng_m for n in nodes]
    ys = [(n["lat"] - lat0) * lat_m for n in nodes]

    n = len(xs)
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += xs[i] * ys[j]
        area -= xs[j] * ys[i]

    return abs(area) / 2.0


def _build_query(lat: float, lng: float, radius_m: int) -> str:
    lines = ["[out:json][timeout:30];", "("]
    for tag in _GREEN_TAGS:
        lines.append(f'  way{tag}(around:{radius_m},{lat},{lng});')
        lines.append(f'  relation{tag}(around:{radius_m},{lat},{lng});')
    lines += [");", "out geom;"]
    return "\n".join(lines)


def _parse_elements(elements: list) -> float:
    """OSM element listesinden toplam yeşil alan m² hesapla."""
    total_m2 = 0.0
    seen_ids: set = set()

    for el in elements:
        el_id = (el.get("type"), el.get("id"))
        if el_id in seen_ids:
            continue
        seen_ids.add(el_id)

        if el["type"] == "way":
            nodes = el.get("geometry", [])
            total_m2 += _polygon_area_m2(nodes)

        elif el["type"] == "relation":
            for member in el.get("members", []):
                if member.get("role") == "outer":
                    nodes = member.get("geometry", [])
                    total_m2 += _polygon_area_m2(nodes)

    return total_m2


async def fetch_green_coverage(
    lat: float,
    lng: float,
    radius_m: int = 5_000,
) -> dict:
    """
    Döner:
    {
      "green_area_m2"  : int,
      "coverage_ratio" : float,   # 0.0–1.0
      "score"          : float,   # 0–100  (None → hata, mock'a düş)
      "feature_count"  : int,
      "source"         : "OSM" | "simüle"
    }
    """
    key = _cache_key(lat, lng, radius_m)
    if key in _cache:
        return _cache[key]

    query = _build_query(lat, lng, radius_m)
    circle_area_m2 = math.pi * radius_m ** 2   # ~78.5 km²

    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(OVERPASS_URL, data={"data": query})
            resp.raise_for_status()
            osm = resp.json()

        elements = osm.get("elements", [])
        green_m2 = _parse_elements(elements)
        feature_count = len({(e.get("type"), e.get("id")) for e in elements})

        coverage = min(green_m2 / circle_area_m2, 1.0)

        # %40 örtü → 100 puan (kentsel ölçek için gerçekçi üst sınır)
        score = round(min(coverage / 0.40 * 100, 100.0), 1)

        result = {
            "green_area_m2": round(green_m2),
            "coverage_ratio": round(coverage, 4),
            "score": score,
            "feature_count": feature_count,
            "source": "OSM",
        }

    except Exception as exc:
        result = {
            "green_area_m2": 0,
            "coverage_ratio": 0.0,
            "score": None,          # None → main.py mock'a düşer
            "feature_count": 0,
            "source": "simüle",
            "error": str(exc),
        }

    _cache[key] = result
    return result
