'use strict';
const $ = id => document.getElementById(id);
const taskNames = {pending_payment:'等待支付',pending:'排队中',running:'处理中',success:'已完成',failed:'失败',cancelled:'已取消'};
const payNames = {unknown:'未核验 / 查询不可用',paid:'已核验支付',unpaid:'待支付',closed:'已关闭',amount_mismatch:'金额待核对'};
let csrf = '', page = 1, total = 0, listGeneration = 0;
function node(tag, text, cls) { const n=document.createElement(tag); if(text!=null)n.textContent=text; if(cls)n.className=cls; return n; }
function error(message) { $('error').textContent=message; $('error').hidden=!message; }
async function api(path, data) {
 const response=await fetch('/api/admin'+path,{credentials:'same-origin',cache:'no-store',method:data===undefined?'GET':'POST',headers:data===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:data===undefined?undefined:JSON.stringify(data)});
 const body=await response.json();
 if(!response.ok){if(response.status===401){showLogin();}throw new Error(typeof body.detail==='string'?body.detail:'参数无效，请检查输入');} return body;
}
function showLogin(){csrf='';listGeneration++;$('rows').replaceChildren();$('detail-body').replaceChildren();$('detail').close();$('login').hidden=false;$('dashboard').hidden=true;$('identity').hidden=true;}
function loggedIn(data){csrf=data.csrf;$('username').textContent=data.username;$('login').hidden=true;$('dashboard').hidden=false;$('identity').hidden=false;$('password').value='';}
function dateText(v){return v?new Date(/Z$|[+-]\d\d:\d\d$/.test(v)?v:v+'Z').toLocaleString('zh-CN',{hour12:false}):'—';}
function costText(c){return formatLedgerCost(c.ledger);}
function costCoverage(c){const l=c.ledger;return l?`已计价 ${l.priced_requests} / ${l.requests} 个请求 · ${l.coverage==='complete'?'已记录请求完整':'存在待核实或历史缺口'}`:'历史记录不完整';}
function badge(text, state){return node('span',text,'badge '+state);}
async function load(){
 const generation=++listGeneration;error('');
 const query=new URLSearchParams();for(const [k,v] of new FormData($('filters')))if(v)query.set(k,v);query.set('page',page);
 $('rows').replaceChildren();const loading=node('td','正在查询…','empty');loading.colSpan=8;const lr=node('tr');lr.append(loading);$('rows').append(lr);
 try{const data=await api('/orders?'+query);if(generation!==listGeneration)return;total=data.total;$('total').textContent=total+' 条任务';$('rows').replaceChildren();
 for(const row of data.items){const tr=node('tr');let td=node('td');td.append(node('div',row.filename,'filename'),node('div',row.order_no,'small'));tr.append(td);
 tr.append(node('td',dateText(row.created_at)));td=node('td');td.append(badge(taskNames[row.status]||row.status,row.status));tr.append(td);
 td=node('td',row.price_cny?'¥'+row.price_cny:'未知');if(row.batch_id)td.append(node('div','整批价格','small'));tr.append(td);
 td=node('td');td.append(badge(payNames[row.payment.status],row.payment.status));tr.append(td);
 td=node('td');for(const [key,label] of [['quote_shown','报价已展示'],['payment_clicked','已点击付款'],['payment_succeeded','支付已确认']])if(row.checkout?.[key])td.append(node('div',label,'small'));if(!td.childNodes.length)td.textContent='暂无记录';tr.append(td);
 td=node('td',costText(row.cost));td.append(node('div',costCoverage(row.cost),'small'));tr.append(td);
 td=node('td');const b=node('button','详情','secondary');b.onclick=()=>detail(row.id);td.append(b);tr.append(td);$('rows').append(tr);}
 if(!data.items.length){const tr=node('tr'),td=node('td','没有符合条件的订单','empty');td.colSpan=8;tr.append(td);$('rows').append(tr);}
 $('page-info').textContent=`第 ${page} / ${Math.max(1,Math.ceil(total/25))} 页`;$('previous').disabled=page<=1;$('next').disabled=page*25>=total;
 }catch(e){if(generation===listGeneration)error(e.message);}
}
async function detail(id){
 try{const d=await api('/orders/'+encodeURIComponent(id));const body=$('detail-body');body.replaceChildren(node('h3',d.filename,'detail-title'));const grid=node('dl',null,'detail-grid');
 const l=d.cost.ledger||{};const bill=Object.entries(l.bill_imported_totals||{}).map(([currency,amount])=>currency+' '+amount).join(' + ')||'尚未导入供应商账单';
 for(const [label,value] of [['商户订单号',d.order_no],['任务状态',taskNames[d.status]],['订单价格',d.price_cny?'¥'+d.price_cny+' · '+d.price_scope:'未知'],['支付状态',payNames[d.payment.status]],['支付宝查询金额',d.payment.amount?'¥'+d.payment.amount:'未知'],['最近核验',dateText(d.payment.checked_at)],['创建时间',dateText(d.created_at)],['模型',d.model||'不适用'],['Token 成本',costText(d.cost)],['费用记录覆盖',costCoverage(d.cost)],['已记录输入 Token',l.prompt_tokens??'未知'],['已记录输出 Token',l.completion_tokens??'未知'],['已记录缓存命中 Token',l.cache_hit_tokens??'未知'],['已记录缓存未命中 Token',l.cache_miss_tokens??'未知'],['账单导入金额',bill],['历史未记录尝试',l.historical_untracked_attempts??'未知']]){const el=node('div');el.append(node('dt',label),node('dd',value));grid.append(el);}body.append(grid,node('p',l.note||d.cost.note,'notice'));
 if(!l.tokens_complete||!l.cache_tokens_complete)body.append(node('p','以上 Token 仅为已记录部分；缺失 usage、缓存拆分或历史记录不能推算为 0。','notice'));
 const stageNames={body:'正文 / 目录与脚注',glossary:'术语',book_profile:'图书画像',book_title:'书名',rescue:'补译',semantic_review:'语义审校',style_guide:'风格档案',literary_polish:'文学润色',literary_verify:'文学复核',precision_polish:'繁简精校'};
 const costs=node('div',null,'notice');costs.append(node('h2','模型调用分阶段统计'));for(const [stage,v]of Object.entries(l.stages||{}))costs.append(node('p',`${stageNames[stage]||stage}：${v.requests} 次请求，${v.priced_requests} 次已计价，已记录 ${v.total_tokens} tokens`));
 const usageButton=node('button','查看逐请求费用明细','secondary');costs.append(usageButton);const usageRows=node('div');costs.append(usageRows);body.append(costs);let usagePage=1;
 usageButton.onclick=async()=>{usageButton.disabled=true;try{const data=await api('/orders/'+encodeURIComponent(id)+'/usage?page='+usagePage);if(usagePage===1)usageRows.replaceChildren();for(const r of data.items){usageRows.append(node('p',`${dateText(r.started_at)} · ${stageNames[r.stage]||r.stage} · ${r.provider} / ${r.response_model||r.requested_model} · ${r.total_tokens??'未知'} tokens · ${r.calculated_cost!==null?r.currency+' '+r.calculated_cost:'待核实：'+r.price_status} · 请求 ${r.response_id||r.id}`,'small'));}usagePage++;usageButton.hidden=(usagePage-1)*data.page_size>=data.summary.requests;usageButton.textContent='加载下一页明细';}catch(e){usageRows.append(node('p',e.message,'failure'));}finally{usageButton.disabled=false;}};
 const funnel=node('div',null,'notice');funnel.append(node('h2','付款过程（首次记录时间）'));for(const [key,label] of [['quote_shown','展示报价'],['payment_clicked','点击付款'],['payment_succeeded','支付成功']]){const event=d.checkout?.[key];funnel.append(node('p',label+'：'+(event?dateText(event.at)+(event.source==='verified_query'?'（查单确认时间）':''):'无记录 / 未知')));}funnel.append(node('p','报价与点击来自浏览器上报；扫码行为无法直接观测。无记录不等于未操作，历史记录不回填。','small'));body.append(funnel);
 body.append(node('h2','处理结果 / 失败原因'),node('p',[d.error_code,d.message].filter(Boolean).join('\n')||'暂无记录',d.status==='failed'?'failure':'muted'));
 const actions=node('div',null,'actions');for(const [kind,label]of [['source','下载原文件'],['output','下载结果文件']]){if(d.files[kind]){const a=node('a',label,'file-link');a.href='/api/admin/orders/'+encodeURIComponent(id)+'/files/'+kind;actions.append(a);}else actions.append(node('span',label.replace('下载','')+'不可用','muted'));}
 const payment=node('button','核验支付宝支付','secondary');actions.append(payment);
 const retry=node('button','重试失败订单');retry.disabled=d.status!=='failed'||!d.files.source;actions.append(retry);body.append(actions);
 const status=node('p','','dialog-error');status.setAttribute('role','status');body.append(status);
 payment.onclick=async()=>{payment.disabled=true;status.textContent='正在查询支付宝…';try{await api('/orders/'+id+'/payment',{});await detail(id);await load();}catch(e){status.textContent=e.message;}finally{payment.disabled=false;}};
 retry.onclick=async()=>{if(!confirm('将重新核验支付并复用现有缓存重试。重试可能产生新的模型费用。确认继续？'))return;retry.disabled=true;status.textContent='正在核验支付并安排重试…';try{await api('/orders/'+id+'/retry',{acknowledge_cost:true});await detail(id);await load();}catch(e){status.textContent=e.message;}finally{retry.disabled=d.status!=='failed'||!d.files.source;}};
 const stages=node('div',null,'stages');stages.append(node('h2','最近处理阶段'));for(const s of d.stages)stages.append(node('p',`${dateText(s.started_at)} · ${s.name} · ${s.status}${s.elapsed_ms!=null?' · '+s.elapsed_ms+' ms':''}${s.previous_failure?' · 上次失败：'+s.previous_failure:''}`));if(!d.stages.length)stages.append(node('p','暂无阶段记录','muted'));body.append(stages);if(!$('detail').open)$('detail').showModal();
 }catch(e){error(e.message);}
}
$('login-form').onsubmit=async e=>{e.preventDefault();error('');$('login-button').disabled=true;try{loggedIn(await api('/login',{username:$('user').value,password:$('password').value}));await load();}catch(e){error(e.message);}finally{$('login-button').disabled=false;}};
$('logout').onclick=async()=>{try{await api('/logout',{});showLogin();}catch(e){error(e.message);}};
$('filters').onsubmit=e=>{e.preventDefault();page=1;load();};$('filters').onreset=()=>{setTimeout(()=>{page=1;load();},0);};
$('refresh').onclick=()=>load();$('previous').onclick=()=>{page--;load();};$('next').onclick=()=>{page++;load();};$('close').onclick=()=>$('detail').close();
api('/session').then(async data=>{loggedIn(data);await load();}).catch(e=>{if(e.message!=='请登录管理员账号')error(e.message);});
