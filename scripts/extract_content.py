#!/usr/bin/env python3
"""Local, read-only content inventory. No automatic upload, OCR, or ASR."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')

MAX_MEMBER = 40 * 1024 * 1024
TEXT_EXT={'.txt','.md','.markdown','.srt','.vtt','.csv','.tsv','.json','.yaml','.yml','.log'}
IMAGE_EXT={'.png','.jpg','.jpeg','.webp','.gif','.bmp','.tif','.tiff'}
VIDEO_EXT={'.mp4','.mov','.mkv','.webm','.avi','.m4v'}
AUDIO_EXT={'.wav','.mp3','.m4a','.aac','.flac','.ogg'}

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()

def decode(data):
    encodings=['utf-16','utf-8-sig','gb18030'] if data.startswith((b'\xff\xfe',b'\xfe\xff')) else ['utf-8-sig','gb18030']
    for enc in encodings:
        try:return data.decode(enc),enc
        except UnicodeError: pass
    return data.decode('utf-8',errors='replace'),'utf-8-replacement'

def xml_text(elem):
    return ''.join(e.text or '' for e in elem.iter() if e.tag.rsplit('}',1)[-1] in ('t','v'))

def read_xml(z,name):
    if z.getinfo(name).file_size>MAX_MEMBER: raise ValueError('Oversized XML member: '+name)
    data=z.read(name)
    if b'<!DOCTYPE' in data or b'<!ENTITY' in data: raise ValueError('Unsupported XML DTD/entity declaration')
    return ET.fromstring(data)

def office(path,out,rec):
    segments=rec['segments']; ext=path.suffix.lower()
    with zipfile.ZipFile(path) as z:
        if sum(i.file_size for i in z.infolist())>300*1024*1024: raise ValueError('Office archive expands beyond 300 MiB')
        names=z.namelist()
        if ext=='.docx':
            parts=[n for n in names if re.match(r'word/(document|header\d+|footer\d+|footnotes|endnotes|comments)\.xml$',n)]
            for n in parts:
                tree=read_xml(z,n)
                for i,p in enumerate((e for e in tree.iter() if e.tag.endswith('}p')),1):
                    t=xml_text(p)
                    if t.strip():segments.append({'location':f'{n} / 段落{i}','text':t})
            rec['unreviewed'].append('排版、文本框位置、图片OCR、超链接目标与视觉语境需另查')
        elif ext=='.pptx':
            parts=sorted([n for n in names if re.match(r'ppt/(slides/slide|notesSlides/notesSlide)\d+\.xml$',n)],key=lambda n:(0 if '/slides/' in n else 1,int(re.search(r'(\d+)\.xml$',n).group(1))))
            for n in parts:
                tree=read_xml(z,n)
                for i,p in enumerate((e for e in tree.iter() if e.tag.endswith('}p')),1):
                    t=xml_text(p)
                    if t.strip():segments.append({'location':f'{n} / 文本块{i}','text':t})
            rec['unreviewed'].append('图表、隐藏页状态、动画与视觉语境需另查')
        elif ext=='.xlsx':
            strings=[]
            if 'xl/sharedStrings.xml' in names: strings=[xml_text(e) for e in read_xml(z,'xl/sharedStrings.xml') if e.tag.endswith('}si')]
            for n in sorted(n for n in names if re.match(r'xl/worksheets/sheet\d+\.xml$',n)):
                for c in read_xml(z,n).iter():
                    if not c.tag.endswith('}c'):continue
                    value=xml_text(c)
                    if c.get('t')=='s' and value.isdigit(): value=strings[int(value)]
                    formula=next((e.text for e in c if e.tag.endswith('}f')),None)
                    if value or formula:segments.append({'location':f"{n} / {c.get('r','?')}",'text':value,'formula':formula})
            rec['unreviewed'].append('单元格文本及公式已提取；公式未重算，图表、隐藏状态、图片和批注需另查')
        for i,n in enumerate(n for n in names if '/media/' in n and Path(n).suffix.lower() in IMAGE_EXT):
            if z.getinfo(n).file_size>MAX_MEMBER:rec['unreviewed'].append('嵌入图片过大未提取：'+n);continue
            target=out/(f'media-{i+1}'+Path(n).suffix.lower());target.write_bytes(z.read(n))
            rec['media'].append({'source_part':n,'path':str(target.resolve()),'needs_visual_review':True})
        rels=[]
        for n in names:
            if not n.endswith('.rels'):continue
            for r in read_xml(z,n):
                if r.get('TargetMode')=='External':rels.append({'part':n,'target':r.get('Target'),'type':r.get('Type')})
        rec['external_links']=rels

def media(path,out,rec,args):
    probe=shutil.which('ffprobe'); ffmpeg=shutil.which('ffmpeg')
    if not probe: rec['unreviewed'].append('未找到ffprobe，媒体时长和音轨未读取'); return
    result=subprocess.run([probe,'-v','error','-show_format','-show_streams','-of','json',str(path)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=60)
    if result.returncode:raise RuntimeError(result.stderr[:1000])
    info=json.loads(result.stdout);rec['probe']=info
    duration=float(info.get('format',{}).get('duration',0)); streams=info.get('streams',[])
    rec['unreviewed'].append('未执行OCR／ASR或人工听看；提取媒体不等于内容已审查')
    if not ffmpeg:rec['unreviewed'].append('未找到ffmpeg，无法导出画面／音轨');return
    if path.suffix.lower() in VIDEO_EXT and duration>0:
        step=max(args.frame_interval,duration/args.max_frames)
        frames=out/'frames';frames.mkdir(exist_ok=True)
        command=[ffmpeg,'-hide_banner','-loglevel','error','-nostdin','-i',str(path),'-vf',f'fps=1/{step},scale=960:-2','-frames:v',str(args.max_frames),str(frames/'frame-%06d.jpg')]
        r=subprocess.run(command,capture_output=True,text=True,timeout=max(90,min(900,int(duration)*2)))
        if r.returncode:rec['unreviewed'].append('抽帧失败：'+r.stderr[:300])
        rec['frames']=[{'path':str(f.resolve()),'approx_seconds':(i+0.5)*step} for i,f in enumerate(sorted(frames.glob('*.jpg')))]
        rec['sampling']={'mode':'periodic_samples_not_full_review','duration_seconds':duration,'interval_seconds':step,'max_frames':args.max_frames,'timestamp_note':'fps filter sample timestamps are approximate; verify against source video for precise citations'}
    if args.export_audio and any(s.get('codec_type')=='audio' for s in streams):
        target=out/'audio-16k-mono.wav'
        r=subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-nostdin','-i',str(path),'-vn','-ac','1','-ar','16000',str(target)],capture_output=True,text=True,timeout=600)
        if r.returncode:rec['unreviewed'].append('音频导出失败：'+r.stderr[:300])
        else:rec['audio_export']=str(target.resolve())

def extract(path,out,args):
    out.mkdir(parents=True,exist_ok=True)
    rec={'file':str(path.resolve()),'sha256':sha(path),'size_bytes':path.stat().st_size,'segments':[],'media':[],'unreviewed':[],'status':'extracted'}
    try:
        ext=path.suffix.lower()
        if ext in TEXT_EXT:
            text,enc=decode(path.read_bytes());rec['encoding']=enc
            rec['segments']=[{'location':f'行{i}','text':line} for i,line in enumerate(text.splitlines(),1) if line.strip()]
            if ext in ['.srt','.vtt']:rec['unreviewed'].append('字幕文本已读取，须与原片口播及画面核对；字幕不是全片审阅证据')
        elif ext in ['.docx','.pptx','.xlsx']:office(path,out,rec)
        elif ext=='.pdf':
            try:from pypdf import PdfReader
            except ImportError:raise RuntimeError('缺少可选依赖pypdf，PDF未提取')
            reader=PdfReader(str(path))
            if reader.is_encrypted:raise RuntimeError('PDF受密码保护，未读取')
            rec['page_count']=len(reader.pages)
            for i,p in enumerate(reader.pages,1):
                text=p.extract_text() or '';rec['segments'].append({'location':f'第{i}页','text':text})
                if len(text.strip())<30:rec['unreviewed'].append(f'第{i}页文本不足，需渲染／OCR确认')
            rec['unreviewed'].append('PDF图片、布局、小字和嵌入链接需视觉补查')
        elif ext in IMAGE_EXT:
            rec['media'].append({'path':str(path.resolve()),'needs_visual_review':True})
            try:
                from PIL import Image
                with Image.open(path) as im:rec['image']={'width':im.width,'height':im.height,'frames':getattr(im,'n_frames',1),'format':im.format}
            except ImportError:pass
            rec['unreviewed'].append('图片尚未视觉读取／OCR；动图需要覆盖所有相关帧')
        elif ext in VIDEO_EXT|AUDIO_EXT:media(path,out,rec,args)
        else:rec['status']='unsupported';rec['unreviewed'].append('当前提取器不支持此格式，需适当工具转换／读取')
    except Exception as e:rec['status']='error';rec['error']=str(e)
    return rec

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('input');p.add_argument('--out',required=True)
    p.add_argument('--frame-interval',type=float,default=5);p.add_argument('--max-frames',type=int,default=120);p.add_argument('--export-audio',action='store_true');args=p.parse_args()
    if args.frame_interval<=0 or args.max_frames<=0:p.error('Frame interval and max frames must be positive')
    src=Path(args.input).resolve();out=Path(args.out).resolve()
    if not src.exists():p.error('Input path does not exist: '+str(src))
    if out.exists() and any(out.iterdir()):p.error('Output must be new or empty; preserve previous review output')
    files=[src] if src.is_file() else [f for f in src.rglob('*') if f.is_file() and out not in f.parents]
    out.mkdir(parents=True,exist_ok=True)
    records=[extract(f,out/f'{i+1:03d}-{hashlib.sha256(str(f).encode()).hexdigest()[:8]}',args) for i,f in enumerate(files)]
    manifest={'input':str(src),'automatic_review_complete':False,'files':records}
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    lines=[]
    for r in records:
        lines += ['# '+r['file'],'状态：'+r['status']]
        for s in r['segments']:lines += ['['+s['location']+']',s['text']]
        lines += ['未核验：'+item for item in r['unreviewed']]
        if r.get('error'):lines.append('错误：'+r['error'])
    (out/'extracted.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({'files':len(records),'segments':sum(len(r['segments']) for r in records),'errors':sum(r['status'] in ['error','unsupported'] for r in records),'manifest':str(out/'manifest.json')},ensure_ascii=False))
if __name__=='__main__':main()
