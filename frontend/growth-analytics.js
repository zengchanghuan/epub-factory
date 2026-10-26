/* First-party, anonymous 30-minute session telemetry. Never sends content or URLs. */
(function(root){
  'use strict';
  function source(referrer,search){
    const campaign=new URLSearchParams(search).get('utm_source');
    if(campaign)return ['community','social','search'].includes(campaign)?campaign:'campaign';
    if(!referrer)return 'direct';
    try {const host=new URL(referrer).hostname;
      if(/(^|\.)(google\.(com|com.hk|com.tw|co.jp|co.kr|cn)|bing\.com|baidu\.com|yahoo\.(com|co.jp)|naver\.com)$/.test(host))return 'search';
      if(/(^|\.)(reddit\.com|zhihu\.com|mobileread\.com)$/.test(host))return 'community';
      if(/(^|\.)(youtube\.com|xiaohongshu\.com|x\.com|facebook\.com)$/.test(host))return 'social';
      return 'other';
    }catch(_){return 'other';}
  }
  if(typeof module==='object'&&module.exports){module.exports={source};return;}
  if(root.location.hostname.toLowerCase().replace(/\.$/,'')==='tv.fixepub.com')return;
  if(root.navigator.doNotTrack==='1'||root.location.hostname==='localhost'||root.location.hostname==='127.0.0.1')return;
  function excluded(){
    try{return root.localStorage.getItem('fixepub_analytics_excluded')==='1';}catch(_){return true;}
  }
  if(excluded())return;
  let session,info;
  try {
    info=JSON.parse(root.sessionStorage.getItem('fixepub_funnel')||'null');
    if(!info||! /^[a-f0-9]{32}$/.test(info.id)||Date.now()-info.at>30*60*1000){info={id:root.crypto.randomUUID().replaceAll('-',''),at:Date.now(),source:source(root.document.referrer,root.location.search)};root.sessionStorage.setItem('fixepub_funnel',JSON.stringify(info));}
    session=info.id;
  }catch(_){return;}
  root.FixEpubGrowth={get session(){return excluded()?undefined:session;}};
  function send(event,service){
    if(excluded())return;
    try {if(root.localStorage.getItem('admin_key')||root.sessionStorage.getItem('admin_key'))return;}catch(_){return;}
    const language=root.FixEpubI18n?.locale||'zh-CN';
    root.fetch('/api/v2/analytics/events',{method:'POST',keepalive:true,headers:{'Content-Type':'application/json'},body:JSON.stringify({session,event,...(service?{service}:{}),language,source:info.source,device:root.matchMedia('(max-width: 767px)').matches?'mobile':'desktop'})}).catch(()=>{});
  }
  function currentService(){
    if(root.location.pathname==='/epub-repair.html')return 'repair';
    if(root.document.querySelector?.('input[name="taskMode"]:checked')?.value==='translate')return 'translation';
    return root.document.getElementById?.('enablePrecisionPolish')?.checked?'proofreading':'conversion';
  }
  root.FixEpubGrowth.track=(event,service)=>{try{send(event,service);}catch(_){}};
  function start(){send('visit');
    const selectors=[['#translationModeCard','translation'],['#taskModeGrid .mode-card:first-child','conversion'],['#enablePrecisionPolish','proofreading'],['a[href="/epub-repair.html"]','repair'],['#pdfUploadBtn','pdf'],['#dropzone','repair']];
    const observer=root.IntersectionObserver?new root.IntersectionObserver(entries=>{for(const e of entries)if(e.isIntersecting&&e.intersectionRatio>=0.5){send('service_exposed',e.target.dataset.funnelService);observer.unobserve(e.target);}},{threshold:0.5}):null;
    for(const [selector,service] of selectors)for(const node of root.document.querySelectorAll?.(selector)||[]){node.dataset.funnelService=service;observer?.observe(node);node.addEventListener('click',()=>send('service_clicked',service));}
root.document.addEventListener?.('change',e=>{if(e.target?.type==='file'&&e.target.files?.length)send('file_selected',e.target?.id==='pdfFileInput'?'pdf':currentService());});root.addEventListener('fixepub:files-selected',e=>{if(e.detail?.length)send('file_selected',currentService());});}
  if(root.document.readyState==='loading')root.document.addEventListener('DOMContentLoaded',start,{once:true});else start();
})(typeof window!=='undefined'?window:globalThis);
