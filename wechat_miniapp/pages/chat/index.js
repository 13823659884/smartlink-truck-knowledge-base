const api = require("../../utils/api");

let messageSequence = 1;

function nextMessageId() {
  messageSequence += 1;
  return messageSequence;
}

function formatAnswer(data) {
  const parts = [data.answer || "暂未获得回答"];
  if (data.solution_steps && data.solution_steps.length) {
    parts.push(
      `处理步骤：\n${data.solution_steps.map((item, index) => `${index + 1}. ${item}`).join("\n")}`,
    );
  }
  if (data.safety_notice) {
    parts.push(`安全提示：${data.safety_notice}`);
  }
  return parts.join("\n\n");
}

function citationContent(value) {
  const citations = [];
  const text = String(value || "").replace(/[【\[]资料\s*(\d+)[】\]]/g, (token, index) => {
    const number = Number(index);
    if (!citations.includes(number)) citations.push(number);
    return "";
  }).trim();
  return { text, citations };
}

function answerParagraphs(value) {
  return String(value || "").split(/\n+/).map((line) => line.trim()).filter(Boolean).map(citationContent);
}

function isContextualReply(value) {
  const normalized = String(value || "").replace(/[\s，。！？,.!?]/g, "");
  return /^(是|是的|对|有|有的|没有|否|不是|亮了|点亮了|没亮|未点亮|正常|不正常|能|不能|可以|不可以|不确定|不知道|偶发|一直)$/.test(normalized);
}

const TASK_LABELS = {
  vin: "VIN车辆信息",
  fault_code: "故障码诊断",
  symptom_diagnosis: "症状诊断",
  usage: "用车知识",
  maintenance: "保养知识",
  warranty: "保用知识",
  service_technical: "服务技术",
  drawing: "图纸资料",
  claim_case: "索赔案例",
  general: "综合知识",
};

const INTENT_TASK_TYPES = {
  vin: "vin",
  fault: "fault_code",
  symptom: "symptom_diagnosis",
  usage: "usage",
  maintenance: "maintenance",
  warranty: "warranty",
};

function formatVectorCount(value) {
  const number = Number(value || 0);
  if (!number) return "";
  return number.toLocaleString("zh-CN");
}

function retrievalSummary(data) {
  const retrieval = (data && data.retrieval) || {};
  const scope = retrieval.scope || "professional";
  const exactCount = Number(retrieval.exact_fault_match_count || 0);
  const taskType = retrieval.task_type || (scope === "general" ? "general" : "");
  const retrievalMs = Number(
    (data && data.timing && data.timing.retrieval_ms) || data.retrieval_ms || 0,
  );
  return {
    visible: Boolean(taskType || retrieval.method || scope),
    taskType,
    taskLabel: TASK_LABELS[taskType] || "智能分类",
    routeLabel: scope === "general"
      ? "通用模型回答"
      : (exactCount > 0 ? "故障码精确索引" : "分类混合检索"),
    exactCount,
    sourceCount: Number(retrieval.source_count || data.source_count || 0),
    retrievalMs: retrievalMs ? Math.round(retrievalMs) : 0,
  };
}

function openKnowledgeDocument(url, fileName) {
  if (!url) {
    wx.showToast({ title: "该资料暂无可用链接", icon: "none" });
    return;
  }
  wx.showLoading({ title: "正在打开资料" });
  wx.downloadFile({
    url,
    timeout: 120000,
    success(response) {
      if (response.statusCode !== 200) {
        wx.showToast({ title: "资料下载失败", icon: "none" });
        return;
      }
      wx.openDocument({
        filePath: response.tempFilePath,
        fileName: fileName || "资料",
        showMenu: true,
        fail() {
          wx.showToast({ title: "该格式暂不支持预览", icon: "none" });
        },
      });
    },
    fail() {
      wx.showToast({ title: "无法下载资料", icon: "none" });
    },
    complete() {
      wx.hideLoading();
    },
  });
}

Page({
  data: {
    statusBarHeight: 20,
    navBarHeight: 44,
    navRight: 96,
    navHeight: 64,
    composerHeight: 176,
    connected: false,
    statusText: "正在连接知识库...",
    retrievalReady: false,
    retrievalText: "正在检查向量检索服务...",
    retrievalModel: "",
    vectorPoints: "",
    agentModes: {},
    agentProvider: "火山方舟",
    vehicleOptions: [
      "全部车型",
      "J7",
      "J6",
      "J6P",
      "J6L",
      "JH6",
      "JK6",
      "J6F",
      "J5",
      "虎6G",
      "领途",
      "鹰途",
      "鹰阵",
      "V卡",
    ],
    vehicleIndex: 0,
    sceneOptions: ["全部场景", "用", "养", "修", "保"],
    sceneIndex: 0,
    answerMode: "fast",
    activeIntent: "all",
    activeCapability: "智能问答",
    capabilityOptions: [
      { id: "all", name: "智能问答", icon: "问", scene: "", example: "刹车不灵敏怎么排查？" },
      { id: "vin", name: "VIN查询", icon: "车", scene: "", example: "请输入17位VIN" },
      { id: "fault", name: "故障码", icon: "码", scene: "修", example: "例如：P312700" },
      { id: "symptom", name: "症状查询", icon: "诊", scene: "修", example: "例如：动力不足" },
      { id: "usage", name: "用车知识", icon: "用", scene: "用", example: "驻车再生怎么操作？" },
      { id: "maintenance", name: "保养知识", icon: "养", scene: "养", example: "变速箱多久保养？" },
      { id: "warranty", name: "保用知识", icon: "保", scene: "保", example: "制动部件是否在保？" },
    ],
    recentHistory: [],
    quickQuestions: [
      { icon: "诊", title: "故障诊断", intent: "symptom", question: "无法上高压怎么排查？" },
      { icon: "养", title: "维护保养", intent: "maintenance", question: "变速箱怎么保养？" },
      { icon: "码", title: "故障码", intent: "fault", question: "BMS故障码怎么排查？" },
      { icon: "制", title: "制动系统", intent: "symptom", question: "刹车片磨损怎么检查？" },
    ],
    messages: [
      {
        id: 1,
        role: "assistant",
        text: "你好，我可以查询车辆用、养、修、保资料。答案下方会列出原始文档、对应页码和参考内容。",
        images: [],
        documents: [],
        references: [],
        relatedQuestions: [],
        canFeedback: false,
        loading: false,
      },
    ],
    showQuick: true,
    inputValue: "",
    pendingImage: null,
    recognizing: false,
    sending: false,
    conversationId: "",
    diagnosticPrompt: "",
    scrollIntoView: "message-bottom",
  },

  onLoad() {
    const windowInfo = wx.getWindowInfo
      ? wx.getWindowInfo()
      : wx.getSystemInfoSync();
    const savedMode = wx.getStorageSync("answerMode");
    const statusBarHeight = windowInfo.statusBarHeight || 20;
    let menuRect = null;
    try {
      menuRect = wx.getMenuButtonBoundingClientRect
        ? wx.getMenuButtonBoundingClientRect()
        : null;
    } catch (error) {
      menuRect = null;
    }
    const navBarHeight = menuRect && menuRect.height
      ? Math.max(40, (menuRect.top - statusBarHeight) * 2 + menuRect.height)
      : 44;
    const navRight = menuRect && menuRect.left
      ? Math.max(88, windowInfo.windowWidth - menuRect.left + 8)
      : 96;
    this.setData({
      statusBarHeight,
      navBarHeight,
      navRight,
      navHeight: statusBarHeight + navBarHeight,
      answerMode: savedMode === "deep" ? "deep" : "fast",
    });
  },

  onReady() {
    this.measureComposer();
  },

  onShow() {
    this.checkConnection();
    this.loadCapabilities();
    this.loadHistory();
    this.measureComposer();
  },

  async loadCapabilities() {
    try {
      const result = await api.request("/api/intents");
      if (!result.intents || !result.intents.length) return;
      const icons = { all: "问", vin: "车", fault: "码", symptom: "诊", usage: "用", maintenance: "养", warranty: "保" };
      this.setData({
        capabilityOptions: result.intents.map((item) => ({
          id: item.id,
          name: item.name,
          icon: icons[item.id] || item.name.slice(0, 1),
          scene: item.scene || "",
          example: item.example_question || "请输入车辆问题",
        })),
      });
    } catch (error) {}
  },

  async loadHistory() {
    try {
      const result = await api.request("/api/history?limit=8");
      this.setData({ recentHistory: result.history || [] });
    } catch (error) {
      this.setData({ recentHistory: [] });
    }
  },

  selectCapability(event) {
    const id = event.currentTarget.dataset.id;
    const item = this.data.capabilityOptions.find((value) => value.id === id);
    if (!item) return;
    const sceneIndex = item.scene
      ? Math.max(0, this.data.sceneOptions.indexOf(item.scene))
      : (["all", "vin"].includes(item.id) ? 0 : this.data.sceneIndex);
    this.setData({ activeIntent: id, activeCapability: item.name, sceneIndex, inputValue: "" });
  },

  async openHistory(event) {
    const conversationId = event.currentTarget.dataset.id;
    if (!conversationId) return;
    try {
      const result = await api.request(`/api/history?conversation_id=${encodeURIComponent(conversationId)}`);
      const messages = (result.messages || []).map((item) => ({
        id: nextMessageId(), role: item.role, text: item.content,
        images: [], documents: [], references: [], relatedQuestions: [],
        diagnosis: null, canFeedback: false, loading: false,
      }));
      this.setData({ conversationId, messages, showQuick: false, scrollIntoView: "message-bottom" });
    } catch (error) {
      wx.showToast({ title: "历史记录读取失败", icon: "none" });
    }
  },

  measureComposer() {
    wx.nextTick(() => {
      wx.createSelectorQuery()
        .in(this)
        .select(".composer")
        .boundingClientRect((rect) => {
          if (!rect || !rect.height) return;
          const height = Math.ceil(rect.height);
          if (Math.abs(height - this.data.composerHeight) > 1) {
            this.setData({ composerHeight: height });
          }
        })
        .exec();
    });
  },

  async checkConnection() {
    try {
      const health = await api.request("/api/health", { timeout: 10000 });
      const result = health.agent || {};
      const qdrant = (health.retrieval && health.retrieval.qdrant) || {};
      const provider = result.provider_name || "火山方舟";
      const modes = result.modes || {};
      const activeMode = modes[this.data.answerMode] || {};
      this.setData({
        connected: health.status === "ok",
        agentModes: modes,
        agentProvider: provider,
        statusText: result.configured
          ? `${activeMode.label || provider} · ${activeMode.model || result.model}`
          : "知识库在线 · 智能体待配置",
        retrievalReady: Boolean(qdrant.ready),
        retrievalModel: qdrant.model || "",
        vectorPoints: formatVectorCount(qdrant.points),
        retrievalText: qdrant.ready
          ? `${qdrant.model || "向量模型"} · ${formatVectorCount(qdrant.points)} 条知识向量`
          : "知识库在线 · 向量检索尚未就绪",
      });
    } catch (error) {
      this.setData({
        connected: false,
        retrievalReady: false,
        statusText: "知识库暂时无法连接",
        retrievalText: "请在设置中检查新版服务地址",
      });
    }
  },

  onVehicleChange(event) {
    this.setData({ vehicleIndex: Number(event.detail.value) });
  },

  onSceneChange(event) {
    this.setData({ sceneIndex: Number(event.detail.value) });
  },

  onModeChange(event) {
    const mode = event.currentTarget.dataset.mode;
    if (mode === "fast" || mode === "deep") {
      wx.setStorageSync("answerMode", mode);
      const activeMode = this.data.agentModes[mode] || {};
      this.setData({
        answerMode: mode,
        statusText: this.data.connected && activeMode.model
          ? `${activeMode.label || this.data.agentProvider} · ${activeMode.model}`
          : this.data.statusText,
      });
    }
  },

  onInput(event) {
    this.setData({ inputValue: event.detail.value }, () => this.measureComposer());
  },

  askQuick(event) {
    const question = event.currentTarget.dataset.question;
    const intent = event.currentTarget.dataset.intent || "all";
    const capability = this.data.capabilityOptions.find((item) => item.id === intent);
    const sceneIndex = capability && capability.scene
      ? Math.max(0, this.data.sceneOptions.indexOf(capability.scene))
      : this.data.sceneIndex;
    this.setData({
      activeIntent: intent,
      activeCapability: capability ? capability.name : "智能问答",
      sceneIndex,
    }, () => this.ask(question));
  },

  askRelated(event) {
    this.ask(event.currentTarget.dataset.question);
  },

  answerDiagnosis(event) {
    const answer = event.currentTarget.dataset.answer;
    const prompt = event.currentTarget.dataset.prompt;
    if (answer && prompt) this.ask(answer, { replyTo: prompt });
  },

  sendQuestion() {
    const question = this.data.inputValue.trim();
    if (!question || this.data.sending) return;
    this.setData({ inputValue: "" });
    this.ask(question);
  },

  useVoiceInput() {
    wx.showToast({ title: "请点击输入框并使用系统键盘语音输入", icon: "none", duration: 2200 });
  },

  chooseImage() {
    if (this.data.sending || this.data.recognizing) return;
    wx.chooseMedia({
      count: 1,
      mediaType: ["image"],
      sourceType: ["camera", "album"],
      sizeType: ["compressed"],
      success: async (result) => {
        const file = result.tempFiles && result.tempFiles[0];
        if (!file) return;
        this.setData({ recognizing: true });
        wx.showLoading({ title: "正在识别图片" });
        try {
          const imageBase64 = await new Promise((resolve, reject) => {
            wx.getFileSystemManager().readFile({
              filePath: file.tempFilePath,
              encoding: "base64",
              success: (value) => resolve(value.data),
              fail: reject,
            });
          });
          const fileName = file.tempFilePath.split("/").pop() || "车辆图片.jpg";
          const ocr = await api.request("/api/image/recognize", {
            method: "POST",
            timeout: 120000,
            data: { image_base64: imageBase64, file_name: fileName },
          });
          this.setData({
            pendingImage: {
              tempFilePath: file.tempFilePath,
              fileName,
              text: ocr.text,
              confidence: Math.round(Number(ocr.confidence || 0) * 100),
            },
            inputValue: this.data.inputValue || ocr.suggested_question,
          }, () => this.measureComposer());
          wx.showToast({
            title: `识别到${ocr.line_count}行文字`,
            icon: "success",
          });
        } catch (error) {
          wx.showToast({
            title: error.message || "图片识别失败",
            icon: "none",
          });
        } finally {
          wx.hideLoading();
          this.setData({ recognizing: false });
        }
      },
    });
  },

  removePendingImage() {
    this.setData({ pendingImage: null }, () => this.measureComposer());
  },

  async ask(question, options) {
    if (!question || this.data.sending) return;
    if (this.data.activeIntent === "vin") {
      this.askVin(question);
      return;
    }
    const config = options || {};
    const replyTo =
      config.replyTo ||
      (isContextualReply(question) ? this.data.diagnosticPrompt : "") ||
      "";
    const pendingImage = this.data.pendingImage;
    const userMessage = {
      id: nextMessageId(),
      role: "user",
      text: `${replyTo ? `${question}\n（回答：${replyTo}）` : question}${
        pendingImage ? "\n[已上传图片并完成文字识别]" : ""
      }`,
      images: [],
      documents: [],
      references: [],
      relatedQuestions: [],
      diagnosis: null,
      canFeedback: false,
      loading: false,
    };
    const loadingId = nextMessageId();
    const loadingMessage = {
      id: loadingId,
      role: "assistant",
      text: "正在检索企业资料并分析",
      images: [],
      documents: [],
      references: [],
      relatedQuestions: [],
      canFeedback: false,
      loading: true,
    };
    this.setData({
      messages: this.data.messages.concat([userMessage, loadingMessage]),
      showQuick: false,
      sending: true,
      pendingImage: null,
      scrollIntoView: "message-bottom",
    }, () => this.measureComposer());

    let streamRenderer = { stop() {}, async finish() {} };
    try {
      const vehicle = this.data.vehicleOptions[this.data.vehicleIndex];
      const scene = this.data.sceneOptions[this.data.sceneIndex];
      let streamedText = "";
      const updateLoadingMessage = (patch) => {
        this.setData({
          messages: this.data.messages.map((item) =>
            item.id === loadingId ? { ...item, ...patch } : item,
          ),
          scrollIntoView: "message-bottom",
        });
      };
      let displayedText = "";
      let streamTimer = null;
      const paintStream = () => {
        const pending = streamedText.length - displayedText.length;
        if (pending <= 0) return;
        const take = pending <= 14
          ? pending
          : Math.min(14, Math.max(4, Math.ceil(pending / 4)));
        displayedText = streamedText.slice(0, displayedText.length + take);
        updateLoadingMessage({
          text: displayedText,
          loading: false,
          streaming: true,
        });
      };
      const ensureStreamTimer = () => {
        if (!streamTimer) streamTimer = setInterval(paintStream, 40);
      };
      streamRenderer = {
        stop() {
          if (streamTimer) clearInterval(streamTimer);
          streamTimer = null;
        },
        async finish() {
          this.stop();
          const deadline = Date.now() + 500;
          while (displayedText.length < streamedText.length && Date.now() < deadline) {
            const pending = streamedText.length - displayedText.length;
            const take = Math.min(28, Math.max(5, Math.ceil(pending / 5)));
            displayedText = streamedText.slice(0, displayedText.length + take);
            updateLoadingMessage({
              text: displayedText,
              loading: false,
              streaming: true,
            });
            await new Promise((resolve) => setTimeout(resolve, 24));
          }
          displayedText = streamedText;
          if (displayedText) {
            updateLoadingMessage({
              text: displayedText,
              loading: false,
              streaming: true,
            });
          }
        },
      };
      const result = await api.streamRequest("/api/search/stream", {
        method: "POST",
        timeout: 180000,
        data: {
          question,
          intent: this.data.activeIntent,
          task_type: INTENT_TASK_TYPES[this.data.activeIntent] || "",
          conversation_id: this.data.conversationId || undefined,
          vehicle_series: vehicle === "全部车型" ? "" : vehicle,
          scene: scene === "全部场景" ? "" : scene,
          image_ocr_text: pendingImage ? pendingImage.text : "",
          image_name: pendingImage ? pendingImage.fileName : "",
          reply_to_question: replyTo,
          use_agent: true,
          answer_mode: this.data.answerMode,
          include_images: false,
        },
      }, {
        onStatus: (event) => {
          if (!streamedText) updateLoadingMessage({ text: event.text, loading: true });
        },
        onMeta: (event) => {
          if (!streamedText) {
            updateLoadingMessage({
              text: event.text,
              loading: true,
              retrievalInfo: retrievalSummary(event),
            });
          }
        },
        onFallback: (event) => {
          if (event.answer_mode === "fast" || event.answer_mode === "deep") {
            wx.setStorageSync("answerMode", event.answer_mode);
            this.setData({ answerMode: event.answer_mode });
          }
          if (!streamedText) updateLoadingMessage({ text: event.text, loading: true });
        },
        onDelta: (event) => {
          streamedText += event.text || "";
          ensureStreamTimer();
        },
      });
      await streamRenderer.finish();
      const documents = (result.related_documents || []).map((item) => ({
        url: api.absoluteUrl(item.url),
        fileUrl: api.absoluteUrl(item.file_url || item.url).split("#")[0],
        fileName: item.file_name,
        sourceLocator: item.source_locator,
      }));
      const references = (
        result.reference_materials || result.sources || []
      ).slice(0, 12).map((item, index) => ({
        key: `${item.file_name || "资料"}-${item.source_locator || index}`,
        index: index + 1,
        fileName: item.file_name,
        sourceLocator: item.source_locator,
        excerpt: String(item.excerpt || "").slice(0, 600),
        fileUrl: api.absoluteUrl(item.file_url || item.document_url || "").split("#")[0],
      }));
      const assistantMessage = {
        id: nextMessageId(),
        role: "assistant",
        text: formatAnswer(result),
        answerParagraphs: answerParagraphs(result.answer),
        solutionSteps: (result.solution_steps || []).map(citationContent),
        safetyContent: citationContent(result.safety_notice || ""),
        agentError: result.agent && !result.agent.ok ? result.agent.error : "",
        images: [],
        documents,
        references,
        relatedQuestions: result.related_questions || [],
        diagnosis: result.diagnosis || null,
        retrievalInfo: retrievalSummary(result),
        canFeedback: true,
        messageId: result.message_id,
        question,
        answer: result.answer,
        loading: false,
      };
      this.setData({
        messages: this.data.messages.map((item) =>
          item.id === loadingId ? assistantMessage : item,
        ),
        conversationId: result.conversation_id,
        diagnosticPrompt:
          (result.diagnosis && result.diagnosis.pending_question) || "",
        answerMode:
          result.answer_mode === "deep" ? "deep" : "fast",
        sending: false,
        scrollIntoView: "message-bottom",
      });
      this.loadHistory();
    } catch (error) {
      streamRenderer.stop();
      const errorMessage = {
        id: nextMessageId(),
        role: "assistant",
        text: `问答失败：${error.message}`,
        agentError: "请在“设置”中检查知识库地址",
        images: [],
        documents: [],
        references: [],
        relatedQuestions: [],
        diagnosis: null,
        canFeedback: false,
        loading: false,
      };
      const remaining = this.data.messages.filter(
        (item) => item.id !== loadingId,
      );
      this.setData({
        messages: remaining.concat([errorMessage]),
        sending: false,
        scrollIntoView: "message-bottom",
      });
    }
  },

  async askVin(vin) {
    const userMessage = { id: nextMessageId(), role: "user", text: vin, images: [], documents: [], references: [], relatedQuestions: [], diagnosis: null, canFeedback: false, loading: false };
    this.setData({ messages: this.data.messages.concat([userMessage]), showQuick: false, sending: true, scrollIntoView: "message-bottom" });
    try {
      const result = await api.request(`/api/vin?q=${encodeURIComponent(vin)}`);
      const labels = [
        ["vin", "VIN"], ["vehicle_type", "车辆类型"], ["chassis_no", "底盘号"], ["emission_type", "排放种类"], ["vehicle_series", "车系"],
        ["fuel_type", "燃料种类"], ["announcement_model", "公告型号"], ["factory_model_code", "车厂车型码"],
        ["rear_axle", "后桥"], ["tire_spec", "轮胎规格"], ["engine_type", "发动机类型"],
        ["engine_model", "发动机型号"], ["transmission_model", "变速箱型号"], ["offline_time", "下线时间"],
        ["vehicle_note", "车型备注"], ["engine_name", "发动机名称"], ["device_app_version", "设备应用版本"],
        ["mcu_version", "MCU版本"], ["sim_match", "SIM匹配型号"],
      ];
      const vinFields = result.found ? labels.map(([key, label]) => ({ label, value: key === "vin" ? result.vin : result.record[key] || "—" })) : [];
      const assistant = { id: nextMessageId(), role: "assistant", text: result.message, vinResult: true, vin: result.vin, vinFields, images: [], documents: [], references: [], relatedQuestions: [], diagnosis: null, canFeedback: false, loading: false };
      this.setData({ messages: this.data.messages.concat([assistant]), sending: false, scrollIntoView: "message-bottom" });
    } catch (error) {
      const assistant = { id: nextMessageId(), role: "assistant", text: error.message, vinResult: true, vin, vinFields: [], images: [], documents: [], references: [], relatedQuestions: [], diagnosis: null, canFeedback: false, loading: false };
      this.setData({ messages: this.data.messages.concat([assistant]), sending: false, scrollIntoView: "message-bottom" });
    }
  },

  openCitation(event) {
    const messageId = event.currentTarget.dataset.messageId;
    const citationIndex = Number(event.currentTarget.dataset.index);
    const message = this.data.messages.find(
      (item) => String(item.id) === String(messageId),
    );
    const reference = message && message.references
      ? message.references.find((item) => Number(item.index) === citationIndex)
      : null;
    if (!reference) {
      wx.showToast({ title: "未找到对应资料链接", icon: "none" });
      return;
    }
    openKnowledgeDocument(reference.fileUrl, reference.fileName);
  },

  openDocument(event) {
    const url = event.currentTarget.dataset.url;
    const fileName = event.currentTarget.dataset.name || "资料";
    openKnowledgeDocument(url, fileName);
  },

  async sendFeedback(event) {
    const rating = event.currentTarget.dataset.rating;
    const messageId = event.currentTarget.dataset.messageId;
    const message = this.data.messages.find(
      (item) => String(item.messageId) === String(messageId),
    );
    if (!message) return;
    let comment = "";
    if (rating === "down") {
      const modal = await new Promise((resolve) => wx.showModal({ title: "需要纠偏", content: "请填写需要纠正或补充的内容（选填）", editable: true, placeholderText: "请输入反馈意见", success: resolve, fail: () => resolve({ confirm: false }) }));
      if (!modal.confirm) return;
      comment = modal.content || "";
    }
    try {
      await api.request("/api/feedback", {
        method: "POST",
        data: {
          rating,
          comment,
          message_id: messageId,
          conversation_id: this.data.conversationId,
          question: message.question,
          answer: message.answer,
          vehicle_series: this.data.vehicleOptions[this.data.vehicleIndex],
          scene: this.data.sceneOptions[this.data.sceneIndex],
        },
      });
      wx.showToast({ title: "评价已记录", icon: "success" });
    } catch (error) {
      wx.showToast({ title: error.message, icon: "none" });
    }
  },

  newConversation() {
    this.setData({
      conversationId: "",
      diagnosticPrompt: "",
      pendingImage: null,
      showQuick: true,
      messages: [
        {
          id: nextMessageId(),
          role: "assistant",
          text: "新对话已开始，请描述车辆问题。",
          images: [],
          documents: [],
          references: [],
          relatedQuestions: [],
          canFeedback: false,
          loading: false,
        },
      ],
      scrollIntoView: "message-bottom",
    });
  },
});
