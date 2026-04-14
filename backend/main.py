from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio, math, hashlib, random, os

from data_loader import load_all, nufus_data, egitim_data
from geocoder import reverse_geocode
from osm_fetcher import fetch_green_coverage
from kira_loader import kira_data
from aqi_fetcher import fetch_aqi, _status_pm25
from security_fetcher import fetch_security
from traffic_fetcher import fetch_traffic


# ── Uygulama başlangıcında TÜİK verilerini yükle ─────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    load_all()
    kira_data.load()
    yield


app = FastAPI(title="GeoInsight AI", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Schemas ───────────────────────────────────────────────────────────────────
class AnalyzeRequest(BaseModel):
    lat: float
    lng: float
    radius_km: float = 5.0

class MetricDetail(BaseModel):
    label: str
    value: float
    unit: str
    status: str
    source: str = "simüle"   # "TÜİK" veya "simüle"

class AnalyzeResponse(BaseModel):
    lat: float
    lng: float
    province: str | None = None
    environmental_score: float
    risk_score: float
    livability_score: float
    overall_score: float
    environmental_details: list[MetricDetail]
    risk_details: list[MetricDetail]
    livability_details: list[MetricDetail]
    summary: str
    recommendation: str
    data_note: str


# ── Mock yardımcıları ─────────────────────────────────────────────────────────
def _seed(lat: float, lng: float, salt: str) -> float:
    key = f"{round(lat, 3)}{round(lng, 3)}{salt}"
    digest = hashlib.md5(key.encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF

def _mock_score(lat: float, lng: float, salt: str,
                base: float = 0.5, spread: float = 0.45) -> float:
    raw      = _seed(lat, lng, salt)
    geo_wave = (math.sin(lat * 0.3) * math.cos(lng * 0.2) + 1) / 2 * 0.15
    value    = base + (raw - 0.5) * spread * 2 + geo_wave
    return round(min(max(value, 0.05), 0.99) * 100, 1)

def _status(score: float) -> str:
    if score >= 70: return "good"
    if score >= 45: return "moderate"
    return "poor"

def _blend(real: float | None, mock: float, weight: float = 0.65) -> float:
    """Gerçek veri varsa ağırlıklı karıştır (real * weight + mock * (1-weight))."""
    if real is None:
        return mock
    return round(real * weight + mock * (1 - weight), 1)


# ── Ana analiz endpoint'i ─────────────────────────────────────────────────────
@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze(req: AnalyzeRequest):
    lat, lng = req.lat, req.lng

    # 1. Tüm harici API'leri paralel çek (gecikme toplam değil maksimum olur)
    radius_m = int(req.radius_km * 1000)
    geo, osm, aqi_data, sec, trf = await asyncio.gather(
        reverse_geocode(lat, lng),
        fetch_green_coverage(lat, lng, radius_m),
        fetch_aqi(lat, lng),
        fetch_security(lat, lng, radius_m),
        fetch_traffic(lat, lng, radius_m),
    )

    province  = geo.get("province") or None
    district  = geo.get("district") or ""
    il_data   = nufus_data.get(province) if province else None
    egitim_il = egitim_data.get(province) if province else None
    kira_il   = kira_data.get(province, district) if province else None

    # Veri kaynağı notu
    sources = []
    if aqi_data["source"] != "simüle":
        sources.append(
            f"hava kalitesi ({aqi_data['source']} · AQI {aqi_data['aqi']} · "
            f"PM2.5 {aqi_data['pm25']} µg/m³)"
        )
    if trf["source"] == "OSM":
        sources.append(
            f"ulaşım (OSM · {trf['total_road_km']} km yol · "
            f"{trf['total_density']} km/km²)"
        )
    if sec["source"] == "OSM":
        sources.append(
            f"güvenlik proxy (OSM · {sec['lamp_count']} lamba · "
            f"{sec['commercial_count']} ticari node)"
        )
    if osm["source"] == "OSM":
        sources.append(
            f"yeşillik (OSM · {osm['feature_count']} alan · "
            f"%{round(osm['coverage_ratio']*100,1)} örtü)"
        )
    if il_data:
        sources.append("nüfus/ekonomi (TÜİK ADNKS 2025)")
    if egitim_il:
        sources.append("eğitim/okuryazarlık (TÜİK 2021)")
    if kira_il:
        kira_scope = kira_il["ilce"] if kira_il["ilce"] else kira_il["il"]
        sources.append(f"kira/konut (Endeksa 2024 · {kira_scope})")

    if sources:
        data_note = (
            ("" if not province else f"{province} · ")
            + "Gerçek veri: " + ", ".join(sources)
            + ". Diğer metrikler simüle edilmiştir."
        )
    else:
        data_note = "Tüm metrikler simüle edilmiştir (gerçek veri değildir)."

    # ── Çevresel ─────────────────────────────────────────────────────────────
    aqi_mock       = _mock_score(lat, lng, "aqi",   base=0.65)
    green_mock     = _mock_score(lat, lng, "green", base=0.55)
    water_mock     = _mock_score(lat, lng, "water", base=0.60)
    noise_mock     = _mock_score(lat, lng, "noise", base=0.55)

    # Yeşil alan: OSM gerçek veri varsa kullan, yoksa nüfus yoğunluğu tahminine düş
    if osm["score"] is not None:
        green_cov = _blend(osm["score"], green_mock, weight=0.80)
        green_src = "OSM"
    elif il_data:
        cevre_penalty = il_data["cevre_penalty"]
        green_real    = round(max(100 - cevre_penalty * 0.5, 15), 1)
        green_cov     = _blend(green_real, green_mock)
        green_src     = "TÜİK + simüle"
    else:
        green_cov = green_mock
        green_src = "simüle"

    # Hava kalitesi: IQAir/OWM gerçek veri → en güçlü sinyal
    if aqi_data["score"] is not None:
        aqi     = _blend(aqi_data["score"], aqi_mock, weight=0.90)
        aqi_src = aqi_data["source"]
    elif il_data:
        # Gerçek API yoksa nüfus yoğunluğundan tahmin
        cevre_penalty = il_data["cevre_penalty"]
        aqi_real      = round(max(100 - cevre_penalty * 0.6, 20), 1)
        aqi           = _blend(aqi_real, aqi_mock)
        aqi_src       = "TÜİK + simüle"
    else:
        aqi     = aqi_mock
        aqi_src = "simüle"

    water_qual = water_mock
    noise_pol  = noise_mock
    env_score  = round((aqi + green_cov + water_qual + noise_pol) / 4, 1)

    # Yeşil alan birim: OSM varsa "puan (OSM)" göster, yoksa skor
    green_label = "Yeşil Alan Örtüsü"
    green_unit  = "puan"
    if osm["source"] == "OSM":
        green_unit = f"puan · {round(osm['green_area_m2']/1_000_000, 2)} km²"

    env_details = [
        MetricDetail(label="Hava Kalitesi (AQI)", value=aqi,
                     unit="puan", status=_status(aqi),       source=aqi_src),
        MetricDetail(label=green_label,            value=green_cov,
                     unit=green_unit, status=_status(green_cov), source=green_src),
        MetricDetail(label="Su Kalitesi",         value=water_qual,
                     unit="puan", status=_status(water_qual), source="simüle"),
        MetricDetail(label="Gürültü Kirliliği",   value=round(100 - noise_pol, 1),
                     unit="dB indeks", status=_status(100 - noise_pol), source="simüle"),
    ]

    # PM2.5 gerçek konsantrasyon — API varsa ekle
    if aqi_data["pm25"] is not None:
        aqi_label = f"AQI {aqi_data['aqi']}" if aqi_data["aqi"] else "PM2.5"
        env_details.append(MetricDetail(
            label=f"PM2.5 ({aqi_label})",
            value=aqi_data["pm25"],
            unit="µg/m³",
            status=_status_pm25(aqi_data["pm25"]),
            source=aqi_data["source"],
        ))

    # ── Risk ──────────────────────────────────────────────────────────────────
    flood_risk   = _mock_score(lat, lng, "flood",   base=0.35)
    seismic_risk = _mock_score(lat, lng, "seismic", base=0.40)
    fire_risk    = _mock_score(lat, lng, "fire",    base=0.30)
    infra_risk   = _mock_score(lat, lng, "infra",   base=0.45)

    # Güvenlik: OSM proxy (lamba + ticari + issiz alan) ya da mock
    if sec["score"] is not None:
        security_score = _blend(sec["score"], _mock_score(lat, lng, "security", base=0.55), weight=0.80)
        security_src   = "OSM proxy"
    else:
        security_score = _mock_score(lat, lng, "security", base=0.55)
        security_src   = "simüle"

    # Risk skoru: natural hazards inverse + altyapı + güvenlik
    risk_score = round(
        (100 - flood_risk + 100 - seismic_risk + 100 - fire_risk
         + infra_risk + security_score) / 5,
        1
    )

    risk_details = [
        MetricDetail(label="Sel Riski",              value=flood_risk,
                     unit="puan", status=_status(100 - flood_risk),   source="simüle"),
        MetricDetail(label="Deprem Riski",            value=seismic_risk,
                     unit="puan", status=_status(100 - seismic_risk), source="simüle"),
        MetricDetail(label="Yangın Riski",            value=fire_risk,
                     unit="puan", status=_status(100 - fire_risk),    source="simüle"),
        MetricDetail(label="Altyapı Güvenilirliği",  value=infra_risk,
                     unit="puan", status=_status(infra_risk),         source="simüle"),
        MetricDetail(label="Güvenlik Endeksi",        value=security_score,
                     unit="puan", status=_status(security_score),     source=security_src),
    ]

    # OSM güvenlik bileşen detayları
    if sec["source"] == "OSM":
        risk_details.append(MetricDetail(
            label="Aydınlatma Yoğunluğu",
            value=round(sec["lamp_density"], 1),
            unit="lamba/km²",
            status=_status(sec["lamp_score"]),
            source="OSM",
        ))
        risk_details.append(MetricDetail(
            label="Ticari Yoğunluk",
            value=round(sec["commercial_density"], 1),
            unit="node/km²",
            status=_status(sec["commercial_score"]),
            source="OSM",
        ))

    # ── Yaşam Uygunluğu ───────────────────────────────────────────────────────
    transport_mock  = _mock_score(lat, lng, "transport", base=0.60)
    education_mock  = _mock_score(lat, lng, "education", base=0.58)
    healthcare_mock = _mock_score(lat, lng, "health",    base=0.62)
    economy_mock    = _mock_score(lat, lng, "economy",   base=0.55)

    # Ekonomi: kira verisi en güçlü sinyal; yoksa nüfus yoğunluğu; yoksa mock
    if kira_il:
        # Kira skoru %70 + nüfus skoru %15 + mock %15
        nufus_econ = il_data["ekonomi_score"] if il_data else economy_mock
        economy     = round(
            kira_il["ekonomi_score"] * 0.70
            + nufus_econ * 0.15
            + economy_mock * 0.15,
            1
        )
        economy_src = "Endeksa + simüle"
    elif il_data:
        economy     = _blend(il_data["ekonomi_score"], economy_mock)
        economy_src = "TÜİK + simüle"
    else:
        economy     = economy_mock
        economy_src = "simüle"

    if trf["score"] is not None:
        transport     = _blend(trf["score"], transport_mock, weight=0.85)
        transport_src = "OSM"
    else:
        transport     = transport_mock
        transport_src = "simüle"
    if egitim_il:
        education     = _blend(egitim_il["egitim_score"], education_mock)
        education_src = "TÜİK + simüle"
    else:
        education     = education_mock
        education_src = "simüle"
    healthcare = healthcare_mock
    livability_score = round((transport + education + healthcare + economy) / 4, 1)

    liv_details = [
        MetricDetail(label="Ulaşım Erişimi",    value=transport,
                     unit="puan", status=_status(transport),  source=transport_src),
        MetricDetail(label="Eğitim Kalitesi",   value=education,
                     unit="puan", status=_status(education),  source=education_src),
        MetricDetail(label="Sağlık Hizmetleri", value=healthcare,
                     unit="puan", status=_status(healthcare), source="simüle"),
        MetricDetail(label="Ekonomik Canlılık", value=economy,
                     unit="puan", status=_status(economy),    source=economy_src),
    ]

    # Yol ağı detayları (trf OSM ise)
    if trf["source"] == "OSM":
        liv_details.append(MetricDetail(
            label="Yol Yoğunluğu",
            value=trf["total_density"],
            unit="km/km²",
            status=_status(trf["local_score"]),
            source="OSM",
        ))
        liv_details.append(MetricDetail(
            label="Ana Arterler",
            value=trf["artery_km"],
            unit="km (5 km yarıçap)",
            status=_status(trf["artery_score"]),
            source="OSM",
        ))

    # Kira ve konut değeri (kira_il varsa)
    if kira_il:
        liv_details.append(MetricDetail(
            label="Ortalama Kira",
            value=round(kira_il["ort_kira_tl"] / 1000, 1),
            unit="₺k/ay",
            status=_status(kira_il["ekonomi_score"]),
            source="Endeksa 2024",
        ))
        liv_details.append(MetricDetail(
            label="Konut m² Fiyatı",
            value=round(kira_il["m2_fiyat_tl"] / 1000, 1),
            unit="₺k/m²",
            status=_status(kira_il["ekonomi_score"]),
            source="Endeksa 2024",
        ))

    # Okuryazarlık oranı (egitim_il varsa)
    if egitim_il:
        liv_details.append(MetricDetail(
            label="Okuryazarlık Oranı",
            value=egitim_il["okur_yazarlik_orani"],
            unit="%",
            status=_status(egitim_il["egitim_score"]),
            source="TÜİK 2021",
        ))

    # Nüfus yoğunluğu bilgi satırı (il_data varsa)
    if il_data:
        liv_details.append(MetricDetail(
            label="Nüfus Yoğunluğu",
            value=float(il_data["yogunluk"]),
            unit="kişi/km²",
            status=_status(min(il_data["ekonomi_score"], 100)),
            source="TÜİK ADNKS 2025",
        ))

    # ── Genel skor & özet ─────────────────────────────────────────────────────
    overall = round((env_score + risk_score + livability_score) / 3, 1)

    province_str = f"{province} ili, " if province else ""

    if overall >= 70:
        summary        = f"{province_str}bu bölge genel metriklere göre yüksek yaşam kalitesi sunmaktadır."
        recommendation = "Yatırım ve yerleşim için uygun. Çevresel değerlerin korunması önerilir."
    elif overall >= 50:
        summary        = f"{province_str}bölge orta düzeyde bir profil sergilemektedir; bazı metrikler iyileştirme gerektirmektedir."
        recommendation = "Riskler ve altyapı eksiklikleri değerlendirildikten sonra karar verilmesi önerilir."
    else:
        summary        = f"{province_str}bu bölgede belirgin riskler ve düşük yaşam kalitesi metrikleri tespit edilmiştir."
        recommendation = "Öncelikli iyileştirme müdahalesi veya alternatif bölge araştırması tavsiye edilir."

    return AnalyzeResponse(
        lat=lat, lng=lng,
        province=province,
        environmental_score=env_score,
        risk_score=risk_score,
        livability_score=livability_score,
        overall_score=overall,
        environmental_details=env_details,
        risk_details=risk_details,
        livability_details=liv_details,
        summary=summary,
        recommendation=recommendation,
        data_note=data_note,
    )


# ── Heatmap ───────────────────────────────────────────────────────────────────
@app.get("/heatmap-points")
def heatmap_points():
    random.seed(42)
    points = []
    for _ in range(120):
        lat = random.uniform(36.0, 42.0)
        lng = random.uniform(26.0, 45.0)
        digest = hashlib.md5(f"{lat}{lng}heat".encode()).hexdigest()
        intensity = int(digest[:8], 16) / 0xFFFFFFFF
        points.append({"lat": round(lat, 4), "lng": round(lng, 4), "intensity": round(intensity, 3)})
    return {"points": points}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "GeoInsight AI",
        "tuik_nufus_loaded": nufus_data.loaded,
        "tuik_egitim_loaded": egitim_data.loaded,
        "endeksa_kira_loaded": kira_data.loaded,
        "osm_green_cache_size": len(__import__("osm_fetcher")._cache),
        "aqi_cache_size": len(__import__("aqi_fetcher")._cache),
        "aqi_source": "IQAir" if os.environ.get("IQAIR_API_KEY") else (
                       "OpenWeatherMap" if os.environ.get("OWM_API_KEY") else "simüle"),
    }
