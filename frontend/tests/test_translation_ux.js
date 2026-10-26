const ux=require('../translation-ux');
const fs=require('fs'),path=require('path');
const html=fs.readFileSync(path.join(__dirname,'../index.html'),'utf8');
test('Translation labels cover all five languages without placeholders leaking',()=>{
 for(const [key,row] of Object.entries(ux.messages))for(const lang of ['zh-CN','zh-TW','en','ja','ko']){
  assert(row[lang]);assert(!/\{\w+\}/.test(ux.text(key,{source:'English',target:'한국어',amount:'US$1.49',count:5,toc:2,samples:3,chars:4,line:1},lang)));
 }
});
test('Translation return link keeps language choices but drops task tokens and private parameters',()=>{
 const link=ux.entry('?tool=translate&source=en&target=ko&job_id=private&token=secret&email=private','ja');
 const q=new URL('https://fixepub.test'+link).searchParams;
 assert.equal(q.get('tool'),'translate');assert.equal(q.get('lang'),'ja');assert.equal(q.get('source'),'en');assert.equal(q.get('target'),'ko');assert.equal(q.size,4);
 assert(!ux.entry('?source=invalid&target=invalid','en','translate').includes('invalid'));
});
test('Fresh translations need a target; all twenty language pairs and auto source are allowed',()=>{
 const langs=['zh-CN','zh-TW','en','ja','ko'];
 for(const source of langs)for(const target of langs)assert.equal(ux.validDirection(source,target),source!==target);
 assert(ux.validDirection('auto','en'));assert(!ux.validDirection('auto',''));assert(!ux.validDirection('invalid','en'));
 assert(/id="targetLang"[^>]*>\s*<option value=""/.test(html));
});
test('Displayed preflight quote and settings confirmation have distinct events',()=>{
 const preflight=html.slice(html.indexOf('function renderTranslationPreflight'),html.indexOf('function parsePreflightCharacters'));
 assert(preflight.includes('reportCheckoutEvent("quote_shown")'));
 assert(html.includes('reportCheckoutEvent("profile_confirmed")'));
 assert(html.includes('if(uploadInFlight)return;'));
});
