"""
Hava Kalitesi Verisi — IQAir veya OpenWeatherMap

Öncelik:
  1. IQAIR_API_KEY env var varsa  → IQAir nearest_city (US AQI + tahmini PM2.5)
  2. OWM_API_KEY env var varsa    → OpenWeatherMap Air Pollution (AQI 1-5 + gerçek PM2.5)
  3. İkisi de yoksa               → None döner, main.py mock'a düşer

IQAir ücretsiz kayıt : https://www.iqair.com/dashboard (Community plan)
OWM ücretsiz kayıt   : https://openweathermap.org/api/air-pollution (1M istek/ay)

US AQI → Skor (0–100) dönüşümü — EPA breakpoints:
  0–50  : İyi         → 90–100
  51–100: Orta        → 70–89
  101–150: Hassas     → 45–69
  151–200: Sağlıksız  → 20–44
  201–300: Çok kötü   → 5–19
  300+  : Tehlikeli   → 0–4
"""

import os, time, math
import httpx

IQAIR_URL = "https://api.airvisual.com/v2/nearest_city"
OWM_URL   = "https://api.openweathermap.org/data/2.5/air_pollution"

_cache: dict = {}
_CACHE_TTL = 1800   # 30 dakika


# ── Dönüşüm fonksiyonları ─────────────────────────────────────────────────────
_AQI_SCORE_BP = [
    (0,   90.0),
    (50,  90.0),
    (100, 70.0),
    (150, 45.0),
    (200, 20.0),
    (300,  5.0),
    (500,  0.0),
]

def _aqi_to_score(aqi: float) -> float:
    """US AQI → 0-100 skor (düşük kirlilik = yüksek skor)."""
    for i in range(len(_AQI_SCORE_BP) - 1):
        a1, s1 = _AQI_SCORE_BP[i]
        a2, s2 = _AQI_SCORE_BP[i + 1]
        if aqi <= a2:
            t = (aqi - a1) / (a2 - a1)
            return round(s1 + (s2 - s1) * t, 1)
    return 0.0


_PM25_AQI_BP = [        # (pm25_lo, pm25_hi, aqi_lo, aqi_hi)
    (0.0,   12.0,   0,   50),
    (12.1,  35.4,  51,  100),
    (35.5,  55.4, 101,  150),
    (55.5, 150.4, 151,  200),
    (150.5, 250.4, 201, 300),
    (250.5, 350.4, 301, 400),
    (350.5, 500.4, 401, 500),
]

def _pm25_to_aqi(pm25: float) -> int:
    """PM2.5 µg/m³ → US AQI (EPA 24h avg formula)."""
    for c_lo, c_hi, i_lo, i_hi in _PM25_AQI_BP:
        if c_lo <= pm25 <= c_hi:
            return round((i_hi - i_lo) / (c_hi - c_lo) * (pm25 - c_lo) + i_lo)
    return 500 if pm25 > 500 else 0


def _owm_aqi_to_us(owm_aqi: int, pm25: float) -> int:
    """
    OWM AQI (1-5 European scale) → US AQI.
    PM2.5 µg/m³ varsa doğrudan EPA formülü kullan (daha doğru).
    """
    if pm25 is not None and pm25 >= 0:
        return _pm25_to_aqi(pm25)
    # Kaba dönüşüm (PM2.5 yoksa)
    return {1: 25, 2: 75, 3: 125, 4: 175, 5: 250}.get(owm_aqi, 100)


def _status_pm25(pm25: float) -> str:
    """WHO kılavuz değerlerine göre durum."""
    if pm25 <= 15: return "good"       # WHO 24s kılavuzu: 15 µg/m³
    if pm25 <= 35: return "moderate"
    return "poor"


def _cache_key(lat: float, lng: float) -> tuple:
    return (round(lat, 1), round(lng, 1))   # ~10 km grid


# ── IQAir ─────────────────────────────────────────────────────────────────────
async def _fetch_iqair(lat: float, lng: float, api_key: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(IQAIR_URL, params={
            "lat": lat, "lon": lng, "key": api_key,
        })
        resp.raise_for_status()
        d = resp.json()

    if d.get("status") != "success":
        raise ValueError(f"IQAir: {d.get('status')} — {d.get('data',{})}")

    pollution = d["data"]["current"]["pollution"]
    aqi_us    = int(pollution.get("aqius", 0))
    city      = d["data"].get("city", "")

    # Ücretsiz planda PM2.5 konsantrasyonu gelmez → AQI'dan tahmin et
    pm25 = round(_pm25_to_aqi.__doc__ and 0 or 0, 1)   # placeholder
    # Gerçek hesap: inverse EPA formülü
    for c_lo, c_hi, i_lo, i_hi in _PM25_AQI_BP:
        if i_lo <= aqi_us <= i_hi:
            pm25 = round((c_hi - c_lo) / (i_hi - i_lo) * (aqi_us - i_lo) + c_lo, 1)
            break

    return {
        "aqi": aqi_us,
        "pm25": pm25,
        "score": _aqi_to_score(aqi_us),
        "city": city,
        "source": "IQAir",
    }


# ── OpenWeatherMap ────────────────────────────────────────────────────────────
async def _fetch_owm(lat: float, lng: float, api_key: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(OWM_URL, params={
            "lat": lat, "lon": lng, "appid": api_key,
        })
        resp.raise_for_status()
        d = resp.json()

    item      = d["list"][0]
    owm_aqi   = item["main"]["aqi"]
    comp      = item.get("components", {})
    pm25      = round(comp.get("pm2_5", 0.0), 1)
    aqi_us    = _owm_aqi_to_us(owm_aqi, pm25)

    return {
        "aqi": aqi_us,
        "pm25": pm25,
        "score": _aqi_to_score(aqi_us),
        "city": "",
        "source": "OpenWeatherMap",
    }


# ── Ana fonksiyon ─────────────────────────────────────────────────────────────
async def fetch_aqi(lat: float, lng: float) -> dict:
    """
    Döner:
    {
      "aqi"   : int,    # US AQI
      "pm25"  : float,  # PM2.5 µg/m³
      "score" : float,  # 0–100 (None → API yok, mock'a düş)
      "city"  : str,
      "source": "IQAir" | "OpenWeatherMap" | "simüle"
    }
    """
    iqair_key = os.environ.get("IQAIR_API_KEY", "").strip()
    owm_key   = os.environ.get("OWM_API_KEY",   "").strip()

    if not iqair_key and not owm_key:
        return {"aqi": None, "pm25": None, "score": None, "city": "", "source": "simüle"}

    key = _cache_key(lat, lng)
    now = time.time()
    if key in _cache and (now - _cache[key]["ts"]) < _CACHE_TTL:
        return _cache[key]["data"]

    result = None
    if iqair_key:
        try:
            result = await _fetch_iqair(lat, lng, iqair_key)
        except Exception as e:
            print(f"[aqi_fetcher] IQAir hata: {e}")

    if result is None and owm_key:
        try:
            result = await _fetch_owm(lat, lng, owm_key)
        except Exception as e:
            print(f"[aqi_fetcher] OWM hata: {e}")

    if result is None:
        result = {"aqi": None, "pm25": None, "score": None, "city": "", "source": "simüle"}

    _cache[key] = {"data": result, "ts": now}
    return result
