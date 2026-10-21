(function(){
  'use strict';
  const key='fixepub_analytics_excluded',box=document.getElementById('excludeVisits'),state=document.getElementById('preferenceState');
  let failed=false;
  function render(){
    try{box.checked=localStorage.getItem(key)==='1';}catch(_){failed=true;}
    state.className='pref-state '+(failed?'is-error':box.checked?'is-on':'is-off');
    state.textContent=window.FixEpubI18n.t(failed?'g_exclude_error':box.checked?'g_exclude_on':'g_exclude_off');
    document.title=window.FixEpubI18n.t('g_exclude_title')+' · FixEpub';
  }
  box.addEventListener('change',()=>{
    failed=false;
    try{
      if(box.checked)localStorage.setItem(key,'1');else localStorage.removeItem(key);
      sessionStorage.removeItem('fixepub_funnel');
    }catch(_){failed=true;}
    render();
  });
  window.addEventListener('storage',e=>{if(e.key===key){failed=false;render();}});
  window.addEventListener('fixepub:locale',render);render();
})();
