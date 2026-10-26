(function(){
  const $=id=>document.getElementById(id),t=key=>window.FixEpubI18n.t(key);
  let data=null,generation=0,stateKey=null;
  function render(){
    if(!data)return;
    $('metrics').replaceChildren();$('services').replaceChildren();$('geography').replaceChildren();$('geoState').textContent='';
    for(const [key,value] of Object.entries({...data.counts,quote_to_paid:data.quote_to_paid,paid_to_download:data.paid_to_download})){
      const card=document.createElement('section'),label=document.createElement('h2'),number=document.createElement('p');card.className='metric';label.textContent=t('g_'+key);number.textContent=value===null?'—':key.includes('_to_')?(value*100).toFixed(1)+'%':String(value);card.append(label,number);$('metrics').append(card);
    }
    const table=document.createElement('table'),head=document.createElement('tr');
    for(const key of ['service','exposed','clicked','selected','uploaded','quoted','paid']){const th=document.createElement('th');th.textContent=t('g_'+(key==='clicked'?'entry_clicked':key));head.append(th);}table.append(head);
    for(const [key,counts] of Object.entries(data.services)){const row=document.createElement('tr');for(const value of [t('g_'+(['translation','proofreading'].includes(key)?key+'_service':key)),counts.exposed||0,counts.clicked||0,counts.selected||0,counts.uploaded||0,counts.quoted,counts.paid]){const cell=document.createElement('td');cell.textContent=value;row.append(cell);}table.append(row);}$('services').append(table);
    const geoTable=document.createElement('table'),geoHead=document.createElement('tr');
    for(const key of ['country','region','city','visits']){const cell=document.createElement('th');cell.textContent=t('g_'+key);geoHead.append(cell);}geoTable.append(geoHead);
    const locale=window.FixEpubI18n.locale;
    const place=names=>names?.[locale]||(locale==='zh-TW'?names?.['zh-CN']:null)||names?.en||t('g_unknown');
    for(const entry of data.geography||[]){
      const row=document.createElement('tr');
      for(const value of [place(entry.country),place(entry.region),place(entry.city),entry.sessions]){const cell=document.createElement('td');cell.textContent=value;row.append(cell);}geoTable.append(row);
    }
    $('geography').append(geoTable);
    $('geoState').textContent=data.geo_database?.available?((data.geography||[]).length?'':t('g_empty')):t('g_geo_unavailable');
    $('state').textContent=data.counts.visits?'':t('g_empty');
  }
  async function load(){
    const ticket=++generation;data=null;stateKey=null;$('metrics').replaceChildren();$('services').replaceChildren();$('geography').replaceChildren();$('geoState').textContent='';$('state').textContent='…';
    const query=new URLSearchParams();for(const id of ['days','language','source','device'])query.set(id,$(id).value);
    try{const r=await fetch('/api/admin/funnel?'+query,{credentials:'same-origin',cache:'no-store'});if(ticket!==generation)return;if(!r.ok)throw Error(r.status===401||r.status===403?'g_login':'g_error');data=await r.json();if(ticket===generation)render();}
    catch(error){if(ticket===generation){stateKey=error.message==='g_login'?'g_login':'g_error';$('state').textContent=t(stateKey);}}
  }
  $('filters').addEventListener('submit',event=>{event.preventDefault();load();});window.addEventListener('fixepub:locale',()=>{document.title=t('g_funnel')+' · FixEpub';if(data)render();else if(stateKey)$('state').textContent=t(stateKey);});load();
})();
