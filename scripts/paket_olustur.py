#!/usr/bin/env python3
"""Canlı siteye yalnızca HTTPS GET yapan, elde yükleme için ayrık paket üreticisi.

Bu program FTPS kullanmaz ve bilgibeceri.com'a hiçbir yazma isteği göndermez.
Katalog üretimini sayfa ZIP'inden ayırır; eski veya uyumsuz veri gördüğünde durur.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / 'motor'))
import generator  # noqa: E402

LIVE_URL = 'https://bilgibeceri.com/data/catalog.json'
CANONICAL_ORIGIN = 'https://bilgibeceri.com'
MAX_BYTES = 50_000_000
ID_RE = re.compile(r'^[a-f0-9]{12}$')
PATH_RE = re.compile(r'^/arsiv/([a-z0-9]+(?:-[a-z0-9]+)*)/default\.asp$')
EXPECTED_INCLUDES = tuple(
    f'<!--#include virtual="/{name}.asp" -->'
    for name in ('head', 'header', 'asie', 'center', 'shorts', 'footer')
)


class PackageError(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def read_live_catalog() -> bytes:
    """Sabit HTTPS adresten yalnız katalog GET isteği yap; sunucuya yazma yok."""
    url = LIVE_URL + '?paket_kontrol=' + str(time.time_ns())
    request = urllib.request.Request(url, headers={
        'User-Agent': 'BilgiBeceri-ManualPackage/1.0',
        'Accept': 'application/json',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        target = urllib.parse.urlsplit(response.geturl())
        if target.scheme != 'https' or target.hostname != 'bilgibeceri.com' or target.path != '/data/catalog.json':
            raise PackageError('Canlı katalog beklenmeyen bir adrese yönlendirildi; paket oluşturulmadı.')
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise PackageError('Canlı katalog boyut sınırını aşıyor; paket oluşturulmadı.')
    return data


def decode_catalog(raw: bytes, name: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if len(raw) > MAX_BYTES:
        raise PackageError(name + ' katalog boyut sınırını aşıyor.')
    try:
        payload = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PackageError(name + ' katalog JSON biçiminde değil.') from exc
    if not isinstance(payload, dict) or payload.get('version') != 1 or not isinstance(payload.get('items'), list):
        raise PackageError(name + ' katalog biçimi veya sürümü uyumsuz.')
    if not isinstance(payload.get('categories'), list):
        raise PackageError(name + ' kategori listesi geçersiz.')
    if len(payload['items']) > 100_000:
        raise PackageError(name + ' katalog kayıt sayısı güvenlik sınırını aşıyor.')
    by_id: dict[str, dict[str, Any]] = {}
    seen_paths: set[str] = set()
    for item in payload['items']:
        if not isinstance(item, dict):
            raise PackageError(name + ' katalog kaydı nesne değil.')
        rid = item.get('id')
        path = item.get('path')
        if not isinstance(rid, str) or not ID_RE.fullmatch(rid):
            raise PackageError(name + ' katalogda geçersiz kayıt kimliği var.')
        m = PATH_RE.fullmatch(path) if isinstance(path, str) else None
        if not m or not m.group(1).endswith('-' + rid):
            raise PackageError(name + ' katalogda geçersiz veya kimlikle uyuşmayan sayfa yolu var.')
        if rid in by_id or path in seen_paths:
            raise PackageError(name + ' katalogda yinelenen kimlik veya sayfa yolu var.')
        by_id[rid] = item
        seen_paths.add(path)
    return payload, by_id


def category_map(payload: dict[str, Any], name: str) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for cat in payload['categories']:
        if not isinstance(cat, dict):
            raise PackageError(name + ' kategori kaydı geçersiz.')
        cid, title, context = cat.get('id'), cat.get('name'), cat.get('context', '')
        if not isinstance(cid, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', cid):
            raise PackageError(name + ' kategori kimliği geçersiz.')
        if cid in result or not isinstance(title, str) or not isinstance(context, str):
            raise PackageError(name + ' kategorilerinde tekrar veya geçersiz alan var.')
        result[cid] = (title, context)
    return result


def load_baseline(root: Path) -> set[str]:
    try:
        payload = json.loads((root / 'baseline-live.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise PackageError('Canlı kayıt güvenlik listesi okunamadı.') from exc
    if not isinstance(payload, dict) or payload.get('domain') != CANONICAL_ORIGIN or not isinstance(payload.get('ids'), list):
        raise PackageError('Canlı kayıt güvenlik listesi geçersiz.')
    ids = payload['ids']
    if any(not isinstance(x, str) or not ID_RE.fullmatch(x) for x in ids) or len(ids) != len(set(ids)):
        raise PackageError('Canlı kayıt güvenlik listesinde geçersiz kimlik var.')
    return set(ids)


def record_files(root: Path, item: dict[str, Any], require_current_template: bool) -> list[tuple[str, bytes]]:
    rid = item['id']
    folder = PATH_RE.fullmatch(item['path']).group(1)
    archive = root / 'arsiv'
    directory = archive / folder
    if archive.is_symlink() or directory.is_symlink() or not directory.is_dir():
        raise PackageError('Arşiv klasörü yok veya bağlantı: ' + folder)
    results = []
    for name in ('default.asp', 'meta.json'):
        target = directory / name
        if target.is_symlink() or not target.is_file() or target.stat().st_size > 100_000:
            raise PackageError('Eksik, çok büyük veya bağlantı olan sayfa dosyası: ' + folder + '/' + name)
        results.append(('arsiv/' + folder + '/' + name, target.read_bytes()))
    try:
        page = results[0][1].decode('utf-8')
        meta = json.loads(results[1][1].decode('utf-8-sig'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PackageError('Sayfa veya meta biçimi geçersiz: ' + folder) from exc
    if not isinstance(meta, dict) or any(meta.get(key) != value for key, value in item.items()):
        raise PackageError('Katalog kaydı ile meta dosyası uyuşmuyor: ' + folder)
    if any(page.count(part) != 1 for part in EXPECTED_INCLUDES) or page.count('<!--#include virtual=') != 6:
        raise PackageError('Altı ortak ASP include doğrulanamadı: ' + folder)
    if require_current_template:
        try:
            expected_page = generator.build_asp(meta)
        except (KeyError, ValueError, TypeError) as exc:
            raise PackageError('Yeni sayfanın şablonu veya canonical adresi geçersiz: ' + folder) from exc
        if page != expected_page:
            raise PackageError('Yeni sayfa güvenli şablonla uyuşmuyor: ' + folder)
        header = 'Response.AddHeader "Link", "<' + CANONICAL_ORIGIN + item['path'] + '>; rel=""canonical"""'
        if page.count(header) != 1:
            raise PackageError('Yeni sayfanın canonical başlığı doğrulanamadı: ' + folder)
    return results


def make_package(root: Path, live_raw: bytes, output: Path) -> dict[str, Any]:
    root = root.resolve()
    if not (root / 'SUNUCUYA-YUKLEMEYIN.txt').is_file():
        raise PackageError('Üretim veri kökü güvenlik işareti bulunamadı.')
    try:
        local_raw = (root / 'data/catalog.json').read_bytes()
    except OSError as exc:
        raise PackageError('Yerel katalog okunamadı.') from exc
    live, live_items = decode_catalog(live_raw, 'Canlı')
    local, local_items = decode_catalog(local_raw, 'Depo')
    known_live = load_baseline(root)
    if not known_live.issubset(live_items):
        raise PackageError('Daha önce canlıda görülen kayıtlar eksik; eski paketle üzerine yazmayı önlemek için duruldu.')
    if not set(live_items).issubset(local_items):
        raise PackageError('Canlı katalogda depoda bulunmayan kayıtlar var; otomatik birleştirme yapılmadı.')
    for rid, item in live_items.items():
        if item != local_items[rid]:
            raise PackageError('Canlı kayıt ile depodaki kayıt farklı; otomatik üzerine yazma yapılmadı: ' + rid)
    live_categories = category_map(live, 'Canlı')
    local_categories = category_map(local, 'Depo')
    for cid, details in live_categories.items():
        if local_categories.get(cid) != details:
            raise PackageError('Canlı kategori adı veya bağlamı değişmiş; otomatik üzerine yazma yapılmadı: ' + cid)

    # read_existing hatalı bir meta dosyasını atlayabilir; hiçbir arşiv klasörü
    # katalog dışında kalmasın ve bekleyen eski sayfalar sessizce kaybolmasın.
    archive_dir = root / 'arsiv'
    if archive_dir.is_symlink() or not archive_dir.is_dir():
        raise PackageError('Arşiv klasörü bulunamadı veya bağlantı.')
    entries = list(archive_dir.iterdir())
    if any(x.is_symlink() or not x.is_dir() for x in entries):
        raise PackageError('Arşiv kökünde beklenmeyen dosya veya bağlantı var.')
    actual_folders = {x.name for x in entries}
    expected_folders = {PATH_RE.fullmatch(item['path']).group(1) for item in local_items.values()}
    if actual_folders != expected_folders:
        raise PackageError('Arşiv klasörleri ile depo kataloğu uyuşmuyor; içerik silinmedi.')

    # Katalogda adı geçen her kaydın dosyası ve meta bilgisi tutarlı olmalı.
    # Yeni sayfalar için ayrıca güncel canonical + birebir şablon kontrolü yapılır.
    pending_id_set = set(local_items) - set(live_items)
    pending_ids = sorted(pending_id_set)
    pending_files: list[tuple[str, bytes]] = []
    for rid, item in sorted(local_items.items()):
        files = record_files(root, item, rid in pending_id_set)
        if rid in pending_id_set:
            pending_files.extend(files)

    report = {
        'hazirlanma_utc': now_iso(),
        'hedef_site': CANONICAL_ORIGIN,
        'canli_katalog_sha256': hashlib.sha256(live_raw).hexdigest(),
        'canli_kayit_sayisi': len(live_items),
        'paket_sonrasi_kayit_sayisi': len(local_items),
        'canlida_olmayan_kayit_sayisi': len(pending_ids),
        'yeni_sayfalar': [CANONICAL_ORIGIN + local_items[rid]['path'] for rid in pending_ids],
        'uyari': 'Canlı katalog değişmiş olabilir. Kataloğu güncellemeden önce yeniden kontrol ve onay gerekir.',
    }
    if output.is_symlink():
        raise PackageError('Paket çıktı yolu sembolik bağlantı olamaz.')
    output = output.resolve()
    if (output.name != 'paket-cikti' or output == root or root in output.parents
            or output in root.parents or output == PROJECT):
        raise PackageError('Güvenli olmayan paket çıktı yolu.')
    if PROJECT in output.parents and output != (PROJECT / 'paket-cikti').resolve():
        raise PackageError('Proje içindeki paket çıktı yolu sabit olmalıdır.')
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.bb-paket-', dir=output.parent))
    try:
        if pending_ids:
            with zipfile.ZipFile(stage / '01-YENI-ASP-SAYFALARI.zip', 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                for name, content in pending_files:
                    z.writestr(name, content)
            (stage / '02-KATALOG-ONAY-GEREKIR.json').write_bytes(local_raw)
        summary = [
            'BİLGİ BECERİ • KONTROLLÜ ELLE YÜKLEME PAKETİ',
            '',
            'Bu paket GitHub tarafından üretildi; sunucuya hiçbir şey yüklenmedi.',
            f'Canlı katalogda görülen kayıt: {len(live_items)}',
            f'Canlıda henüz görünmeyen sayfa: {len(pending_ids)}',
            f'Yeni katalogdaki toplam kayıt: {len(local_items)}',
            f'Canlı katalog SHA-256: {report["canli_katalog_sha256"]}',
            '',
            '01-YENI-ASP-SAYFALARI.zip yalnız arsiv/ altında yeni sayfalar ve meta dosyaları içerir.',
            'Kökteki index.html, default.asp ve altı ortak include bu ZIP içinde yoktur.',
            'Sunucuda adaş bir klasör varsa mevcut dosyaları değiştirmeyin; önce inceleyin.',
            '02-KATALOG-ONAY-GEREKIR.json canlı katalog DEĞİLDİR; doğrudan yüklemeyin.',
            'Katalog güncellemesinden önce canlı durum, yedek ve olası erişim etkisi ayrıca değerlendirilmeli.',
            'Önce yeni sayfalar yüklenip doğrulanmadan katalog değiştirilemez.',
            'Paketi hazırlarken görülen canlı katalog, sonradan değişmiş olabilir.',
            'Her yeni sayfa kendi bilgibeceri.com adresini HTTP Link canonical başlığıyla belirtir.',
            'Bu, arama motorlarının indeksleme tercihi için bir ipucudur; sonuç garanti edilmez.',
        ]
        if not pending_ids:
            summary.append('Yeni sayfa bulunmadı; sunucuya yüklemek için sayfa veya katalog dosyası üretilmedi.')
        (stage / '00-ONCE-OKUYUN.txt').write_text('\n'.join(summary) + '\n', encoding='utf-8')
        (stage / 'MANIFEST.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if output.exists():
            if (not output.is_dir() or any(output.iterdir()) and not (
                    (output / 'MANIFEST.json').is_file() and (output / '00-ONCE-OKUYUN.txt').is_file())):
                raise PackageError('Var olan çıktı klasörü önceki paket olarak doğrulanamadı.')
            shutil.rmtree(output)
        stage.rename(output)
        # Bir sonraki çalışmada, daha önce gözlenen canlı kayıtların kaybını yakala.
        new_baseline = {
            'domain': CANONICAL_ORIGIN,
            'description': 'Daha önce canlı katalogda gözlenmiş kayıt kimlikleri; eksilme varsa güvenlik için durulur.',
            'ids': sorted(live_items),
        }
        generator.atomic_json(root / 'baseline-live.json', new_baseline)
        return report
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def main() -> int:
    try:
        report = make_package(PROJECT / 'veri', read_live_catalog(), PROJECT / 'paket-cikti')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (PackageError, OSError, ValueError) as exc:
        print('Paket hazırlanmadı:', str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
