#!/usr/bin/env python3
"""Archive official public rules, keep revisions, and expose coverage gaps. Stdlib only."""
import argparse
import concurrent.futures
import contextlib
import datetime as dt
import difflib
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ('douyin', 'xiaohongshu', 'bilibili', 'wechat-channels', 'tiktok', 'x', 'instagram')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36'
MAX_BYTES = 12 * 1024 * 1024
KEYWORDS = re.compile(r'规则|规范|公约|协议|申诉|违规|公告|准入|审核|政策|准则|导流|变现|rule|polic|guideline|standard|monetiz|appeal|branded|commercial|recommendation', re.I)

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')

def load_json(path, default=None):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else default

def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temp, path)

def canonical(url):
    p = urlsplit(html.unescape(url))
    skip = {'utm_source','utm_medium','utm_campaign','utm_content','utm_term','fbclid','from_module','spm_id_from','from_spmid','share_source','btm_ppre','btm_pre','btm','enter_method'}
    query = urlencode([(k,v) for k,v in parse_qsl(p.query, keep_blank_values=True) if k not in skip])
    return urlunsplit((p.scheme.lower(),p.netloc.lower(),p.path or '/',query,''))

def allowed(url, config):
    p = urlsplit(url)
    return p.scheme in ('https','http') and not p.username and not p.password and p.hostname in config['official_hosts']

def safe_id(source):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,95}', source['id']):
        raise ValueError('Unsafe source id')
    return source['id']

class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[]; self.links=[]; self.hidden=0; self.in_title=False; self.title=[]; self.anchor=None
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in ('script','style','noscript','svg'): self.hidden += 1
        if tag=='title': self.in_title=True
        if self.hidden: return
        if tag=='a' and attrs.get('href'): self.anchor={'url':attrs['href'],'text':''}
        if tag in ('p','div','br','li','h1','h2','h3','h4','section','article','tr','td'): self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript','svg') and self.hidden: self.hidden-=1
        if tag=='title': self.in_title=False
        if tag=='a' and self.anchor: self.links.append(self.anchor); self.anchor=None
        if tag in ('p','div','li','section','article','tr'): self.parts.append('\n')
    def handle_data(self,data):
        if self.hidden: return
        if self.in_title: self.title.append(data)
        self.parts.append(data)
        if self.anchor: self.anchor['text'] += data
    def result(self,base):
        text='\n'.join(re.sub(r'[ \t\u00a0]+',' ',line).strip() for line in ''.join(self.parts).splitlines())
        text=re.sub(r'\n{3,}','\n\n',text).strip()
        links=[{'url':canonical(urljoin(base,x['url'])),'text':x['text'].strip()} for x in self.links if not x['url'].startswith(('javascript:','mailto:','tel:','#'))]
        return {'text':text,'title':''.join(self.title).strip(),'links':links}

def parse_page(raw, base):
    p=PageParser(); p.feed(raw); return p.result(base)

def quality(text, source):
    compact=re.sub(r'\s+',' ',text).strip()
    if len(compact)<int(source.get('min_chars',500)): return 'insufficient_text'
    blocked=re.compile(r'access denied|request blocked|verify you are human|just a moment|robot check|请完成.{0,8}验证|安全验证|网页无法访问|页面不存在|页面已失效|系统繁忙', re.I)
    if blocked.search(compact[:1000]) and len(compact)<3000: return 'challenge_or_error'
    if not KEYWORDS.search(compact): return 'unrecognized_content'
    return None

def dates_in(text):
    # Hints only: never infer an effective date from publication or fetch dates.
    found=[]
    for line in text.splitlines():
        if re.search(r'生效|施行|effective',line,re.I) and re.search(r'20\d{2}',line):
            found.append(line.strip()[:400])
    return found[:20]

class CheckedRedirect(HTTPRedirectHandler):
    def __init__(self,config): self.config=config
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if not allowed(newurl,self.config): raise ValueError('Redirect to an unregistered official host: '+newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

HOST_GATES={}; HOST_LOCK=threading.Lock()
def fetch(source, config, old, timeout, root=ROOT):
    url=source['url']
    if not allowed(url,config): return {'status':'unapproved_host','error':url}
    with HOST_LOCK: gate=HOST_GATES.setdefault(urlsplit(url).hostname,threading.Semaphore(2))
    headers={'User-Agent':UA,'Accept':'text/html,application/xhtml+xml','Accept-Language':config.get('accept_language','en,zh;q=0.8')}
    if old.get('current') and (root/old['current']['text_path']).exists():
        for k,h in [('etag','If-None-Match'),('last_modified','If-Modified-Since')]:
            if old.get(k): headers[h]=old[k]
    with gate:
        try:
            with build_opener(CheckedRedirect(config)).open(Request(url,headers=headers),timeout=timeout) as r:
                final=canonical(r.url)
                if not allowed(final,config): return {'status':'unapproved_host','error':final}
                data=r.read(MAX_BYTES+1)
                if len(data)>MAX_BYTES: return {'status':'too_large','error':'Response exceeds 12 MiB'}
                if 'html' not in r.headers.get('Content-Type','').lower(): return {'status':'unsupported_format','error':r.headers.get('Content-Type','')}
                raw=data.decode(r.headers.get_content_charset() or 'utf-8',errors='replace')
                result=parse_page(raw,final)
                result.update(status='fetched',raw=raw,final_url=final,method='http',etag=r.headers.get('ETag'),last_modified=r.headers.get('Last-Modified'))
                return result
        except HTTPError as e:
            if e.code==304 and old.get('current'): return {'status':'not_modified','method':'http'}
            return {'status':'http_error','error':str(e),'http_status':e.code}
        except Exception as e: return {'status':'fetch_error','error':str(e)}
        finally: time.sleep(0.15)

def archive_result(root, platform, source, result, old=None):
    old=old or {}; imported_at=now(); stamp=result.get('rendered_at') or result.get('retrieved_at') or imported_at; rec=dict(old)
    # Importing an old capture must not make it appear freshly fetched.
    parsed_stamp=dt.datetime.fromisoformat(stamp.replace('Z','+00:00'))
    if parsed_stamp.tzinfo is None or parsed_stamp > dt.datetime.now(dt.timezone.utc)+dt.timedelta(minutes=5):
        raise ValueError('Capture time must include timezone and cannot be in the future')
    previous_checks=[dt.datetime.fromisoformat(old[k].replace('Z','+00:00')) for k in ('last_success_at','last_attempt_at') if old.get(k)]
    if previous_checks and parsed_stamp < max(previous_checks):
        rec.update(last_import_attempt_at=imported_at,last_import_result='older_capture_ignored')
        return rec
    rec.update(id=safe_id(source),url=source['url'],title=source['title'],last_attempt_at=stamp,last_result=result['status'],last_method=result.get('method','http'))
    if result['status']=='not_modified':
        rec.update(last_success_at=stamp,last_result='unchanged'); return rec
    if result['status']!='fetched':
        rec['last_error']=result.get('error',result['status'])
        rec['last_failure']={'at':stamp,'status':result['status'],'method':result.get('method','http'),'error':rec['last_error']}
        return rec
    reason=quality(result['text'],source)
    text=result['text'].replace('\r\n','\n').replace('\r','\n').strip()+'\n'
    digest=hashlib.sha256(text.encode('utf-8')).hexdigest()
    previous=old.get('current')
    if previous and previous['sha256']==digest and not reason:
        rec.update(last_success_at=stamp,last_result='unchanged',last_method=result['method'],etag=result.get('etag'),last_modified=result.get('last_modified'))
        rec.pop('last_error',None); return rec
    if previous and len(text)<previous['chars']*0.6 and not reason: reason='large_truncation_requires_review'
    folder=root/'archives'/platform/'snapshots'/source['id']
    folder.mkdir(parents=True,exist_ok=True)
    prefix=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-'+digest[:12]
    # Failed/challenge bodies are diagnostic evidence, never replacements for good rules.
    raw_path=folder/(prefix+('.web.txt' if result['method']=='web' else '.html')); text_path=folder/(prefix+'.txt')
    raw_path.write_text(result.get('raw',''),encoding='utf-8'); text_path.write_text(text,encoding='utf-8',newline='\n')
    snap={'sha256':digest,'hash_basis':'utf8_text_lf_newlines','chars':len(text),'fetched_at':stamp,'imported_at':imported_at,'method':result['method'],'final_url':result['final_url'],'title_observed':result.get('title',''),'raw_path':raw_path.relative_to(root).as_posix(),'text_path':text_path.relative_to(root).as_posix(),'effective_date_hints':dates_in(text),'human_verified':False,'coverage':{'browser':'rendered_page_text','web':'tool_extracted_text','http':'static_html_text'}[result['method']]}
    if result.get('sections'):
        section_path=folder/(prefix+'.sections.json')
        save_json(section_path,result['sections']);snap['sections_path']=section_path.relative_to(root).as_posix()
        snap['section_errors']=[s for s in result['sections'] if s.get('error')]
    if result.get('line_coverage'):snap['line_coverage']=result['line_coverage']
    if reason:
        rec.update(last_result='needs_browser' if reason=='insufficient_text' and result['method']=='http' else 'quarantined',last_error=reason,latest_failed_snapshot=snap)
        return rec
    if previous:
        previous_text=(root/previous['text_path']).read_text(encoding='utf-8')
        diff_path=folder/(prefix+'.diff')
        diff_path.write_text('\n'.join(difflib.unified_diff(previous_text.splitlines(),text.splitlines(),fromfile=previous['text_path'],tofile=snap['text_path'],lineterm=''))+'\n',encoding='utf-8')
        snap['diff_path']=diff_path.relative_to(root).as_posix()
        rec.setdefault('history',[]).append(previous)
    rec.update(current=snap,last_result='changed' if previous else 'created',last_success_at=stamp,last_method=result['method'],needs_policy_review=True,etag=result.get('etag'),last_modified=result.get('last_modified'))
    rec.pop('last_error',None)
    return rec

def discover(links, config, known, source):
    result=[]
    for link in links:
        url=canonical(link['url']); title=link.get('text','').strip()
        if url in known or not allowed(url,config): continue
        if not any(re.search(pattern,url) for pattern in config.get('crawl_patterns',[])): continue
        if not KEYWORDS.search(title+' '+url): continue
        if re.search(r'privacy|cookie|隐私|隐私权|隐私政策',title+' '+url,re.I) and not re.search(r'violation|违规',title+' '+url,re.I): continue
        if re.search(r'\.(png|jpg|jpeg|gif|mp4|zip)(?:\?|$)',url,re.I): continue
        known.add(url)
        result.append({'id':'page-'+hashlib.sha256(url.encode()).hexdigest()[:16],'title':title[:180] or url,'url':url,'kind':'linked_document','scopes':source.get('scopes',[]),'region':source.get('region','unspecified'),'language':source.get('language','unspecified'),'provenance':{'url':source['url'],'relation':'official_page_link'},'discovered_at':now(),'index':False})
    return result

@contextlib.contextmanager
def archive_lock(root):
    path=root/'archives'/'.update.lock'; path.parent.mkdir(parents=True,exist_ok=True)
    try: fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError: raise SystemExit('Another update may be running. Inspect archives/.update.lock; do not delete an active lock.')
    try:
        os.write(fd,json.dumps({'pid':os.getpid(),'started_at':now()}).encode()); os.close(fd); yield
    finally: path.unlink(missing_ok=True)

def write_index(root, platform, config, state, catalog):
    total=len(catalog); good=sum(bool(state.get(s['id'],{}).get('current')) for s in catalog)
    lines=[f"# {config['name']}官方规则档案",'',f'生成时间（UTC）：{now()}',f'已登记 {total} 个来源，{good} 个已有可读正文。这里只表示登记来源的获取情况，不表示平台全部规则已穷尽。','', '完整性状态：**未证明全量覆盖**。动态目录、登录页、未发现的专项规则和地区差异必须继续核验。','', '| 来源 | 类型 / 范围 | 最近结果 | 正文 |','|---|---|---|---|']
    for s in catalog:
        r=state.get(s['id'],{}); cur=r.get('current')
        body=f"[正文]({Path(cur['text_path']).relative_to(Path('archives')/platform).as_posix()})" if cur else '尚未取得'
        title=re.sub(r'\s+',' ',s['title']).replace('|','／')
        lines.append(f"| [{title}]({s['url']}) | {s['kind']} / {', '.join(s.get('scopes',[]))} | {r.get('last_result','not_checked')} | {body} |")
    lines += ['', '元数据与历史：`state.json`；完整来源：`catalog.json`；版本正文、原始页面和差异：`snapshots/`。','导航页、服务入口与规则正文分别使用，不能把页面可访问等同于规则已读。']
    (root/'archives'/platform/'INDEX.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')

def sync(args):
    root=Path(args.root).resolve(); chosen=PLATFORMS if args.platform=='all' else [args.platform]
    report={'started_at':now(),'platforms':{},'complete':False,'note':'Registered sources only; no claim of exhaustive platform coverage.'}
    with archive_lock(root):
        for platform in chosen:
            config=load_json(root/'sources'/f'{platform}.json')
            adir=root/'archives'/platform; adir.mkdir(parents=True,exist_ok=True)
            catalog=load_json(adir/'catalog.json',[])
            byurl={canonical(s['url']):s for s in catalog}
            for s in config['sources']: byurl[canonical(s['url'])]=s
            catalog=list(byurl.values()); state=load_json(adir/'state.json',{})
            selected=[s for s in catalog if (not args.entry_only or s.get('index')) and (not args.pending_only or s['id'] not in state)]
            known=set(byurl); queue=selected[:]; new_fetched=0; visited=set(); pending=[]
            while queue:
                batch=queue[:args.workers]; queue=queue[args.workers:]
                with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                    futures={pool.submit(fetch,s,config,state.get(s['id'],{}),args.timeout,root):s for s in batch}
                    for f in concurrent.futures.as_completed(futures):
                        s=futures[f]; visited.add(s['id']); result=f.result()
                        state[s['id']]=archive_result(root,platform,s,result,state.get(s['id']))
                        if result.get('links'):
                            extra=discover(result['links'],config,known,s); catalog.extend(extra)
                            for item in extra:
                                if not args.entry_only and new_fetched<args.max_new: queue.append(item); new_fetched+=1
                                else: pending.append(item['id'])
                save_json(adir/'catalog.json',catalog); save_json(adir/'state.json',state)
            write_index(root,platform,config,state,catalog)
            counts={}
            for sid in visited:
                status=state[sid]['last_result']; counts[status]=counts.get(status,0)+1
            report['platforms'][platform]={'attempted':len(visited),'registered':len(catalog),'readable':sum(bool(v.get('current')) for v in state.values()),'results':counts,'new_unfetched':pending}
            print(json.dumps({platform:report['platforms'][platform]},ensure_ascii=False),flush=True)
        report['finished_at']=now()
        report_path=root/'archives'/'runs'/(dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
        save_json(report_path,report); save_json(root/'archives'/'last-run.json',report)
    return 0

def import_browser(args):
    root=Path(args.root).resolve(); platform=args.platform
    if platform=='all': raise SystemExit('Import requires one platform')
    config=load_json(root/'sources'/f'{platform}.json'); adir=root/'archives'/platform
    with archive_lock(root):
        catalog=load_json(adir/'catalog.json',config['sources']); state=load_json(adir/'state.json',{})
        source=next((s for s in catalog if s['id']==args.source),None)
        if not source: raise SystemExit('Unknown source id')
        result=load_json(Path(args.input)); final=canonical(result.get('final_url',result.get('url','')))
        if result.get('error') and result.get('at'): result['rendered_at']=result['at']
        if not result.get('rendered_at') and not result.get('retrieved_at'):
            raise SystemExit('Import requires rendered_at or retrieved_at (actual capture time with timezone)')
        if canonical(result.get('url',''))!=canonical(source['url']): raise SystemExit('Browser requested URL must match source URL')
        if not allowed(final,config): raise SystemExit('Browser destination is not an approved official host')
        result.update(status='browser_error' if result.get('error') else 'fetched',method=args.method,final_url=final)
        rawpath=Path(args.input).with_suffix('.html')
        if rawpath.exists(): result['raw']=rawpath.read_text(encoding='utf-8')
        state[source['id']]=archive_result(root,platform,source,result,state.get(source['id']))
        known={canonical(s['url']) for s in catalog}; catalog.extend(discover(result.get('links',[]),config,known,source))
        save_json(adir/'state.json',state); save_json(adir/'catalog.json',catalog); write_index(root,platform,config,state,catalog)
        print(json.dumps({'platform':platform,'source':source['id'],'result':state[source['id']]['last_result'],'chars':len(result.get('text',''))},ensure_ascii=False))

def status(args):
    root=Path(args.root).resolve()
    for p in PLATFORMS if args.platform=='all' else [args.platform]:
        state=load_json(root/'archives'/p/'state.json',{}); cat=load_json(root/'archives'/p/'catalog.json',[])
        print(json.dumps({'platform':p,'registered':len(cat),'readable':sum(bool(x.get('current')) for x in state.values()),'pending':[{'id':s['id'],'title':s['title'],'result':state.get(s['id'],{}).get('last_result','not_checked')} for s in cat if state.get(s['id'],{}).get('last_result') not in ('created','changed','unchanged')]},ensure_ascii=False))

def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('command',choices=['sync','import-browser','import-page','status'])
    p.add_argument('--root',default=str(ROOT)); p.add_argument('--platform',choices=['all',*PLATFORMS],default='all')
    p.add_argument('--entry-only',action='store_true'); p.add_argument('--max-new',type=int,default=80)
    p.add_argument('--pending-only',action='store_true',help='Fetch newly registered sources with no attempt yet; not a substitute for freshness checks')
    p.add_argument('--workers',type=int,choices=range(1,7),default=4); p.add_argument('--timeout',type=int,default=15)
    p.add_argument('--source'); p.add_argument('--input'); p.add_argument('--method',choices=['browser','web'],default='browser'); args=p.parse_args()
    if args.command=='sync': return sync(args)
    if args.command in ('import-browser','import-page'): return import_browser(args)
    return status(args)
if __name__=='__main__': main()
