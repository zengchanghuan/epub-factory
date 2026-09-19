/* Shared return-later controls. No address or bearer token is rendered as HTML. */
(function (root) {
  "use strict";
  function safeStorage(host, name) {
    const memory = new Map();
    let native;
    try { native = host[name || "localStorage"]; } catch (_) { /* restricted browser */ }
    const storage = {
      available: !!native,
      getItem(key) {
        if (!storage.available && memory.has(key)) return memory.get(key);
        try { if (native) { const value = native.getItem(key); if (value != null) memory.set(key, value); else memory.delete(key); } }
        catch (_) { storage.available = false; }
        return memory.get(key) || null;
      },
      setItem(key, value) {
        memory.set(key, String(value));
        try { if (!native) throw new Error("unavailable"); native.setItem(key, String(value)); }
        catch (_) { storage.available = false; }
      },
      removeItem(key) {
        memory.delete(key);
        try { if (native) native.removeItem(key); } catch (_) { storage.available = false; }
      },
    };
    if (native) {
      try { native.setItem("fixepub_storage_check", "1"); native.removeItem("fixepub_storage_check"); }
      catch (_) { storage.available = false; }
    }
    return storage;
  }
  function validTask(task) {
    return task && ["job", "batch", "repair"].includes(task.kind || "job") && /^[a-zA-Z0-9_-]{1,100}$/.test(task.id || task.jobId || "");
  }
  function taskLink(task) {
    if (!validTask(task)) return "";
    const kind = task.kind || "job", id = task.id || task.jobId;
    return `${kind === "repair" ? "/epub-repair.html" : "/"}?${kind === "batch" ? "batch_id" : "job_id"}=${encodeURIComponent(id)}`;
  }
  function importFragment(location, history, saveToken, repair) {
    const params = new URLSearchParams(location.search);
    const fragment = new URLSearchParams(location.hash.replace(/^#/, ""));
    const token = fragment.get("access_token");
    const id = params.get("batch_id") || params.get("job_id");
    if (token && id && validTask({ kind: repair ? "repair" : "job", id })) {
      saveToken(params.has("batch_id") ? `batch:${id}` : id, token);
      fragment.delete("access_token");
      history.replaceState(null, "", `${location.pathname}${location.search}${fragment.toString() ? "#" + fragment.toString() : ""}`);
    }
  }
  function endpoint(task) {
    const collection = { job: "jobs", batch: "batches", repair: "repair" }[task.kind];
    return `/api/v2/${collection}/${encodeURIComponent(task.id)}/notification-email`;
  }
  function mount(options) {
    const { root: panel, storage, loadHistory, saveHistory } = options;
    const doc = panel.ownerDocument;
    const request = options.fetch || root.fetch.bind(root);
    const api = options.api || "";
    let current = null, available = false, saving = false, sequence = 0, inputVersion = 0;
    let saveDone = Promise.resolve();
    panel.className = "return-later-panel";
    panel.innerHTML = '<strong>不必一直等在这里</strong><p>付款成功后，任务会在后台继续处理，可以关闭页面。稍后或隔天用<strong>同一浏览器</strong>回到本页、刷新查看进度，完成后即可下载；请勿清理本站记录。处理时间取决于书籍大小和排队情况。</p><div class="return-later-email"><label for="notificationEmail">完成后邮件提醒（选填）</label><p id="notificationTaskLabel">将用于下一次上传创建的任务。</p><div class="return-later-email-row"><input id="notificationEmail" type="email" maxlength="254" autocomplete="email" placeholder="你的邮箱地址" aria-describedby="notificationEmailHint"><button type="button" id="saveNotificationEmail">保存邮箱</button></div><p id="notificationEmailHint" role="status" aria-live="polite">正在检查邮件通知服务…</p><p class="return-later-privacy">仅用于当前任务的结果通知，邮件中提供下载入口。留空并保存可取消通知。</p></div><p id="returnLaterStorageWarning" class="return-later-warning" hidden>浏览器未允许保存任务记录，关闭后无法保证恢复。建议先保存邮箱，收到通知后从邮件进入下载。</p><div id="returnLaterRecent" hidden><strong>本机最近任务</strong><ul id="returnLaterTaskList"></ul></div>';
    const input = panel.querySelector("#notificationEmail");
    const button = panel.querySelector("#saveNotificationEmail");
    const hint = panel.querySelector("#notificationEmailHint");
    input.addEventListener("input", () => { inputVersion++; });
    function warnStorage() { panel.querySelector("#returnLaterStorageWarning").hidden = storage.available !== false; }
    function auth(task) { return options.authHeaders ? options.authHeaders(task.kind === "batch" ? `batch:${task.id}` : task.id) : {}; }
    function displayStatus(data) {
      if (!data || !data.enabled) return "未设置邮件通知；仍可回到本页查看任务。";
      if (data.status === "sent" || data.delivery_status === "sent") return "结果邮件已发送，请检查收件箱和垃圾邮件。";
      if (data.status === "failed" || data.delivery_status === "failed") return "邮箱已保存，但邮件暂未发送成功；请回到本页下载。";
      return "邮箱已保存，完成后将发送结果通知。";
    }
    function render() {
      const list = loadHistory(storage).filter(validTask);
      const ul = panel.querySelector("#returnLaterTaskList");
      ul.replaceChildren();
      panel.querySelector("#returnLaterRecent").hidden = list.length === 0;
      list.forEach(entry => {
        const task = { kind: entry.kind || "job", id: entry.jobId };
        const li = doc.createElement("li"), link = doc.createElement("a"), status = doc.createElement("span");
        link.href = taskLink(task);
        link.textContent = entry.filename || (task.kind === "batch" ? "批量任务" : "电子书任务");
        const labels = { pending_payment: "等待支付", pending: "等待处理", awaiting_confirmation: "待确认", queued: "排队中", running: "处理中", paid: "修复中", completed: "已完成", repaired: "已修复", success: "已完成", partial_completed: "部分完成", failed: "失败", cancelled: "已停止" };
        status.textContent = `${labels[entry.status] || "查看最新状态"} · 查看 / 下载`;
        link.appendChild(status);
        link.addEventListener("click", event => {
          if (options.onOpen && options.onOpen(task) !== false) event.preventDefault();
        });
        li.appendChild(link); ul.appendChild(li);
      });
      warnStorage();
    }
    function remember(task) {
      if (!validTask(task)) return;
      const old = loadHistory(storage).find(item => item.jobId === task.id && (item.kind || "job") === task.kind) || {};
      saveHistory(storage, { ...old, kind: task.kind, jobId: task.id, filename: task.filename || old.filename || "电子书任务", status: task.status || old.status || "pending", savedAt: old.savedAt || new Date().toISOString(), downloadUrl: null });
      if (current && current.kind === task.kind && current.id === task.id) panel.querySelector("#notificationTaskLabel").textContent = `当前任务：${task.filename || old.filename || task.id}`;
      render();
    }
    async function save() {
      if (saving) {
        const waitingGeneration = sequence;
        await saveDone;
        if (waitingGeneration !== sequence) return;
        return save();
      }
      if (!available && input.value.trim()) { hint.textContent = "邮件服务尚未启用，请稍后回到本页查看和下载；已有通知可清空邮箱后保存取消。"; return; }
      if (!input.checkValidity()) { input.reportValidity(); return; }
      if (!current) { hint.textContent = "已填写邮箱，上传创建任务后会自动保存。"; return; }
      const target = current, email = input.value.trim();
      const generation = sequence;
      let finishSave;
      saveDone = new Promise(resolve => { finishSave = resolve; });
      saving = true; button.disabled = true; hint.textContent = "正在保存邮箱…";
      try {
        const response = await request(api + endpoint(target), { method: "PUT", headers: { ...auth(target), "Content-Type": "application/json" }, body: JSON.stringify({ email }) });
        if (!response.ok) throw new Error("save failed");
        const data = await response.json();
        if (generation === sequence) hint.textContent = displayStatus(data) + (data.enabled && target.kind === "batch" ? " 批量任务将按每本书分别发送通知。" : "");
      } catch (_) {
        if (generation === sequence) hint.textContent = "邮箱未保存成功，请重试保存；任务处理不受影响。";
      } finally { saving = false; button.disabled = false; finishSave(); }
    }
    async function setTask(task, created) {
      current = task;
      const generation = ++sequence;
      if (!task) { if (!created) input.value = ""; panel.querySelector("#notificationTaskLabel").textContent = "将用于下一次上传创建的任务。"; hint.textContent = available ? "邮箱选填，上传后自动保存到新任务。" : "邮件服务尚未启用，可回到本页下载。"; return; }
      remember(task);
      if (created && input.value.trim()) { await capability; if (generation === sequence) await save(); return; }
      input.value = "";
      const version = inputVersion;
      try {
        const response = await request(api + endpoint(task), { headers: auth(task), cache: "no-store" });
        if (!response.ok) throw new Error("load failed");
        const data = await response.json();
        if (generation !== sequence) return;
        if (version === inputVersion) input.value = data.email || "";
        hint.textContent = available ? displayStatus(data) + (data.enabled && task.kind === "batch" ? " 批量任务将按每本书分别发送通知。" : "") : "邮件服务尚未启用，可回到本页下载；已有通知可清空邮箱后保存取消。";
      } catch (_) { if (generation === sequence) hint.textContent = "暂时无法读取邮箱设置；任务仍可在本页查看。"; }
    }
    const capability = request(api + "/api/v2/email-capabilities", { cache: "no-store" })
      .then(response => response.ok ? response.json() : {})
      .then(data => { available = data.available === true; button.disabled = false; hint.textContent = available ? "可先填写邮箱，创建任务后自动保存；已有任务请点击保存。" : "邮件服务尚未启用，请用同一浏览器回来查看和下载；已有通知可清空邮箱后保存取消。"; })
      .catch(() => { button.disabled = false; hint.textContent = "暂时无法确认邮件服务，请用同一浏览器回来查看和下载；已有通知可清空邮箱后保存取消。"; });
    button.disabled = true;
    button.addEventListener("click", save);
    render();
    return { remember, setTask, render, save };
  }
  const api = { safeStorage, validTask, taskLink, importFragment, endpoint, mount };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FixEpubReturnLater = api;
})(typeof window !== "undefined" ? window : globalThis);
