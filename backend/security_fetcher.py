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
OSM KATMANLARI:

  Aydınlatma      : node["highway"="street_lamp"]
                    → lambalar/km²
                    Not: Türkiye'de kapsama oranı değişken;
                         düşük kapsama → muhafazakâr tahmin

  Ticari yoğunluk : node["shop"], node["amenity"]
                    way["landuse"="commercial|retail"]
                    → insan varlığı + gözetim proxy'si

  Issız alan      : way["landuse"="industrial|brownfield|wasteland|landfill"]
                    → toplam alanın %X'ini oluşturuyorsa penaltı

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
# Gerçek İstanbul merkezi: ~65 lamba/km², ~165 node/km²
# Referans = üst sınır → bu değerde skor=100 üretir
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


def _build_query(lat: float, lng: float, r: int) -> str:
    return f"""
[out:json][timeout:45][maxsize:10000000];
(
  node["highway"="street_lamp"](around:{r},{lat},{lng});
  node["shop"](around:{r},{lat},{lng});
  node["amenity"](around:{r},{lat},{lng});
  way["landuse"~"^(commercial|retail)$"](around:{r},{lat},{lng});
  way["landuse"~"^(industrial|brownfield|wasteland|landfill)$"](around:{r},{lat},{lng});
);
out geom qt;
""".strip()


def _parse(elements: list, circle_area_m2: float) -> dict:
    """
    Element listesini parse et → bileşen sayıları + alanlar.
    """
    lamp_count       = 0
    commercial_count = 0
    empty_area_m2    = 0.0

    seen: set = set()

    for el in elements:
        eid = (el.get("type"), el.get("id"))
        if eid in seen:
            continue
        seen.add(eid)

        tags = el.get("tags", {})
        t    = el.get("type")
        lu   = tags.get("landuse", "")

        if t == "node":
            if tags.get("highway") == "street_lamp":
                lamp_count += 1
            elif "shop" in tags or "amenity" in tags:
                commercial_count += 1

        elif t == "way":
            if lu in ("commercial", "retail"):
                # Ticari alan way'i → yaklaşık node eşdeğeri
                area = _polygon_area_m2(el.get("geometry", []))
                # Her 2500 m² ≈ 1 ticari node (küçük bir dükkan büyüklüğü)
                commercial_count += max(1, int(area / 2500))
            elif lu in ("industrial", "brownfield", "wasteland", "landfill"):
                empty_area_m2 += _polygon_area_m2(el.get("geometry", []))

    circle_area_km2  = circle_area_m2 / 1_000_000
    lamp_density     = lamp_count / circle_area_km2          # /km²
    commercial_density = commercial_count / circle_area_km2  # /km²
    empty_ratio      = min(empty_area_m2 / circle_area_m2, 1.0)

    # Bileşen skorları (0–100) — karekök ölçekleme
    # √(density/ref) * 100: az veri bölgelerinde daha adil, yoğun bölgelerde daha yavaş artar
    lamp_score       = round(min(math.sqrt(lamp_density / _LAMP_REF) * 100, 100), 1)
    commercial_score = round(min(math.sqrt(commercial_density / _COMMERCIAL_REF) * 100, 100), 1)
    empty_penalty    = round(empty_ratio * 100, 1)   # %100 boş → 100 puan penaltı

    # Ağırlıklı toplam
    raw = (
        lamp_score       * _W_LAMP
        + commercial_score * _W_COMMERCIAL
        - empty_penalty    * _W_EMPTY
    )
    score = round(max(5.0, min(95.0, raw)), 1)

    return {
        "lamp_count":        lamp_count,
        "commercial_count":  commercial_count,
        "empty_area_m2":     round(empty_area_m2),
        "lamp_density":      round(lamp_density, 2),
        "commercial_density":round(commercial_density, 2),
        "lamp_score":        lamp_score,
        "commercial_score":  commercial_score,
        "empty_penalty":     empty_penalty,
        "score":             score,
    }


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
    query = _build_query(lat, lng, radius_m)

    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(OVERPASS_URL, data={"data": query})
            resp.raise_for_status()
            osm = resp.json()

        # Overpass bazen HTTP 200 döndürür ama remark ile maxsize aşıldığını bildirir
        if "remark" in osm and "exceeded" in osm.get("remark", "").lower():
            raise RuntimeError(f"Overpass maxsize aşıldı: {osm['remark']}")

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
