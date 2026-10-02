'use strict';
const $ = id => document.getElementById(id);
const taskNames = {pending_payment:'等待支付',pending:'排队中',running:'处理中',success:'已完成',failed:'失败',cancelled:'已取消'};
const payNames = {unknown:'未核验 / 查询不可用',paid:'已核验支付',unpaid:'待支付',closed:'已关闭',amount_mismatch:'金额待核对'};
let csrf = '', page = 1, total = 0, listGeneration = 0, authGeneration = 0, detailGeneration = 0, detailJobId = null;
const reviewRequests = new Map(), reviewInFlight = new Set();
const reviewActionNames = {note:'添加处理备注',fulfill:'核验并恢复履约',record_external_refund:'登记线下已完成退款',close_review:'结束人工跟进'};
const reviewStateNames = {open:'人工跟进开放',closed:'已登记关闭工单'};
function node(tag, text, cls) { const n=document.createElement(tag); if(text!=null)n.textContent=text; if(cls)n.className=cls; return n; }
function error(message) { $('error').textContent=message; $('error').hidden=!message; }
async function api(path, data) {
 const generation=authGeneration, token=csrf;
 const response=await fetch('/api/admin'+path,{credentials:'same-origin',cache:'no-store',method:data===undefined?'GET':'POST',headers:data===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:data===undefined?undefined:JSON.stringify(data)});
 const body=await response.json();
 if(!response.ok){if(response.status===401&&path!=='/login'&&generation===authGeneration&&token===csrf){showLogin();}const failure=new Error(typeof body.detail==='string'?body.detail:'参数无效，请检查输入');failure.status=response.status;throw failure;} return body;
}
function invalidateDetail(){detailGeneration++;detailJobId=null;}
function showLogin(){authGeneration++;csrf='';listGeneration++;invalidateDetail();reviewRequests.clear();$('rows').replaceChildren();$('detail-body').replaceChildren();$('detail').close();$('login').hidden=false;$('dashboard').hidden=true;$('identity').hidden=true;}
function loggedIn(data){authGeneration++;listGeneration++;invalidateDetail();csrf=data.csrf;$('username').textContent=data.username;$('login').hidden=true;$('dashboard').hidden=false;$('identity').hidden=false;$('password').value='';}
function dateText(v){return v?new Date(/Z$|[+-]\d\d:\d\d$/.test(v)?v:v+'Z').toLocaleString('zh-CN',{hour12:false}):'—';}
function costText(c){return formatLedgerCost(c.ledger);}
function costCoverage(c){const l=c.ledger;return l?`已计价 ${l.priced_requests} / ${l.requests} 个请求 · ${l.coverage==='complete'?'已记录请求完整':'存在待核实或历史缺口'}`:'历史记录不完整';}
function badge(text, state){return node('span',text,'badge '+state);}
function reviewLabel(review){return review?.needs_attention?'需要人工处理':(reviewStateNames[review?.state]||review?.state||'暂无人工处置');}
function requestId(){
 if(typeof crypto.randomUUID==='function')return crypto.randomUUID();
 const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
 const hex=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}
function renderReview(d, body, active){
 const review=d.review, panel=node('section',null,'review-panel');panel.append(node('h2','异常订单人工处理'));
 const facts=d.payment_resolution||{}, factsBox=node('div',null,'review-facts');
 const factStates={paid_review:'付款已核验，取消后待人工处理（尚未退款）',paid:'付款已核验',closed:'网关已关单，不代表已退款',external_refund_recorded:'已登记外部全额退款（未经网关退款核验）'};
 for(const [label,value] of [['付款处置',factStates[facts.state]||facts.state],['已核验金额',facts.amount?'¥'+facts.amount:null],['核验时间',facts.verified_at?dateText(facts.verified_at):null],['原取消原因',facts.original_cancel_message],['原错误码',facts.original_error_code]])if(value)factsBox.append(node('p',label+'：'+value));
 panel.append(factsBox);
 if(!review){panel.append(node('p','人工处理信息暂不可用，请刷新后再试。','muted'));body.append(panel);return;}
 panel.append(badge(reviewLabel(review),review.needs_attention?'amount_mismatch':'success'));
 if(review.state==='closed'&&review.needs_attention)panel.append(node('p','此前工单已登记关闭，但当前事实或异常需要重新核对；关闭记录不代表新增异常已解决。','notice review-warning'));
 for(const reason of review.reasons||[])panel.append(node('p',reason.label||reason.code,'notice review-warning'));
 if(Number(review.scope_count)>1)panel.append(node('p',`此操作关联整批订单 ${review.order_no}（${review.scope_count} 个任务）。金额是整批金额，不得按子任务重复登记退款；处置以服务器核验结果为准。`,'notice review-warning'));
 panel.append(node('p','这里不会创建新的付款订单或猜测旧支付通道。费用账本缺口不等于未付款，也不按零费用处理。','small'));
 const actionable=d.metadata_invalid!==true&&Number.isInteger(review.revision)&&review.revision>=0&&typeof review.context==='string'&&review.context&&typeof review.order_no==='string'&&review.order_no;
 const allowed=actionable&&Array.isArray(review.allowed_actions)?review.allowed_actions.filter(action=>Object.hasOwn(reviewActionNames,action)):[];
 if(allowed.length){
  const form=node('form',null,'review-form'), controls=[];
  const field=(label,kind,id,max)=>{const wrapper=node('label',label),input=node(kind);input.id=id;input.maxLength=max;wrapper.append(input);form.append(wrapper);controls.push(input);return {wrapper,input};};
  const {input:action}=field('处理动作','select','review-action',100);
  for(const name of allowed){const option=node('option',reviewActionNames[name]);option.value=name;action.append(option);}action.value=allowed[0];
  const {input:note}=field('处理说明（必填）','textarea','review-note',4000);note.required=true;
  const proof=field('依据 / 外部处理凭据','textarea','review-evidence',4000);
  const reference=field('线下退款参考号','input','review-refund-reference',200);
  const warning=node('p','','notice review-warning');form.append(warning);
  const ackLabel=node('label',null,'review-check'),ack=node('input');ack.type='checkbox';ack.id='review-acknowledge';ackLabel.append(ack,node('span','已确认核验原订单及冻结金额；恢复履约可能产生模型费用。'));form.append(ackLabel);controls.push(ack);
  const submit=node('button','提交处置');submit.type='submit';submit.id='review-submit';form.append(submit);controls.push(submit);
  const status=node('p','','dialog-error');status.id='review-status';status.setAttribute('role','status');form.append(status);
  const update=()=>{const refund=action.value==='record_external_refund',fulfill=action.value==='fulfill';reference.wrapper.hidden=!refund;reference.input.required=refund;proof.input.required=action.value!=='note';ackLabel.hidden=!fulfill;ack.required=fulfill;ack.checked=false;warning.textContent=refund?'仅登记线下已经完成的退款：不调用退款接口，未经网关退款核验。不得把关单或付款截图当作退款凭据。':fulfill?'将重新向支付宝核验原订单与冻结金额。只有核验通过、当前状态仍允许时才恢复履约；不会要求用户再次付款。':action.value==='close_review'?'结束人工跟进不代表已退款、已交付或付款通道已修复；请填写处理依据。':'备注仅记录人工跟进，不改变付款或执行状态。';submit.textContent=reviewActionNames[action.value];submit.className=refund?'danger':'';};
  action.onchange=update;update();
  form.onsubmit=async event=>{
   event.preventDefault();if(!active())return;if(reviewInFlight.has(review.order_no)){status.textContent='此订单已有操作提交中，请等待结果后刷新。';return;}
   const selected=action.value,session=authGeneration;if(!allowed.includes(selected))return;
   const payload={action:selected,expected_revision:review.revision,expected_context:review.context,note:note.value.trim(),evidence:proof.input.value.trim(),refund_reference:selected==='record_external_refund'?reference.input.value.trim():'',acknowledge_cost:selected==='fulfill'&&ack.checked};
   if(!payload.note||(selected!=='note'&&!payload.evidence)||(selected==='record_external_refund'&&!payload.refund_reference)||(selected==='fulfill'&&!payload.acknowledge_cost)){status.textContent='请补齐说明、所需凭据和确认项。';return;}
   if(selected==='fulfill'&&!confirm('确认重新核验原订单及冻结金额，并恢复履约？可能产生模型费用；不会再次向用户收费。'))return;
   if(selected==='record_external_refund'&&!confirm('确认线下退款已经完成并有依据？这里只登记人工记录，不调用退款接口，也不代表已通过网关退款核验。'))return;
   const key=JSON.stringify([review.order_no,payload]);let entry=reviewRequests.get(key);
   if(!entry){entry=requestId();reviewRequests.set(key,entry);if(reviewRequests.size>100)reviewRequests.delete(reviewRequests.keys().next().value);}
   reviewInFlight.add(review.order_no);controls.forEach(c=>c.disabled=true);status.textContent='正在提交，请勿重复操作…';
   try{await api('/orders/'+encodeURIComponent(d.id)+'/review',{...payload,request_id:entry});reviewRequests.delete(key);if(active()){await detail(d.id);if(session===authGeneration)await load();}}
   catch(e){if(active()){if(e.status===409){await detail(d.id);if(session===authGeneration)await load();if(session===authGeneration&&detailJobId===d.id&&csrf)error('订单已变化，已刷新详情。请核对最新状态后重新操作。');}else status.textContent=e.message+'；如需重试，请保持原说明，系统会复用本次请求编号。';}}
   finally{reviewInFlight.delete(review.order_no);controls.forEach(c=>c.disabled=false);}
  };
  panel.append(form);
 }else panel.append(node('p','当前状态不允许新的人工处置，请查看下方历史。','muted'));
 const history=node('div',null,'review-history'),historyStatus=node('p','','dialog-error'),more=node('button','查看人工处理历史','secondary');more.id='review-history-more';more.disabled=d.metadata_invalid===true;historyStatus.setAttribute('role','status');if(more.disabled)historyStatus.textContent='历史元数据异常，当前仅展示只读诊断，相关操作已禁用。';panel.append(history,historyStatus,more);let cursor=null,loading=false,started=false;const seen=new Set();
 more.onclick=async()=>{if(loading||more.disabled||!active())return;loading=true;more.disabled=true;historyStatus.textContent='';try{const query=new URLSearchParams({limit:'20'});if(cursor)query.set('before',cursor);const data=await api('/orders/'+encodeURIComponent(d.id)+'/review-history?'+query);if(!active())return;for(const item of data.items||[]){if(seen.has(item.id))continue;seen.add(item.id);const row=node('article');row.append(node('h3',(reviewActionNames[item.action]||item.action)+' · '+dateText(item.created_at)));for(const [label,value]of [['处理人',item.actor],['说明',item.note],['依据',item.evidence],['退款参考号',item.refund_reference],['结果',typeof item.result==='object'?JSON.stringify(item.result):item.result]])if(value)row.append(node('p',label+'：'+value));history.append(row);}if(!started&&!seen.size)history.append(node('p','暂无人工处理记录','muted'));started=true;cursor=data.next_cursor||null;more.hidden=!cursor;more.textContent='加载更早记录';}catch(e){if(active())historyStatus.textContent=e.message;}finally{loading=false;more.disabled=d.metadata_invalid===true;}};
 body.append(panel);
}
async function load(){
 if(!csrf)return;
 const generation=++listGeneration,session=authGeneration;error('');
 const query=new URLSearchParams();for(const [k,v] of new FormData($('filters')))if(v)query.set(k,v);query.set('page',page);
 $('rows').replaceChildren();const loading=node('td','正在查询…','empty');loading.colSpan=8;const lr=node('tr');lr.append(loading);$('rows').append(lr);
 try{const data=await api('/orders?'+query);if(generation!==listGeneration||session!==authGeneration)return;total=data.total;$('total').textContent=total+' 条任务';$('rows').replaceChildren();
 for(const row of data.items){const tr=node('tr');let td=node('td');td.append(node('div',row.filename,'filename'),node('div',row.order_no,'small'));tr.append(td);
 tr.append(node('td',dateText(row.created_at)));td=node('td');td.append(badge(taskNames[row.status]||row.status,row.status));tr.append(td);
 td=node('td',row.price_cny?'¥'+row.price_cny:'未知');if(row.batch_id)td.append(node('div','整批价格','small'));tr.append(td);
 td=node('td');td.append(badge(payNames[row.payment.status],row.payment.status));if(row.review)td.append(node('div',reviewLabel(row.review),'review-badge small'));tr.append(td);
 td=node('td');for(const [key,label] of [['quote_shown','报价已展示'],['payment_clicked','已点击付款'],['payment_succeeded','支付已确认']])if(row.checkout?.[key])td.append(node('div',label,'small'));if(!td.childNodes.length)td.textContent='暂无记录';tr.append(td);
 td=node('td',costText(row.cost));td.append(node('div',costCoverage(row.cost),'small'));tr.append(td);
 td=node('td');const b=node('button','详情','secondary');b.onclick=()=>detail(row.id);td.append(b);tr.append(td);$('rows').append(tr);}
 if(!data.items.length){const tr=node('tr'),td=node('td','没有符合条件的订单','empty');td.colSpan=8;tr.append(td);$('rows').append(tr);}
 $('page-info').textContent=`第 ${page} / ${Math.max(1,Math.ceil(total/25))} 页`;$('previous').disabled=page<=1;$('next').disabled=page*25>=total;
 }catch(e){if(generation===listGeneration&&session===authGeneration)error(e.message);}
}
async function detail(id){
 if(!csrf)return;
 const generation=++detailGeneration,session=authGeneration;detailJobId=id;const active=()=>generation===detailGeneration&&session===authGeneration&&detailJobId===id&&Boolean(csrf);$('detail-body').replaceChildren(node('p','正在查询详情…','muted'));
 try{const d=await api('/orders/'+encodeURIComponent(id));if(!active())return;const metadataInvalid=d.metadata_invalid===true,body=$('detail-body');body.replaceChildren(node('h3',d.filename,'detail-title'));const grid=node('dl',null,'detail-grid');
 const l=d.cost.ledger||{};const bill=Object.entries(l.bill_imported_totals||{}).map(([currency,amount])=>currency+' '+amount).join(' + ')||'尚未导入供应商账单';
 for(const [label,value] of [['商户订单号',d.order_no],['任务状态',taskNames[d.status]||d.status||'未知'],['订单价格',d.price_cny?'¥'+d.price_cny+' · '+d.price_scope:'未知'],['支付状态',payNames[d.payment.status]],['支付宝查询金额',d.payment.amount?'¥'+d.payment.amount:'未知'],['最近核验',dateText(d.payment.checked_at)],['创建时间',dateText(d.created_at)],['模型',d.model||'不适用'],['Token 成本',costText(d.cost)],['费用记录覆盖',costCoverage(d.cost)],['已记录输入 Token',l.prompt_tokens??'未知'],['已记录输出 Token',l.completion_tokens??'未知'],['已记录缓存命中 Token',l.cache_hit_tokens??'未知'],['已记录缓存未命中 Token',l.cache_miss_tokens??'未知'],['账单导入金额',bill],['历史未记录尝试',l.historical_untracked_attempts??'未知']]){const el=node('div');el.append(node('dt',label),node('dd',value));grid.append(el);}body.append(grid,node('p',l.note||d.cost.note,'notice'));
 if(!l.tokens_complete||!l.cache_tokens_complete)body.append(node('p','以上 Token 仅为已记录部分；缺失 usage、缓存拆分或历史记录不能推算为 0。','notice'));
 const stageNames={body:'正文 / 目录与脚注',glossary:'术语',book_profile:'图书画像',book_title:'书名',rescue:'补译',semantic_review:'语义审校',style_guide:'风格档案',literary_polish:'文学润色',literary_verify:'文学复核',precision_polish:'繁简精校'};
 const costs=node('div',null,'notice');costs.append(node('h2','模型调用分阶段统计'));for(const [stage,v]of Object.entries(l.stages||{}))costs.append(node('p',`${stageNames[stage]||stage}：${v.requests} 次请求，${v.priced_requests} 次已计价，已记录 ${v.total_tokens} tokens`));
 const usageButton=node('button','查看逐请求费用明细','secondary');usageButton.disabled=metadataInvalid;costs.append(usageButton);const usageRows=node('div');costs.append(usageRows);body.append(costs);let usagePage=1;
 usageButton.onclick=async()=>{if(!active()||usageButton.disabled)return;usageButton.disabled=true;try{const data=await api('/orders/'+encodeURIComponent(id)+'/usage?page='+usagePage);if(!active())return;if(usagePage===1)usageRows.replaceChildren();for(const r of data.items){usageRows.append(node('p',`${dateText(r.started_at)} · ${stageNames[r.stage]||r.stage} · ${r.provider} / ${r.response_model||r.requested_model} · ${r.total_tokens??'未知'} tokens · ${r.calculated_cost!==null?r.currency+' '+r.calculated_cost:'待核实：'+r.price_status} · 请求 ${r.response_id||r.id}`,'small'));}usagePage++;usageButton.hidden=(usagePage-1)*data.page_size>=data.summary.requests;usageButton.textContent='加载下一页明细';}catch(e){if(active())usageRows.append(node('p',e.message,'failure'));}finally{usageButton.disabled=false;}};
 const funnel=node('div',null,'notice');funnel.append(node('h2','付款过程（首次记录时间）'));for(const [key,label] of [['quote_shown','展示报价'],['payment_clicked','点击付款'],['payment_succeeded','支付成功']]){const event=d.checkout?.[key];funnel.append(node('p',label+'：'+(event?dateText(event.at)+(event.source==='verified_query'?'（查单确认时间）':''):'无记录 / 未知')));}funnel.append(node('p','报价与点击来自浏览器上报；扫码行为无法直接观测。无记录不等于未操作，历史记录不回填。','small'));body.append(funnel);
 body.append(node('h2','处理结果 / 失败原因'),node('p',[d.error_code,d.message].filter(Boolean).join('\n')||'暂无记录',d.status==='failed'?'failure':'muted'));
 renderReview(d,body,active);
 const actions=node('div',null,'actions');for(const [kind,label]of [['source','下载原文件'],['output','下载结果文件']]){if(!metadataInvalid&&d.files[kind]){const a=node('a',label,'file-link');a.href='/api/admin/orders/'+encodeURIComponent(id)+'/files/'+kind;actions.append(a);}else actions.append(node('span',label.replace('下载','')+'不可用','muted'));}
 const payment=node('button','核验支付宝支付','secondary');payment.disabled=metadataInvalid;actions.append(payment);
 const retry=node('button','重试失败订单');retry.disabled=metadataInvalid||d.status!=='failed'||!d.files.source;actions.append(retry);body.append(actions);
 const status=node('p','','dialog-error');status.setAttribute('role','status');body.append(status);
 payment.onclick=async()=>{if(!active()||payment.disabled)return;payment.disabled=true;status.textContent='正在查询支付宝…';try{await api('/orders/'+encodeURIComponent(id)+'/payment',{});if(active()){await detail(id);if(session===authGeneration)await load();}}catch(e){if(active())status.textContent=e.message;}finally{payment.disabled=metadataInvalid;}};
 retry.onclick=async()=>{if(!active()||retry.disabled||!confirm('将重新核验支付并复用现有缓存重试。重试可能产生新的模型费用。确认继续？'))return;retry.disabled=true;status.textContent='正在核验支付并安排重试…';try{await api('/orders/'+encodeURIComponent(id)+'/retry',{acknowledge_cost:true});if(active()){await detail(id);if(session===authGeneration)await load();}}catch(e){if(active())status.textContent=e.message;}finally{retry.disabled=metadataInvalid||d.status!=='failed'||!d.files.source;}};
 const stages=node('div',null,'stages');stages.append(node('h2','最近处理阶段'));for(const s of d.stages)stages.append(node('p',`${dateText(s.started_at)} · ${s.name} · ${s.status}${s.elapsed_ms!=null?' · '+s.elapsed_ms+' ms':''}${s.previous_failure?' · 上次失败：'+s.previous_failure:''}`));if(!d.stages.length)stages.append(node('p','暂无阶段记录','muted'));body.append(stages);if(!$('detail').open)$('detail').showModal();
 }catch(e){if(active())error(e.message);}
}
$('login-form').onsubmit=async e=>{e.preventDefault();const generation=++authGeneration;error('');$('login-button').disabled=true;try{const data=await api('/login',{username:$('user').value,password:$('password').value});if(generation!==authGeneration)return;loggedIn(data);await load();}catch(e){if(generation===authGeneration)error(e.message);}finally{$('login-button').disabled=false;}};
$('logout').onclick=async()=>{const work=api('/logout',{});showLogin();const generation=authGeneration;try{await work;}catch(e){if(generation===authGeneration)error(e.message);}};
$('filters').onsubmit=e=>{e.preventDefault();page=1;load();};$('filters').onreset=()=>{setTimeout(()=>{page=1;load();},0);};
$('refresh').onclick=()=>load();$('previous').onclick=()=>{page--;load();};$('next').onclick=()=>{page++;load();};$('close').onclick=()=>{invalidateDetail();$('detail').close();};$('detail').addEventListener('cancel',invalidateDetail);
const initialSessionGeneration=authGeneration;
api('/session').then(async data=>{if(initialSessionGeneration!==authGeneration)return;loggedIn(data);await load();}).catch(e=>{if(initialSessionGeneration===authGeneration&&e.message!=='请登录管理员账号')error(e.message);});
