/* SEO pages only navigate. Upload, payment, recovery and delivery live on index.html. */
(function (root) {
  "use strict";
  const TOOLS = Object.freeze(["translate", "horizontal", "simplified"]);

  function readIntent(search) {
    const params = new URLSearchParams(search);
    const values = params.getAll("tool");
    return {
      tool: values.length === 1 && TOOLS.includes(values[0]) ? values[0] : null,
      tasks: params.getAll("view").length === 1 && params.get("view") === "tasks",
      restoring: params.has("job_id") || params.has("batch_id"),
    };
  }

  function buildTarget(tool, location, tasks) {
    // Never accept a return URL/origin from query parameters. Keep credentials in
    // the fragment and let the existing homepage importer consume them.
    const source = new URL(location.href);
    const target = new URL(source.protocol === "file:" ? "index.html" : "./", source);
    for (const key of ["job_id", "batch_id", "admin_key"]) {
      for (const value of source.searchParams.getAll(key)) target.searchParams.append(key, value);
    }
    const restoring = readIntent(source.search).restoring;
    if (!restoring) {
      if (tasks) target.searchParams.set("view", "tasks");
      else if (TOOLS.includes(tool)) target.searchParams.set("tool", tool);
    }
    target.hash = source.hash;
    return target.href;
  }

  function mountLanding(host) {
    const doc = host.document;
    const tool = doc.body && doc.body.dataset.toolEntry;
    if (!TOOLS.includes(tool)) return;
    doc.querySelectorAll("[data-entry-link]").forEach(link => {
      link.href = buildTarget(tool, host.location, link.dataset.entryLink === "tasks");
    });
    if (readIntent(host.location.search).restoring) {
      host.location.replace(buildTarget(tool, host.location, false));
    }
  }

  function hasRestoredInputs(form) {
    if (!form) return false;
    return Array.from(form.querySelectorAll("input, select, textarea")).some(input => {
      if (input.type === "file") return !!(input.files && input.files.length);
      if (["checkbox", "radio"].includes(input.type)) return input.checked !== input.defaultChecked;
      if (input.tagName === "SELECT") {
        const options = Array.from(input.options);
        const defaults = options.filter(option => option.defaultSelected);
        const expected = defaults.length ? defaults : options.slice(0, 1);
        return options.some(option => option.selected !== expected.includes(option));
      }
      return input.value !== input.defaultValue;
    });
  }

  function capture(options) {
    const { location, history, form } = options;
    // Capture before normal UI initialization mutates hidden fields/radios.
    const intent = readIntent(location.search);
    let dirty = hasRestoredInputs(form), consumed = false;
    const markDirty = () => { dirty = true; };
    if (form) {
      form.addEventListener("input", markDirty, true);
      form.addEventListener("change", markDirty, true);
    }
    const params = new URLSearchParams(location.search);
    if (params.has("tool") || intent.tasks) {
      params.delete("tool");
      if (intent.tasks) params.delete("view");
      const query = params.toString();
      try { history.replaceState(null, "", location.pathname + (query ? "?" + query : "") + location.hash); }
      catch (_) { /* Restricted history must not prevent upload/task recovery. */ }
    }
    return {
      apply(state) {
        if (consumed) return false;
        consumed = true;
        if (form) {
          form.removeEventListener("input", markDirty, true);
          form.removeEventListener("change", markDirty, true);
        }
        if (intent.restoring || state.hasActiveTask || state.hasFiles || dirty) return false;
        if (intent.tasks) { state.showTasks(); return true; }
        if (!intent.tool) return false;
        if (intent.tool === "simplified") state.setConversion("simplified|auto");
        state.setMode(intent.tool === "translate" ? "translate" : "convert");
        return true;
      },
    };
  }

  const api = { readIntent, buildTarget, mountLanding, hasRestoredInputs, capture };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else {
    root.FixEpubToolEntry = api;
    if (root.document.readyState === "loading") {
      root.document.addEventListener("DOMContentLoaded", () => mountLanding(root), { once: true });
    } else mountLanding(root);
  }
})(typeof window !== "undefined" ? window : globalThis);
