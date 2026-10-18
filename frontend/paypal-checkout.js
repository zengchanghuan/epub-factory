(async function () {
  "use strict";
  const $ = id => document.getElementById(id);
  let localId, token, auth, info, busy = false;
  const message = text => { $("status").textContent = text; };
  async function api(path, method = "GET") {
    const response = await fetch(path, { method, headers: {
      ...(token ? { "X-Job-Token": token } : {}), ...(auth ? { Authorization: `Bearer ${auth}` } : {}),
    }, cache: "no-store" });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "订单暂不可用，请返回任务中心查看。");
    return data;
  }
  function complete() {
    $("paypal-buttons").hidden = true;
    $("recover").hidden = true;
    message("付款已由服务器核验。任务会在后台继续，请返回 FixEpub 查看进度和下载结果。");
  }
  try {
    localId = new URLSearchParams(location.search).get("order") || "";
    if (!/^(?:batch_)?[A-Za-z0-9_-]{1,64}$/.test(localId)) throw new Error("请从 FixEpub 原订单打开付款页。");
    const key = localId.startsWith("batch_") ? `batch:${localId.slice(6)}` : localId;
    try {
      token = JSON.parse(localStorage.getItem("job_access_tokens_v1") || "{}")[key] || "";
      auth = localStorage.getItem("fixepub_auth_token") || "";
    } catch (_) { throw new Error("无法读取订单凭证，请使用原下单浏览器从任务中心重新打开。"); }
    info = await api(`/api/v2/paypal/orders/${encodeURIComponent(localId)}`);
    if (info.local_id !== localId || info.currency !== "USD" || !/^\d+\.\d{2}$/.test(info.amount)) throw new Error("订单信息不一致，请返回任务中心。");
    $("description").textContent = `电子书格式转换 · ${info.file_count} 本`;
    $("price").textContent = `US$${info.amount}`;
    $("return").href = info.batch_id ? `/?batch_id=${encodeURIComponent(info.batch_id)}` : `/?job_id=${encodeURIComponent(info.job_id)}`;
    if (info.status !== "pending_payment") {
      message("此订单当前无需付款，请返回任务中心查看状态。");
      return;
    }
    const config = await api("/api/v2/paypal/config");
    if (!config.enabled || !/^[A-Za-z0-9_-]+$/.test(config.client_id || "") || config.currency !== "USD") throw new Error("PayPal 暂不可用，请稍后重试。");
    await new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = `https://www.paypal.com/sdk/js?client-id=${encodeURIComponent(config.client_id)}&currency=USD&intent=capture&components=buttons&disable-funding=card,credit,paylater,venmo`;
      script.onload = resolve; script.onerror = () => reject(new Error("PayPal 支付组件加载失败，请刷新页面。"));
      document.head.append(script);
    });
    async function capture() {
      if (busy) return;
      busy = true; $("recover").disabled = true;
      try {
        message("正在核验 PayPal 付款，请勿重复付款…");
        const result = await api(`/api/v2/paypal/orders/${encodeURIComponent(localId)}/capture`, "POST");
        if (result.status !== "COMPLETED" || result.local_id !== localId) throw new Error("付款尚未确认，请稍后核验。");
        complete();
      } catch (error) {
        message(error.message); $("recover").hidden = false;
      } finally { busy = false; $("recover").disabled = false; }
    }
    $("recover").onclick = capture;
    await window.paypal.Buttons({
      style: { layout: "vertical", label: "pay", shape: "rect", color: "gold" },
      createOrder: async () => {
        message("");
        const order = await api(`/api/v2/paypal/orders/${encodeURIComponent(localId)}`, "POST");
        if (!/^[A-Z0-9]{8,40}$/.test(order.id || "")) throw new Error("PayPal 订单创建结果无效。");
        return order.id;
      },
      onApprove: capture,
      onCancel: () => message("已取消本次付款。原订单仍可在有效期内继续支付。"),
      onError: () => { message("PayPal 暂未完成付款。若已扣款，请点击重新核验；请勿重复付款。"); $("recover").hidden = false; },
    }).render("#paypal-buttons");
  } catch (error) { message(error.message || "付款页暂不可用，请返回任务中心。"); }
})();
