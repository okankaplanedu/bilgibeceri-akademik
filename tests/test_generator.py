import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'motor'))
import generator as g


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'site'
        self.root.mkdir()
        (self.root / 'index.html').write_text('ORIGINAL HOME', encoding='utf-8')
        (self.root / 'SUNUCUYA-YUKLEMEYIN.txt').write_text('ARŞİV VERİSİ', encoding='utf-8')
        (self.root / 'arsiv').mkdir()
        (self.root / 'data').mkdir()
        self.cat = {'id':'egitim','name':'Eğitim','context':'öğrenme araştırmaları',
                    'tokens':['learning','education'],'queries':['learning sciences']}
        self.cfg = {'site_root':str(self.root),'results_per_query_per_source':2,'token_query_limit':0,
                    'max_new_per_run':30,'timeout_seconds':5,'pause_seconds':0,
                    'sources':{'openalex':True,'crossref':True,'arxiv':False,'semanticscholar':False},
                    'categories':[self.cat]}
        self.first={'title':'A substantial scholarly reference title','authors':'Ada Researcher',
                    'year':2025,'doi':'10.1234/Example','url':'https://doi.org/10.1234/Example',
                    'provider_id':'https://openalex.org/W1','venue':'An example journal','work_type':'article'}

    def tearDown(self): self.tmp.cleanup()

    def test_dedup_idempotency_rebuild_and_includes(self):
        def oa(q,cfg): return [self.first]
        def crossref(q,cfg): return [dict(self.first, doi='https://doi.org/10.1234/example', provider_id='10.1234/example')]
        with patch.dict(g.FETCHERS, {'openalex':oa,'crossref':crossref}, clear=False):
            first=g.run(self.cfg,self.root)
            second=g.run(self.cfg,self.root)
        self.assertEqual(first['new'],1)
        self.assertEqual(second['new'],0)
        self.assertEqual(second['total'],1)
        self.assertEqual((self.root/'index.html').read_text(), 'ORIGINAL HOME')
        files=list((self.root/'arsiv').glob('*/default.asp'))
        self.assertEqual(len(files),1)
        page=files[0].read_text(encoding='utf-8')
        for name in ('head','header','asie','center','shorts','footer'):
            self.assertIn(f'<!--#include virtual="/{name}.asp" -->',page)
        self.assertEqual(page.count('<!--#include virtual='),6)
        self.assertEqual(g.catalog(self.root,self.cfg),1)
        cat=json.loads((self.root/'data'/'catalog.json').read_text(encoding='utf-8'))
        self.assertEqual(len(cat['items']),1)
        self.assertTrue((self.root/cat['items'][0]['path'].lstrip('/')).exists())

    def test_partial_failure_preserves_other_sources(self):
        def bad(q,cfg): raise urllib.error.HTTPError('https://example.invalid',429,'rate',None,None)
        def good(q,cfg): return [self.first]
        with patch.dict(g.FETCHERS, {'openalex':bad,'crossref':good}, clear=False):
            result=g.run(self.cfg,self.root)
        self.assertEqual(result['new'],1)
        self.assertEqual(result['sources']['OpenAlex']['state'],'error')
        self.assertEqual(result['sources']['Crossref']['state'],'ok')

    def test_one_source_success_then_rate_limit_is_partial_success(self):
        calls = []
        def sometimes(q, cfg):
            calls.append(q)
            if len(calls) == 1:
                return [self.first]
            raise urllib.error.HTTPError('https://example.invalid', 429, 'rate', None, None)
        cfg = dict(self.cfg, sources={'openalex': False, 'crossref': True, 'arxiv': False, 'semanticscholar': False})
        cfg['categories'] = [dict(self.cat, queries=['learning sciences', 'digital education'])]
        with patch.dict(g.FETCHERS, {'crossref': sometimes}, clear=False):
            result = g.run(cfg, self.root)
        self.assertEqual(result['new'], 1)
        self.assertEqual(result['sources']['Crossref']['state'], 'partial')
        self.assertEqual(result['sources']['Crossref']['message'], 'HTTP 429')

    def test_vbscript_injection_and_url_validation(self):
        self.assertEqual(g.safe_url('javascript:alert(1)'), '')
        self.assertEqual(g.safe_url('https://user:pass@example.com/'), '')
        self.assertEqual(g.normalized_doi('https://doi.org/10.1234/Aa'), '10.1234/aa')
        payload='Dangerous " : Response.Write("bad") : " %> Unexpected'
        literal=g.vb_literal(payload)
        self.assertNotIn('%>',literal)
        self.assertIn('""',literal)
        r=g.normalize(dict(self.first,title=payload),self.cat,'learning','openalex')
        r['id']='abcdef123456'
        r['path']='/arsiv/ornek-abcdef123456/default.asp'
        asp=g.build_asp(r)
        self.assertEqual(asp.count('%>'),2) # directive + real block closing only
        huge=('a %> " xyz ' * 400)
        lines=g.vb_assignment('bbTitle',huge).splitlines()
        self.assertGreater(len(lines),10)
        self.assertTrue(all(len(line)<900 for line in lines))
        self.assertTrue(all('%>' not in line for line in lines))

    def test_token_expansion_is_limited_and_distinct(self):
        c=dict(self.cat,queries=['learning sciences'],tokens=['learning','education','teaching'])
        self.assertEqual(g.expanded_queries(c,{'token_query_limit':1}), ['learning sciences','learning education'])
        self.assertEqual(g.expanded_queries(c,{'token_query_limit':0}), ['learning sciences'])
        c['queries']=['learning education']
        self.assertEqual(g.expanded_queries(c,{'token_query_limit':1}),['learning education'])

    def test_refresh_recreates_page_from_metadata(self):
        r=g.normalize(self.first,self.cat,'learning','openalex')
        self.assertTrue(g.publish(r,self.root))
        target=self.root/r['path'].lstrip('/')
        target.write_text('replaced',encoding='utf-8')
        self.assertEqual(g.refresh_pages(self.root),1)
        self.assertIn('<!--#include virtual="/head.asp" -->',target.read_text(encoding='utf-8'))

    def test_lock_blocks_overlapping_runs(self):
        with patch.object(g,'HERE',Path(self.tmp.name)):
            with g.single_run_lock():
                with self.assertRaises(RuntimeError):
                    with g.single_run_lock(): pass
            self.assertFalse((Path(self.tmp.name)/'.generator.lock').exists())

    def test_adapter_parsers(self):
        oa={'results':[{'title':'OpenAlex sample','id':'https://openalex.org/W1','doi':None,'publication_year':2024,
                        'authorships':[{'author':{'display_name':'A Author'}}], 'primary_location':{'source':{'display_name':'Journal'}},'type':'article'}]}
        cr={'message':{'items':[{'title':['Crossref sample'],'DOI':'10.1234/x','URL':'https://doi.org/10.1234/x',
                    'author':[{'given':'A','family':'B'}],'published':{'date-parts':[[2020,1,1]]},'container-title':['Journal']}]}}
        sem={'data':[{'title':'Semantic sample','paperId':'123','url':'https://www.semanticscholar.org/paper/123','year':2022,'externalIds':{'DOI':'10.1234/x'},'authors':[{'name':'A'}]}]}
        atom=b'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"><entry><id>https://arxiv.org/abs/2401.12345v2</id><title>Atom sample</title><published>2024-01-01T00:00:00Z</published><author><name>A Person</name></author><arxiv:doi>10.1234/abc</arxiv:doi></entry></feed>'''
        responses={'openalex':json.dumps(oa).encode(),'crossref':json.dumps(cr).encode(),'semanticscholar':json.dumps(sem).encode(),'arxiv':atom}
        def fake_request(url,params,cfg,provider): return responses[provider]
        with patch.object(g,'request_bytes',fake_request):
            self.assertEqual(g.fetch_openalex('q',self.cfg)[0]['authors'],'A Author')
            self.assertEqual(g.fetch_crossref('q',self.cfg)[0]['year'],'2020')
            self.assertEqual(g.fetch_semanticscholar('q',self.cfg)[0]['provider_id'],'123')
            self.assertEqual(g.fetch_arxiv('q',self.cfg)[0]['arxiv_id'],'2401.12345v2')

if __name__=='__main__': unittest.main()
