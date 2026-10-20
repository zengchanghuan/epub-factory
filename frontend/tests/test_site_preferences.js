const fs=require('fs'),path=require('path');
const preferences=require('../site-preferences');
const messages=require('../locales/ui');
test('五种界面语言覆盖所有已标记文案',()=>{
  for(const file of ['index.html','epub-repair.html','paypal-checkout.html']){
    const html=fs.readFileSync(path.join(__dirname,'..',file),'utf8');
    for(const [,key] of html.matchAll(/data-i18n="([^"]+)"/g)){
      for(const locale of ['zh-CN','zh-TW','en','ja','ko']) assert(messages[key]?.[locale],`${file}: ${key}/${locale}`);
    }
  }
});
test('浏览器地区语言映射及安全回退',()=>{
  for(const [input,expected] of [['en-US','en'],['ja-JP','ja'],['ko-KR','ko'],['zh-Hant-HK','zh-TW'],['zh-TW','zh-TW'],['zh-CN','zh-CN'],['unsupported','zh-CN']]) assert.equal(preferences.normalizeLocale(input),expected);
});
test('浅色、深色和系统模式均可确定解析',()=>{
  assert.equal(preferences.effectiveTheme('light',true),'light');
  assert.equal(preferences.effectiveTheme('dark',false),'dark');
  assert.equal(preferences.effectiveTheme('system',true),'dark');
  assert.equal(preferences.effectiveTheme('system',false),'light');
});
test('界面目录不包含脚本或HTML插值，运行时使用纯文本',()=>{
  for(const entry of Object.values(messages)) for(const value of Object.values(entry)) assert(!/<script|<iframe|javascript:/i.test(value));
  const js=fs.readFileSync(path.join(__dirname,'..','site-preferences.js'),'utf8');
  assert(!js.includes('.innerHTML'));
  assert(!js.includes('fetch('));
  assert(js.includes('fixepub_ui_locale'));assert(js.includes('fixepub_theme'));
});
test('源语言与目标语言选择分别提交且包括英语',()=>{
  const html=fs.readFileSync(path.join(__dirname,'..','index.html'),'utf8');
  for(const id of ['sourceLang','targetLang']){
    const select=html.match(new RegExp('<select id="'+id+'">([\\s\\S]*?)</select>'))[1];
    for(const code of ['en','ja','ko','zh-CN','zh-TW'])assert(select.includes('value="'+code+'"'));
  }
  assert(html.includes('form.append("source_lang", $("sourceLang").value)'));
});
function paymentSelect(){
  const options=[{value:'alipay'},{value:'paypal'}], listeners=[];
  return {value:'alipay',disabled:false,ownerDocument:{defaultView:{Event:class{constructor(type){this.type=type;}}}},
    get firstElementChild(){return options[0];},querySelector(q){return options.find(o=>q.includes('"'+o.value+'"'));},
    insertBefore(o){options.splice(options.indexOf(o),1);options.unshift(o);},
    addEventListener(type,fn){listeners.push(fn);},dispatchEvent(){listeners.forEach(fn=>fn());}};
}
test('非简体中文默认PayPal置顶，简体默认支付宝；配置到达和语言切换均生效',()=>{
  let locale='zh-CN';const payments=preferences.paymentPreferences(()=>locale),select=paymentSelect();
  payments.configure(select,true);assert.equal(select.value,'alipay');
  for(locale of ['en','ja','ko','zh-TW']){payments.refresh();assert.equal(select.value,'paypal');assert.equal(select.firstElementChild.value,'paypal');}
  locale='zh-CN';payments.refresh();assert.equal(select.value,'alipay');
  locale='en';payments.configure(select,false);assert.equal(select.value,'alipay');
  payments.configure(select,true);assert.equal(select.value,'paypal');
});
test('尊重手动选择，支付不可用时回退；锁定订单不切换支付方式',()=>{
  let locale='en';const payments=preferences.paymentPreferences(()=>locale),select=paymentSelect();
  payments.configure(select,true);select.value='alipay';select.dispatchEvent();
  locale='ja';payments.refresh();assert.equal(select.value,'alipay');assert.equal(select.firstElementChild.value,'paypal');
  select.value='paypal';select.dispatchEvent();payments.configure(select,false);assert.equal(select.value,'alipay');
  select.value='paypal';select.disabled=true;locale='zh-CN';payments.refresh();assert.equal(select.value,'paypal');
});
