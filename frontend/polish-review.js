/* Authorized book text is rendered with textContent only, never HTML. */
(function(root){
  function mount({doc, api, headers, translate, fetcher=root.fetch.bind(root)}) {
    const get=id=>doc.getElementById(id), rejected=new Set();
    let job=null, revision='', offset=0, total=0, generation=0, busy=false;
    const t=key=>translate(key);
    function status(message){get('polishReviewStatus').textContent=message;}
    function controls(){
      get('polishReviewPrevious').disabled=busy || offset===0;
      get('polishReviewNext').disabled=busy || offset+20>=total;
      get('polishReviewDownload').disabled=busy || !revision;
    }
    async function load(next){
      const ticket=++generation, identity=job;
      busy=true;controls();status(t('review_loading'));
      get('polishReviewChanges').replaceChildren();
      try {
        const res=await fetcher(`${api}/api/v2/jobs/${encodeURIComponent(identity)}/polish-review?offset=${next}`,{headers:headers(identity),cache:'no-store'});
        const data=await res.json();
        if(ticket!==generation)return;
        if(!res.ok)throw Error(data.detail || t('review_error'));
        if(revision && revision!==data.revision){rejected.clear();}
        revision=data.revision;offset=data.offset;total=data.total;
        for(const change of data.changes){
          const article=doc.createElement('article');article.className='polish-change';
          const label=doc.createElement('label'),check=doc.createElement('input');check.type='checkbox';check.checked=!rejected.has(change.id);
          check.addEventListener('change',()=>{if(check.checked)rejected.delete(change.id);else rejected.add(change.id);});
          label.append(check,doc.createTextNode(t('review_keep')+' #'+(Number(change.id)+1)));
          article.append(label);
          for(const [key,value] of [['review_before',change.before],['review_after',change.after]]){
            const heading=doc.createElement('h3'),text=doc.createElement('p');heading.textContent=t(key);text.textContent=value;article.append(heading,text);
          }
          get('polishReviewChanges').append(article);
        }
        status(total ? `${offset+1}–${Math.min(offset+20,total)} / ${total}` : t('review_empty'));
      } catch(error){if(ticket===generation)status(error.message);}
      finally {if(ticket===generation){busy=false;controls();}}
    }
    function close(){generation++;busy=false;revision='';rejected.clear();get('polishReviewChanges').replaceChildren();if(get('polishReviewDialog').open)get('polishReviewDialog').close();}
    get('polishReviewOpen').addEventListener('click',()=>{if(!job)return;revision='';offset=0;rejected.clear();get('polishReviewDialog').showModal();load(0);});
    get('polishReviewClose').addEventListener('click',close);
    get('polishReviewDialog').addEventListener('cancel',event=>{event.preventDefault();close();});
    get('polishReviewPrevious').addEventListener('click',()=>load(Math.max(0,offset-20)));
    get('polishReviewNext').addEventListener('click',()=>load(offset+20));
    get('polishReviewDownload').addEventListener('click',async()=>{
      if(busy||!revision)return;
      busy=true;controls();status(t('review_loading'));
      const ticket=++generation,identity=job;
      try{
        const res=await fetcher(`${api}/api/v2/jobs/${encodeURIComponent(identity)}/polish-review/download`,{
          method:'POST',headers:{...headers(identity),'Content-Type':'application/json'},cache:'no-store',
          body:JSON.stringify({revision,rejected:Array.from(rejected)})});
        if(!res.ok){const data=await res.json();throw Error(data.detail||t('review_error'));}
        const blob=await res.blob();if(ticket!==generation)return;
        const url=root.URL.createObjectURL(blob),link=doc.createElement('a');link.href=url;link.download='proofread-selection.epub';doc.body.append(link);link.click();link.remove();
        root.setTimeout(()=>root.URL.revokeObjectURL(url),30000);status(t('review_saved'));
      }catch(error){if(ticket===generation)status(error.message);}
      finally{if(ticket===generation){busy=false;controls();}}
    });
    return {setJob(data){
      const report=data&&(data.precision_polish||data.translation_stats?.precision_polish);
      const next=report?.status==='completed' && data.download_url ? data.job_id : null;
      if(next!==job){close();job=next;}
      get('polishReviewOpen').hidden=!job;
    }};
  }
  if(typeof module==='object'&&module.exports)module.exports={mount};else root.FixEpubPolishReview={mount};
})(typeof window!=='undefined'?window:globalThis);
