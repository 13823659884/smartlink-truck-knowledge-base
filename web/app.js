const series = ["J7", "J6", "J6P", "J6L", "JH6", "JK6", "J6F", "J5", "虎6G", "领途", "鹰途", "鹰阵", "V卡"];
const fallbackIntents = [
  { id: "all", name: "智能问答", scene: "", description: "自动识别问题意图", example_question: "刹车不灵敏怎么排查？" },
  { id: "vin", name: "VIN查询", scene: "", description: "车辆静态字段", example_question: "请输入17位VIN" },
  { id: "fault", name: "故障码查询", scene: "修", description: "P码、SPN+FMI与诊断", example_question: "例如：P312700" },
  { id: "symptom", name: "症状查询", scene: "修", description: "故障现象与连续追问", example_question: "例如：动力不足" },
  { id: "usage", name: "用车知识", scene: "用", description: "车辆操作与使用", example_question: "驻车再生怎么操作？" },
  { id: "maintenance", name: "保养知识", scene: "养", description: "周期、油液和部件维护", example_question: "变速箱多久保养？" },
  { id: "warranty", name: "保用知识", scene: "保", description: "保用标准和适用条件", example_question: "制动部件是否在保？" },
];

const answerModePolicy = "fast-default-20260805";
if (localStorage.getItem("answerModePolicy") !== answerModePolicy) {
  localStorage.setItem("answerMode", "fast");
  localStorage.setItem("answerModePolicy", answerModePolicy);
}
const state = {
  conversationId: null,
  last: null,
  answerMode: localStorage.getItem("answerMode") === "deep" ? "deep" : "fast",
  intent: "all",
  intents: fallbackIntents,
};
const batchState = {
  rows: [],
  answers: new Map(),
  exportRows: [],
  running: false,
  stopRequested: false,
  mode: "fast",
};
const batchCategories = [
  ["fault_code", "故障码查询"], ["symptom_diagnosis", "故障诊断"],
  ["usage", "用车与操作"], ["maintenance", "保养知识"],
  ["warranty", "保用保修"], ["service_technical", "服务咨询"],
  ["drawing", "图纸电路"], ["vin", "VIN查询"], ["general", "通用知识"],
];
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char]);

async function getJson(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}

async function getBlob(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const text = await response.text();
    try { throw new Error(JSON.parse(text).error || `请求失败（${response.status}）`); }
    catch (error) { if (error instanceof SyntaxError) throw new Error(`请求失败（${response.status}）`); throw error; }
  }
  return response.blob();
}

async function getStream(url, options, handlers = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const text = await response.text();
    try { throw new Error(JSON.parse(text).error || `请求失败（${response.status}）`); }
    catch (error) { if (error instanceof SyntaxError) throw new Error(`请求失败（${response.status}）`); throw error; }
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result = null;
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      const event = JSON.parse(line);
      if (event.type === "status") handlers.onStatus?.(event);
      if (event.type === "meta") handlers.onMeta?.(event);
      if (event.type === "delta") handlers.onDelta?.(event);
      if (event.type === "mode_fallback") handlers.onFallback?.(event);
      if (event.type === "error") throw new Error(event.error || "流式回答失败");
      if (event.type === "done") result = event.data;
    }
    if (done) break;
  }
  if (!result) throw new Error("流式响应未完成");
  return result;
}

function applyAnswerMode(mode) {
  if (!["fast", "deep"].includes(mode)) return;
  state.answerMode = mode;
  localStorage.setItem("answerMode", mode);
  document.querySelectorAll(".desktop-mode-option").forEach((item) => item.classList.toggle("active", item.dataset.mode === mode));
}

function selectIntent(id) {
  const intent = state.intents.find((item) => item.id === id) || state.intents[0];
  state.intent = intent.id;
  document.querySelectorAll(".capability-item").forEach((item) => item.classList.toggle("active", item.dataset.intent === intent.id));
  $("activeIntentLabel").textContent = intent.name;
  $("composerIntent").textContent = intent.name;
  $("question").placeholder = intent.example_question || "请输入车辆问题";
  if (intent.scene) $("scene").value = intent.scene;
  else if (["all", "vin"].includes(intent.id)) $("scene").value = "";
}

function renderCapabilities() {
  $("capabilityMenu").innerHTML = state.intents.map((item) => `
    <button type="button" class="capability-item ${item.id === state.intent ? "active" : ""}" data-intent="${esc(item.id)}">
      <span class="capability-icon">${esc(item.name.slice(0, 1))}</span>
      <span><b>${esc(item.name)}</b><small>${esc(item.description)}</small></span>
    </button>`).join("");
  document.querySelectorAll(".capability-item").forEach((button) => button.addEventListener("click", () => selectIntent(button.dataset.intent)));
  selectIntent(state.intent);
}

async function loadCapabilities() {
  try {
    const data = await getJson("/api/intents");
    if (data.intents?.length) state.intents = data.intents;
  } catch (error) {}
  renderCapabilities();
}

async function loadStats() {
  try {
    const [data, agent] = await Promise.all([getJson("/api/stats"), getJson("/api/agent/config")]);
    const provider = agent.provider_name || agent.provider;
    $("health").classList.add("online");
    $("health").innerHTML = `<i></i>${agent.configured ? `知识库在线 · ${esc(provider)}` : "知识库在线 · 智能体待配置"}`;
    $("stats").innerHTML = [["文档", data.documents], ["知识切片", data.chunks], ["实体", data.entities], ["知识关系", data.triples]].map(([key, value]) => `<div>${key}<b>${Number(value).toLocaleString()}</b></div>`).join("");
  } catch (error) {
    $("health").classList.remove("online");
    $("health").innerHTML = "<i></i>知识库连接失败";
  }
}

function clearEmpty() { document.querySelector(".empty")?.remove(); }
function appendMessageNode(node) { clearEmpty(); $("conversation").append(node); $("conversation").scrollTop = $("conversation").scrollHeight; }

function addUserMessage(text) {
  const node = document.createElement("div");
  node.className = "message user";
  node.textContent = text;
  appendMessageNode(node);
}

function focusSource(index) {
  document.querySelector('.tabs button[data-tab="sources"]')?.click();
  requestAnimationFrame(() => {
    const target = $(`source-ref-${index}`);
    if (!target) return;
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    target.classList.remove("citation-target");
    void target.offsetWidth;
    target.classList.add("citation-target");
    setTimeout(() => target.classList.remove("citation-target"), 1800);
  });
}

function appendRichText(container, value) {
  const text = String(value || "");
  const pattern = /(\*\*[^*]+\*\*|[【\[]资料\s*\d+[】\]])/g;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > offset) container.append(document.createTextNode(text.slice(offset, match.index)));
    const token = match[0];
    const citation = token.match(/[【\[]资料\s*(\d+)[】\]]/);
    if (citation) {
      const index = Number(citation[1]);
      const link = document.createElement("a");
      link.className = "citation-link";
      link.href = `#source-ref-${index}`;
      link.textContent = `【资料${index}】`;
      link.addEventListener("click", (event) => { event.preventDefault(); focusSource(index); });
      container.append(link);
    } else {
      const strong = document.createElement("strong");
      strong.textContent = token.slice(2, -2);
      container.append(strong);
    }
    offset = match.index + token.length;
  }
  if (offset < text.length) container.append(document.createTextNode(text.slice(offset)));
}

function addSection(container, number, title, value, kind = "main") {
  if (!value || (Array.isArray(value) && !value.length)) return;
  const section = document.createElement("section");
  section.className = `answer-section answer-${kind}`;
  section.innerHTML = `<div class="answer-heading"><span>${String(number).padStart(2, "0")}</span><b>${esc(title)}</b></div>`;
  if (Array.isArray(value)) {
    const list = document.createElement("ol");
    value.forEach((item) => { const li = document.createElement("li"); appendRichText(li, String(item).replace(/^\s*(?:\d+[.、]|[•-])\s*/, "")); list.append(li); });
    section.append(list);
  } else {
    String(value).split(/\n+/).map((line) => line.trim()).filter(Boolean).forEach((line) => { const p = document.createElement("p"); appendRichText(p, line); section.append(p); });
  }
  container.append(section);
}

function renderDiagnosis(container, diagnosis) {
  if (!diagnosis?.enabled) return;
  const section = document.createElement("section");
  section.className = `answer-section answer-related diagnosis-card ${diagnosis.safety_level === "高" ? "high-risk" : ""}`;
  section.innerHTML = `<div class="answer-heading"><span>诊</span><b>${esc(diagnosis.title || "故障诊断")}</b></div><p>证据：${esc(diagnosis.evidence_status)} · 安全等级：${esc(diagnosis.safety_level)}</p>`;
  const list = document.createElement("ol");
  (diagnosis.checklist || []).forEach((item) => { const li = document.createElement("li"); li.textContent = item; list.append(li); });
  section.append(list);
  if (diagnosis.pending_question) {
    const prompt = document.createElement("p"); prompt.innerHTML = `<strong>下一步请确认：</strong>${esc(diagnosis.pending_question)}`; section.append(prompt);
    const actions = document.createElement("div"); actions.className = "diagnosis-options";
    (diagnosis.reply_options || []).forEach((answer) => { const button = document.createElement("button"); button.type = "button"; button.textContent = answer; button.addEventListener("click", () => ask(answer)); actions.append(button); });
    section.append(actions);
  }
  container.append(section);
}

function addAssistantMessage(data, question) {
  const node = document.createElement("div");
  node.className = "message assistant";
  const content = document.createElement("div");
  content.className = "message-text";
  content.innerHTML = `<div class="assistant-answer-header"><strong><i>AI</i> 智能诊断助手</strong><span>${(data.sources || []).length} 条参考资料</span></div>`;
  addSection(content, 1, "分析与结论", data.answer, "main");
  addSection(content, 2, "处理步骤", data.solution_steps || [], "steps");
  renderDiagnosis(content, data.diagnosis);
  addSection(content, 3, "相关问题", data.related_questions || [], "related");
  addSection(content, 4, "安全提示", data.safety_notice, "safety");
  node.append(content);
  const feedback = document.createElement("div");
  feedback.className = "feedback";
  feedback.innerHTML = '<button data-rating="up">👍 有帮助</button><button data-rating="down">👎 需纠偏</button>';
  feedback.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => saveFeedback(button.dataset.rating, data, question, feedback)));
  node.append(feedback);
  appendMessageNode(node);
}

function addStreamingMessage() {
  const node = document.createElement("div"); node.className = "message assistant streaming";
  const content = document.createElement("div"); content.className = "message-text"; content.textContent = "正在理解问题并检索企业知识库"; node.append(content);
  appendMessageNode(node); return { node, content };
}

function createStreamRenderer(live) {
  let received = ""; let stopped = false; let frame = 0;
  const paint = () => {
    frame = 0;
    if (stopped) return;
    live.content.textContent = received;
    $("conversation").scrollTop = $("conversation").scrollHeight;
  };
  const schedulePaint = () => {
    if (!frame) frame = requestAnimationFrame(paint);
  };
  return {
    status(text) {
      if (!received) {
        live.content.textContent = text;
        $("conversation").scrollTop = $("conversation").scrollHeight;
      }
    },
    push(text) {
      if (!text || stopped) return;
      received += text;
      schedulePaint();
    },
    async finish() {
      if (frame) cancelAnimationFrame(frame);
      live.content.textContent = received;
      $("conversation").scrollTop = $("conversation").scrollHeight;
      stopped = true;
    },
    stop() {
      stopped = true;
      if (frame) cancelAnimationFrame(frame);
    },
  };
}

function renderEvidence(data) {
  const sources = data.sources || [];
  $("sources").innerHTML = sources.map((item, index) => `<article class="source" id="source-ref-${index + 1}"><div class="source-title"><span>资料${index + 1}</span><b>${esc(item.file_name)}</b></div><small>${esc(item.source_locator)} · ${esc(item.scene || "")}</small><p>${esc(String(item.excerpt || "").slice(0, 360))}</p><div class="source-actions"><a href="${esc(item.document_url || item.file_url)}" target="_blank">定位原文</a></div></article>`).join("") || "<p>暂无引用来源</p>";
  $("triples").innerHTML = (data.triples || []).map((item) => `<article class="triple">${esc(item.subject)}<br><em>— ${esc(item.predicate_name || item.predicate)} →</em><br>${esc(item.object)}</article>`).join("") || "<p>暂无关联三元组</p>";
  const documents = (data.related_documents || []).map((item) => `<a class="document-card" href="${esc(item.url)}" target="_blank"><b>${esc(item.file_name)}</b><small>${esc(item.source_locator || "打开原文")}</small></a>`).join("");
  $("attachments").innerHTML = documents ? `<h3>相关文档</h3>${documents}` : "<p>暂无相关文档</p>";
}

function addVinResult(data) {
  const node = document.createElement("div"); node.className = "message assistant";
  const labels = { vin: "VIN", vehicle_type: "车辆类型", chassis_no: "底盘号", emission_type: "排放种类", vehicle_series: "车系", fuel_type: "燃料种类", announcement_model: "公告型号", factory_model_code: "车厂车型码", rear_axle: "后桥", tire_spec: "轮胎规格", engine_type: "发动机类型", engine_model: "发动机型号", transmission_model: "变速箱型号", offline_time: "下线时间", vehicle_note: "车型备注", engine_name: "发动机名称", device_app_version: "设备应用版本", mcu_version: "MCU版本", sim_match: "SIM匹配型号" };
  const fields = data.record ? Object.entries(labels).map(([key, label]) => `<div class="vin-field"><small>${label}</small><b>${esc(key === "vin" ? data.vin : data.record[key] || "—")}</b></div>`).join("") : "";
  node.innerHTML = `<div class="message-text"><div class="assistant-answer-header"><strong><i>VIN</i> 车辆静态信息</strong><span>${esc(data.vin)}</span></div>${data.found ? `<div class="vin-result">${fields}</div>` : `<div class="vin-empty">${esc(data.message)}<br>VIN查询接口已经就绪，导入车辆主数据后即可显示完整车辆静态字段。</div>`}</div>`;
  appendMessageNode(node);
}

async function ask(question) {
  if (!question) return;
  addUserMessage(question);
  if (state.intent === "vin") {
    try { addVinResult(await getJson(`/api/vin?q=${encodeURIComponent(question)}`)); }
    catch (error) { addVinResult({ vin: question, found: false, message: error.message }); }
    return;
  }
  const live = addStreamingMessage(); const renderer = createStreamRenderer(live); const submit = document.querySelector('#askForm button[type="submit"]'); submit.disabled = true;
  try {
    const data = await getStream("/api/search/stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question, intent: state.intent, conversation_id: state.conversationId, vehicle_id: $("vehicleId").value, vehicle_series: $("vehicleSeries").value, scene: $("scene").value, energy_type: $("energyType").value, use_agent: true, answer_mode: state.answerMode, include_images: false }) }, { onStatus: (event) => renderer.status(event.text), onMeta: (event) => renderer.status(event.text), onFallback: (event) => { applyAnswerMode(event.answer_mode); renderer.status(event.text); }, onDelta: (event) => renderer.push(event.text || "") });
    await renderer.finish(); applyAnswerMode(data.answer_mode || state.answerMode); state.conversationId = data.conversation_id; state.last = { question, data }; live.node.remove(); addAssistantMessage(data, question); renderEvidence(data); loadHistory();
  } catch (error) { renderer.stop(); live.node.classList.remove("streaming"); live.content.textContent = `问答失败：${error.message}`; }
  finally { renderer.stop(); submit.disabled = false; }
}

async function saveFeedback(rating, data, question, container) {
  const comment = rating === "down" ? (prompt("请填写需要纠正或补充的内容（选填）") || "") : "";
  try {
    await getJson("/api/feedback", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rating, comment, message_id: data.message_id, conversation_id: data.conversation_id, vehicle_id: $("vehicleId").value, vehicle_series: $("vehicleSeries").value, scene: $("scene").value, question, answer: data.answer }) });
    container.textContent = "评价与意见已记录";
  } catch (error) { container.textContent = error.message; }
}

async function loadHistory() {
  try {
    const data = await getJson("/api/history?limit=12");
    $("history").innerHTML = (data.history || []).map((item) => `<button class="history-card" data-conversation="${esc(item.id)}"><b>${esc(item.question || "未命名查询")}</b><p>${esc(String(item.answer || "").slice(0, 120))}</p><small>${esc(item.vehicle_series || item.scene || "智能问答")} · ${esc(String(item.updated_at || "").replace("T", " ").slice(0, 16))}</small></button>`).join("") || "<p>暂无查询记录</p>";
    document.querySelectorAll(".history-card").forEach((button) => button.addEventListener("click", () => restoreHistory(button.dataset.conversation)));
  } catch (error) { $("history").innerHTML = "<p>历史记录暂不可用</p>"; }
}

async function restoreHistory(conversationId) {
  const data = await getJson(`/api/history?conversation_id=${encodeURIComponent(conversationId)}`);
  $("conversation").innerHTML = "";
  (data.messages || []).forEach((item) => {
    if (item.role === "user") addUserMessage(item.content);
    else {
      const node = document.createElement("div"); node.className = "message assistant"; const content = document.createElement("div"); content.className = "message-text"; content.textContent = item.content; node.append(content); appendMessageNode(node);
    }
  });
  state.conversationId = conversationId;
}

function setupVoiceInput() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const button = $("voiceButton");
  if (!Recognition) { button.addEventListener("click", () => alert("当前浏览器不支持语音识别，请使用文本输入。")); return; }
  const recognition = new Recognition(); recognition.lang = "zh-CN"; recognition.interimResults = true;
  recognition.onstart = () => button.classList.add("listening"); recognition.onend = () => button.classList.remove("listening");
  recognition.onresult = (event) => { $("question").value = Array.from(event.results).map((item) => item[0].transcript).join(""); };
  button.addEventListener("click", () => recognition.start());
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || "").split(",", 2)[1] || "");
    reader.onerror = () => reject(new Error("读取Excel文件失败"));
    reader.readAsDataURL(file);
  });
}

function updateBatchSummary(statusText = "") {
  const completed = batchState.rows.filter((row) => ["success", "no_knowledge", "failed"].includes(row.status)).length;
  const failed = batchState.rows.filter((row) => row.status === "failed").length;
  const percent = batchState.rows.length ? Math.round(completed / batchState.rows.length * 100) : 0;
  $("batchTotal").textContent = batchState.rows.length;
  $("batchCompleted").textContent = completed;
  $("batchFailed").textContent = failed;
  $("batchPairCount").textContent = batchState.exportRows.filter((row) => row.status === "成功").length;
  $("batchPercent").textContent = `${percent}%`;
  $("batchProgress").value = percent;
  if (statusText) $("batchStatusText").textContent = statusText;
  $("batchStartButton").disabled = batchState.running || !batchState.rows.length;
  $("batchStopButton").disabled = !batchState.running;
  $("batchExportButton").disabled = batchState.running || !batchState.exportRows.length;
}

function batchStatusLabel(status) {
  return { pending: "待处理", running: "生成中", success: "已完成", no_knowledge: "待补充知识", failed: "失败", stopped: "已停止" }[status] || "待处理";
}

function renderBatchRows() {
  if (!batchState.rows.length) {
    $("batchTableBody").innerHTML = '<tr class="batch-empty-row"><td colspan="8">请选择包含“故障现象”或“客户询问问题”列的Excel文件</td></tr>';
    return;
  }
  $("batchTableBody").innerHTML = batchState.rows.map((row, index) => {
    const answer = batchState.answers.get(row.id);
    const pairs = answer?.pairs?.length ? answer.pairs : [{}];
    return pairs.map((pair, pairIndex) => `<tr data-batch-row="${esc(row.id)}">
      <td>${pairIndex ? "" : index + 1}</td>
      <td>${pairIndex ? "" : esc(row.vehicle_series)}</td>
      <td>${pairIndex ? "" : `<select class="batch-category-select" data-category-row="${esc(row.id)}" ${batchState.running ? "disabled" : ""}>${batchCategories.map(([value, label]) => `<option value="${value}" ${row.task_type === value ? "selected" : ""}>${label}</option>`).join("")}</select><small class="batch-category-reason">${esc(row.reason || "自动识别")}</small>`}</td>
      <td>${pairIndex ? "" : esc(row.original_question || row.symptom)}</td>
      <td>${pairIndex ? "" : esc(row.engineer_question || row.symptom)}</td>
      <td>${esc(pair.cause || (row.error ? `处理失败：${row.error}` : "—"))}</td>
      <td>${esc(pair.repair_plan || "—")}${pair.verification ? `<br><small>验证：${esc(pair.verification)}</small>` : ""}</td>
      <td>${pairIndex ? "" : `<span class="batch-status-pill ${esc(row.status || "pending")}">${batchStatusLabel(row.status)}</span>`}</td>
    </tr>`).join("");
  }).join("");
  document.querySelectorAll("[data-category-row]").forEach((select) => select.addEventListener("change", () => {
    const row = batchState.rows.find((item) => item.id === select.dataset.categoryRow);
    if (!row) return;
    row.task_type = select.value;
    row.task_label = batchCategories.find(([value]) => value === select.value)?.[1] || select.value;
    row.reason = "用户手动修正分类";
    row.automatic = false;
    renderBatchRows();
  }));
}

function openBatchModal() {
  $("batchModal").classList.remove("hidden");
  document.body.classList.add("batch-open");
}

function closeBatchModal() {
  if (batchState.running && !confirm("批量回答仍在进行，关闭窗口不会停止任务。确定关闭吗？")) return;
  $("batchModal").classList.add("hidden");
  document.body.classList.remove("batch-open");
}

async function importBatchFile(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".xlsx")) {
    alert("目前支持.xlsx格式，请将旧版.xls另存为.xlsx后导入。");
    return;
  }
  batchState.rows = [];
  batchState.answers.clear();
  batchState.exportRows = [];
  updateBatchSummary(`正在读取 ${file.name}`);
  renderBatchRows();
  try {
    const data = await getJson("/api/batch/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        file_name: file.name,
        file_base64: await fileToBase64(file),
        default_vehicle_series: $("batchDefaultSeries").value.trim() || "JH6",
      }),
    });
    batchState.rows = data.rows.map((row) => ({ ...row, status: "pending", error: "" }));
    renderBatchRows();
    const categoryCounts = new Map();
    batchState.rows.forEach((row) => categoryCounts.set(row.task_label, (categoryCounts.get(row.task_label) || 0) + 1));
    const categorySummary = [...categoryCounts.entries()].map(([label, count]) => `${label}${count}条`).join("、");
    updateBatchSummary(`已导入 ${data.count} 条：${categorySummary}${data.skipped ? `；跳过 ${data.skipped} 条空行` : ""}`);
  } catch (error) {
    updateBatchSummary(`导入失败：${error.message}`);
    alert(error.message);
  }
}

function referenceText(references) {
  return (references || []).map((item) => `资料${item.index}：${item.file_name}${item.source_locator ? `（${item.source_locator}）` : ""}`).join("\n");
}

function appendBatchExportRows(row, data) {
  const base = {
    sheet: row.sheet,
    source_row: row.source_row,
    vehicle_series: row.vehicle_series,
    original_question: row.original_question || row.symptom,
    engineer_question: row.engineer_question || row.symptom,
    task_type: row.task_type,
    task_label: data?.classification?.task_label || row.task_label,
    summary: data?.summary || "",
    safety_notice: data?.safety_notice || "",
    references: referenceText(data?.references),
    model: data?.agent?.model || "",
  };
  if (["success", "no_knowledge"].includes(row.status)) {
    (data.pairs || []).forEach((pair) => batchState.exportRows.push({
      ...base,
      cause: pair.cause || "",
      repair_plan: pair.repair_plan || "",
      verification: pair.verification || "",
      status: row.status === "no_knowledge" ? "成功（待补充知识库）" : "成功",
      error: "",
    }));
  } else {
    batchState.exportRows.push({ ...base, cause: "", repair_plan: "", verification: "", status: "失败", error: row.error || "未知错误" });
  }
}

async function requestBatchDiagnosis(payload) {
  let lastError = new Error("批量诊断请求未完成");
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      const response = await fetch("/api/batch/diagnose", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (response.ok) return data;
      const error = new Error(data.error || `批量诊断请求失败（${response.status}）`);
      error.status = response.status;
      throw error;
    } catch (error) {
      lastError = error;
      const text = String(error?.message || "").toLowerCase();
      const retryable = !error?.status || [429, 502, 503, 504].includes(error.status)
        || /timeout|timed out|network|failed to fetch|ssl|connection|频率|限流|无响应/.test(text);
      if (!retryable || attempt === 3) throw error;
      await new Promise((resolve) => setTimeout(resolve, attempt * 1500));
    }
  }
  throw lastError;
}

async function startBatchDiagnosis() {
  if (batchState.running || !batchState.rows.length) return;
  batchState.running = true;
  batchState.stopRequested = false;
  batchState.answers.clear();
  batchState.exportRows = [];
  batchState.rows.forEach((row) => { row.status = "pending"; row.error = ""; });
  renderBatchRows();
  updateBatchSummary("批量回答开始");
  for (let index = 0; index < batchState.rows.length; index += 1) {
    if (batchState.stopRequested) break;
    const row = batchState.rows[index];
    row.status = "running";
    renderBatchRows();
    updateBatchSummary(`正在处理第 ${index + 1}/${batchState.rows.length} 条：${row.symptom.slice(0, 28)}`);
    try {
      const data = await requestBatchDiagnosis({
        vehicle_series: row.vehicle_series,
        symptom: row.original_question || row.symptom,
        engineer_question: row.engineer_question || row.symptom,
        task_type: row.task_type,
        answer_mode: batchState.mode,
      });
      row.status = data.no_knowledge ? "no_knowledge" : "success";
      batchState.answers.set(row.id, data);
      appendBatchExportRows(row, data);
    } catch (error) {
      row.status = "failed";
      row.error = error.message;
      appendBatchExportRows(row, null);
      if (error.message.includes("月度套餐额度已用完")) {
        batchState.stopRequested = true;
      }
    }
    renderBatchRows();
    updateBatchSummary(`已完成 ${index + 1}/${batchState.rows.length} 条`);
  }
  if (batchState.stopRequested) {
    batchState.rows.filter((row) => row.status === "pending").forEach((row) => { row.status = "stopped"; });
  }
  batchState.running = false;
  renderBatchRows();
  updateBatchSummary(batchState.stopRequested ? "批量回答已停止，可导出已完成结果" : "批量回答完成，可以导出Excel");
}

async function exportBatchResults() {
  if (!batchState.exportRows.length) return;
  $("batchExportButton").disabled = true;
  $("batchStatusText").textContent = "正在生成Excel文件";
  try {
    const blob = await getBlob("/api/batch/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rows: batchState.exportRows }),
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `JH6批量诊断结果_${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "")}.xlsx`;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
    $("batchStatusText").textContent = "Excel导出完成";
  } catch (error) {
    $("batchStatusText").textContent = `导出失败：${error.message}`;
    alert(error.message);
  } finally {
    $("batchExportButton").disabled = !batchState.exportRows.length;
  }
}

function setupBatchDiagnosis() {
  $("batchButton").addEventListener("click", openBatchModal);
  $("batchCloseButton").addEventListener("click", closeBatchModal);
  document.querySelectorAll("[data-batch-close]").forEach((node) => node.addEventListener("click", closeBatchModal));
  $("batchFile").addEventListener("change", (event) => importBatchFile(event.target.files?.[0]));
  document.querySelectorAll("[data-batch-mode]").forEach((button) => button.addEventListener("click", () => {
    batchState.mode = button.dataset.batchMode;
    document.querySelectorAll("[data-batch-mode]").forEach((item) => item.classList.toggle("active", item === button));
  }));
  $("batchStartButton").addEventListener("click", startBatchDiagnosis);
  $("batchStopButton").addEventListener("click", () => { batchState.stopRequested = true; $("batchStatusText").textContent = "将在当前问题完成后停止"; });
  $("batchExportButton").addEventListener("click", exportBatchResults);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !$("batchModal").classList.contains("hidden")) closeBatchModal(); });
  updateBatchSummary();
}

const importDocState = { file: null, running: false };

function openImportDocModal() {
  $("importDocModal").classList.remove("hidden");
  document.body.classList.add("batch-open");
}

function closeImportDocModal() {
  if (importDocState.running && !confirm("文档导入仍在进行，关闭窗口不会中断服务端处理。确定关闭吗？")) return;
  $("importDocModal").classList.add("hidden");
  document.body.classList.remove("batch-open");
}

function escDoc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char]);
}

function renderImportDocResult(report) {
  const body = $("importDocResultBody");
  body.innerHTML = "";
  const row = document.createElement("tr");
  const task = report.task || {};
  const cells = [
    `${escDoc(report.file_name)}${report.superseded ? `（停用旧版本 ${report.superseded} 份）` : ""}`,
    escDoc(report.scene),
    String(report.chunks ?? 0),
    report.vector_failed ? `${report.vectorized}/${report.chunks}（失败${report.vector_failed}）` : String(report.vectorized ?? 0),
    escDoc(task.task_label || "—"),
    report.ok ? "已导入" : "失败",
  ];
  cells.forEach((text) => {
    const cell = document.createElement("td");
    cell.textContent = text;
    row.appendChild(cell);
  });
  body.appendChild(row);
}

async function importDocumentFile() {
  const file = importDocState.file;
  if (!file || importDocState.running) return;
  if (!/\.(pdf|docx|pptx|xlsx|doc|xls)$/i.test(file.name)) {
    alert("不支持的文件格式，支持：pdf、docx、pptx、xlsx、doc、xls。");
    return;
  }
  if (file.size > 10 * 1024 * 1024) {
    alert("单个文档不能超过10MB。");
    return;
  }
  importDocState.running = true;
  $("importDocStartButton").disabled = true;
  $("importDocProgress").value = 0;
  $("importDocStatusText").textContent = `正在导入 ${file.name}（解析、切片、向量化可能需要数分钟）`;
  $("importDocResultBody").innerHTML = '<tr class="batch-empty-row"><td colspan="6">处理中，请保持页面打开…</td></tr>';
  try {
    const report = await getJson("/api/import/document", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        file_name: file.name,
        file_base64: await fileToBase64(file),
        scene: $("importDocScene").value,
      }),
    });
    $("importDocProgress").value = 100;
    $("importDocStatusText").textContent = `导入完成：${report.chunks} 个切片，向量化 ${report.vectorized} 条，耗时 ${report.elapsed_seconds} 秒`;
    renderImportDocResult(report);
    loadStats();
  } catch (error) {
    $("importDocProgress").value = 100;
    $("importDocStatusText").textContent = `导入失败：${error.message}`;
    $("importDocResultBody").innerHTML = `<tr class="batch-empty-row"><td colspan="6">导入失败：${escDoc(error.message)}</td></tr>`;
  } finally {
    importDocState.running = false;
    $("importDocStartButton").disabled = !importDocState.file;
  }
}

function setupDocumentImport() {
  $("importDocButton").addEventListener("click", openImportDocModal);
  $("importDocCloseButton").addEventListener("click", closeImportDocModal);
  document.querySelectorAll("[data-import-doc-close]").forEach((node) => node.addEventListener("click", closeImportDocModal));
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !$("importDocModal").classList.contains("hidden")) closeImportDocModal(); });
  $("importDocFile").addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    importDocState.file = file || null;
    $("importDocStartButton").disabled = !file;
    $("importDocStatusText").textContent = file ? `已选择 ${file.name}（${(file.size / 1024 / 1024).toFixed(2)} MB）` : "等待选择文档";
  });
  $("importDocStartButton").addEventListener("click", importDocumentFile);
}

series.forEach((value) => { const option = document.createElement("option"); option.value = value; option.textContent = value; $("vehicleSeries").append(option); });
$("askForm").addEventListener("submit", (event) => { event.preventDefault(); const question = $("question").value.trim(); if (question) { $("question").value = ""; ask(question); } });
$("question").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("askForm").requestSubmit(); } });
document.querySelectorAll(".quick button").forEach((button) => button.addEventListener("click", () => ask(button.textContent)));
document.querySelectorAll(".desktop-mode-option").forEach((button) => button.addEventListener("click", () => applyAnswerMode(button.dataset.mode)));
document.querySelectorAll(".tabs button").forEach((button) => button.addEventListener("click", () => { document.querySelectorAll(".tabs button").forEach((item) => item.classList.toggle("active", item === button)); document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.toggle("hidden", panel.id !== button.dataset.tab)); }));
$("historyButton").addEventListener("click", () => document.querySelector('.tabs button[data-tab="history"]')?.click());
applyAnswerMode(state.answerMode); renderCapabilities(); loadCapabilities(); loadStats(); loadHistory(); setupVoiceInput(); setupBatchDiagnosis(); setupDocumentImport();
