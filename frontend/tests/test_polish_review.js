const {mount}=require('../polish-review');
function setup(fetcher){
  const nodes=new Map();
  class Element{
    constructor(tag){this.tag=tag;this.listeners={};this.children=[];this.open=false;this.textContent='';}
    addEventListener(name,handler){this.listeners[name]=handler;}
    append(...nodes){this.children.push(...nodes);}
    replaceChildren(...nodes){this.children=nodes;}
    showModal(){this.open=true;}
    close(){this.open=false;}
    click(){return this.listeners.click?.({preventDefault(){}});}
    remove(){}
  }
  const doc={getElementById(id){if(!nodes.has(id))nodes.set(id,new Element(id));return nodes.get(id);},
    createElement:tag=>new Element(tag),createTextNode:text=>({textContent:text}),body:new Element('body')};
  const controller=mount({doc,api:'',headers:id=>({'X-Job-Token':'token-'+id}),translate:key=>key,fetcher});
  const node=id=>doc.getElementById(id);
  controller.setJob({job_id:'one',download_url:'/download',precision_polish:{status:'completed'}});
  return {controller,node,doc};
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const page=(offset=0)=>({revision:'revision-1',total:21,offset,changes:[{id:String(offset),before:'<img onerror=alert(1)>',after:'Safe text.'}]});
test('对照使用授权请求和纯文本；翻页保留取消选项，下载不会覆盖成品',async()=>{
  const calls=[];
  const {node}=setup(async(url,options)=>{calls.push({url,options});return options.method==='POST'?{ok:true,blob:async()=>new Blob(['epub'])}:{ok:true,json:async()=>page(url.endsWith('20')?20:0)};});
  node('polishReviewOpen').click();await settle();
  assert.equal(calls[0].options.headers['X-Job-Token'],'token-one');
  let article=node('polishReviewChanges').children[0];
  assert.equal(article.children[2].textContent,'<img onerror=alert(1)>');
  const checkbox=article.children[0].children[0];checkbox.checked=false;checkbox.listeners.change();
  node('polishReviewNext').click();await settle();
  node('polishReviewPrevious').click();await settle();
  assert.equal(node('polishReviewChanges').children[0].children[0].children[0].checked,false);
  const prior=setTimeout;global.setTimeout=fn=>fn();
  try{await node('polishReviewDownload').click();}finally{global.setTimeout=prior;}
  assert.deepEqual(JSON.parse(calls.at(-1).options.body),{revision:'revision-1',rejected:['0']});
  assert(calls.at(-1).url.endsWith('/polish-review/download'));
});
test('关闭或切换任务后迟到结果不能显示旧书正文',async()=>{
  let resolve;
  const {controller,node}=setup(()=>new Promise(r=>resolve=r));
  node('polishReviewOpen').click();
  controller.setJob({job_id:'two',download_url:'/download',precision_polish:{status:'completed'}});
  resolve({ok:true,json:async()=>page()});await settle();
  assert.equal(node('polishReviewChanges').children.length,0);
  assert.equal(node('polishReviewDialog').open,false);
});
test('无精校成品不显示入口，零修改也可下载',async()=>{
  const {controller,node}=setup(async()=>({ok:true,json:async()=>({revision:'r',total:0,offset:0,changes:[]})}));
  node('polishReviewOpen').click();await settle();
  assert.equal(node('polishReviewStatus').textContent,'review_empty');
  assert.equal(node('polishReviewDownload').disabled,false);
  controller.setJob(null);assert.equal(node('polishReviewOpen').hidden,true);
});
