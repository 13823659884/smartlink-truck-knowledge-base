App({
  globalData: {
    defaultApiBaseUrl: "http://127.0.0.1:8009"
  },

  onLaunch() {
    const savedUrl = wx.getStorageSync("apiBaseUrl")
    // 自动迁移旧版默认端口；用户手动填写的局域网或线上地址保持不变。
    if (!savedUrl || savedUrl === "http://127.0.0.1:8008") {
      wx.setStorageSync("apiBaseUrl", this.globalData.defaultApiBaseUrl)
    }
  }
})
