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
      if(kind==='translation'){const action=document.createElement('a');action.className='button';action.dataset.growthService='translation';action.textContent=t('tx_quote');action.href=window.FixEpubTranslationUX.entry(location.search,window.FixEpubI18n.locale,'translate');article.append(action);}
      if(row.before===row.after){const p=document.createElement('p');p.textContent=t('g_unchanged');article.append(p);}
      target.append(article);
    }
    const link=document.querySelector('[data-i18n=g_upload]');link.href=window.FixEpubTranslationUX.entry(location.search,window.FixEpubI18n.locale);
    if(new URLSearchParams(location.search).get('tool')==='translate') {
      link.dataset.growthService='translation';
      const sample=data.translation, preview=document.createElement('section');preview.className='reading-preview';
      const heading=document.createElement('h2');heading.textContent=t('tx_layout');preview.append(heading);
      const note=document.createElement('p');note.textContent=t('tx_layout_note');preview.append(note);
      const chapter=document.createElement('h3');chapter.textContent=t('tx_chapter');preview.append(chapter);
      for(const [content,language] of [[sample.before,sample.source_language],[sample.after,window.FixEpubI18n.locale]]){const p=document.createElement('p');p.lang=language;p.textContent=content;preview.append(p);}
      const terms=document.createElement('p');terms.textContent=t('tx_terms');preview.append(terms);target.prepend(preview);
    }
    window.dispatchEvent(new Event('fixepub:entries-ready'));
  }
  window.addEventListener('fixepub:locale',render);
  fetch('/examples-data.json',{cache:'no-cache'}).then(r=>{if(!r.ok)throw Error();return r.json();}).then(data=>{samples=data;render();}).catch(()=>{document.getElementById('examples').textContent=t('g_error');});
})();
