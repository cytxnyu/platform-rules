#!/usr/bin/env python3
"""Search archived page text with exact paths, URL, and retrieval status."""
import argparse
import json
from pathlib import Path
import re
import sys

if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('query');p.add_argument('--platform',default='all');p.add_argument('--limit',type=int,default=25);args=p.parse_args()
 root=Path(__file__).resolve().parents[1];pattern=re.compile(args.query,re.I);hits=[];seen=set();seen_snapshots=set()
 for statefile in sorted((root/'archives').glob('*/state.json')):
  if args.platform!='all' and statefile.parent.name!=args.platform:continue
  state=json.loads(statefile.read_text(encoding='utf-8'))
  for record in state.values():
   current=record.get('current')
   if not current:continue
   if current['sha256'] in seen_snapshots:continue
   seen_snapshots.add(current['sha256'])
   textpath=root/current['text_path'];lines=textpath.read_text(encoding='utf-8').splitlines()
   for i,line in enumerate(lines):
    if pattern.search(line):
     if line in seen:continue
     seen.add(line)
     hits.append({'platform':statefile.parent.name,'source_id':record['id'],'title':record['title'],'url':record['url'],'file':current['text_path'],'line':i+1,'text':line[:1800],'context_before':lines[max(0,i-2):i],'context_after':lines[i+1:i+3],'snapshot_at':current['fetched_at'],'last_check':record.get('last_attempt_at'),'last_result':record['last_result'],'human_verified':current.get('human_verified',False)})
 hits.sort(key=lambda h:(len(h['text'])<35,-len(pattern.findall(h['text']))))
 print(json.dumps({'query':args.query,'hits':hits[:args.limit],'total_matches':len(hits),'limit':args.limit,'note':'Search matches are leads, not compliance conclusions. Check full clause and applicability.'},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
