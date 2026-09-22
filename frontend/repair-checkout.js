/* Independent repair checkout. Payment success is accepted only from the server. */
(function (root) {
  'use strict';
  function mount(options) {
    const win = options.window;
    const doc = options.document;
    const fetcher = options.fetch || win.fetch.bind(win);
    const later = options.returnLater;
    const headers = options.authHeaders || (() => ({}));
    const now = options.now || Date.now;
    const schedule = options.setTimeout || win.setTimeout.bind(win);
    const cancel = options.clearTimeout || win.clearTimeout.bind(win);
    const ids = ['dropzone', 'fileInput', 'dropHint', 'diagStatus', 'reportBlock', 'okBanner', 'issueArea',
      'issueList', 'summaryBadge', 'summaryText', 'summaryMeta', 'payBtn', 'payButtonLabel', 'priceLabel',
      'payBox', 'payPrice', 'qrImg', 'qrPayment', 'webPayment', 'alipayLink', 'checkPaymentBtn',
      'fixStatus', 'downloadArea', 'downloadBtn', 'resetBtn', 'resetBtn2'];
    const el = Object.fromEntries(ids.map(id => [id, doc.getElementById(id)]));
    const minRecoveryDelay = 45000;
    const maxRecoveryChecks = 10;
    let epoch = 0;
    let state = null;
    let timer = null;
    let popup = null;
    const endpoint = (id, action) => `/api/v2/repair/${encodeURIComponent(id)}/${action}`;
    const active = s => s && state === s && s.epoch === epoch;
    const terminal = s => ['paid', 'repaired', 'failed'].includes(s.status);
    function text(node, value) { node.textContent = value; }
    function closePopup() {
      try { if (popup && !popup.closed) popup.close(); } catch (_) { /* browser window policy */ }
      popup = null;
    }
    function setPrice(value) {
      if (value == null || !/^\d+(?:\.\d{1,2})?$/.test(String(value))) return;
      const price = Number(value).toFixed(2);
      text(el.priceLabel, '¥' + price);
      text(el.payPrice, '¥' + price + ' / 次');
    }
    function clearPayment() {
      el.payBox.classList.remove('visible');
      el.qrPayment.hidden = true;
      el.webPayment.hidden = true;
      el.qrImg.removeAttribute('src');
      el.alipayLink.removeAttribute('href');
      el.checkPaymentBtn.hidden = true;
    }
    function updateButton(s) {
      const eligible = active(s) && s.canPay && !terminal(s);
      el.payBtn.disabled = !eligible || s.paying;
      text(el.payButtonLabel, s && s.paying ? '正在生成支付入口…' : s && s.hasPayment ? '重新获取支付入口' : '🔧 一键修复');
      const canCheck = active(s) && !terminal(s) && (s.canPay || s.paymentStarted);
      el.checkPaymentBtn.hidden = !canCheck;
      el.checkPaymentBtn.disabled = !canCheck || s.recovering;
    }
    function begin() {
      epoch += 1;
      cancel(timer);
      timer = null;
      state = null;
      closePopup();
      clearPayment();
      el.payBtn.disabled = true;
      text(el.payButtonLabel, '🔧 一键修复');
      setPrice('2.99');
      el.reportBlock.classList.remove('visible');
      el.okBanner.style.display = 'none';
      el.issueArea.style.display = 'none';
      el.downloadArea.style.display = 'none';
      el.downloadBtn.removeAttribute('href');
      for (const id of ['diagStatus', 'fixStatus', 'summaryBadge', 'summaryText', 'summaryMeta']) text(el[id], '');
      el.issueList.replaceChildren();
      return epoch;
    }
    function reset() {
      begin();
      later.setTask(null, true);
      options.onClear();
      el.fileInput.value = '';
      text(el.dropHint, '仅支持 .epub，最大 60MB');
    }
    function activate(id, filename, preserveEmail) {
      const s = { id, epoch, status: 'pending_payment', canPay: false, paying: false, recovering: false,
        reading: false, hasPayment: false, recoveryChecks: 0, nextRecoveryAt: 0, mayRecover: true,
        quoteReported: false, checkResult: '', report: null, paymentStarted: false };
      state = s;
      options.onTask(id);
      later.setTask({ kind: 'repair', id, filename, status: s.status }, !!preserveEmail);
      return s;
    }
    async function reportEvent(s, event) {
      if (!active(s)) return;
      try {
        await fetcher(endpoint(s.id, 'checkout-events'), {
          method: 'POST', headers: { ...headers(s.id), 'Content-Type': 'application/json' },
          body: JSON.stringify({ event }),
        });
      } catch (_) { /* analytics must never block payment */ }
    }
    function showPending(s) {
      if (!active(s) || terminal(s)) return;
      if (!s.canPay && !s.paymentStarted) {
        text(el.fixStatus, '此文件当前无需修复或无法自动修复，不会发起付款。');
      } else if (s.checkResult === 'unavailable') {
        text(el.fixStatus, '暂时无法可靠确认支付宝付款结果。若已付款，请勿重复支付；系统会继续核实，也可联系客服。');
      } else if (s.checkResult === 'manual_review_required') {
        text(el.fixStatus, '付款状态仍待核实，自动核实期限已结束。若已付款请勿重复支付，请联系客服协助核对。');
      } else if (!s.canPay) {
        text(el.fixStatus, '此文件无法自动修复，已有付款仍待核实。若已付款请勿重复支付，请联系客服协助处理。');
      } else if (s.recoveryChecks >= maxRecoveryChecks) {
        text(el.fixStatus, '本页暂未收到付款确认，自动查询已暂停。服务器会继续核实；若已付款请勿重复支付，可稍后返回或联系客服。');
      } else if (s.hasPayment || s.checkResult === 'pending' || s.checkResult === 'throttled') {
        text(el.fixStatus, '等待服务端确认付款。若已付款请勿重复支付，确认后会自动修复；可以关闭页面稍后回来。');
      } else {
        text(el.fixStatus, '尚未收到付款确认。完成付款后会在后台修复，可关闭页面稍后回来。');
      }
    }
    function applyStatus(s, data) {
      if (!active(s)) return;
      // An older in-flight status read must not regress verified payment or completion.
      if (data.status && ((terminal(s) && data.status === 'pending_payment') ||
          (['repaired', 'failed'].includes(s.status) && data.status !== s.status))) return;
      if (['unavailable', 'manual_review_required', 'pending', 'verified_paid'].includes(data.payment_check)) s.checkResult = data.payment_check;
      if (s.checkResult === 'manual_review_required') s.mayRecover = false;
      if (typeof data.payment_started === 'boolean') {
        s.paymentStarted = data.payment_started;
        if (!data.payment_started) s.mayRecover = false;
      }
      if (data.price_cny != null) setPrice(data.price_cny);
      if (typeof data.can_pay === 'boolean') s.canPay = data.can_pay;
      if (data.status) s.status = data.status;
      if (data.report) {
        // The immutable diagnosis can be large: do not recreate its DOM every poll.
        if (!s.report || JSON.stringify(s.report) !== JSON.stringify(data.report)) {
          s.report = data.report;
          options.renderReport(data.report);
        }
      } else if (!s.report && s.canPay) {
        el.reportBlock.classList.add('visible');
        el.issueArea.style.display = 'block';
        text(el.summaryText, '已恢复原修复任务，无需重新上传。');
      }
      later.remember({ kind: 'repair', id: s.id, status: s.status });
      if (s.status === 'paid') {
        closePopup(); clearPayment();
        text(el.fixStatus, '支付已确认，正在后台修复文件，可以关闭页面。');
      } else if (s.status === 'repaired') {
        cancel(timer); closePopup(); clearPayment();
        text(el.fixStatus, '');
        el.downloadBtn.href = endpoint(s.id, 'download');
        el.downloadArea.style.display = 'block';
      } else if (s.status === 'failed') {
        cancel(timer); closePopup(); clearPayment();
        text(el.fixStatus, '修复失败：' + (data.error || '请联系客服协助处理。'));
      } else {
        showPending(s);
        if (s.canPay && !s.quoteReported) {
          s.quoteReported = true;
          reportEvent(s, 'quote_shown');
        }
      }
      updateButton(s);
    }
    async function checkStatus(s) {
      if (!active(s) || s.reading) return;
      s.reading = true;
      const statusWhenRequested = s.status;
      try {
        const res = await fetcher(endpoint(s.id, 'status'), { cache: 'no-store', headers: headers(s.id) });
        if (!active(s)) return;
        if (!res.ok) {
          if (terminal(s) && s.status !== statusWhenRequested) return;
          if (res.status === 404 || res.status === 403) {
            s.canPay = false; s.stopped = true;
            clearPayment(); updateButton(s);
            text(el.fixStatus, res.status === 404 ? '任务记录不存在或已过期，请联系客服。' : '无法读取此任务，请从原浏览器或邮件中的入口打开。');
          } else {
            text(el.fixStatus, '暂时无法读取最新状态，稍后自动重试；这不代表未付款或任务失败。');
          }
          return;
        }
        const data = await res.json();
        if (active(s)) applyStatus(s, data);
      } catch (_) {
        if (active(s) && (!terminal(s) || s.status === statusWhenRequested)) text(el.fixStatus, '暂时无法读取最新状态，稍后自动重试；这不代表未付款或任务失败。');
      } finally { s.reading = false; }
    }
    async function recover(s, manual) {
      if (!active(s) || !(s.canPay || s.paymentStarted) || terminal(s) || s.recovering || s.stopped) return;
      if (s.recoveryChecks >= maxRecoveryChecks) { if (manual) showPending(s); return; }
      if (now() < s.nextRecoveryAt) {
        if (manual && s.checkResult !== 'unavailable') text(el.fixStatus, '正在等待付款确认，请稍后再查询；若已付款请勿重复支付。');
        return;
      }
      if (!manual && !s.mayRecover) return;
      s.recovering = true;
      s.recoveryChecks += 1;
      s.nextRecoveryAt = now() + minRecoveryDelay;
      updateButton(s);
      try {
        const res = await fetcher(endpoint(s.id, 'recover'), { method: 'POST', headers: headers(s.id) });
        if (!active(s)) return;
        if (!res.ok) throw new Error('Payment confirmation unavailable');
        const data = await res.json();
        if (!active(s)) return;
        if (!(data.payment_check === 'throttled' && ['unavailable', 'manual_review_required'].includes(s.checkResult))) s.checkResult = data.payment_check || 'unavailable';
        const retryAfter = Number(data.retry_after_seconds);
        if (Number.isFinite(retryAfter) && retryAfter > 0) s.nextRecoveryAt = now() + Math.max(minRecoveryDelay, retryAfter * 1000);
        if (s.checkResult === 'not_started' || s.checkResult === 'manual_review_required') s.mayRecover = false;
        applyStatus(s, data);
        // Some recover versions only return payment status. Refresh download/error details.
        if (s.checkResult === 'verified_paid') await checkStatus(s);
      } catch (_) {
        if (active(s)) { s.checkResult = 'unavailable'; showPending(s); }
      } finally {
        s.recovering = false;
        if (active(s)) updateButton(s);
      }
    }
    function queuePoll(s) {
      cancel(timer);
      timer = schedule(() => poll(s), 3000);
    }
    async function poll(s) {
      if (!active(s) || s.stopped) return;
      await checkStatus(s);
      if (!active(s) || s.stopped || ['repaired', 'failed'].includes(s.status)) return;
      // Local status polling never performs a gateway query. Explicit recovery is bounded separately.
      await recover(s, false);
      if (active(s) && !s.stopped && !['repaired', 'failed'].includes(s.status)) queuePoll(s);
    }
    function resume(jobId) {
      if (!/^[a-zA-Z0-9_-]{1,100}$/.test(jobId)) return;
      begin();
      const s = activate(jobId);
      el.reportBlock.classList.add('visible');
      text(el.fixStatus, '正在读取任务及付款状态…');
      return poll(s);
    }
    async function handleFile(file) {
      if (!file.name.toLowerCase().endsWith('.epub') || file.size > 60 * 1024 * 1024) {
        text(el.diagStatus, file.size > 60 * 1024 * 1024 ? '文件超过 60MB 限制' : '请选择 .epub 文件');
        return;
      }
      const uploadEpoch = begin();
      later.setTask(null, true);
      options.onClear();
      text(el.dropHint, file.name);
      text(el.diagStatus, '正在诊断文件格式…');
      const form = new win.FormData();
      form.append('file', file);
      try {
        const res = await fetcher('/api/v2/repair/diagnose', { method: 'POST', body: form });
        const data = await res.json();
        if (epoch !== uploadEpoch) return;
        if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请稍后重试');
        const s = activate(data.job_id, file.name, true);
        text(el.diagStatus, '');
        // The server owns eligibility and the frozen quote. Never infer a cheaper admin price locally.
        applyStatus(s, data);
        if (typeof data.can_pay !== 'boolean') await checkStatus(s);
        if (active(s) && s.canPay) queuePoll(s);
      } catch (error) {
        if (epoch === uploadEpoch) text(el.diagStatus, '诊断失败：' + error.message);
      }
    }
    function adminKey() {
      for (const storage of [options.storage, options.sessionStorage]) {
        try { const key = storage && storage.getItem('admin_key'); if (key) return key; } catch (_) { /* restricted storage */ }
      }
      return null;
    }
    async function pay() {
      const s = state;
      if (!active(s) || !s.canPay || terminal(s) || s.paying) return;
      s.paying = true;
      clearPayment();
      updateButton(s);
      text(el.diagStatus, '');
      reportEvent(s, 'payment_clicked');
      try {
        const key = adminKey();
        const form = new win.FormData();
        if (key) form.append('admin_key', key);
        const res = await fetcher(endpoint(s.id, 'pay'), { method: 'POST', headers: headers(s.id), body: key ? form : undefined });
        const data = await res.json();
        if (!active(s)) return;
        if (!res.ok) {
          if (res.status === 409) await checkStatus(s);
          throw new Error(typeof data.detail === 'string' ? data.detail : '暂时无法生成支付入口，请稍后重试');
        }
        if (terminal(s)) return;
        if (data.price_cny != null) setPrice(data.price_cny);
        if (['paid', 'repaired', 'failed'].includes(data.status)) {
          applyStatus(s, data);
        } else {
          if (data.qr_code) {
            const qr = options.QRCode || win.QRCode;
            if (!qr || typeof qr.toDataURL !== 'function') throw new Error('二维码组件未能加载，请检查网络后重试');
            const url = await qr.toDataURL(data.qr_code, { width: 200, margin: 1 });
            if (!active(s) || terminal(s)) return;
            el.qrImg.src = url;
            el.qrPayment.hidden = false;
          } else if (data.pay_url) {
            const url = new URL(data.pay_url);
            if (url.protocol !== 'https:' || !(url.hostname === 'alipay.com' || url.hostname.endsWith('.alipay.com'))) throw new Error('支付链接不正确，请联系客服');
            el.alipayLink.href = url.href;
            el.webPayment.hidden = false;
          } else { throw new Error('未取得支付入口，请稍后重试'); }
          s.hasPayment = true;
          s.paymentStarted = true;
          s.mayRecover = true;
          s.checkResult = '';
          el.payBox.classList.add('visible');
          showPending(s);
        }
        cancel(timer);
        if (!['repaired', 'failed'].includes(s.status)) queuePoll(s);
      } catch (error) {
        if (active(s) && !terminal(s)) text(el.diagStatus, '支付发起失败：' + error.message);
      } finally {
        s.paying = false;
        if (active(s)) updateButton(s);
      }
    }
    function openAlipay(event) {
      event.preventDefault();
      const s = state;
      if (!active(s) || !s.hasPayment || terminal(s) || el.webPayment.hidden) return;
      const url = el.alipayLink.getAttribute('href');
      if (!url) return;
      const width = 520, height = 720;
      const left = Math.max(0, Math.round(win.screenX + (win.outerWidth - width) / 2));
      const top = Math.max(0, Math.round(win.screenY + (win.outerHeight - height) / 2));
      popup = win.open(url, 'fixepub-alipay', `popup=yes,width=${width},height=${height},left=${left},top=${top},resizable=yes,scrollbars=yes`);
      if (popup) popup.focus(); else win.open(url, '_blank', 'noopener');
    }
    async function refresh() {
      const s = state;
      if (!active(s) || s.stopped) return;
      await checkStatus(s);
      if (active(s)) await recover(s, false);
    }
    el.payBtn.addEventListener('click', pay);
    el.checkPaymentBtn.addEventListener('click', () => recover(state, true));
    el.alipayLink.addEventListener('click', openAlipay);
    el.resetBtn.addEventListener('click', reset);
    el.resetBtn2.addEventListener('click', reset);
    el.dropzone.addEventListener('click', () => el.fileInput.click());
    el.dropzone.addEventListener('dragover', e => { e.preventDefault(); el.dropzone.classList.add('active'); });
    el.dropzone.addEventListener('dragleave', () => el.dropzone.classList.remove('active'));
    el.dropzone.addEventListener('drop', e => { e.preventDefault(); el.dropzone.classList.remove('active'); if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]); });
    el.fileInput.addEventListener('change', () => { if (el.fileInput.files[0]) handleFile(el.fileInput.files[0]); });
    win.addEventListener('focus', refresh);
    doc.addEventListener('visibilitychange', () => { if (doc.visibilityState === 'visible') refresh(); });
    updateButton(null);
    return { resume, handleFile, pay, reset, refresh, checkPayment: () => recover(state, true) };
  }
  const api = { mount };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.FixEpubRepairCheckout = api;
})(typeof window !== 'undefined' ? window : globalThis);
