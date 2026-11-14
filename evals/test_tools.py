import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import sync_rules as sr
import extract_content as ec

class ArchiveTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  self.s={'id':'example-rule','url':'https://example.com/rules','title':'Community policy','min_chars':100}
  self.good={'status':'fetched','text':'Community policy\n'+('Real rule: do not deceive readers.\n'*30),'raw':'<html>policy</html>','final_url':self.s['url'],'method':'http'}
 def tearDown(self):self.tmp.cleanup()
 def test_success_unchanged_and_revision(self):
  a=sr.archive_result(self.root,'x',self.s,self.good)
  b=sr.archive_result(self.root,'x',self.s,self.good,a)
  self.assertEqual(b['last_result'],'unchanged');self.assertEqual(a['current'],b['current'])
  c=sr.archive_result(self.root,'x',self.s,{**self.good,'text':self.good['text']+'New policy: label paid partnerships.'},b)
  self.assertEqual(c['last_result'],'changed');self.assertEqual(c['history'][0],a['current'])
  self.assertIn('label paid partnerships',(self.root/c['current']['diff_path']).read_text(encoding='utf-8'))
 def test_failed_update_preserves_good_archive(self):
  a=sr.archive_result(self.root,'x',self.s,self.good)
  for r in [{'status':'http_error','error':'404'},{'status':'fetch_error','error':'timeout'},{'status':'browser_error','error':'navigation failed','method':'browser'},{**self.good,'text':'Access denied'}]:
   b=sr.archive_result(self.root,'x',self.s,r,a)
   self.assertEqual(b['current'],a['current']);self.assertEqual(b['last_success_at'],a['last_success_at'])
 def test_truncation_is_quarantined(self):
  a=sr.archive_result(self.root,'x',self.s,self.good)
  b=sr.archive_result(self.root,'x',self.s,{**self.good,'text':'Community rules\n'+('Short text. '*15)},a)
  self.assertEqual(b['last_result'],'quarantined');self.assertEqual(a['current'],b['current'])
 def test_official_scope_and_path_safety(self):
  c={'official_hosts':['help.x.com'],'crawl_patterns':[r'help.x.com/en/rules-and-policies/']}
  links=[{'url':'https://help.x.com/en/rules-and-policies/new-policy','text':'Policy'},{'url':'https://help.x.com.evil.test/rules','text':'Policy'},{'url':'https://help.x.com/login','text':'Policy'}]
  self.assertEqual(len(sr.discover(links,c,set(),self.s)),1)
  with self.assertRaises(ValueError):sr.safe_id({'id':'../../escape'})
  self.assertFalse(sr.allowed('https://help.x.com@evil.test/rules',c))
 def test_parser_does_not_execute_or_include_scripts(self):
  r=sr.parse_page('<html><title>Policy</title><script>delete_all()</script><p>Actual rule</p><a href="/more">More rules</a></html>','https://example.com/rules')
  self.assertNotIn('delete_all',r['text']);self.assertIn('Actual rule',r['text']);self.assertEqual(r['links'][0]['url'],'https://example.com/more')
 def test_browser_section_failures_are_preserved(self):
  r=sr.archive_result(self.root,'x',self.s,{**self.good,'method':'browser','sections':[{'name':'extra','error':'login required'}]})
  self.assertEqual(r['current']['section_errors'][0]['error'],'login required')
 def test_future_effective_date_stays_hint(self):
  r=sr.archive_result(self.root,'x',self.s,{**self.good,'text':self.good['text']+'\n生效日期：2099年1月1日'})
  self.assertTrue(r['needs_policy_review']);self.assertFalse(r['current']['human_verified']);self.assertIn('2099',r['current']['effective_date_hints'][0])
 def test_old_capture_does_not_gain_current_fetch_date(self):
  r=sr.archive_result(self.root,'x',self.s,{**self.good,'method':'browser','rendered_at':'2025-01-01T00:00:00+00:00'})
  self.assertEqual(r['last_success_at'],'2025-01-01T00:00:00+00:00');self.assertEqual(r['current']['fetched_at'],r['last_success_at'])
  fresh=sr.archive_result(self.root,'x',self.s,self.good)
  stale=sr.archive_result(self.root,'x',self.s,{**self.good,'method':'browser','rendered_at':'2025-01-01T00:00:00+00:00'},fresh)
  self.assertEqual(stale['current'],fresh['current']);self.assertEqual(stale['last_success_at'],fresh['last_success_at'])
  with self.assertRaises(ValueError):sr.archive_result(self.root,'x',self.s,{**self.good,'rendered_at':'2099-01-01T00:00:00+00:00'})

class InputTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.args=argparse.Namespace(frame_interval=5,max_frames=120,export_audio=False)
 def tearDown(self):self.tmp.cleanup()
 def test_docx_paragraph_and_external_target(self):
  f=self.root/'sample.docx'
  with zipfile.ZipFile(f,'w') as z:
   z.writestr('word/document.xml','<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>保证收益</w:t></w:r></w:p></w:body></w:document>')
   z.writestr('word/_rels/document.xml.rels','<Relationships><Relationship TargetMode="External" Target="https://example.com/landing" Type="hyperlink"/></Relationships>')
  r=ec.extract(f,self.root/'out',self.args)
  self.assertEqual(r['segments'][0]['text'],'保证收益');self.assertIn('段落1',r['segments'][0]['location']);self.assertEqual(r['external_links'][0]['target'],'https://example.com/landing')
 def test_subtitle_preserves_timestamp_and_partial_review(self):
  f=self.root/'s.srt';f.write_text('1\n00:00:01,000 --> 00:00:03,000\nAI生成演示\n',encoding='utf-8')
  r=ec.extract(f,self.root/'out',self.args)
  self.assertTrue(any('-->' in s['text'] for s in r['segments']));self.assertTrue(r['unreviewed'])
 def test_unsupported_is_explicit(self):
  f=self.root/'old.doc';f.write_bytes(b'legacy format')
  r=ec.extract(f,self.root/'out',self.args)
  self.assertEqual(r['status'],'unsupported');self.assertTrue(r['unreviewed'])
 def test_chinese_legacy_encoding_is_not_misread_as_utf16(self):
  text,encoding=ec.decode('中文素材'.encode('gb18030'))
  self.assertEqual(text,'中文素材');self.assertEqual(encoding,'gb18030')
 def test_office_entity_is_not_processed(self):
  f=self.root/'unsafe.docx'
  with zipfile.ZipFile(f,'w') as z:z.writestr('word/document.xml','<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///private">]><x>&secret;</x>')
  r=ec.extract(f,self.root/'out',self.args);self.assertEqual(r['status'],'error');self.assertIn('DTD',r['error'])

if __name__=='__main__':unittest.main(verbosity=2)
