// 后台服务：使用 Chrome Debugger API 捕获网络请求
let recording = false;
let requests = [];
let serverUrl = "";

// 监听来自 popup 的消息
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "start") {
    startRecording(msg.serverUrl).then(() => sendResponse({ ok: true }));
    return true;
  } else if (msg.type === "stop") {
    stopRecording().then((reqs) => sendResponse({ ok: true, requests: reqs }));
    return true;
  } else if (msg.type === "status") {
    sendResponse({ recording, count: requests.length });
    return true;
  } else if (msg.type === "clear") {
    requests = [];
    sendResponse({ ok: true });
    return true;
  }
});

async function startRecording(url) {
  serverUrl = url;
  requests = [];
  recording = true;

  // 获取当前活动标签页
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tabs.length) return;

  const tabId = tabs[0].id;
  try {
    await chrome.debugger.attach({ tabId }, "1.3");
    await chrome.debugger.sendCommand({ tabId }, "Network.enable");
    // 监听网络事件
    chrome.debugger.onEvent.addListener(onDebuggerEvent);
  } catch (e) {
    recording = false;
    throw e;
  }
}

function onDebuggerEvent(source, method, params) {
  if (!recording) return;
  if (method === "Network.requestWillBeSent") {
    const req = params.request;
    const url = req.url;
    // 只记录 API 请求（xhr/fetch），跳过静态资源
    if (/\.(js|css|png|jpg|jpeg|gif|svg|ico|woff|woff2|ttf|map)(\?|$)/i.test(url)) {
      return;
    }
    const rec = {
      method: req.method,
      url: url,
      headers: req.headers || {},
      body: req.postData || "",
      ts: Date.now() / 1000,
    };
    requests.push(rec);

    // multipart 请求的 postData 通常为空，尝试用 getRequestPostData 获取
    const ct = (req.headers || {})["content-type"] || (req.headers || {})["Content-Type"] || "";
    if (req.method === "POST" && ct.includes("multipart/form-data")) {
      const requestId = params.requestId;
      chrome.debugger.sendCommand(source, "Network.getRequestPostData", { requestId })
        .then((data) => {
          if (data && data.postData) {
            // 解析 multipart 里的普通字段（非文件字段），存为 JSON
            rec.body = parseMultipartFields(data.postData);
          }
        })
        .catch(() => {});
    }
  }
}

// 解析 multipart body，提取普通字段（非文件字段），返回 JSON 字符串
function parseMultipartFields(postData) {
  const fields = {};
  // 按 boundary 分割
  const boundaryMatch = postData.match(/^--([^\r\n]+)/);
  if (!boundaryMatch) return "";
  const boundary = boundaryMatch[1];
  const parts = postData.split("--" + boundary);
  for (const part of parts) {
    const headerEnd = part.indexOf("\r\n\r\n");
    if (headerEnd === -1) continue;
    const header = part.slice(0, headerEnd);
    const value = part.slice(headerEnd + 4).replace(/\r\n--$/, "").replace(/\r\n$/, "");
    // 提取 name 和 filename
    const nameMatch = header.match(/name="([^"]+)"/);
    if (!nameMatch) continue;
    const name = nameMatch[1];
    const filenameMatch = header.match(/filename="([^"]+)"/);
    if (filenameMatch) {
      // 文件字段：跳过（文件通过平台上传后下拉选择）
      continue;
    }
    fields[name] = value;
  }
  return JSON.stringify(fields);
}

async function stopRecording() {
  recording = false;
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tabs.length) {
    const tabId = tabs[0].id;
    try {
      chrome.debugger.onEvent.removeListener(onDebuggerEvent);
      await chrome.debugger.detach({ tabId });
    } catch (e) {}
  }
  return requests;
}
