# Filament Takip

Anycubic Kobra S1 + ACE Pro ve Bambu Lab (AMS) için filament stoğu, baskı maliyeti ve müşteri siparişi takibi.
Mac'te yerel çalışır; tarayıcıda `http://127.0.0.1:8765` olarak açılır. Python 3 dışında hiçbir şey kurmak gerekmez
(sadece standart kütüphane, harici paket yok).

## Neler yapar

**Makaralar ve stok**
- ACE Pro / AMS yuvaları ve raftaki makaralar ayrı gösterilir; makaraların sırası sabit kalır.
- Baskı ilerledikçe takılı yuvadaki makaradan gram otomatik düşer (Anycubic Cloud, Anycubic LAN ya da Bambu Lab LAN).
- Bağlantı olmadan da kullanılabilir: "✓ Baskı ekle" ile elle kayıt.
- Azalan makaralar için uyarı, renk / malzeme bazında gram fiyatı tablosu.

**Maliyet**
- Maliyet = filament + elektrik + işçilik + ek masraf.
- Filament fiyatı renge göre hesaplanır: makaranın kendi fiyatı → aynı renk → aynı malzeme → genel ortalama.
- Ayarlardaki kâr marjına göre önerilen satış fiyatı.

**Siparişler ve ürün kataloğu**
- Müşteri siparişleri (Bekliyor → Basılıyor → Hazır → Teslim), teslim tarihi, geciken uyarısı.
- Sipariş "Basılıyor" olunca tahmini gram stoktan düşer; gerçek baskı siparişe bağlandıkça mahsup edilir,
  aynı filament iki kez düşülmez (Ayarlar → Tercihler'den kapatılabilir).
- Açık siparişlere göre stok planı: "bu siparişlerden sonra kırmızı X g kalacak" ve eksikler için alışveriş listesi.
- 💰 Beklenen para: hazır, teslim edilmemiş siparişlerin müşteri bazında toplamı.
- Sık basılan ürünler için katalog: gram, süre ve renkler bir kere girilir, siparişe tek tıkla eklenir.
- Filament alımları (Alınacak → Sipariş verildi → Kargoda → Teslim alındı); gelen makaralar stoğa eklenir.

**Slicer'dan içe aktarma**
- G-code ya da `.gcode.3mf` sürükle bırak: gram, renk ve süre otomatik dolar, renkler stoktaki makaralarla eşleşir.
- "Slicer'daki son dilimleme" butonu Anycubic Slicer Next'te son dilimlediğin işi okur.
- Dilimlenmemiş `.3mf` proje dosyaları, Anycubic Slicer Next yüklüyse arka planda dilimlenir (birkaç saniye).
- Çok tablalı projelerde tabla seçilebilir.

## Kurulum

```bash
git clone https://github.com/gulbaharr/filament-takip.git
cd filament-takip
./install.sh
```

`~/Applications/Filament Takip.app` oluşur ve açılır. Sonraki açılışlarda uygulamaya çift tıklamak yeterli.
Kodu değiştirince yeniden `./install.sh` çalıştır.

Kurmadan denemek için: `python3 server.py` (port değiştirmek için `FILAMENT_PORT=9000 python3 server.py`).

## Yazıcı bağlantısı

İkisi de isteğe bağlı; ikisi olmadan uygulama elle kullanılabilir.

| Yöntem | Nasıl | Not |
|---|---|---|
| **Anycubic Cloud** | cloud-universe.anycubic.com'a Chrome / Brave / Edge / Arc ile giriş yap → Ayarlar → "Tarayıcıdan otomatik bul". Ya da tarayıcı konsolunda `copy(localStorage['XX-Token'])` ile alıp yapıştır. | Token yaklaşık 90 gün geçerli. |
| **LAN (yerel ağ)** | Yazıcıda LAN modunu aç → Ayarlar → yazıcının IP adresini gir → "Test et" → "Bağlan". | Sadece Kobra S1 için yazıldı; internet gerekmez. |
| **Bambu Lab** | Ayarlar → 🟩 Bambu Lab → IP, seri numarası ve erişim kodu (yazıcı ekranı: Ayarlar → WLAN / Cihaz) → "Test et" → "Bağlan". | X1 / P1 / A1 / H2. Yeni firmware'lerde LAN Only + Developer Mode gerekebilir. |

Gram şöyle hesaplanır:
- **Anycubic:** baskıda kullanılan uzunluk (mm) ya da ilerleme × tahmini uzunluk → `uzunluk × π × 0,875² × yoğunluk`.
- **Bambu:** baskı başlayınca yazıcıdaki `.gcode.3mf` FTPS ile okunur, `slice_info` içindeki toplam gram × ilerleme.

Her iki durumda da gram, o anda takılı (AMS / ACE'den beslenen) yuvadaki makaradan düşülür. Yazıcı kısa süre
"boşta" görünürse baskı bitmiş sayılmaz (3 dakika beklenir); iptal edilen baskıda sadece basılan kısım düşer.

## Hangi yazıcı ve slicer'larla çalışır?

| | Durum |
|---|---|
| Anycubic Kobra S1 + ACE Pro | ✅ Canlı takip (Cloud ve LAN) |
| Diğer Anycubic yazıcılar (Cloud) | ⚠️ Büyük ihtimalle çalışır, denenmedi |
| Bambu Lab X1 / P1 / A1 / H2 (+ AMS) | ✅ Canlı takip (yerel ağ); sahte yazıcıyla test edildi, gerçek cihazda denenmedi |
| Prusa, Creality vb. | ❌ Canlı takip yok; elle kayıt ve dosya içe aktarma çalışır |
| Anycubic Slicer Next, Bambu Studio, OrcaSlicer G-code / `.gcode.3mf` | ✅ Gram, renk, süre okunur |
| PrusaSlicer G-code | ⚠️ Aynı satır biçimini kullanıyor, çalışması beklenir; denenmedi |
| Cura G-code | ❌ Gram yazmıyor (sadece metre), okunamaz |
| Dilimlenmemiş `.3mf` | Anycubic Slicer Next yüklüyse arka planda dilimlenir |

## Veriler

- Tüm veriler: `~/Library/Application Support/FilamentTakip/data.json` (yedek almak için bu dosyayı kopyala).
- Log: aynı klasörde `server.log`.
- Veri dosyası repoda yoktur (`.gitignore`); GitHub'a gitmez.
- Sunucu sadece `127.0.0.1`'i dinler, ağdaki başka cihazlar erişemez. Yazma isteklerinde `X-Filament: 1` başlığı istenir.

## Dosyalar

| Dosya | Görevi |
|---|---|
| `server.py` | HTTP sunucusu, veri, Anycubic Cloud, baskı takibi, maliyet / rezervasyon |
| `bambu.py` | Bambu Lab yerel ağ bağlantısı (MQTT 8883 + FTPS 990) |
| `lan.py` | Kobra S1 LAN bağlantısı (MQTT); protokol [chrisfore/anycubic_ha_local](https://github.com/chrisfore/anycubic_ha_local) belgelerine dayanır |
| `slicer.py` | G-code / 3mf okuma, Anycubic Slicer Next ile arka planda dilimleme |
| `index.html` | Arayüzün tamamı (tek sayfa) |
| `install.sh` | macOS uygulamasını kurar / günceller |

Cloud imza yöntemi: [Nino6689/hass-anycubic](https://github.com/Nino6689/hass-anycubic) (web modu).

## Sınırlamalar

- Sadece macOS'ta denendi (`install.sh` ve tarayıcıdan token bulma macOS'a özel; `server.py` başka sistemlerde de çalışmalı).
- Vercel gibi sunucusuz ortamlarda olduğu gibi çalışmaz: kalıcı dosya, arka plan takibi ve yerel ağ erişimi gerekir.
- Arka planda dilimleme projenin kendi ayarlarını kullanır; bazı dosyalarda slicer'ın komut satırı çökebilir.
  O zaman dosyayı slicer'da dilimleyip "Slicer'daki son dilimleme" butonunu kullan.
