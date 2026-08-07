function getBaseUrl() {
  const app = getApp()
  return wx.getStorageSync("apiBaseUrl") || app.globalData.defaultApiBaseUrl
}

function absoluteUrl(path) {
  if (!path) return ""
  if (/^https?:\/\//i.test(path)) return path
  return `${getBaseUrl()}${path.startsWith("/") ? "" : "/"}${path}`
}

function request(path, options) {
  const config = options || {}
  return new Promise((resolve, reject) => {
    wx.request({
      url: absoluteUrl(path),
      method: config.method || "GET",
      data: config.data || {},
      timeout: config.timeout || 180000,
      header: {
        "content-type": "application/json",
        ...(config.header || {})
      },
      success(response) {
        if (response.statusCode >= 200 && response.statusCode < 300) {
          resolve(response.data)
          return
        }
        const message = response.data && response.data.error
          ? response.data.error
          : `接口请求失败（${response.statusCode}）`
        reject(new Error(message))
      },
      fail(error) {
        reject(new Error(error.errMsg || "无法连接知识库服务"))
      }
    })
  })
}

function createUtf8Decoder() {
  let pending = []
  return function decode(arrayBuffer) {
    const incoming = Array.from(new Uint8Array(arrayBuffer || new ArrayBuffer(0)))
    const bytes = pending.concat(incoming)
    pending = []
    let output = ""
    let index = 0
    while (index < bytes.length) {
      const first = bytes[index]
      let needed = 1
      let codePoint = first
      if (first >= 0xc2 && first <= 0xdf) {
        needed = 2
        codePoint = first & 0x1f
      } else if (first >= 0xe0 && first <= 0xef) {
        needed = 3
        codePoint = first & 0x0f
      } else if (first >= 0xf0 && first <= 0xf4) {
        needed = 4
        codePoint = first & 0x07
      } else if (first >= 0x80) {
        output += "�"
        index += 1
        continue
      }
      if (index + needed > bytes.length) {
        pending = bytes.slice(index)
        break
      }
      let valid = true
      for (let offset = 1; offset < needed; offset += 1) {
        const value = bytes[index + offset]
        if ((value & 0xc0) !== 0x80) {
          valid = false
          break
        }
        codePoint = (codePoint << 6) | (value & 0x3f)
      }
      if (!valid) {
        output += "�"
        index += 1
        continue
      }
      output += String.fromCodePoint(codePoint)
      index += needed
    }
    return output
  }
}

function streamRequest(path, options, handlers) {
  const config = options || {}
  const callbacks = handlers || {}
  return new Promise((resolve, reject) => {
    const decode = createUtf8Decoder()
    let buffer = ""
    let settled = false

    function handleEvent(event) {
      if (!event || !event.type) return
      if (event.type === "status" && callbacks.onStatus) callbacks.onStatus(event)
      if (event.type === "meta" && callbacks.onMeta) callbacks.onMeta(event)
      if (event.type === "delta" && callbacks.onDelta) callbacks.onDelta(event)
      if (event.type === "mode_fallback" && callbacks.onFallback) callbacks.onFallback(event)
      if (event.type === "done" && !settled) {
        settled = true
        resolve(event.data)
      }
      if (event.type === "error" && !settled) {
        settled = true
        reject(new Error(event.error || "流式回答失败"))
      }
    }

    function consume(text) {
      buffer += text
      const lines = buffer.split("\n")
      buffer = lines.pop() || ""
      lines.forEach((line) => {
        const value = line.trim()
        if (!value) return
        try {
          handleEvent(JSON.parse(value))
        } catch (error) {
          if (!settled) {
            settled = true
            reject(new Error("流式数据解析失败"))
          }
        }
      })
    }

    const task = wx.request({
      url: absoluteUrl(path),
      method: config.method || "POST",
      data: config.data || {},
      timeout: config.timeout || 180000,
      enableChunked: true,
      responseType: "arraybuffer",
      header: {
        "content-type": "application/json",
        ...(config.header || {})
      },
      success(response) {
        if (response.statusCode < 200 || response.statusCode >= 300) {
          if (!settled) {
            settled = true
            reject(new Error(`接口请求失败（${response.statusCode}）`))
          }
          return
        }
        if (response.data && response.data.byteLength) consume(decode(response.data))
        if (buffer.trim() && !settled) {
          try {
            handleEvent(JSON.parse(buffer.trim()))
          } catch (error) {
            settled = true
            reject(new Error("流式响应未完整结束"))
          }
        }
        if (!settled) {
          settled = true
          reject(new Error("流式响应未返回完成事件"))
        }
      },
      fail(error) {
        if (!settled) {
          settled = true
          reject(new Error(error.errMsg || "无法连接知识库服务"))
        }
      }
    })
    task.onChunkReceived((response) => consume(decode(response.data)))
  })
}

module.exports = {
  getBaseUrl,
  absoluteUrl,
  request,
  streamRequest
}
