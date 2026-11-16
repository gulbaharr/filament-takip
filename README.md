# Filament Takip

Yerel filament stok takibi + Anycubic Cloud (Kobra S1 / ACE Pro) entegrasyonu. Ek kurulum gerekmez (Python 3 standart kütüphane).

- Uygulama: `~/Applications/Filament Takip.app` (çift tıkla → tarayıcıda http://127.0.0.1:8765 açılır)
- Veri: `~/Library/Application Support/FilamentTakip/data.json`
- Kod değişince: `./install.sh`

## Anycubic bağlantısı
cloud-universe.anycubic.com'a Chrome/Brave/Edge/Arc ile giriş yap → Ayarlar → "Tarayıcıdan otomatik bul".
Ya da Console'da `copy(localStorage['XX-Token'])` ile alıp yapıştır. Token ~90 gün geçerli.

## Hesaplama
Anycubic kalan miktarı vermez. Baskı sırasında `supplies_usage` (mm) ya da ilerleme × `estimate_supplies_usage` (m)
üzerinden gram = uzunluk × π·0.875² × yoğunluk; takılı (loaded) slota atanmış makaradan düşülür.
İmza yöntemi: github.com/Nino6689/hass-anycubic (anycubic-cloud-api) web modu.
