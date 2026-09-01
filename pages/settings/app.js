import {
  addSourceUi,
  createSourceUi,
  markSourcesSaved,
  moveSourceUi,
  removeSourceUi,
} from "./source-ui-state.mjs";

const bridge = window.AstrBotPluginPage;
const state = {
  schema: null,
  baseline: null,
  draft: null,
  pendingImport: null,
  controls: new Map(),
  saving: false,
  sourceUi: null,
  pendingDeleteId: null,
};

const $ = (selector) => document.querySelector(selector);
const clone = (value) => JSON.parse(JSON.stringify(value));
const elements = {
  sources: $("#sources"),
  dirtyBadge: $("#dirtyBadge"),
  saveState: $("#saveState"),
  message: $("#globalMessage"),
  status: $("#statusGrid"),
  dialog: $("#importDialog"),
  importChanges: $("#importChanges"),
  importFile: $("#importFile"),
  errorSummary: $("#errorSummary"),
  deleteDialog: $("#deleteSourceDialog"),
  deleteSourceName: $("#deleteSourceName"),
};

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function isDirty() {
  return state.baseline && state.draft && JSON.stringify(state.baseline) !== JSON.stringify(state.draft);
}

function updateDirtyUI() {
  const dirty = isDirty();
  elements.dirtyBadge.hidden = !dirty;
  elements.saveState.textContent = dirty ? "更改尚未保存" : "配置已同步";
}

function changed(message = "") {
  elements.message.textContent = message;
  updateDirtyUI();
}

function fieldError(path, control) {
  const error = node("p", "error-text");
  error.id = `error-${path.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
  control.setAttribute("aria-describedby", error.id);
  state.controls.set(path, { control, error });
  return error;
}

function clearErrors() {
  for (const { control, error } of state.controls.values()) {
    control.removeAttribute("aria-invalid");
    error.textContent = "";
  }
  elements.errorSummary.hidden = true;
  elements.errorSummary.textContent = "";
}

function showErrors(errors = {}) {
  clearErrors();
  const entries = Object.entries(errors);
  if (!entries.length) return;
  elements.errorSummary.hidden = false;
  elements.errorSummary.textContent = `配置校验失败，共 ${entries.length} 项。请检查标出的字段。`;
  let first = null;
  for (const [path, message] of entries) {
    const target = state.controls.get(path) || state.controls.get(path.replace(/\[\d+\]$/, ""));
    if (!target) continue;
    const sourceMatch = path.match(/^sources\[(\d+)\]/);
    if (sourceMatch) {
      const sourceId = state.sourceUi?.ids[Number(sourceMatch[1])];
      if (sourceId) {
        state.sourceUi.openIds.add(sourceId);
        const card = document.querySelector(`[data-source-id="${sourceId}"]`);
        if (card) card.open = true;
      }
    }
    target.control.setAttribute("aria-invalid", "true");
    target.error.textContent = message;
    first ||= target.control;
  }
  (first || elements.errorSummary).focus();
  (first || elements.errorSummary).scrollIntoView({ block: "center", behavior: "smooth" });
}

function getErrorPayload(error) {
  return error?.errors || error?.data?.errors || error?.response?.errors || {};
}

function createTopField(key, rule) {
  const wrapper = node("div", `field${key === "catgirl_suffix" ? " full" : ""}`);
  const label = node("label", "field-label", rule.description || key);
  let control;
  if (rule.type === "bool") {
    label.className = "switch";
    control = document.createElement("input");
    control.type = "checkbox";
    control.checked = state.draft[key];
    label.prepend(control);
  } else {
    control = document.createElement("input");
    control.type = rule.type === "int" ? "number" : "text";
    control.value = state.draft[key];
    if (rule.minimum !== undefined) control.min = rule.minimum;
    if (rule.maximum !== undefined) control.max = rule.maximum;
    label.htmlFor = `field-${key}`;
    control.id = `field-${key}`;
  }
  control.dataset.path = key;
  control.addEventListener("input", () => {
    state.draft[key] = rule.type === "bool" ? control.checked : rule.type === "int" ? Number(control.value) : control.value;
    changed();
  });
  if (rule.type === "bool") wrapper.append(label);
  else wrapper.append(label, control);
  if (rule.hint) wrapper.append(node("p", "field-hint", rule.hint));
  wrapper.append(fieldError(key, control));
  return wrapper;
}

function renderTopFields() {
  state.controls.clear();
  const groups = {
    sendingFields: ["batch_force_forward", "batch_forward_threshold", "batch_max_count", "send_retries", "cooldown"],
    imageFields: ["compress_enable", "compress_threshold", "compress_quality", "verify_ssl"],
    textFields: ["catgirl_enable", "catgirl_suffix"],
  };
  for (const [containerId, keys] of Object.entries(groups)) {
    const container = document.getElementById(containerId);
    container.replaceChildren(...keys.map((key) => createTopField(key, state.schema[key])));
  }
}

function listEditor(sourceIndex, key, title, { api = false } = {}) {
  const wrapper = node("div", "list-editor");
  const heading = node("div", "section-heading");
  heading.append(node("span", "list-title", title));
  const add = node("button", "button small quiet", "添加");
  add.type = "button";
  add.addEventListener("click", () => {
    state.draft.sources[sourceIndex][key].push("");
    renderSources();
    changed();
    const path = `${key === "apis" ? "sources" : "sources"}[${sourceIndex}].${key}[${state.draft.sources[sourceIndex][key].length - 1}]`;
    state.controls.get(path)?.control.focus();
  });
  heading.append(add);
  wrapper.append(heading);
  const rows = node("div", "list-rows");
  const items = state.draft.sources[sourceIndex][key];
  if (!items.length) rows.append(node("p", "empty-note", "暂无项目，点击“添加”创建。"));
  items.forEach((value, itemIndex) => {
    const path = `sources[${sourceIndex}].${key}[${itemIndex}]`;
    const row = node("div", api ? "list-row api-row" : "list-row");
    const input = document.createElement("input");
    input.type = "text";
    input.value = value;
    input.placeholder = api ? "https://api.example.com/image" : "请输入内容";
    input.setAttribute("aria-label", `${title} ${itemIndex + 1}`);
    input.addEventListener("input", () => {
      state.draft.sources[sourceIndex][key][itemIndex] = input.value;
      changed();
    });
    row.append(input);
    let diagnostic = null;
    if (api) {
      const test = node("button", "button small test-api", "检测");
      test.type = "button";
      diagnostic = node("p", "diagnostic");
      diagnostic.setAttribute("role", "status");
      diagnostic.setAttribute("aria-live", "polite");
      test.addEventListener("click", async () => {
        test.disabled = true;
        diagnostic.className = "diagnostic";
        diagnostic.textContent = "正在检测当前地址…";
        try {
          const result = await bridge.apiPost("api/test", { url: input.value });
          diagnostic.classList.add(result.ok ? "success" : "failure");
          diagnostic.textContent = result.ok
            ? `可用 · HTTP ${result.status} · ${result.content_type || "未知类型"} · ${result.elapsed_ms} ms`
            : `${result.error || "检测失败"}${result.status ? ` · HTTP ${result.status}` : ""} · ${result.elapsed_ms} ms`;
        } catch (error) {
          diagnostic.classList.add("failure");
          diagnostic.textContent = error?.message || "检测请求失败";
        } finally {
          test.disabled = false;
        }
      });
      row.append(test);
    }
    const remove = node("button", "button small danger delete-row", "删除");
    remove.type = "button";
    remove.addEventListener("click", () => {
      state.draft.sources[sourceIndex][key].splice(itemIndex, 1);
      renderSources();
      changed();
    });
    row.append(remove);
    if (diagnostic) row.append(diagnostic);
    row.append(fieldError(path, input));
    rows.append(row);
  });
  wrapper.append(rows);
  const aggregatePath = `sources[${sourceIndex}].${key}`;
  const aggregate = node("p", "error-text");
  state.controls.set(aggregatePath, { control: add, error: aggregate });
  wrapper.append(aggregate);
  return wrapper;
}

function sourceField(sourceIndex, key, rule) {
  const path = `sources[${sourceIndex}].${key}`;
  const wrapper = node("div", "field");
  let control;
  const label = node("label", rule.type === "bool" ? "switch" : "field-label", rule.description || key);
  if (rule.type === "bool") {
    control = document.createElement("input");
    control.type = "checkbox";
    control.checked = state.draft.sources[sourceIndex][key];
    label.prepend(control);
  } else if (rule.options) {
    control = document.createElement("select");
    for (const value of rule.options) {
      const option = node("option", "", value);
      option.value = value;
      control.append(option);
    }
    control.value = state.draft.sources[sourceIndex][key];
  } else {
    control = document.createElement("input");
    control.type = rule.type === "int" ? "number" : "text";
    control.value = state.draft.sources[sourceIndex][key];
    if (rule.minimum !== undefined) control.min = rule.minimum;
    if (rule.maximum !== undefined) control.max = rule.maximum;
  }
  if (rule.type !== "bool") label.htmlFor = `source-${sourceIndex}-${key}`;
  control.id = `source-${sourceIndex}-${key}`;
  control.addEventListener("input", () => {
    state.draft.sources[sourceIndex][key] = rule.type === "bool" ? control.checked : rule.type === "int" ? Number(control.value) : control.value;
    changed();
    if (key === "name") renderSourceSummary(sourceIndex);
  });
  if (rule.type === "bool") wrapper.append(label);
  else wrapper.append(label, control);
  if (rule.hint) wrapper.append(node("p", "field-hint", rule.hint));
  wrapper.append(fieldError(path, control));
  return wrapper;
}

function renderSourceSummary(index) {
  const summary = document.querySelector(`[data-source-summary="${index}"]`);
  if (summary) summary.textContent = `${index + 1}. ${state.draft.sources[index].name || "未命名图源"}`;
}

function renderSources() {
  for (const key of [...state.controls.keys()]) if (key.startsWith("sources")) state.controls.delete(key);
  const sourceRules = state.schema.sources.templates.default_source.items;
  const cards = state.draft.sources.map((source, index) => {
    const sourceId = state.sourceUi.ids[index];
    const details = node("details", "source-card");
    details.dataset.sourceId = sourceId;
    details.open = state.sourceUi.openIds.has(sourceId);
    details.addEventListener("toggle", () => {
      if (details.open) state.sourceUi.openIds.add(sourceId);
      else state.sourceUi.openIds.delete(sourceId);
    });
    const summary = document.createElement("summary");
    const summaryText = node("span", "source-summary", `${index + 1}. ${source.name || "未命名图源"}`);
    summaryText.dataset.sourceSummary = index;
    summary.append(summaryText);
    const body = node("div", "source-body");
    const fields = node("div", "source-fields");
    fields.append(
      sourceField(index, "name", sourceRules.name),
      sourceField(index, "use_forward", sourceRules.use_forward),
      sourceField(index, "list_mode", sourceRules.list_mode),
      sourceField(index, "recall_delay", sourceRules.recall_delay),
    );
    body.append(fields);
    body.append(listEditor(index, "keywords", "触发词"));
    body.append(listEditor(index, "apis", "API 地址", { api: true }));
    body.append(listEditor(index, "group_list", "群号名单"));
    const actions = node("div", "source-actions");
    const up = node("button", "button small quiet", "上移");
    const down = node("button", "button small quiet", "下移");
    const remove = node("button", "button small danger", "删除图源");
    up.type = down.type = remove.type = "button";
    up.disabled = index === 0;
    down.disabled = index === state.draft.sources.length - 1;
    up.addEventListener("click", () => moveSource(index, -1));
    down.addEventListener("click", () => moveSource(index, 1));
    remove.addEventListener("click", () => {
      if (state.draft.sources.length === 1) {
        changed("至少保留一个图源。");
        return;
      }
      if (state.sourceUi.newIds.has(sourceId)) {
        deleteSource(sourceId);
        return;
      }
      state.pendingDeleteId = sourceId;
      elements.deleteSourceName.textContent = source.name || "未命名图源";
      elements.deleteDialog.showModal();
      $("#confirmDeleteSource").focus();
    });
    actions.append(up, down, remove);
    body.append(actions);
    details.append(summary, body);
    return details;
  });
  elements.sources.replaceChildren(...cards);
}

function moveSource(index, offset) {
  const [source] = state.draft.sources.splice(index, 1);
  state.draft.sources.splice(index + offset, 0, source);
  moveSourceUi(state.sourceUi, index, offset);
  renderSources();
  changed();
}

function deleteSource(sourceId) {
  const index = state.sourceUi.ids.indexOf(sourceId);
  if (index < 0 || state.draft.sources.length === 1) return;
  state.draft.sources.splice(index, 1);
  removeSourceUi(state.sourceUi, index);
  state.pendingDeleteId = null;
  renderSources();
  changed();
}

function newSource() {
  const items = state.schema.sources.templates.default_source.items;
  const source = { __template_key: "default_source" };
  for (const [key, rule] of Object.entries(items)) source[key] = clone(rule.default);
  if (!source.apis.length) source.apis.push("");
  return source;
}

function describeChange(change) {
  if (change.kind === "source_added") return `新增图源：${change.name}`;
  if (change.kind === "source_removed") return `移除图源：${change.name}`;
  if (change.kind === "sources_reordered") return "调整图源顺序";
  return `修改 ${change.path}`;
}

async function refreshStatus() {
  elements.status.setAttribute("aria-busy", "true");
  try {
    const status = await bridge.apiGet("status");
    const values = [
      ["插件版本", status.version],
      ["图源数量", status.source_count],
      ["冷却记录", status.cooldown_count],
      ["网络会话", status.session],
      ["最近保存", status.last_saved_at ? new Date(status.last_saved_at).toLocaleString() : "尚未保存"],
    ];
    elements.status.replaceChildren(...values.map(([label, value]) => {
      const item = node("div", "status-item");
      item.append(node("dt", "", label), node("dd", "", String(value)));
      return item;
    }));
  } catch (error) {
    elements.status.replaceChildren(node("p", "error-text", error?.message || "状态加载失败"));
  } finally {
    elements.status.removeAttribute("aria-busy");
  }
}

async function saveDraft() {
  if (state.saving || !state.draft) return;
  state.saving = true;
  clearErrors();
  document.querySelectorAll("#saveTop, #saveBottom").forEach((button) => { button.disabled = true; });
  changed("正在校验并保存…");
  try {
    const result = await bridge.apiPost("config/save", clone(state.draft));
    if (result.saved === false) {
      showErrors(result.errors);
      changed(result.message || "请修正标出的配置项。");
      return;
    }
    const savedConfig = clone(result.config || state.draft);
    state.baseline = clone(savedConfig);
    state.draft = savedConfig;
    markSourcesSaved(state.sourceUi);
    renderTopFields();
    renderSources();
    changed(`保存成功，共 ${result.changes?.length || 0} 项变更。`);
    await refreshStatus();
  } catch (error) {
    showErrors(getErrorPayload(error));
    changed(error?.message || "保存失败，请检查配置。" );
  } finally {
    state.saving = false;
    document.querySelectorAll("#saveTop, #saveBottom").forEach((button) => { button.disabled = false; });
    updateDirtyUI();
  }
}

async function previewImport(file) {
  if (!file) return;
  changed("正在读取导入文件…");
  try {
    const result = await bridge.upload("config/import", file);
    if (result.saved === false) {
      showErrors(result.errors);
      changed(result.message || "导入配置存在字段错误。");
      return;
    }
    state.pendingImport = result.config;
    const changes = result.changes || [];
    elements.importChanges.replaceChildren(...(changes.length
      ? changes.map((change) => node("li", "", describeChange(change)))
      : [node("li", "", "导入内容与当前配置一致。")]
    ));
    elements.dialog.showModal();
    changed();
  } catch (error) {
    changed(error?.message || "导入预览失败。" );
  } finally {
    elements.importFile.value = "";
  }
}

async function confirmImport() {
  if (!state.pendingImport) return;
  $("#confirmImport").disabled = true;
  try {
    const result = await bridge.apiPost("config/import", { config: state.pendingImport, confirm: true });
    if (result.saved === false) {
      showErrors(result.errors);
      changed(result.message || "导入配置存在字段错误。");
      return;
    }
    const savedConfig = clone(result.config || state.pendingImport);
    state.baseline = clone(savedConfig);
    state.draft = savedConfig;
    state.sourceUi = createSourceUi(state.draft.sources);
    markSourcesSaved(state.sourceUi);
    state.pendingImport = null;
    renderTopFields();
    renderSources();
    elements.dialog.close();
    changed(`导入成功，共 ${result.changes?.length || 0} 项变更。`);
    await refreshStatus();
  } catch (error) {
    changed(error?.message || "导入应用失败。" );
  } finally {
    $("#confirmImport").disabled = false;
  }
}

async function initialize() {
  if (!bridge) {
    changed("Page bridge 尚未加载，请从 AstrBot 插件详情页打开本页面。" );
    return;
  }
  try {
    await bridge.ready();
    const data = await bridge.apiGet("config");
    state.schema = data.schema;
    state.baseline = clone(data.config);
    state.draft = clone(data.config);
    state.sourceUi = createSourceUi(state.draft.sources);
    renderTopFields();
    renderSources();
    updateDirtyUI();
    await refreshStatus();
  } catch (error) {
    changed(error?.message || "配置加载失败。" );
  }
}

$("#addSource").addEventListener("click", () => {
  if (!state.draft || !state.schema) return;
  state.draft.sources.push(newSource());
  addSourceUi(state.sourceUi);
  renderSources();
  changed();
  state.controls.get(`sources[${state.draft.sources.length - 1}].name`)?.control.focus();
});
$("#refreshStatus").addEventListener("click", refreshStatus);
$("#saveTop").addEventListener("click", saveDraft);
$("#saveBottom").addEventListener("click", saveDraft);
$("#exportConfig").addEventListener("click", async () => {
  try {
    await bridge.download("config/export", {}, "endworld-img-config.json");
    changed("配置已导出。" );
  } catch (error) {
    changed(error?.message || "导出失败。" );
  }
});
elements.importFile.addEventListener("change", () => previewImport(elements.importFile.files[0]));
$("#confirmImport").addEventListener("click", confirmImport);
$("#confirmDeleteSource").addEventListener("click", () => {
  if (state.pendingDeleteId) deleteSource(state.pendingDeleteId);
  elements.deleteDialog.close();
});
elements.deleteDialog.addEventListener("close", () => {
  state.pendingDeleteId = null;
});
window.addEventListener("beforeunload", (event) => {
  if (!isDirty()) return;
  event.preventDefault();
  event.returnValue = "";
});

initialize();
