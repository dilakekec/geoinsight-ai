"""
OSM Güvenlik Proxy Modeli

────────────────────────────────────────────────────────
FORMÜL:

  guvenlik = aydinlatma * 0.40 + ticari_yogunluk * 0.60
           (- issiz_alan proxy isteğe bağlı, küçük ağırlık)

────────────────────────────────────────────────────────
SORGU MİMARİSİ (tek sorgu):

  Yarıçap: sabit 2 km (güvenlik mahalle ölçeğinde anlam taşır)
  Çıktı:   out tags qt  → geometri YOK, sadece etiketler
           Node başına ~100 byte → İstanbul merkezi 2km = ~600 KB

  Alternatifin neden terk edildiği:
  - 3 ayrı count sorgusu → Render 30s timeout aşılıyordu
  - 5km + out geom → maxsize (2-10MB) aşılıyordu

────────────────────────────────────────────────────────
NORMALLEŞTIRME:

  Yoğun kentsel  → 60 lamba/km², 200 ticari node/km²  → ~90 puan
  Banliyö        → 20 lamba/km², 60 node/km²          → ~55 puan
  Kırsal         → 2 lamba/km², 5 node/km²            → ~15 puan
"""

import math
import httpx

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Güvenlik her zaman 2 km yarıçapında hesaplanır
_SECURITY_RADIUS_M = 2_000

# √ ölçekleme referans değerleri (bu yoğunlukta skor=100)
_LAMP_REF       = 50.0    # 50 lamba/km²
_COMMERCIAL_REF = 100.0   # 100 node/km²

_cache: dict = {}


def _cache_key(lat: float, lng: float) -> tuple:
    return (round(lat, 2), round(lng, 2))


def _build_query(lat: float, lng: float) -> str:
    r = _SECURITY_RADIUS_M
    return (
        f'[out:json][timeout:30][maxsize:4000000];'
        f'('
        f'node["highway"="street_lamp"](around:{r},{lat},{lng});'
        f'node["shop"](around:{r},{lat},{lng});'
        f'node["amenity"](around:{r},{lat},{lng});'
        f'way["landuse"~"^(industrial|brownfield|wasteland|landfill)$"]'
        f'(around:{r},{lat},{lng});'
        f');'
        f'out tags qt;'
    )


def _parse(elements: list, circle_area_m2: float) -> dict:
    lamp_count       = 0
    commercial_count = 0
    industrial_ways  = 0

    for el in elements:
        tags = el.get("tags", {})
        t    = el.get("type")

        if t == "node":
            if tags.get("highway") == "street_lamp":
                lamp_count += 1
            elif "shop" in tags or "amenity" in tags:
                commercial_count += 1
        elif t == "way":
            lu = tags.get("landuse", "")
            if lu in ("industrial", "brownfield", "wasteland", "landfill"):
                industrial_ways += 1

    circle_area_km2    = circle_area_m2 / 1_000_000
    lamp_density       = lamp_count / circle_area_km2
    commercial_density = commercial_count / circle_area_km2

    # Issız alan proxy: her way ≈ 0.25 km² ortalama
    empty_ratio = min(industrial_ways * 250_000 / circle_area_m2, 1.0)

    lamp_score       = round(min(math.sqrt(lamp_density / _LAMP_REF) * 100, 100), 1)
    commercial_score = round(min(math.sqrt(commercial_density / _COMMERCIAL_REF) * 100, 100), 1)
    empty_penalty    = round(empty_ratio * 100, 1)

    raw   = lamp_score * 0.40 + commercial_score * 0.60 - empty_penalty * 0.20
    score = round(max(5.0, min(95.0, raw)), 1)

    return {
        "lamp_count":          lamp_count,
        "commercial_count":    commercial_count,
        "empty_area_m2":       industrial_ways * 250_000,
        "lamp_density":        round(lamp_density, 2),
        "commercial_density":  round(commercial_density, 2),
        "lamp_score":          lamp_score,
        "commercial_score":    commercial_score,
        "empty_penalty":       empty_penalty,
        "score":               score,
    }


async def fetch_security(lat: float, lng: float, radius_m: int = 5_000) -> dict:
    """
    radius_m parametresi kabul edilir ama dahili olarak 2 km kullanılır.
    (Güvenlik mahalle ölçeğinde; 5 km çok geniş ve maxsize aşılır.)
    """
    key = _cache_key(lat, lng)
    if key in _cache:
        return _cache[key]

    circle_area_m2 = math.pi * _SECURITY_RADIUS_M ** 2
    query = _build_query(lat, lng)

    try:
        async with httpx.AsyncClient(timeout=28.0) as client:
            resp = await client.post(OVERPASS_URL, data={"data": query})
            resp.raise_for_status()
            osm = resp.json()

        if "remark" in osm and "exceeded" in str(osm.get("remark", "")).lower():
            raise RuntimeError(f"Overpass maxsize: {osm['remark']}")

        result = _parse(osm.get("elements", []), circle_area_m2)
        result["source"] = "OSM"

    except Exception as exc:
        result = {
            "lamp_count": 0, "commercial_count": 0, "empty_area_m2": 0,
            "lamp_density": 0, "commercial_density": 0,
            "lamp_score": 0, "commercial_score": 0, "empty_penalty": 0,
            "score": None,
            "source": "simüle",
            "error": str(exc),
        }

    _cache[key] = result
    return result
