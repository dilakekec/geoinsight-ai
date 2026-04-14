"""
OSM Trafik / Ulaşım Yoğunluğu Modeli

Ulaşım erişiminin proxy'si olarak yol ağı yoğunluğu ve
ana arterler kullanılır.

────────────────────────────────────────────────────────
FORMÜL:

  ulasim = yerel_yol_skoru * 0.60 + arter_skoru * 0.40

  yerel_yol_skoru: primary/secondary/tertiary/residential
                   → mahalleye erişim kalitesi
  arter_skoru    : motorway/trunk
                   → şehirlerarası bağlantı + büyük arteri erişimi

────────────────────────────────────────────────────────
OSM YOL HİYERARŞİSİ:

  Arterler   : highway=motorway, trunk          → bölgesel erişim
  Birincil   : highway=primary                  → şehir ana yolları
  İkincil    : highway=secondary, tertiary      → semt yolları
  Yerel      : highway=residential, unclassified→ konut yolları

────────────────────────────────────────────────────────
NORMALLEŞTIRME (√ ölçek):

  Yoğun kentsel : yerel ~12 km/km², arter ~4 km/km²  → ~80 puan
  Banliyö       : yerel ~5 km/km²,  arter ~1 km/km²  → ~50 puan
  Kırsal        : yerel ~0.5 km/km², arter ~0.1 km/km²→ ~15 puan

  Referans: yerel=12 km/km² → yerel_skor=100
            arter=4 km/km²  → arter_skor=100
"""

import math
import httpx

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Skor 100 için referans yoğunluklar (km/km²)
_LOCAL_REF  = 12.0   # yerel yol yoğunluğu
_ARTERY_REF =  4.0   # ana arter yoğunluğu

# Ağırlıklar
_W_LOCAL  = 0.60
_W_ARTERY = 0.40

# Yol sınıflandırması
_ARTERY = {"motorway", "trunk"}
_PRIMARY = {"primary"}
_LOCAL  = {"secondary", "tertiary", "residential", "unclassified", "living_street"}

_cache: dict = {}


def _cache_key(lat: float, lng: float, r: int) -> tuple:
    return (round(lat, 2), round(lng, 2), r)


def _way_length_m(nodes: list[dict]) -> float:
    """Way geometrisinden toplam uzunluk (m)."""
    if len(nodes) < 2:
        return 0.0
    total = 0.0
    for i in range(len(nodes) - 1):
        lat1, lon1 = nodes[i]["lat"],   nodes[i]["lon"]
        lat2, lon2 = nodes[i+1]["lat"], nodes[i+1]["lon"]
        dlat = (lat2 - lat1) * 111_320.0
        dlng = (lon2 - lon1) * 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2))
        total += math.sqrt(dlat**2 + dlng**2)
    return total


def _build_query(lat: float, lng: float, r: int) -> str:
    types = "|".join(_ARTERY | _PRIMARY | _LOCAL)
    return f"""
[out:json][timeout:30][maxsize:4000000];
way["highway"~"^({types})$"](around:{r},{lat},{lng});
out geom qt;
""".strip()


def _parse(elements: list, circle_area_m2: float) -> dict:
    artery_m  = 0.0   # motorway + trunk
    primary_m = 0.0   # primary
    local_m   = 0.0   # secondary + tertiary + residential + ...
    seen: set = set()

    for el in elements:
        if el.get("type") != "way":
            continue
        eid = el.get("id")
        if eid in seen:
            continue
        seen.add(eid)

        hw    = el.get("tags", {}).get("highway", "")
        nodes = el.get("geometry", [])
        length = _way_length_m(nodes)

        if hw in _ARTERY:
            artery_m  += length
        elif hw in _PRIMARY:
            primary_m += length
        elif hw in _LOCAL:
            local_m   += length

    total_m         = artery_m + primary_m + local_m
    circle_area_km2 = circle_area_m2 / 1_000_000

    # km/km²
    artery_density  = (artery_m  + primary_m) / 1000 / circle_area_km2
    local_density   = (local_m   + primary_m) / 1000 / circle_area_km2
    total_density   = total_m / 1000 / circle_area_km2

    # Bileşen skorları — karekök ölçekleme
    local_score  = round(min(math.sqrt(local_density  / _LOCAL_REF)  * 100, 100), 1)
    artery_score = round(min(math.sqrt(artery_density / _ARTERY_REF) * 100, 100), 1)

    # Kombine ulaşım skoru
    raw   = local_score * _W_LOCAL + artery_score * _W_ARTERY
    score = round(max(5.0, min(95.0, raw)), 1)

    return {
        "total_road_km":    round(total_m / 1000, 1),
        "artery_km":        round((artery_m + primary_m) / 1000, 1),
        "local_km":         round(local_m / 1000, 1),
        "total_density":    round(total_density, 2),   # km/km²
        "artery_density":   round(artery_density, 2),
        "local_density":    round(local_density, 2),
        "local_score":      local_score,
        "artery_score":     artery_score,
        "score":            score,
        "way_count":        len(seen),
    }


async def fetch_traffic(lat: float, lng: float, radius_m: int = 5_000) -> dict:
    """
    Döner:
    {
      "total_road_km" : float,
      "artery_km"     : float,
      "total_density" : float,   # km/km²
      "artery_density": float,   # km/km²
      "local_score"   : float,   # 0–100
      "artery_score"  : float,   # 0–100
      "score"         : float,   # 0–100 ulaşım skoru (None → hata)
      "way_count"     : int,
      "source"        : "OSM" | "simüle"
    }
    """
    key = _cache_key(lat, lng, radius_m)
    if key in _cache:
        return _cache[key]

    circle_area_m2 = math.pi * radius_m ** 2
    query = _build_query(lat, lng, radius_m)

    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(OVERPASS_URL, data={"data": query})
            resp.raise_for_status()
            osm = resp.json()

        result = _parse(osm.get("elements", []), circle_area_m2)
        result["source"] = "OSM"

    except Exception as exc:
        result = {
            "total_road_km": 0, "artery_km": 0, "local_km": 0,
            "total_density": 0, "artery_density": 0, "local_density": 0,
            "local_score": 0, "artery_score": 0,
            "score": None,
            "way_count": 0,
            "source": "simüle",
            "error": str(exc),
        }

    _cache[key] = result
    return result
