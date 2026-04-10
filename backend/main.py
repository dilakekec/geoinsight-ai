from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import math, hashlib, random

app = FastAPI(title="GeoInsight AI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Schemas ──────────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    lat: float
    lng: float
    radius_km: float = 5.0

class MetricDetail(BaseModel):
    label: str
    value: float
    unit: str
    status: str          # good / moderate / poor

class AnalyzeResponse(BaseModel):
    lat: float
    lng: float
    environmental_score: float
    risk_score: float
    livability_score: float
    overall_score: float
    environmental_details: list[MetricDetail]
    risk_details: list[MetricDetail]
    livability_details: list[MetricDetail]
    summary: str
    recommendation: str

# ─── Deterministic pseudo-ML helper ───────────────────────────────────────────

def _seed_value(lat: float, lng: float, salt: str) -> float:
    """Repeatable float in [0, 1] for given coordinates + salt."""
    key = f"{round(lat, 3)}{round(lng, 3)}{salt}"
    digest = hashlib.md5(key.encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF

def _score(lat: float, lng: float, salt: str, base: float = 0.5, spread: float = 0.45) -> float:
    raw = _seed_value(lat, lng, salt)
    # Add slight sine-wave geographic variation
    geo_wave = (math.sin(lat * 0.3) * math.cos(lng * 0.2) + 1) / 2 * 0.15
    value = base + (raw - 0.5) * spread * 2 + geo_wave
    return round(min(max(value, 0.05), 0.99) * 100, 1)   # 0–100

def _status(score: float) -> str:
    if score >= 70: return "good"
    if score >= 45: return "moderate"
    return "poor"

# ─── Endpoint ─────────────────────────────────────────────────────────────────

@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest):
    lat, lng = req.lat, req.lng

    # ── Environmental ────────────────────────────────────────
    aqi        = _score(lat, lng, "aqi",      base=0.65)
    green_cov  = _score(lat, lng, "green",    base=0.55)
    water_qual = _score(lat, lng, "water",    base=0.60)
    noise_pol  = _score(lat, lng, "noise",    base=0.55)
    env_score  = round((aqi + green_cov + water_qual + noise_pol) / 4, 1)

    env_details = [
        MetricDetail(label="Hava Kalitesi (AQI)", value=aqi,
                     unit="puan", status=_status(aqi)),
        MetricDetail(label="Yeşil Alan Örtüsü",  value=green_cov,
                     unit="%",    status=_status(green_cov)),
        MetricDetail(label="Su Kalitesi",         value=water_qual,
                     unit="puan", status=_status(water_qual)),
        MetricDetail(label="Gürültü Kirliliği",   value=100 - noise_pol,
                     unit="dB indeks", status=_status(100 - noise_pol)),
    ]

    # ── Risk ────────────────────────────────────────────────
    flood_risk    = _score(lat, lng, "flood",   base=0.35)
    seismic_risk  = _score(lat, lng, "seismic", base=0.40)
    fire_risk     = _score(lat, lng, "fire",    base=0.30)
    infra_risk    = _score(lat, lng, "infra",   base=0.45)
    # Risk: lower raw → safer → higher safety score
    risk_score = round(100 - (flood_risk + seismic_risk + fire_risk + infra_risk) / 4, 1)

    risk_details = [
        MetricDetail(label="Sel Riski",          value=flood_risk,
                     unit="puan", status=_status(100 - flood_risk)),
        MetricDetail(label="Deprem Riski",        value=seismic_risk,
                     unit="puan", status=_status(100 - seismic_risk)),
        MetricDetail(label="Yangın Riski",        value=fire_risk,
                     unit="puan", status=_status(100 - fire_risk)),
        MetricDetail(label="Altyapı Güvenilirliği", value=infra_risk,
                     unit="puan", status=_status(infra_risk)),
    ]

    # ── Livability ──────────────────────────────────────────
    transport  = _score(lat, lng, "transport", base=0.60)
    education  = _score(lat, lng, "education", base=0.58)
    healthcare = _score(lat, lng, "health",    base=0.62)
    economy    = _score(lat, lng, "economy",   base=0.55)
    livability_score = round((transport + education + healthcare + economy) / 4, 1)

    liv_details = [
        MetricDetail(label="Ulaşım Erişimi",    value=transport,
                     unit="puan", status=_status(transport)),
        MetricDetail(label="Eğitim Kalitesi",   value=education,
                     unit="puan", status=_status(education)),
        MetricDetail(label="Sağlık Hizmetleri", value=healthcare,
                     unit="puan", status=_status(healthcare)),
        MetricDetail(label="Ekonomik Canlılık", value=economy,
                     unit="puan", status=_status(economy)),
    ]

    overall = round((env_score + risk_score + livability_score) / 3, 1)

    # ── Narrative ───────────────────────────────────────────
    if overall >= 70:
        summary = "Bu bölge, genel metriklere göre yüksek yaşam kalitesi sunmaktadır."
        recommendation = "Yatırım ve yerleşim için uygun. Çevresel değerlerin korunması önerilir."
    elif overall >= 50:
        summary = "Bölge orta düzeyde bir profil sergilemektedir; bazı metrikler iyileştirme gerektirmektedir."
        recommendation = "Riskler ve altyapı eksiklikleri değerlendirildikten sonra karar verilmesi önerilir."
    else:
        summary = "Bu bölgede belirgin riskler ve düşük yaşam kalitesi metrikleri tespit edilmiştir."
        recommendation = "Öncelikli iyileştirme müdahalesi veya alternatif bölge araştırması tavsiye edilir."

    return AnalyzeResponse(
        lat=lat, lng=lng,
        environmental_score=env_score,
        risk_score=risk_score,
        livability_score=livability_score,
        overall_score=overall,
        environmental_details=env_details,
        risk_details=risk_details,
        livability_details=liv_details,
        summary=summary,
        recommendation=recommendation,
    )


@app.get("/heatmap-points")
def heatmap_points():
    """Sample points for the demo heatmap layer."""
    random.seed(42)
    points = []
    for _ in range(120):
        lat = random.uniform(36.0, 42.0)
        lng = random.uniform(26.0, 45.0)
        intensity = _seed_value(lat, lng, "heat")
        points.append({"lat": round(lat, 4), "lng": round(lng, 4), "intensity": round(intensity, 3)})
    return {"points": points}


@app.get("/health")
def health():
    return {"status": "ok", "service": "GeoInsight AI"}
