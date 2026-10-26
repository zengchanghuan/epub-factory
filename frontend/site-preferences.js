/* Persist presentation preferences only. No order, payment or book data is changed. */
(function (root) {
  'use strict';
  const locales = ['zh-CN', 'zh-TW', 'en', 'ja', 'ko'];
  function normalizeLocale(value) {
    if (locales.includes(value)) return value;
    const v = String(value || '').toLowerCase();
    if (v.startsWith('zh')) return /tw|hk|hant/.test(v) ? 'zh-TW' : 'zh-CN';
    return ['en','ja','ko'].find(x => v.startsWith(x)) || 'zh-CN';
  }
  function effectiveTheme(value, darkSystem) { return value === 'dark' || (value === 'system' && darkSystem) ? 'dark' : 'light'; }
  function paymentPreferences(getLocale) {
    const states=new Map();
    function update(select,state) {
      const preferred=state.available && normalizeLocale(getLocale())!=='zh-CN' ? 'paypal' : 'alipay';
      const option=select.querySelector('[value="'+preferred+'"]');
      if(option && select.firstElementChild!==option) select.insertBefore(option,select.firstElementChild);
      if(select.disabled) return; // An uploaded/recovered order has a frozen provider.
      const next=!state.available ? 'alipay' : state.manual ? select.value : preferred;
      if(select.value!==next){
        select.value=next;state.applying=true;
        try {select.dispatchEvent(new (select.ownerDocument.defaultView.Event)('change',{bubbles:true}));}
        finally {state.applying=false;}
      }
    }
    return {
      configure(select,available) {
        if(!select) return;
        let state=states.get(select);
        if(!state){state={manual:false,applying:false};states.set(select,state);
          select.addEventListener('change',()=>{if(!state.applying)state.manual=true;});}
        state.available=available===true;
        update(select,state);
      },
      refresh(){states.forEach((state,select)=>update(select,state));}
    };
  }
  if (typeof module === 'object' && module.exports) { module.exports = {normalizeLocale, effectiveTheme, paymentPreferences}; return; }
  const doc = root.document, messages = root.FixEpubMessages || {};
  const read = key => { try { return root.localStorage.getItem(key); } catch (_) { return null; } };
  const save = (key,value) => { try { root.localStorage.setItem(key,value); } catch (_) {} };
  const requestedLocale = new URLSearchParams(root.location.search).get('lang');
  let locale = normalizeLocale((locales.includes(requestedLocale) && requestedLocale) || read('fixepub_ui_locale') || root.navigator.language);
  // A localized entry is an explicit preference; retain it on same-site links.
  if(locales.includes(requestedLocale)) save('fixepub_ui_locale',locale);
  const payments=paymentPreferences(()=>locale);
  let theme = read('fixepub_theme') || 'system';
  if (!['light','dark','system'].includes(theme)) theme = 'system';
  const media = root.matchMedia('(prefers-color-scheme: dark)');
  const originals = new Map(Object.entries(messages).map(([key,v]) => [v['zh-CN'],key]));
  function t(key, values={}) { const value=messages[key]?.[locale] || messages[key]?.['zh-CN'] || key; return value.replace(/\{(\w+)\}/g,(match,name)=>Object.hasOwn(values,name)?String(values[name]):match); }
  function text(value) { const key = originals.get(value); return key ? t(key) : value; }
  function applyTheme() { doc.documentElement.dataset.theme=effectiveTheme(theme,media.matches); doc.documentElement.style.colorScheme=doc.documentElement.dataset.theme; }
  applyTheme();
  media.addEventListener?.('change',applyTheme);
  const dynamicIds = ['submitBtn','translationPayLink','continuePaymentLink','payButtonLabel','alipayLink','diagStatus','fixStatus','status','recover','return','confirmPreflightBtn','feedbackState','conversionStatus','notificationEmailHint','notificationTaskLabel','saveNotificationEmail','statusBadge','statusCardTitle','downloadBtn','paymentWaitText','cancelJobBtn','restartTranslationBtn','retryTranslationBtn','continuePaymentBtn','pdfUploadBtn','pdfConfirmBtn'];
  function translateSlot(node) {
    if (!node || node.children.length || !node.textContent) return;
    const value = node.textContent.trim();
    let key = originals.get(value);
    if (!key) key = Object.keys(messages).find(k => Object.values(messages[k]).includes(value));
    if (key && node.textContent !== t(key)) node.textContent=t(key);
  }
  function mark(node) {
    // Called only on authored UI before file names or other user content is inserted.
    const walker=doc.createTreeWalker(node,4), nodes=[];
    while(walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(n=>{
      if(n.parentElement.closest('script,style,textarea,[data-i18n]')) return;
      const key=originals.get(n.textContent.trim());
      if(key){const span=doc.createElement('span');span.dataset.i18n=key;span.textContent=t(key);n.replaceWith(span);}
    });
    node.querySelectorAll('[placeholder]').forEach(n=>{const key=originals.get(n.placeholder);if(key){n.dataset.i18nPlaceholder=key;n.placeholder=t(key);}});
  }
  function apply() {
    doc.documentElement.lang=locale;
    doc.querySelectorAll('[data-i18n]').forEach(node => {
      // Marked nodes are authored static UI. Never mark source previews or input values.
      const value=t(node.dataset.i18n);
      if(node.textContent!==value) node.textContent=value;
    });
    dynamicIds.forEach(id=>translateSlot(doc.getElementById(id)));
    doc.querySelectorAll('optgroup').forEach(n=>{if(!n.dataset.originalLabel)n.dataset.originalLabel=n.label;n.label=text(n.dataset.originalLabel);});
    doc.querySelectorAll('[data-i18n-placeholder]').forEach(node=>node.placeholder=t(node.dataset.i18nPlaceholder));
    doc.querySelectorAll('.seo-section, .features-section, .stats-section, .testimonials-section, .how-to-section').forEach(node=>{
      // Localized product overview replaces long Chinese marketing copy, not task controls.
      node.hidden=locale!=='zh-CN';
    });
    const overview=doc.getElementById('localizedOverview');
    if(overview) overview.hidden=locale==='zh-CN';
    payments.refresh();
    root.dispatchEvent(new CustomEvent('fixepub:locale',{detail:{locale}}));
  }
  root.FixEpubI18n={t,text,mark,payments,get locale(){return locale;}};
  function start() {
    const bar=doc.createElement('div');bar.className='site-preferences';bar.setAttribute('aria-label','Language and appearance');
    const makeSelect=(id,key,values,value,onChange)=>{
      const label=doc.createElement('label'), caption=doc.createElement('span'), select=doc.createElement('select');
      caption.dataset.i18n=key;caption.textContent=t(key);select.id=id;
      for(const [v,display] of values){const option=doc.createElement('option');option.value=v;option.textContent=display;if(messages[display]){option.dataset.i18n=display;option.textContent=t(display);}select.append(option);}
      select.value=value;select.addEventListener('change',()=>onChange(select.value));label.append(caption,select);bar.append(label);
    };
    makeSelect('uiLocale','locale_label',[['zh-CN','简体中文'],['zh-TW','繁體中文'],['en','English'],['ja','日本語'],['ko','한국어']],locale,value=>{locale=value;save('fixepub_ui_locale',locale);apply();});
    makeSelect('uiTheme','theme_label',[['system','system'],['light','light'],['dark','dark']],theme,value=>{theme=value;save('fixepub_theme',theme);applyTheme();});
    doc.body.prepend(bar);
    if(doc.querySelector('.seo-section')){
      const overview=doc.createElement('section');overview.id='localizedOverview';overview.className='localized-overview';
      const h=doc.createElement('h2'),p=doc.createElement('p');h.dataset.i18n='overview';p.dataset.i18n='overview_body';overview.append(h,p);
      doc.querySelector('.seo-section').before(overview);
    }
    const footer=doc.querySelector('footer');
    if(footer){const note=doc.createElement('p');note.dataset.i18n='support_notice';footer.append(note);}
    apply();
    const observer=new MutationObserver(records=>{
      for(const record of records){const parent=record.target.nodeType===3?record.target.parentElement:record.target;
        if(parent?.id && dynamicIds.includes(parent.id)) translateSlot(parent);
        for(const node of record.addedNodes||[]){if(node.nodeType===1){if(node.matches('[data-i18n]'))node.textContent=t(node.dataset.i18n);node.querySelectorAll('[data-i18n]').forEach(n=>{n.textContent=t(n.dataset.i18n);});}}
      }
    });
    observer.observe(doc.body,{childList:true,subtree:true,characterData:true});
  }
  if(doc.readyState==='loading') doc.addEventListener('DOMContentLoaded',start,{once:true});else start();
})(typeof window !== 'undefined' ? window : globalThis);
