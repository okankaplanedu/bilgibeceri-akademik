# Bilgi Beceri • GitHub'da toplama, elle yükleme

## Önce okuyun

Bu paket **GitHub deposunun kurulum dosyalarıdır; web sunucusuna yükleme paketi değildir.** Sunucuya doğrudan yüklemeyin. Daha sonra GitHub Actions, yeni ASP sayfalarını ayrı bir indirme paketi olarak hazırlayacaktır.

Bu yapı bilgibeceri.com'a FTP/FTPS ile bağlanmaz, hiçbir dosyayı kendiliğinden yüklemez ve mevcut siteyi değiştirmez. Canlı kataloğu yalnız HTTPS `GET` isteğiyle okuyup karşılaştırır. Katalog değişmiş veya depo ile uyuşmazsa paket hazırlamayı durdurur.

- Depo: `okankaplanedu/bilgibeceri-akademik` (**Public**).
- GitHub Pages ayarı gerekmiyor; sayfalar bilgibeceri.com'da Classic ASP ile çalışacak.
- FTP şifresi veya başka bir şifreyi depoya eklemeyin. Bu iş akışı FTP şifresi kullanmaz.
- Mevcut sunucuya kurulmuş kök `index.html`, `default.asp` ve altı ortak include yeniden yüklenmez.
- Kullanılacak mevcut bileşenler: `/head.asp`, `/header.asp`, `/asie.asp`, `/center.asp`, `/shorts.asp`, `/footer.asp`.
- Katalogdaki başlangıç verisi, kurulum anında sitede görülen **23 kayıtla** eşleştirilmiştir. Canlı site daha sonra değişirse kontrol tekrar yapılır.

## 1. Kurulum dosyalarını GitHub'a yükleyin

1. ZIP'i bilgisayarınıza indirin ve tamamen **ayıklayın**. GitHub'a ZIP dosyasının kendisini yüklemeyin.
2. `bilgibeceri-akademik` deposunda **Add file → Upload files** seçin.
3. Ayıkladığınız klasörün içindeki `motor`, `scripts`, `tests`, `veri` klasörleri ile `KURULUM.md` ve `IS-AKISI.txt` dosyalarını yükleme alanına sürükleyin. Üst kapsayıcı klasörü sürüklemeyin.
4. Listede örneğin `motor/generator.py`, `scripts/paket_olustur.py`, `veri/data/catalog.json`, `veri/arsiv/.../default.asp` ve `tests/test_package.py` yollarının görünmesini kontrol edin.
5. **Commit changes** ile `main` dalına kaydedin. Depodaki mevcut `README.md` dosyasına dokunmayın.

## 2. GitHub iş akışını ekleyin

`.github` gizli klasör olduğu için tarayıcıya sürüklerken atlanabilir; bu nedenle iş akışını GitHub'da ayrıca oluşturun:

1. Depoda **Add file → Create new file** seçin.
2. Dosya adını tam olarak `.github/workflows/akademik-paket.yml` yazın.
3. Paket içindeki `IS-AKISI.txt` dosyasının **tüm metnini** bu yeni dosyaya yapıştırın.
4. `main` dalına **Commit changes** ile kaydedin.

Bu adım tamamlandıktan sonra **Actions → Akademik kaynakları topla ve ASP paketi hazırla → Run workflow** açılır. `test` seçeneği yalnız kod/biçim testleri ve yapılandırma kontrolü yapar; kaynak taramaz, siteye dokunmaz ve yayınlamaz.

## 3. Kaynakları tara ve paketi indir

1. `test` başarılı olduktan sonra aynı ekranda yeni bir çalışma başlatın ve `islem: paket` seçin.
2. Python, FOMM ile ilgili OpenAlex, Crossref, arXiv ve Semantic Scholar bağdaştırıcılarını kullanarak yapılandırılmış konu ve anahtar sözcükleri tarar. Bir sağlayıcıya erişilememesi diğerlerinin çalışmasını engellemez. Tüm sağlayıcılar erişilemezse iş hata ile durur.
3. Yeni kayıtlar `veri/arsiv/` altında `default.asp` + `meta.json` olarak üretilir; tekrarlar ayıklanır. Kullanıcı tarafından daha önce yüklenmemiş sayfalar sonraki paketlerde de korunur.
4. Canlı katalog HTTPS ile **yalnızca okunur**; daha önce canlıda görülen kayıtlar eksikse veya canlı kayıtların içeriği/konumu depo ile uyuşmuyorsa paket durdurulur. Canlı durum paket oluşturulduktan sonra da değişebilir.
5. Çalışma başarılı olduğunda GitHub Actions çalışmasının **Artifacts** bölümünden `BilgiBeceri-Elle-Yukleme-...` dosyasını indirin. GitHub dış ZIP'ini açtığınızda şunları görürsünüz:
   - `00-ONCE-OKUYUN.txt`: güvenlik açıklaması.
   - `01-YENI-ASP-SAYFALARI.zip`: yalnızca o anda canlı katalogda görünmeyen `arsiv/<klasör>/default.asp` ve `meta.json` dosyaları.
   - `02-KATALOG-ONAY-GEREKIR.json`: yeni ve eski kayıtları içeren, **henüz canlıya yüklenmemesi gereken** katalog.
   - `MANIFEST.json`: kayıt sayıları, sayfa adresleri ve paketin gördüğü canlı kataloğun SHA-256 özeti.
6. Yeni veya bekleyen kayıt yoksa sayfa ZIP'i ve katalog dosyası üretilmez; yalnız rapor bulunur.

**İndirdiğiniz GitHub Actions artefaktındaki `01` ZIP'i ile bu kurulum ZIP'ini karıştırmayın.** `01` ZIP'inin içinde kök `index.html`, `default.asp`, ortak include'lar veya katalog bulunmaz.

## 4. Canlı sunucuya geçmeden önce durun

Yeni sayfa klasörleri sunucuya eklendikten ve her biri çalıştığı doğrulandıktan sonra, listelenmeleri için kataloğun güncellenmesi gerekir. Sunucuda aynı adlı klasör varsa üzerine yazmayın; durup inceleyin. Eski bir paket kullanmadan önce canlı kataloğun paket oluşturulduğundan beri değişmediğini tekrar doğrulayın.

Mevcut `data/catalog.json` dosyasının Plesk üzerinden üzerine yazılması sırasında erişimin etkilenip etkilenmeyeceği **henüz doğrulanmadı**. Canlı katalog dosyasını silmeyin veya adını değiştirip boşluk oluşturmayın. Yedek, güncel durum ve yöntemin olası etkisi değerlendirilmeden `02` dosyasını yüklemeyin; kullanıcı onayıyla kontrollü adımlar izleyin. Mutlak sıfır kesinti garanti edilemez.

Her yeni ASP sayfası, kendi `https://bilgibeceri.com/arsiv/.../default.asp` adresini `Link: <...>; rel="canonical"` **HTTP yanıt başlığıyla** belirtir. Böylece ortak `head.asp` dosyasını değiştirmek gerekmez. Bu, arama motorlarına bir tercih sinyalidir; indeksleme veya sıralama garantisi değildir. Yeni bir sayfa gerçekten sunucuya yüklendikten sonra başlık ayrıca test edilmelidir.

## Zamanlama ve ayarlar

- İlk kurulumda zamanlanmış **toplama kapalıdır**. Başarılı testlerden sonra istenirse GitHub Actions repository variable olarak `AUTO_COLLECT=true` eklenebilir. Bu yalnız GitHub'da veri toplar ve paket hazırlar; sunucuya yükleme yapmaz.
- Zamanlama `03:23 UTC` (Türkiye'de yaklaşık 06:23); GitHub işleri gecikebilir veya bazen atlanabilir. Artifakt bu iş akışında 14 gün saklanır; hesap/depo ilkeleri uygulanır.
- `motor/settings.json` içinde kategoriler, bağlamlar, arama ifadeleri ve token'lar düzenlenebilir. Bir çalışmada varsayılan en fazla 30 yeni kayıt üretilir; toplam zaman içinde artabilir.
- OpenAlex ve Semantic Scholar anahtarları isteğe bağlıdır. Kullanılacaksa yalnız GitHub Actions Secrets bölümüne, sırasıyla `OPENALEX_API_KEY` ve `SEMANTIC_SCHOLAR_API_KEY` olarak girilir. Bu anahtarları sohbete veya public depo dosyasına yazmayın.
- Sonuçlar bibliyografik üst verilere dayanır. Otomatik paragraflar çalışmaların bulgularının doğrulanmış özeti değildir; özgün kaynakları inceleyin.
