"""
Grid Motoru — Haritayı hücrelere böl, tek Overpass sorgusunda toplu skor üret.

NEDEN BU YAKLAŞIM?
  ✗ Kötü : Her hücre için ayrı Overpass sorgusu → N×API çağrısı, rate limit
  ✓ İyi  : Tek bbox sorgusu → Python'da feature'ları hücrelere dağıt

AKIŞ:
  1. make_grid(bbox, cell_size_m) → hücre ızgarası (row, col koordinatları)
  2. fetch_osm_for_bbox(bbox)     → tek Overpass sorgusu, tüm layer'lar
  3. assign_features(elements)    → her feature'ı hangi hücreye ait, hesapla
  4. score_all_cells()            → her hücre için yeşil / güvenlik / ulaşım skoru
  5. to_geojson()                 → Leaflet'e hazır GeoJSON FeatureCollection

HÜCRELERİN SINIRLAMASI:
  Maksimum bbox: ~0.45° × 0.65° (~50km × ~55km) → ~2500 hücre
  Daha büyük bbox → HTTP 400, frontend zoom in ister

ROAD UZUNLUĞU DAĞITIMI:
  Her way segment'inin midpoint'i hesaplanır → o hücreye atanır
  Böylece 10 hücre kesen 10km yol → 10 hücreye 1'er km dağıtılır
"""

import math
import asyncio
import httpx
from functools import lru_cache

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# 1 saatlik bbox cache (key: rounded bbox + size)
_grid_cache: dict = {}
_GRID_TTL = 3600

# ── Sabitler ──────────────────────────────────────────────────────────────────
_GREEN_LU  = {"park", "forest", "grass", "meadow"}
_GREEN_LE  = {"park", "garden", "nature_reserve"}
_GREEN_NAT = {"wood", "scrub", "heath"}
_EMPTY_LU  = {"industrial", "brownfield", "wasteland", "landfill", "quarry"}
_COMM_LU   = {"commercial", "retail"}
_ARTERY_HW = {"motorway", "trunk", "primary"}
_LOCAL_HW  = {"secondary", "tertiary", "residential", "unclassified", "living_street"}
_ALL_HW    = _ARTERY_HW | _LOCAL_HW


# ── Yardımcı geometri ─────────────────────────────────────────────────────────
def _dist_m(n1: dict, n2: dict) -> float:
    """İki node arasındaki mesafe (m)."""
    dlat = (n2["lat"] - n1["lat"]) * 111_320.0
    dlng = (n2["lon"] - n1["lon"]) * 111_320.0 * math.cos(
        math.radians((n1["lat"] + n2["lat"]) / 2)
    )
    return math.sqrt(dlat**2 + dlng**2)


def _polygon_area_m2(nodes: list[dict]) -> float:
    """Shoelace → m²."""
    if len(nodes) < 3:
        return 0.0
    lat0, lng0 = nodes[0]["lat"], nodes[0]["lon"]
    lm = 111_320.0
    lnm = 111_320.0 * math.cos(math.radians(lat0))
    xs = [(n["lon"] - lng0) * lnm for n in nodes]
    ys = [(n["lat"] - lat0) * lm  for n in nodes]
    n = len(xs)
    area = sum(xs[i]*ys[(i+1)%n] - xs[(i+1)%n]*ys[i] for i in range(n))
    return abs(area) / 2.0


def _centroid(nodes: list[dict]) -> tuple[float, float]:
    lats = [n["lat"] for n in nodes]
    lngs = [n["lon"] for n in nodes]
    return sum(lats)/len(lats), sum(lngs)/len(lngs)


# ── Grid Sınıfı ───────────────────────────────────────────────────────────────
class GridScorer:
    def __init__(self, bbox: dict, cell_size_m: int = 1000):
        self.bbox        = bbox
        self.cell_size_m = cell_size_m

        # Derece başına metre (bbox merkezi referans)
        clat = (bbox["minlat"] + bbox["maxlat"]) / 2
        self.lat_step = cell_size_m / 111_320.0
        self.lng_step = cell_size_m / (111_320.0 * math.cos(math.radians(clat)))

        self.n_rows = max(1, math.ceil((bbox["maxlat"] - bbox["minlat"]) / self.lat_step))
        self.n_cols = max(1, math.ceil((bbox["maxlng"] - bbox["minlng"]) / self.lng_step))

        # Hücre veri deposu: (row, col) → dict
        self._cells: dict = {}

    # ── Hücre indeksi ──────────────────────────────────────────────────────────
    def _idx(self, lat: float, lng: float) -> tuple | None:
        row = int((lat - self.bbox["minlat"]) / self.lat_step)
        col = int((lng - self.bbox["minlng"]) / self.lng_step)
        if 0 <= row < self.n_rows and 0 <= col < self.n_cols:
            return (row, col)
        return None

    def _cell(self, row: int, col: int) -> dict:
        key = (row, col)
        if key not in self._cells:
            self._cells[key] = {
                "lamp":       0,
                "commerce":   0,
                "green_m2":   0.0,
                "empty_m2":   0.0,
                "road_m":     0.0,
                "artery_m":   0.0,
            }
        return self._cells[key]

    # ── Feature atama ─────────────────────────────────────────────────────────
    def add_node(self, lat: float, lng: float, tags: dict) -> None:
        idx = self._idx(lat, lng)
        if not idx:
            return
        c = self._cell(*idx)
        if tags.get("highway") == "street_lamp":
            c["lamp"] += 1
        elif "shop" in tags or "amenity" in tags:
            c["commerce"] += 1

    def add_way(self, nodes: list[dict], tags: dict) -> None:
        if not nodes:
            return
        hw  = tags.get("highway", "")
        lu  = tags.get("landuse",  "")
        le  = tags.get("leisure",  "")
        nat = tags.get("natural",  "")

        # ── Yollar: segment midpoint → hücre ──────────────────────────────────
        if hw in _ALL_HW:
            is_artery = hw in _ARTERY_HW
            for i in range(len(nodes) - 1):
                n1, n2  = nodes[i], nodes[i + 1]
                seg_len = _dist_m(n1, n2)
                mlat    = (n1["lat"] + n2["lat"]) / 2
                mlng    = (n1["lon"] + n2["lon"]) / 2
                idx = self._idx(mlat, mlng)
                if idx:
                    c = self._cell(*idx)
                    c["road_m"] += seg_len
                    if is_artery:
                        c["artery_m"] += seg_len
            return

        # ── Yeşil alanlar: centroid → hücre ───────────────────────────────────
        if lu in _GREEN_LU or le in _GREEN_LE or nat in _GREEN_NAT:
            area = _polygon_area_m2(nodes)
            if area > 0:
                clat, clng = _centroid(nodes)
                idx = self._idx(clat, clng)
                if idx:
                    self._cell(*idx)["green_m2"] += area
            return

        # ── Ticari alanlar: centroid + alan→node dönüşümü ──────────────────────
        if lu in _COMM_LU:
            area = _polygon_area_m2(nodes)
            clat, clng = _centroid(nodes)
            idx = self._idx(clat, clng)
            if idx:
                self._cell(*idx)["commerce"] += max(1, int(area / 2500))
            return

        # ── Issız / sanayi alanlar ─────────────────────────────────────────────
        if lu in _EMPTY_LU:
            area = _polygon_area_m2(nodes)
            if area > 0:
                clat, clng = _centroid(nodes)
                idx = self._idx(clat, clng)
                if idx:
                    self._cell(*idx)["empty_m2"] += area

    # ── Skorlama ──────────────────────────────────────────────────────────────
    def score_all(self) -> list[dict]:
        cell_m2   = self.cell_size_m ** 2
        cell_km2  = cell_m2 / 1_000_000
        results   = []

        for (row, col), d in self._cells.items():
            # Normalize yoğunluklar
            lamp_dens   = d["lamp"]      / cell_km2
            comm_dens   = d["commerce"]  / cell_km2
            road_dens   = d["road_m"]    / 1000 / cell_km2   # km/km²
            artery_dens = d["artery_m"]  / 1000 / cell_km2
            green_ratio = min(d["green_m2"] / cell_m2, 1.0)
            empty_ratio = min(d["empty_m2"] / cell_m2, 1.0)

            # ── Yeşillik skoru ──────────────────────────────────────────────
            green = round(min(math.sqrt(green_ratio / 0.40) * 100, 100), 1)

            # ── Güvenlik skoru ──────────────────────────────────────────────
            lamp_s  = min(math.sqrt(lamp_dens  / 50)  * 100, 100)
            comm_s  = min(math.sqrt(comm_dens  / 100) * 100, 100)
            empty_p = empty_ratio * 100
            security = round(max(5.0, min(95.0,
                lamp_s * 0.35 + comm_s * 0.45 - empty_p * 0.20)), 1)

            # ── Ulaşım skoru ───────────────────────────────────────────────
            local_s  = min(math.sqrt(road_dens   / 12) * 100, 100)
            artery_s = min(math.sqrt(artery_dens /  4) * 100, 100)
            transport = round(max(5.0, min(95.0,
                local_s * 0.60 + artery_s * 0.40)), 1)

            overall = round((green + security + transport) / 3, 1)

            # Hücre merkez koordinatları
            clat = self.bbox["minlat"] + (row + 0.5) * self.lat_step
            clng = self.bbox["minlng"] + (col + 0.5) * self.lng_step

            results.append({
                "lat": round(clat, 5),
                "lng": round(clng, 5),
                "row": row,
                "col": col,
                "overall":   overall,
                "green":     green,
                "security":  security,
                "transport": transport,
                # Raw veriler (debug / detay panel için)
                "lamp":      d["lamp"],
                "commerce":  d["commerce"],
                "road_km":   round(d["road_m"] / 1000, 2),
            })

        return results

    # ── GeoJSON ──────────────────────────────────────────────────────────────
    def to_geojson(self, scored: list[dict]) -> dict:
        features = []
        hs = self.lat_step / 2
        hw = self.lng_step / 2

        for cell in scored:
            lat, lng = cell["lat"], cell["lng"]
            coords = [[
                [lng - hw, lat - hs],
                [lng + hw, lat - hs],
                [lng + hw, lat + hs],
                [lng - hw, lat + hs],
                [lng - hw, lat - hs],
            ]]
            features.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": coords},
                "properties": {
                    "lat":      lat,
                    "lng":      lng,
                    "overall":  cell["overall"],
                    "green":    cell["green"],
                    "security": cell["security"],
                    "transport":cell["transport"],
                    "lamp":     cell["lamp"],
                    "commerce": cell["commerce"],
                    "road_km":  cell["road_km"],
                },
            })

        return {"type": "FeatureCollection", "features": features}


# ── Overpass sorgusu ──────────────────────────────────────────────────────────
def _build_bbox_query(bbox: dict) -> str:
    b = f'{bbox["minlat"]},{bbox["minlng"]},{bbox["maxlat"]},{bbox["maxlng"]}'
    hw_types = "|".join(_ALL_HW)
    return f"""
[out:json][timeout:55][maxsize:20000000];
(
  node["highway"="street_lamp"]({b});
  node["shop"]({b});
  node["amenity"]({b});
  way["landuse"~"^(park|forest|grass|meadow|commercial|retail|industrial|brownfield|wasteland|landfill)$"]({b});
  way["leisure"~"^(park|garden|nature_reserve)$"]({b});
  way["natural"~"^(wood|scrub|heath)$"]({b});
  way["highway"~"^({hw_types})$"]({b});
);
out geom qt;
""".strip()


async def fetch_and_score_grid(
    bbox: dict,
    cell_size_m: int = 1000,
) -> dict:
    """
    Tüm pipeline:
      bbox + cell_size → Overpass sorgusu → hücre atama → skorlama → GeoJSON

    Döner: GeoJSON FeatureCollection | {"error": str}
    """
    # Hücre sayısı kontrolü
    scorer   = GridScorer(bbox, cell_size_m)
    n_cells  = scorer.n_rows * scorer.n_cols
    if n_cells > 3000:
        return {"error": f"Çok fazla hücre ({n_cells}). Haritayı yakınlaştırın."}

    # Cache kontrol
    import time
    ck  = (round(bbox["minlat"],2), round(bbox["minlng"],2),
           round(bbox["maxlat"],2), round(bbox["maxlng"],2), cell_size_m)
    now = time.time()
    if ck in _grid_cache and (now - _grid_cache[ck]["ts"]) < _GRID_TTL:
        return _grid_cache[ck]["data"]

    query = _build_bbox_query(bbox)

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(OVERPASS_URL, data={"data": query})
            resp.raise_for_status()
            osm = resp.json()
    except Exception as exc:
        return {"error": f"Overpass sorgusu başarısız: {exc}"}

    # Feature dağıtımı
    for el in osm.get("elements", []):
        tags = el.get("tags", {})
        if el["type"] == "node":
            scorer.add_node(el.get("lat", 0), el.get("lon", 0), tags)
        elif el["type"] == "way":
            scorer.add_way(el.get("geometry", []), tags)

    scored  = scorer.score_all()
    geojson = scorer.to_geojson(scored)

    # İstatistik ekle
    overalls = [c["overall"] for c in scored]
    geojson["meta"] = {
        "cell_count":  len(scored),
        "cell_size_m": cell_size_m,
        "grid_rows":   scorer.n_rows,
        "grid_cols":   scorer.n_cols,
        "osm_elements":len(osm.get("elements", [])),
        "score_min":   round(min(overalls), 1) if overalls else 0,
        "score_max":   round(max(overalls), 1) if overalls else 0,
        "score_avg":   round(sum(overalls) / len(overalls), 1) if overalls else 0,
    }

    _grid_cache[ck] = {"data": geojson, "ts": now}
    return geojson
