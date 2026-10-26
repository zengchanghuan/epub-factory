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
 assert.deepEqual(Object.keys(h.sent[1].body).sort(),['device','event','language','service','session','source']);assert(!JSON.stringify(h.sent).includes('private-book'));assert(!JSON.stringify(h.sent).includes('SECRET'));
});
test('DNT 和本机测试不记录事件',()=>{
 assert.equal(harness({navigator:{doNotTrack:'1'}}).sent.length,0);
 assert.equal(harness({location:{hostname:'127.0.0.1'}}).sent.length,0);
});
test('排除标记跨重载生效，当前页面停止发事件和归因，不改付款数据',()=>{
 let excluded=true;
 const storage={getItem:k=>k==='fixepub_analytics_excluded'&&excluded?'1':null};
 assert.equal(harness({localStorage:storage}).sent.length,0);
 excluded=false;
 const h=harness({localStorage:storage});assert.equal(h.sent.length,1);assert(h.root.FixEpubGrowth.session);
 excluded=true;
 h.listeners['fixepub:files-selected']({detail:[{name:'private.epub'}]});
 assert.equal(h.sent.length,1);assert.equal(h.root.FixEpubGrowth.session,undefined);
});
test('设置页开关可持久保存并撤销，存储失败明确提示',()=>{
 const values=new Map(),handlers={},box={checked:false,addEventListener:(n,fn)=>handlers[n]=fn},state={};
 let blocked=false;
 const local={getItem:k=>values.get(k),setItem:(k,v)=>{if(blocked)throw Error();values.set(k,v);},removeItem:k=>values.delete(k)};
 const doc={getElementById:id=>id==='excludeVisits'?box:state};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../analytics-preferences.js'),'utf8'),{document:doc,localStorage:local,sessionStorage:{removeItem:()=>{}},window:{FixEpubI18n:{t:k=>k},addEventListener:()=>{}}});
 assert.equal(state.textContent,'g_exclude_off');box.checked=true;handlers.change();assert.equal(state.textContent,'g_exclude_on');assert.equal(values.get('fixepub_analytics_excluded'),'1');
 box.checked=false;handlers.change();assert.equal(state.textContent,'g_exclude_off');assert(!values.has('fixepub_analytics_excluded'));
 blocked=true;box.checked=true;handlers.change();assert.equal(state.textContent,'g_exclude_error');assert.equal(box.checked,false);
});
test('TV 子域名不创建漏斗会话或发送访问，主站不受影响',()=>{
 const h=harness({location:{hostname:'tv.fixepub.com',search:''}});
 assert.equal(h.sent.length,0);assert.equal(h.root.FixEpubGrowth,undefined);
 assert.equal(harness().sent.length,1);
});

test('服务事件只发送允许的类别，并遵守退出设置',()=>{const h=harness();h.root.FixEpubGrowth.track('upload_succeeded','translation');assert.equal(h.sent.at(-1).body.service,'translation');assert.equal(h.sent.at(-1).body.event,'upload_succeeded');h.root.localStorage.getItem=()=> '1';const n=h.sent.length;h.root.FixEpubGrowth.track('service_clicked','translation');assert.equal(h.sent.length,n);});
test('Exposure waits for half-visible entry; clicks retain service attribution',()=>{
 let callback;const events={};const node={dataset:{},addEventListener:(n,f)=>events[n]=f};
 const h=harness({IntersectionObserver:class{constructor(fn){callback=fn;}observe(){}unobserve(){}},document:{readyState:'complete',referrer:'',querySelectorAll:s=>s==='#translationModeCard'?[node]:[]}});
 callback([{target:node,isIntersecting:true,intersectionRatio:0.1}]);assert.equal(h.sent.length,1);
 callback([{target:node,isIntersecting:true,intersectionRatio:0.5}]);assert.equal(h.sent.at(-1).body.event,'service_exposed');assert.equal(h.sent.at(-1).body.service,'translation');
 events.click();assert.equal(h.sent.at(-1).body.event,'service_clicked');
});
