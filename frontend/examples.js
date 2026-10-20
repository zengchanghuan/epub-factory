(function(){
  let samples;
  const t=key=>window.FixEpubI18n.t(key);
  function render(){
    document.title=t('g_title')+' · FixEpub';
    if(!samples)return;
    const target=document.getElementById('examples');target.replaceChildren();
    const data=samples[window.FixEpubI18n.locale];
    for(const kind of ['translation','proofreading']){
      const row=data[kind],article=document.createElement('article'),h=document.createElement('h2');h.textContent=t('g_'+kind);article.append(h);
      const grid=document.createElement('div');grid.className='comparison';
      for(const side of ['before','after']){const box=document.createElement('section'),heading=document.createElement('h3'),text=document.createElement('pre');heading.textContent=t('g_'+side);text.textContent=row[side];text.lang=side==='before'?row.source_language:window.FixEpubI18n.locale;box.append(heading,text);grid.append(box);}
      article.append(grid);
      if(row.before===row.after){const p=document.createElement('p');p.textContent=t('g_unchanged');article.append(p);}
      target.append(article);
    }
    const link=document.querySelector('[data-i18n=g_upload]');link.href='/?lang='+encodeURIComponent(window.FixEpubI18n.locale);
  }
  window.addEventListener('fixepub:locale',render);
  fetch('/examples-data.json',{cache:'no-cache'}).then(r=>{if(!r.ok)throw Error();return r.json();}).then(data=>{samples=data;render();}).catch(()=>{document.getElementById('examples').textContent=t('g_error');});
})();
