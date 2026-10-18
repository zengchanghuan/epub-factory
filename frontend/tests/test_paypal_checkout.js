const fs = require('fs'), path = require('path'), vm = require('vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'paypal-checkout.js'), 'utf8');
async function setup(overrides = {}) {
  const nodes = new Map(), calls = [], scripts = [], opts = {};
  const node = id => { if (!nodes.has(id)) nodes.set(id, {textContent:'',hidden:false,disabled:false}); return nodes.get(id); };
  const context = {URLSearchParams, location: {search:'?order=order-a'},
    localStorage: {getItem: key => key === 'job_access_tokens_v1' ? '{"order-a":"owner-token"}' : ''},
    document: {getElementById: node, createElement: () => ({}), head: {append: script => {scripts.push(script);script.onload();}}},
    fetch: async (url, options) => {
      calls.push({url,options});
      let data = url.endsWith('/config') ? {enabled:true,client_id:'public-client',currency:'USD'}
        : options.method === 'POST' && url.endsWith('/capture') ? {status:'COMPLETED',local_id:'order-a'}
        : options.method === 'POST' ? {id:'ORDER123456789'}
        : {local_id:'order-a',amount:'0.99',currency:'USD',file_count:1,status:'pending_payment',job_id:'order-a'};
      if (overrides.response) data = overrides.response(url,options,data);
      return {ok:true,json:async()=>data};
    },
    window: {paypal:{Buttons: value => {Object.assign(opts,value);return {render:async()=>{}};}}},
  };
  if (overrides.search) context.location.search=overrides.search;
  await vm.runInNewContext(source,context);
  return {node,calls,scripts,opts};
}
test('PayPal shows frozen USD price and loads SDK with only public client id',async()=>{
  const h=await setup(); assert.equal(h.node('price').textContent,'US$0.99');
  assert(h.scripts[0].src.includes('currency=USD')); assert(!h.scripts[0].src.includes('owner-token'));
  assert.equal(h.calls[0].options.headers['X-Job-Token'],'owner-token');
  assert.equal(h.calls.filter(c=>c.options.method==='POST').length,0);
});
test('PayPal approval only asks server to capture its bound local order',async()=>{
  const h=await setup();assert.equal(await h.opts.createOrder(),'ORDER123456789');
  await h.opts.onApprove({orderID:'attacker-order'});
  assert.equal(h.calls.at(-1).url,'/api/v2/paypal/orders/order-a/capture');
  assert(!JSON.stringify(h.calls).includes('attacker-order'));
  assert(h.node('status').textContent.includes('由服务器核验'));
});
test('Wrong identity or currency never renders SDK buttons',async()=>{
  for(const value of [{currency:'CNY'},{local_id:'other'}]){
    const h=await setup({response:(u,o,d)=>({...d,...value})});
    assert.equal(h.scripts.length,0);assert(h.node('status').textContent.includes('不一致'));
  }
});
test('Existing nonpayable order does not create or capture another order',async()=>{
  const h=await setup({response:(u,o,d)=>({...d,status:'completed'})});
  assert.equal(h.scripts.length,0);assert(h.node('status').textContent.includes('无需付款'));
});
test('Cancellation preserves the order and never calls capture',async()=>{
  const h=await setup(); h.opts.onCancel();assert(h.node('status').textContent.includes('已取消'));
  assert.equal(h.calls.filter(c=>c.options.method==='POST').length,0);
});
