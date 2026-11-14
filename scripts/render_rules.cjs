#!/usr/bin/env node
// Optional fallback: reads public official pages in an isolated browser. No login or publishing.
const fs=require('node:fs/promises');
const path=require('node:path');
const crypto=require('node:crypto');
const {chromium}=require('playwright');
const args=process.argv.slice(2);
const arg=(name,fallback)=>{const i=args.indexOf(name);return i<0?fallback:args[i+1];};
const root=path.resolve(arg('--root',path.join(__dirname,'..')));
const out=path.resolve(arg('--out',path.join(root,'browser-staging')));
const platform=arg('--platform','all');
const max=Number(arg('--max-pages','80'));
const only=arg('--source',null);
const sourceIds=only?new Set(only.split(',')):null;
const readJson=async(p,d)=>{try{return JSON.parse(await fs.readFile(p,'utf8'));}catch(e){if(e.code==='ENOENT')return d;throw e;}};

(async()=>{
 await fs.mkdir(out,{recursive:true});
 const configs=(await fs.readdir(path.join(root,'sources'))).filter(n=>n.endsWith('.json'));
 const jobs=[];
 for(const file of configs){
   const c=await readJson(path.join(root,'sources',file)); if(platform!=='all'&&c.platform!==platform)continue;
   const cat=await readJson(path.join(root,'archives',c.platform,'catalog.json'),c.sources);
   const state=await readJson(path.join(root,'archives',c.platform,'state.json'),{});
   for(const s of cat){
     if(sourceIds&&!sourceIds.has(s.id))continue;
     if(!only&&['created','changed','unchanged'].includes(state[s.id]?.last_result)&&(!s.index||args.includes('--failures-only')))continue;
     if(!c.official_hosts.includes(new URL(s.url).hostname))continue;
     jobs.push({c,s,seed:c.sources.some(x=>x.id===s.id)});
   }
 }
 jobs.sort((a,b)=>Number(b.seed)-Number(a.seed));
 const launch={headless:true};
 if(arg('--channel','msedge')!=='chromium')launch.channel=arg('--channel','msedge');
 const browser=await chromium.launch(launch);
 let cursor=0,done=0;
 async function worker(){
  while(cursor<Math.min(jobs.length,max)){
   const {c,s}=jobs[cursor++];
   const page=await browser.newPage({locale:c.platform==='tiktok'||c.platform==='x'?'en-US':'zh-CN'});
   const stem=path.join(out,c.platform+'__'+s.id);
   try{
    await page.goto(s.url,{waitUntil:'domcontentloaded',timeout:22000});
    await page.waitForTimeout(2200);
    const firstUrl=page.url();
    if(!c.official_hosts.includes(new URL(firstUrl).hostname))throw new Error('Redirect destination outside registered official hosts: '+firstUrl);
    let text=await page.locator('body').innerText();
    const initialHtml=await page.content();
    let links=await page.locator('a[href]').evaluateAll(a=>a.map(x=>({text:x.innerText,url:x.href})));
    const sections=[];
    // These rule directories use buttons rather than hyperlinks. Traverse only known read-only tabs.
    const tabs=c.platform==='bilibili'&&s.id==='community'?['违规行为判断','违规行为处置','违法违禁','违背公序良俗','色情低俗','危害或影响未成年人身心健康','违背科学与真实性','不友善行为','违背创作公平','低质及不良观感','扰乱平台秩序','投稿类型错误','撞车及重复投稿问题','标签（TAG）违规问题','非通用语种字幕翻译问题','主动添加创作声明','相关附则']:
      c.platform==='tiktok'&&['community','new-overview'].includes(s.id)?['Community Principles','Youth Safety and Well-Being','Safety and Civility','Mental and Behavioral Health','Sensitive and Mature Themes','Integrity and Authenticity','Regulated Goods, Services, and Commercial Activities','Privacy and Security','For You feed Eligibility Standards','Accounts and Features','Enforcement']:[];
    for(const name of tabs){
      try{
       if(c.platform==='bilibili'){
        await page.goto(s.url,{waitUntil:'domcontentloaded',timeout:18000});await page.waitForTimeout(800);
        await page.getByText(name,{exact:true}).first().click({timeout:5000});
       } else await page.getByText(name,{exact:true}).first().click({timeout:4000});
       await page.waitForTimeout(1700);
       const sectionUrl=page.url();
       if(!c.official_hosts.includes(new URL(sectionUrl).hostname))throw new Error('External navigation');
       const body=await page.locator('body').innerText();
       sections.push({name,url:sectionUrl,text:body,html:await page.content()});
       links.push({text:name,url:sectionUrl});
       links.push(...await page.locator('a[href]').evaluateAll(a=>a.map(x=>({text:x.innerText,url:x.href}))));
      }catch(e){sections.push({name,error:e.message.slice(0,200)});}
    }
    if(sections.length)text+='\n\n'+sections.filter(x=>x.text).map(x=>'===== '+x.name+' | '+x.url+' =====\n'+x.text).join('\n\n');
    const result={url:s.url,final_url:firstUrl,title:await page.title(),text,links,sections,rendered_at:new Date().toISOString(),source:s.id,platform:c.platform};
    await fs.writeFile(stem+'.html',initialHtml);
    await fs.writeFile(stem+'.json',JSON.stringify(result,null,2));
    console.log(JSON.stringify({platform:c.platform,id:s.id,chars:text.length,sections:sections.length,section_errors:sections.filter(x=>x.error).length}));
   }catch(e){await fs.writeFile(stem+'.error.json',JSON.stringify({source:s.id,platform:c.platform,url:s.url,error:e.message,at:new Date().toISOString()},null,2));console.log(JSON.stringify({platform:c.platform,id:s.id,error:e.message.slice(0,200)}));}
   await page.close();done++;
  }
 }
 await Promise.all([worker(),worker()]);
 await browser.close();
 console.log(JSON.stringify({attempted:done,eligible:jobs.length,deferred:Math.max(0,jobs.length-done),out}));
})().catch(e=>{console.error(e);process.exitCode=1;});
