"""
Kira / Konut fiyat verisi yükleyici.

Kaynak: Endeksa 2024 verileri (statik CSV — il ve ilçe bazlı)
Sütunlar: il, ilce, ort_kira_tl, m2_fiyat_tl

Skor mantığı:
  - Log-normalize kira → 0–100 "Ekonomik Canlılık" skoru
  - Yüksek kira = yüksek talep = ekonomik açıdan aktif bölge
  - İlçe verisi varsa → il ortalamasına tercih edilir
  - İlçe bulunamazsa → ağırlıklı il ortalamasına düşer (built-in fallback)

Güncelleme:
  - Endeksa'dan yeni veri çekmek için data/kira.csv'yi güncelleyin
    (https://endeksa.com → İl/İlçe → Ortalama Kira / m² Fiyatı)
"""

import csv
import math
import pathlib

DATA_DIR = pathlib.Path(__file__).parent / "data"


def _normalize(text: str) -> str:
    text = text.lower().strip()
    for src, dst in {
        "ı": "i", "İ": "i", "ğ": "g", "Ğ": "g",
        "ü": "u", "Ü": "u", "ş": "s", "Ş": "s",
        "ö": "o", "Ö": "o", "ç": "c", "Ç": "c",
    }.items():
        text = text.replace(src, dst)
    return text


def _log_normalize(value: float, min_val: float, max_val: float) -> float:
    if value <= 0:
        return 0.0
    log_v   = math.log(value)
    log_min = math.log(max(min_val, 1))
    log_max = math.log(max(max_val, 1))
    if log_max == log_min:
        return 50.0
    return round(min(max((log_v - log_min) / (log_max - log_min) * 100, 0), 100), 1)


class KiraData:
    def __init__(self):
        self._il:   dict[str, dict] = {}   # normalize(il) → entry
        self._ilce: dict[str, dict] = {}   # "normalize(il)/normalize(ilce)" → entry
        self._kira_min = 0.0
        self._kira_max = 0.0
        self._loaded = False

    # ── Yükleme ──────────────────────────────────────────────────────────────
    def load(self):
        path = DATA_DIR / "kira.csv"
        if not path.exists():
            print("[kira_loader] kira.csv bulunamadı, mock ekonomi skoru kullanılacak.")
            return

        rows = []
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                il    = row["il"].strip()
                ilce  = row.get("ilce", "").strip()
                kira  = float(row["ort_kira_tl"])
                m2    = float(row["m2_fiyat_tl"])
                rows.append((il, ilce, kira, m2))

        kira_vals        = [r[2] for r in rows]
        self._kira_min   = min(kira_vals)
        self._kira_max   = max(kira_vals)

        for il, ilce, kira, m2 in rows:
            ekonomi_score = _log_normalize(kira, self._kira_min, self._kira_max)
            entry = {
                "il": il,
                "ilce": ilce,
                "ort_kira_tl": kira,
                "m2_fiyat_tl": m2,
                "ekonomi_score": ekonomi_score,
            }
            il_key = _normalize(il)
            if not ilce:
                # İl bazlı ortalama
                self._il[il_key] = entry
            else:
                # İlçe kaydı
                ilce_key = f"{il_key}/{_normalize(ilce)}"
                self._ilce[ilce_key] = entry
                # İl kaydı henüz yoksa ilk ilçeyi placeholder olarak koy
                if il_key not in self._il:
                    self._il[il_key] = entry

        self._loaded = True
        print(
            f"[kira_loader] {len(self._il)} il, {len(self._ilce)} ilçe yüklendi. "
            f"Kira aralığı: {self._kira_min:,.0f}–{self._kira_max:,.0f} ₺/ay"
        )

    # ── Sorgulama ─────────────────────────────────────────────────────────────
    def get(self, province: str, district: str = "") -> dict | None:
        """
        İlçe varsa ilçe verisini döner; yoksa il ortalamasına düşer.
        İl de bulunamazsa None.
        """
        if not self._loaded:
            return None

        il_key = _normalize(province)

        # 1. İlçe tam eşleşme
        if district:
            ilce_key = f"{il_key}/{_normalize(district)}"
            if ilce_key in self._ilce:
                return self._ilce[ilce_key]

            # 2. İlçe prefix eşleşmesi (örn: "Çankaya İlçesi" → "çankaya")
            d_norm = _normalize(district)
            for k, v in self._ilce.items():
                if not k.startswith(il_key + "/"):
                    continue
                suffix = k[len(il_key) + 1:]
                if suffix.startswith(d_norm) or d_norm.startswith(suffix):
                    return v

        # 3. İl tam eşleşme
        if il_key in self._il:
            return self._il[il_key]

        # 4. İl prefix eşleşmesi
        for k, v in self._il.items():
            if k.startswith(il_key) or il_key.startswith(k):
                return v

        return None

    @property
    def loaded(self) -> bool:
        return self._loaded


# ── Singleton ─────────────────────────────────────────────────────────────────
kira_data = KiraData()
