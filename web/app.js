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
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char]);

async function getJson(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
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
  let received = ""; let shown = ""; let stopped = false;
  const paint = () => { live.content.textContent = shown; $("conversation").scrollTop = $("conversation").scrollHeight; };
  const timer = setInterval(() => { if (stopped || shown.length >= received.length) return; const pending = received.length - shown.length; shown = received.slice(0, shown.length + Math.min(12, Math.max(3, Math.ceil(pending / 4)))); paint(); }, 32);
  return {
    status(text) { if (!received) { shown = text; paint(); shown = ""; } },
    push(text) { if (text) received += text; },
    async finish() { clearInterval(timer); shown = received; paint(); stopped = true; },
    stop() { stopped = true; clearInterval(timer); },
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
  const labels = { vin: "VIN", chassis_no: "底盘号", emission_type: "排放种类", vehicle_series: "车系", fuel_type: "燃料种类", announcement_model: "公告型号", factory_model_code: "车厂车型码", rear_axle: "后桥", tire_spec: "轮胎规格", engine_type: "发动机类型", engine_model: "发动机型号", transmission_model: "变速箱型号", offline_time: "下线时间", vehicle_note: "车型备注", engine_name: "发动机名称" };
  const fields = data.record ? Object.entries(labels).map(([key, label]) => `<div class="vin-field"><small>${label}</small><b>${esc(key === "vin" ? data.vin : data.record[key] || "—")}</b></div>`).join("") : "";
  node.innerHTML = `<div class="message-text"><div class="assistant-answer-header"><strong><i>VIN</i> 车辆静态信息</strong><span>${esc(data.vin)}</span></div>${data.found ? `<div class="vin-result">${fields}</div>` : `<div class="vin-empty">${esc(data.message)}<br>VIN查询接口已经就绪，导入车辆主数据后即可显示全部15项静态字段。</div>`}</div>`;
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

series.forEach((value) => { const option = document.createElement("option"); option.value = value; option.textContent = value; $("vehicleSeries").append(option); });
$("askForm").addEventListener("submit", (event) => { event.preventDefault(); const question = $("question").value.trim(); if (question) { $("question").value = ""; ask(question); } });
$("question").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("askForm").requestSubmit(); } });
document.querySelectorAll(".quick button").forEach((button) => button.addEventListener("click", () => ask(button.textContent)));
document.querySelectorAll(".desktop-mode-option").forEach((button) => button.addEventListener("click", () => applyAnswerMode(button.dataset.mode)));
document.querySelectorAll(".tabs button").forEach((button) => button.addEventListener("click", () => { document.querySelectorAll(".tabs button").forEach((item) => item.classList.toggle("active", item === button)); document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.toggle("hidden", panel.id !== button.dataset.tab)); }));
$("historyButton").addEventListener("click", () => document.querySelector('.tabs button[data-tab="history"]')?.click());
applyAnswerMode(state.answerMode); renderCapabilities(); loadCapabilities(); loadStats(); loadHistory(); setupVoiceInput();
