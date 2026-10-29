import hashlib
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'motor'))
sys.path.insert(0, str(ROOT / 'scripts'))
import generator as g
import paket_olustur as p


class ManualPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / 'veri'
        (self.root / 'arsiv').mkdir(parents=True)
        (self.root / 'data').mkdir()
        (self.root / 'SUNUCUYA-YUKLEMEYIN.txt').write_text('ARŞİV', encoding='utf-8')
        self.output = self.base / 'paket-cikti'
        self.category = {'id': 'egitim', 'name': 'Eğitim', 'context': 'öğrenme',
                         'queries': ['learning'], 'tokens': ['learning']}
        self.cfg = {'categories': [self.category]}
        self.old = self.make_record('A published sample academic research record', '10.1234/old')
        self.assertTrue(g.publish(self.old, self.root))
        g.catalog(self.root, self.cfg, {})
        self.live_raw = (self.root / 'data/catalog.json').read_bytes()
        self.write_baseline([self.old['id']])
        self.new = self.make_record('A new sample academic research record', '10.1234/new')
        self.assertTrue(g.publish(self.new, self.root))
        g.catalog(self.root, self.cfg, {})

    def tearDown(self):
        self.tmp.cleanup()

    def write_baseline(self, ids):
        payload = {'domain': p.CANONICAL_ORIGIN, 'ids': ids}
        (self.root / 'baseline-live.json').write_text(json.dumps(payload), encoding='utf-8')

    def make_record(self, title, doi):
        raw = {'title': title, 'authors': 'Ada Scholar', 'year': 2025, 'doi': doi,
               'url': 'https://doi.org/' + doi, 'provider_id': doi, 'venue': 'Journal'}
        return g.normalize(raw, self.category, 'learning', 'crossref')

    def live_mutated(self, mutate):
        doc = json.loads(self.live_raw)
        mutate(doc)
        return json.dumps(doc, ensure_ascii=False).encode()

    def test_new_pages_only_with_six_includes_and_canonical(self):
        report = p.make_package(self.root, self.live_raw, self.output)
        self.assertEqual(report['canli_kayit_sayisi'], 1)
        self.assertEqual(report['canlida_olmayan_kayit_sayisi'], 1)
        self.assertEqual(report['canli_katalog_sha256'], hashlib.sha256(self.live_raw).hexdigest())
        self.assertEqual(report['yeni_sayfalar'], [p.CANONICAL_ORIGIN + self.new['path']])
        zpath = self.output / '01-YENI-ASP-SAYFALARI.zip'
        with zipfile.ZipFile(zpath) as z:
            names = set(z.namelist())
            folder = self.new['path'].strip('/').rsplit('/', 1)[0]
            self.assertEqual(names, {folder + '/default.asp', folder + '/meta.json'})
            self.assertNotIn('data/catalog.json', names)
            page = z.read(folder + '/default.asp').decode()
            for include in p.EXPECTED_INCLUDES:
                self.assertEqual(page.count(include), 1)
            canonical = 'Response.AddHeader "Link", "<' + p.CANONICAL_ORIGIN + self.new['path'] + '>; rel=""canonical"""'
            self.assertEqual(page.count(canonical), 1)
            self.assertLess(page.index(canonical), page.index('<!--#include virtual="/head.asp" -->'))
        new_catalog = json.loads((self.output / '02-KATALOG-ONAY-GEREKIR.json').read_text())
        self.assertEqual({x['id'] for x in new_catalog['items']}, {self.old['id'], self.new['id']})
        self.assertTrue((self.output / '00-ONCE-OKUYUN.txt').is_file())
        baseline = json.loads((self.root / 'baseline-live.json').read_text())
        self.assertEqual(baseline['ids'], [self.old['id']])

    def test_no_pending_means_no_catalog_or_pages_package(self):
        (self.root / 'data/catalog.json').write_bytes(self.live_raw)
        shutil.rmtree((self.root / self.new['path'].lstrip('/')).parent)
        report = p.make_package(self.root, self.live_raw, self.output)
        self.assertEqual(report['canlida_olmayan_kayit_sayisi'], 0)
        self.assertFalse((self.output / '01-YENI-ASP-SAYFALARI.zip').exists())
        self.assertFalse((self.output / '02-KATALOG-ONAY-GEREKIR.json').exists())

    def test_multiple_runs_keep_unuploaded_pages_and_remember_live(self):
        first = p.make_package(self.root, self.live_raw, self.output)
        self.assertEqual(first['canlida_olmayan_kayit_sayisi'], 1)
        another = self.make_record('A second pending scholarly academic study', '10.1234/another')
        self.assertTrue(g.publish(another, self.root))
        g.catalog(self.root, self.cfg, {})
        second = p.make_package(self.root, self.live_raw, self.output)
        self.assertEqual(second['canlida_olmayan_kayit_sayisi'], 2)
        with zipfile.ZipFile(self.output / '01-YENI-ASP-SAYFALARI.zip') as z:
            self.assertEqual(len(z.namelist()), 4)
        newly_live = (self.root / 'data/catalog.json').read_bytes()
        done = p.make_package(self.root, newly_live, self.output)
        self.assertEqual(done['canlida_olmayan_kayit_sayisi'], 0)
        remembered = json.loads((self.root / 'baseline-live.json').read_text())['ids']
        self.assertEqual(set(remembered), {self.old['id'], self.new['id'], another['id']})
        with self.assertRaisesRegex(p.PackageError, 'eksik'):
            p.make_package(self.root, self.live_raw, self.output)

    def test_missing_previously_live_record_stops(self):
        raw = self.live_mutated(lambda doc: doc.update(items=[]))
        with self.assertRaisesRegex(p.PackageError, 'eksik'):
            p.make_package(self.root, raw, self.output)
        self.assertFalse(self.output.exists())

    def test_unrecognized_live_record_stops(self):
        def mutate(doc):
            x = dict(doc['items'][0])
            x['id'] = 'abcdef123456'
            x['path'] = '/arsiv/unrelated-abcdef123456/default.asp'
            doc['items'].append(x)
        with self.assertRaisesRegex(p.PackageError, 'depoda bulunmayan'):
            p.make_package(self.root, self.live_mutated(mutate), self.output)

    def test_changed_existing_record_stops(self):
        raw = self.live_mutated(lambda doc: doc['items'][0].update(title='Canlıda değiştirildi'))
        with self.assertRaisesRegex(p.PackageError, 'farklı'):
            p.make_package(self.root, raw, self.output)

    def test_changed_category_stops(self):
        raw = self.live_mutated(lambda doc: doc['categories'][0].update(name='Yeni başlık'))
        with self.assertRaisesRegex(p.PackageError, 'kategori'):
            p.make_package(self.root, raw, self.output)

    def test_tampered_asp_stops(self):
        page = self.root / self.new['path'].lstrip('/')
        page.write_text(page.read_text(encoding='utf-8') + '<% unsafe %>', encoding='utf-8')
        with self.assertRaisesRegex(p.PackageError, 'şablonla uyuşmuyor'):
            p.make_package(self.root, self.live_raw, self.output)

    def test_metadata_and_catalog_mismatch_stops(self):
        path = self.root / self.new['path'].lstrip('/')
        meta = path.with_name('meta.json')
        m = json.loads(meta.read_text(encoding='utf-8'))
        m['title'] = 'Farklı başlık'
        meta.write_text(json.dumps(m, ensure_ascii=False), encoding='utf-8')
        with self.assertRaisesRegex(p.PackageError, 'meta dosyası uyuşmuyor'):
            p.make_package(self.root, self.live_raw, self.output)

    def test_unsafe_path_and_duplicate_id_rejected(self):
        for path in ('/arsiv/../../data/catalog.json', '/arsiv/example/default.asp', 'https://evil.test/default.asp'):
            with self.subTest(path=path):
                raw = self.live_mutated(lambda doc: doc['items'][0].update(path=path))
                with self.assertRaises(p.PackageError):
                    p.decode_catalog(raw, 'Canlı')
        raw = self.live_mutated(lambda doc: doc['items'].append(dict(doc['items'][0])))
        with self.assertRaisesRegex(p.PackageError, 'yinelenen'):
            p.decode_catalog(raw, 'Canlı')

    def test_orphan_archive_folder_stops(self):
        (self.root / 'arsiv' / 'orphan-folder').mkdir()
        with self.assertRaisesRegex(p.PackageError, 'kataloğu uyuşmuyor'):
            p.make_package(self.root, self.live_raw, self.output)

    def test_symlink_and_arbitrary_output_refused(self):
        page = self.root / self.new['path'].lstrip('/')
        original = page.read_bytes()
        page.unlink()
        target = self.base / 'outside.asp'
        target.write_bytes(original)
        page.symlink_to(target)
        with self.assertRaisesRegex(p.PackageError, 'bağlantı'):
            p.make_package(self.root, self.live_raw, self.output)
        page.unlink()
        page.write_bytes(original)
        with self.assertRaisesRegex(p.PackageError, 'çıktı yolu'):
            p.make_package(self.root, self.live_raw, self.base)

    def test_dont_delete_non_package_output(self):
        self.output.mkdir()
        (self.output / 'important.txt').write_text('do not delete')
        with self.assertRaisesRegex(p.PackageError, 'önceki paket'):
            p.make_package(self.root, self.live_raw, self.output)
        self.assertEqual((self.output / 'important.txt').read_text(), 'do not delete')

    def test_provider_failure_does_not_write_to_live_site(self):
        with patch.object(p.urllib.request, 'urlopen', side_effect=URLError('offline')) as mock:
            with self.assertRaises(URLError):
                p.read_live_catalog()
            req = mock.call_args.args[0]
            self.assertEqual(req.get_method(), 'GET')
            self.assertTrue(req.full_url.startswith(p.LIVE_URL + '?'))

    def test_canonical_rejects_untrusted_path(self):
        record = dict(self.new, path='/arsiv/abc/default.asp\r\nLink: evil')
        with self.assertRaisesRegex(ValueError, 'Canonical'):
            g.build_asp(record)


if __name__ == '__main__':
    unittest.main()
