"""
TÜİK veri yükleyici ve normalizer.
Uygulama başlangıcında bir kez yüklenir, sonraki sorgularda cache'den okunur.
"""

import csv
import math
import pathlib
import unicodedata

DATA_DIR = pathlib.Path(__file__).parent / "data"


# ── Yardımcı: Türkçe karakterleri normalize et ────────────────────────────────
def _normalize(text: str) -> str:
    """
    'İstanbul' → 'istanbul', 'Ağrı' → 'agri'
    Hem TÜİK sütunlarını hem Nominatim çıktısını aynı forma getirir.
    """
    text = text.lower().strip()
    replacements = {
        "ı": "i", "İ": "i", "ğ": "g", "Ğ": "g",
        "ü": "u", "Ü": "u", "ş": "s", "Ş": "s",
        "ö": "o", "Ö": "o", "ç": "c", "Ç": "c",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return text


# ── Log-scale normalizer (0–100) ──────────────────────────────────────────────
def _log_normalize(value: float, min_val: float, max_val: float) -> float:
    if value <= 0:
        return 0.0
    log_v   = math.log(value)
    log_min = math.log(max(min_val, 1))
    log_max = math.log(max(max_val, 1))
    if log_max == log_min:
        return 50.0
    return round(min(max((log_v - log_min) / (log_max - log_min) * 100, 0), 100), 1)


# ── Nüfus verisi ──────────────────────────────────────────────────────────────
class NufusData:
    def __init__(self):
        self._raw: dict[str, dict] = {}       # normalize_il → {nufus, yogunluk}
        self._yogunluk_min = 0.0
        self._yogunluk_max = 0.0
        self._nufus_min = 0.0
        self._nufus_max = 0.0
        self._loaded = False

    def load(self):
        path = DATA_DIR / "nufus.csv"
        if not path.exists():
            return

        rows = []
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                il        = row["il"].strip()
                nufus     = int(row["toplam_nufus"].replace(",", "").replace(".", ""))
                yogunluk  = int(row["nufus_yogunlugu"].replace(",", "").replace(".", ""))
                rows.append((il, nufus, yogunluk))

        yogunluklar = [r[2] for r in rows]
        nufuslar    = [r[1] for r in rows]
        self._yogunluk_min = min(yogunluklar)
        self._yogunluk_max = max(yogunluklar)
        self._nufus_min    = min(nufuslar)
        self._nufus_max    = max(nufuslar)

        for il, nufus, yogunluk in rows:
            key = _normalize(il)
            self._raw[key] = {
                "il": il,
                "nufus": nufus,
                "yogunluk": yogunluk,
                # Ekonomik canlılık: yüksek yoğunluk → yüksek skor
                "ekonomi_score": _log_normalize(yogunluk, self._yogunluk_min, self._yogunluk_max),
                # Çevre skoru: yüksek yoğunluk → daha düşük çevre skoru (ters ilişki)
                "cevre_penalty": _log_normalize(yogunluk, self._yogunluk_min, self._yogunluk_max),
            }

        self._loaded = True
        print(f"[data_loader] {len(self._raw)} il yüklendi. "
              f"Yoğunluk aralığı: {self._yogunluk_min}–{self._yogunluk_max} kişi/km²")

    def get(self, province_name: str) -> dict | None:
        """Normalize edilmiş il adıyla arama yapar."""
        if not self._loaded:
            return None
        key = _normalize(province_name)
        # Tam eşleşme
        if key in self._raw:
            return self._raw[key]
        # Prefix eşleşmesi (örn: "istanbul" → "istanbul")
        for k, v in self._raw.items():
            if k.startswith(key) or key.startswith(k):
                return v
        return None

    @property
    def loaded(self) -> bool:
        return self._loaded


# ── Eğitim verisi (henüz TÜİK'ten bekleniyor) ────────────────────────────────
class EgitimData:
    def __init__(self):
        self._raw: dict[str, dict] = {}
        self._loaded = False

    def load(self):
        path = DATA_DIR / "egitim.csv"
        if not path.exists():
            print("[data_loader] egitim.csv bulunamadı, mock skor kullanılacak.")
            return

        rows = []
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                il      = row["il"].strip()
                oran    = float(row["okur_yazarlik_orani"])
                rows.append((il, oran))

        oranlar = [r[1] for r in rows]
        oran_min = min(oranlar)   # ~93.4
        oran_max = max(oranlar)   # ~99.1

        for il, oran in rows:
            key = _normalize(il)
            # Okuryazarlık oranını 0–100 eğitim skoruna lineer normalize et
            if oran_max > oran_min:
                egitim_score = round((oran - oran_min) / (oran_max - oran_min) * 100, 1)
            else:
                egitim_score = 50.0
            self._raw[key] = {
                "il": il,
                "okur_yazarlik_orani": oran,
                "egitim_score": egitim_score,
            }

        self._loaded = True
        print(f"[data_loader] {len(self._raw)} il eğitim verisi yüklendi. "
              f"Okuryazarlık aralığı: {oran_min}%–{oran_max}%")

    def get(self, province_name: str) -> dict | None:
        if not self._loaded:
            return None
        key = _normalize(province_name)
        if key in self._raw:
            return self._raw[key]
        for k, v in self._raw.items():
            if k.startswith(key) or key.startswith(k):
                return v
        return None

    @property
    def loaded(self) -> bool:
        return self._loaded


# ── Singleton örnekler ────────────────────────────────────────────────────────
nufus_data  = NufusData()
egitim_data = EgitimData()


def load_all():
    nufus_data.load()
    egitim_data.load()
    # kira_data kira_loader.py'de ayrı yüklenir (main.py'de çağrılır)
