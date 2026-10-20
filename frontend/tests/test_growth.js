const fs=require('fs'),path=require('path'),vm=require('vm');
const analytics=require('../growth-analytics');
const labels=require('../growth-locales');
test('五语言漏斗和报价文案完整，示例来自合成评测证据',()=>{
 for(const values of Object.values(labels))for(const language of ['zh-CN','zh-TW','en','ja','ko'])assert(values[language]);
 const samples=JSON.parse(fs.readFileSync(path.join(__dirname,'../examples-data.json'),'utf8'));
 const outputs=JSON.parse(fs.readFileSync(path.join(__dirname,'../../docs/verification/2026-10-20-language-quality/outputs.json'),'utf8'));
 for(const [language,sample] of Object.entries(samples)){assert.equal(sample.proofreading.after,outputs['polish-'+language]);assert(sample.translation.before);assert(sample.translation.after);}
});
test('来源分类丢弃网址、查询词和任意推广值',()=>{
 assert.equal(analytics.source('https://www.google.com/search?q=private',''),'search');
 assert.equal(analytics.source('https://google.com.evil.test/private',''),'other');
 assert.equal(analytics.source('','?utm_source=private-email'),'campaign');
 assert.equal(analytics.source('','?utm_source=community'),'community');
});
function harness(extra={}){
 const sent=[],listeners={},storage=new Map();
 const root={navigator:{},location:{hostname:'fixepub.test',search:''},document:{readyState:'complete',referrer:''},crypto:{randomUUID:()=> 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'},sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},localStorage:{getItem:()=>null},FixEpubI18n:{locale:'ja'},matchMedia:()=>({matches:false}),fetch:(url,options)=>{sent.push({url,body:JSON.parse(options.body)});return Promise.resolve({ok:true});},addEventListener:(name,fn)=>listeners[name]=fn,...extra};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../growth-analytics.js'),'utf8'),{window:root,URL,URLSearchParams,Date});return {root,sent,listeners};
}
test('真实选文件事件仅发送分类和随机标识，不发送文件名或内容',()=>{
 const h=harness();h.listeners['fixepub:files-selected']({detail:[{name:'private-book.txt',content:'SECRET'}]});
 assert.equal(h.sent.length,2);assert.equal(h.sent[1].body.event,'file_selected');
 assert.deepEqual(Object.keys(h.sent[1].body).sort(),['device','event','language','session','source']);assert(!JSON.stringify(h.sent).includes('private-book'));assert(!JSON.stringify(h.sent).includes('SECRET'));
});
test('DNT 和本机测试不记录事件',()=>{
 assert.equal(harness({navigator:{doNotTrack:'1'}}).sent.length,0);
 assert.equal(harness({location:{hostname:'127.0.0.1'}}).sent.length,0);
});
