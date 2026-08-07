const api = require("../../utils/api")

Page({
  data: {
    apiBaseUrl: "",
    testing: false,
    connected: false,
    resultText: "",
    qualityText: "",
    retrievalText: "正在读取向量库状态"
  },

  onShow() {
    this.setData({ apiBaseUrl: api.getBaseUrl(), resultText: "" })
    this.loadQuality()
    this.loadRuntime()
  },

  async loadRuntime() {
    try {
      const health = await api.request("/api/health", { timeout: 10000 })
      const retrieval = health.retrieval || {}
      const qdrant = retrieval.qdrant || {}
      const points = Number(qdrant.points || 0).toLocaleString("zh-CN")
      this.setData({
        connected: health.status === "ok",
        retrievalText: qdrant.ready
          ? `${qdrant.model || "向量模型"} · ${qdrant.dimensions || "-"}维 · ${points}条向量 · ${qdrant.collection || "Qdrant"}`
          : "向量检索未就绪，请确认后端已启动向量化版本"
      })
    } catch (error) {
      this.setData({ connected: false, retrievalText: "无法读取向量库状态" })
    }
  },

  async loadQuality() {
    try {
      const quality = await api.request("/api/quality", { timeout: 10000 })
      const ocr = quality.ocr || {}
      const parsed = Number((quality.statuses || {}).parsed || 0)
        + Number((quality.statuses || {}).parsed_ocr || 0)
        + Number((quality.statuses || {}).parsed_legacy || 0)
      this.setData({
        qualityText: `已解析 ${parsed}/${quality.documents} 份 · OCR ${ocr.documents || 0} 份 · 分块 ${quality.chunks || 0}`
      })
    } catch (error) {
      this.setData({ qualityText: "尚未读取到构建质量数据" })
    }
  },

  onInput(event) {
    this.setData({ apiBaseUrl: event.detail.value.trim() })
  },

  resetDefault() {
    const value = getApp().globalData.defaultApiBaseUrl
    wx.setStorageSync("apiBaseUrl", value)
    this.setData({ apiBaseUrl: value, resultText: "已恢复本机默认地址", connected: true })
  },

  copyWebsite() {
    wx.setClipboardData({
      data: "https://www.smartlink.com.cn/",
      success() {
        wx.showToast({ title: "官网地址已复制", icon: "success" })
      }
    })
  },

  async saveAndTest() {
    const value = this.data.apiBaseUrl.replace(/\/$/, "")
    if (!/^https?:\/\//i.test(value)) {
      this.setData({ connected: false, resultText: "地址必须以 http:// 或 https:// 开头" })
      return
    }
    wx.setStorageSync("apiBaseUrl", value)
    this.setData({ testing: true, resultText: "正在测试连接..." })
    try {
      const health = await api.request("/api/health", { timeout: 10000 })
      const qdrant = (health.retrieval && health.retrieval.qdrant) || {}
      this.setData({
        connected: health.status === "ok" && Boolean(qdrant.ready),
        resultText: health.status === "ok" && qdrant.ready
          ? "连接成功，智能体与向量检索服务正常"
          : "后端可连接，但向量检索尚未就绪"
      })
      await this.loadQuality()
      await this.loadRuntime()
    } catch (error) {
      this.setData({ connected: false, resultText: `连接失败：${error.message}` })
    } finally {
      this.setData({ testing: false })
    }
  }
})
