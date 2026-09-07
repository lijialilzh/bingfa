// popup 逻辑
let capturedRequests = [];

document.getElementById("btnStart").addEventListener("click", async () => {
  const serverUrl = document.getElementById("serverUrl").value.trim();
  if (!serverUrl) {
    setStatus("请先填写服务器地址", true);
    return;
  }
  // 保存服务器地址
  chrome.storage.local.set({ serverUrl });
  try {
    await chrome.runtime.sendMessage({ type: "start", serverUrl });
    setStatus("录制中… 请在页面上操作");
    document.getElementById("btnStart").style.display = "none";
    document.getElementById("btnStop").style.display = "block";
  } catch (e) {
    setStatus("启动失败：" + e.message, true);
  }
});

document.getElementById("btnStop").addEventListener("click", async () => {
  const res = await chrome.runtime.sendMessage({ type: "stop" });
  capturedRequests = res.requests || [];
  setStatus(`已捕获 ${capturedRequests.length} 个请求`);
  document.getElementById("btnStop").style.display = "none";
  document.getElementById("btnUpload").style.display = "block";
});

document.getElementById("btnUpload").addEventListener("click", async () => {
  const { serverUrl } = await chrome.storage.local.get("serverUrl");
  if (!serverUrl) {
    setStatus("服务器地址为空", true);
    return;
  }
  setStatus("上传中…");
  try {
    const res = await fetch(serverUrl + "/api/record/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ requests: capturedRequests }),
    });
    const data = await res.json();
    if (data.ok) {
      setStatus(`✅ 上传成功，共 ${data.count} 个请求`);
      document.getElementById("btnUpload").style.display = "none";
      document.getElementById("btnStart").style.display = "block";
    } else {
      setStatus("上传失败：" + (data.msg || "未知错误"), true);
    }
  } catch (e) {
    setStatus("上传失败：" + e.message, true);
  }
});

function setStatus(text, isError) {
  const el = document.getElementById("status");
  el.textContent = text;
  el.style.color = isError ? "#ef4444" : "#666";
}

// 初始化：恢复服务器地址
chrome.storage.local.get("serverUrl", (data) => {
  if (data.serverUrl) {
    document.getElementById("serverUrl").value = data.serverUrl;
  }
});
