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
    requests.push({
      method: req.method,
      url: url,
      headers: req.headers || {},
      body: req.postData || "",
      ts: Date.now() / 1000,
    });
  }
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
