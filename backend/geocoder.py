"""
Nominatim reverse geocoding — koordinat → il/ilçe adı
Rate limit: 1 istek/saniye (Nominatim ToS)
"""

import httpx
import asyncio

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
HEADERS = {"User-Agent": "GeoInsightAI/1.0 (contact@geoinsight.demo)"}

# Basit in-memory cache (0.1° grid → ~10km hassasiyet)
_cache: dict = {}


def _cache_key(lat: float, lng: float) -> tuple:
    return (round(lat, 1), round(lng, 1))


async def reverse_geocode(lat: float, lng: float) -> dict:
    """
    Koordinatı Nominatim'e gönderir, il/ilçe bilgisini döner.
    Türkiye dışı koordinatlarda boş dict döner.
    """
    key = _cache_key(lat, lng)
    if key in _cache:
        return _cache[key]

    params = {
        "lat": lat, "lon": lng,
        "format": "json",
        "accept-language": "tr",
        "zoom": 8,
        "addressdetails": 1,
    }
    data = {}
    for attempt in range(2):   # 1 yeniden deneme
        try:
            async with httpx.AsyncClient(headers=HEADERS, timeout=12.0) as client:
                resp = await client.get(NOMINATIM_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
                break
        except Exception:
            if attempt == 0:
                await asyncio.sleep(1)  # kısa bekleme, sonra tekrar dene
    if not data:
        return {}

    address = data.get("address", {})
    country = address.get("country_code", "").lower()

    # Türkiye dışı → boş
    if country != "tr":
        _cache[key] = {}
        return {}

    # Nominatim Türkiye için bazen "province", bazen "state" döner
    province = (
        address.get("province")
        or address.get("state")
        or ""
    ).strip()

    # " İli" / " ili" suffix'ini temizle  (örn: "Ankara İli" → "Ankara")
    for suffix in [" İli", " ili", " Province"]:
        province = province.replace(suffix, "")

    result = {
        "province": province,
        "district": address.get("county") or address.get("district") or "",
        "country_code": country,
    }

    _cache[key] = result
    return result
