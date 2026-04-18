"""
OSM Güvenlik Proxy Modeli

Doğrudan güvenlik istatistiği olmadığından, güvenlikle korelasyonu
kanıtlanmış kentsel göstergelerle proxy skor üretilir.

────────────────────────────────────────────────────────
FORMÜL:

  guvenlik = aydinlatma * 0.35
           + ticari_yogunluk * 0.45
           - issiz_alan_penaltisi * 0.20

────────────────────────────────────────────────────────
SORGU MİMARİSİ:

  Node sayıları için `out count;` kullanılır → sadece sayı döner (~200 byte).
  İstanbul gibi yoğun bölgelerde node["shop"] sorgusu 30.000+ sonuç verir;
  tam liste çekmek 30MB+ olur ve maxsize aşımı sessizce boş döner.

  3 paralel sorgu:
    1. node["highway"="street_lamp"] → out count  (lamba sayısı)
    2. node["shop"] + node["amenity"] → out count  (ticari yoğunluk)
    3. way["landuse"~commercial|retail|industrial...] → out geom  (alan hesabı)

────────────────────────────────────────────────────────
NORMALLEŞTIRME REFERANSları:

  Yoğun kentsel  → 60 lamba/km², 100 ticari node/km²  → ~90 puan
  Banliyö        → 20 lamba/km², 30 node/km²          → ~50 puan
  Kırsal         → 2 lamba/km², 3 node/km²            → ~15 puan
"""

import math
import httpx

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Skor 100 için referans yoğunluklar (birim/km²)  — karekök ölçekleme
_LAMP_REF       = 50.0    # 50 lamba/km²  (√ scaling)
_COMMERCIAL_REF = 100.0   # 100 node/km²  (√ scaling)

# Ağırlıklar
_W_LAMP       = 0.35
_W_COMMERCIAL = 0.45
_W_EMPTY      = 0.20   # penalty weight

_cache: dict = {}


def _cache_key(lat: float, lng: float, r: int) -> tuple:
    return (round(lat, 2), round(lng, 2), r)


def _polygon_area_m2(nodes: list[dict]) -> float:
    """Shoelace formülü → m²."""
    if len(nodes) < 3:
        return 0.0
    lat0 = nodes[0]["lat"]
    lng0 = nodes[0]["lon"]
    lat_m = 111_320.0
    lng_m = 111_320.0 * math.cos(math.radians(lat0))
    xs = [(n["lon"] - lng0) * lng_m for n in nodes]
    ys = [(n["lat"] - lat0) * lat_m for n in nodes]
    n = len(xs)
    area = sum(xs[i] * ys[(i+1) % n] - xs[(i+1) % n] * ys[i] for i in range(n))
    return abs(area) / 2.0


# ── 3 ayrı hafif sorgu ────────────────────────────────────────────────────────

def _q_lamps(lat: float, lng: float, r: int) -> str:
    """Sadece lamba sayısı — out count döner (~200 byte)."""
    return (
        f'[out:json][timeout:30];'
        f'node["highway"="street_lamp"](around:{r},{lat},{lng});'
        f'out count;'
    )


def _q_commercial(lat: float, lng: float, r: int) -> str:
    """Shop + amenity node sayısı — out count döner (~200 byte)."""
    return (
        f'[out:json][timeout:30];'
        f'(node["shop"](around:{r},{lat},{lng});'
        f'node["amenity"](around:{r},{lat},{lng}););'
        f'out count;'
    )


def _q_ways(lat: float, lng: float, r: int) -> str:
    """Ticari ve ıssız alan wayları — geometri gerekli, ama sayı az."""
    return (
        f'[out:json][timeout:30][maxsize:4000000];'
        f'(way["landuse"~"^(commercial|retail)$"](around:{r},{lat},{lng});'
        f'way["landuse"~"^(industrial|brownfield|wasteland|landfill)$"](around:{r},{lat},{lng}););'
        f'out geom qt;'
    )


def _parse_count(data: dict) -> int:
    """Overpass `out count;` sonucundan toplam sayıyı çıkar."""
    for el in data.get("elements", []):
        if el.get("type") == "count":
            return int(el.get("tags", {}).get("total", 0))
    return 0


def _parse_ways(elements: list) -> tuple[int, float]:
    """
    Way listesinden:
    - commercial_extra: ticari alan yüzey × 1/2500 m² (node eşdeğeri)
    - empty_area_m2: ıssız/sanayi alan m²
    """
    commercial_extra = 0
    empty_area_m2    = 0.0
    seen: set = set()

    for el in elements:
        eid = (el.get("type"), el.get("id"))
        if eid in seen:
            continue
        seen.add(eid)
        if el.get("type") != "way":
            continue

        lu   = el.get("tags", {}).get("landuse", "")
        geom = el.get("geometry", [])
        area = _polygon_area_m2(geom)

        if lu in ("commercial", "retail"):
            commercial_extra += max(1, int(area / 2500))
        elif lu in ("industrial", "brownfield", "wasteland", "landfill"):
            empty_area_m2 += area

    return commercial_extra, empty_area_m2


def _score(lamp_count: int, commercial_count: int,
           empty_area_m2: float, circle_area_m2: float) -> dict:
    circle_area_km2    = circle_area_m2 / 1_000_000
    lamp_density       = lamp_count / circle_area_km2
    commercial_density = commercial_count / circle_area_km2
    empty_ratio        = min(empty_area_m2 / circle_area_m2, 1.0)

    lamp_score       = round(min(math.sqrt(lamp_density / _LAMP_REF) * 100, 100), 1)
    commercial_score = round(min(math.sqrt(commercial_density / _COMMERCIAL_REF) * 100, 100), 1)
    empty_penalty    = round(empty_ratio * 100, 1)

    raw = (
        lamp_score       * _W_LAMP
        + commercial_score * _W_COMMERCIAL
        - empty_penalty    * _W_EMPTY
    )
    score = round(max(5.0, min(95.0, raw)), 1)

    return {
        "lamp_count":          lamp_count,
        "commercial_count":    commercial_count,
        "empty_area_m2":       round(empty_area_m2),
        "lamp_density":        round(lamp_density, 2),
        "commercial_density":  round(commercial_density, 2),
        "lamp_score":          lamp_score,
        "commercial_score":    commercial_score,
        "empty_penalty":       empty_penalty,
        "score":               score,
    }


async def _post(client: httpx.AsyncClient, query: str) -> dict:
    resp = await client.post(OVERPASS_URL, data={"data": query})
    resp.raise_for_status()
    return resp.json()


async def fetch_security(lat: float, lng: float, radius_m: int = 5_000) -> dict:
    """
    Döner:
    {
      "lamp_count"      : int,
      "commercial_count": int,
      "empty_area_m2"   : float,
      "lamp_score"      : float,
      "commercial_score": float,
      "empty_penalty"   : float,
      "score"           : float,   # 0–100 (None → hata)
      "source"          : "OSM" | "simüle"
    }
    """
    key = _cache_key(lat, lng, radius_m)
    if key in _cache:
        return _cache[key]

    circle_area_m2 = math.pi * radius_m ** 2

    try:
        # Sıralı istek: 5 eş zamanlı Overpass isteğinden kaçın (rate-limit)
        # Count sorguları çok hızlı (~<1s) → toplam süreye etkisi minimal
        async with httpx.AsyncClient(timeout=35.0) as client:
            lamp_data  = await _post(client, _q_lamps(lat, lng, radius_m))
            comm_data  = await _post(client, _q_commercial(lat, lng, radius_m))
            ways_data  = await _post(client, _q_ways(lat, lng, radius_m))

        lamp_count        = _parse_count(lamp_data)
        commercial_nodes  = _parse_count(comm_data)
        comm_extra, empty = _parse_ways(ways_data.get("elements", []))
        commercial_count  = commercial_nodes + comm_extra

        result = _score(lamp_count, commercial_count, empty, circle_area_m2)
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
