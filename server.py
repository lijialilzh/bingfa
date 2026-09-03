#!/usr/bin/env python3
"""
可视化并发测试 Web 服务：
- 提供前端页面
- 通过 WebSocket 实时推送每个用户的请求/响应/状态
- 提供启动/停止测试的接口
"""

import asyncio
import json
import threading
from pathlib import Path
from typing import Optional, Set, List, Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from engine import TestEngine, DEFAULT_CONFIG, parse_accounts

app = FastAPI(title="阅片并发测试")

# 全局状态
engine: Optional[TestEngine] = None
engine_task: Optional[asyncio.Task] = None
clients: Set[WebSocket] = set()
event_log: List[dict] = []          # 保留最近事件用于回放
MAX_LOG = 5000

CONFIG_FILE = Path(__file__).parent / "config.json"


def load_config() -> dict:
    """从 config.json 读取配置，不存在则用默认值。"""
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg = {**DEFAULT_CONFIG, **saved}
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                           encoding="utf-8")


def broadcast(event: dict) -> None:
    """把事件推送给所有 WebSocket 客户端。"""
    event_log.append(event)
    if len(event_log) > MAX_LOG:
        del event_log[: len(event_log) - MAX_LOG]
    dead = []
    for ws in clients:
        try:
            asyncio.create_task(ws.send_text(json.dumps(event, ensure_ascii=False)))
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    html = Path(__file__).parent / "index.html"
    return html.read_text(encoding="utf-8")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    clients.add(ws)
    # 回放历史事件
    for ev in event_log:
        await ws.send_text(json.dumps(ev, ensure_ascii=False))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        clients.discard(ws)


@app.get("/api/config")
async def get_config() -> dict:
    cfg = load_config()
    return {"ok": True, "config": cfg,
            "accounts_count": len(cfg.get("accounts", []))}


@app.post("/api/config")
async def set_config(payload: dict) -> dict:
    """保存配置。payload 可含 base_url/login_path/viewer_path_template/
    password/study_uid/accounts(列表) 或 accounts_text(文本)。"""
    cfg = load_config()
    for key in ("base_url", "login_path", "viewer_path_template",
                "password", "study_uid"):
        if key in payload and payload[key] is not None:
            cfg[key] = str(payload[key]).strip()

    if "accounts" in payload and payload["accounts"]:
        cfg["accounts"] = [str(a).strip() for a in payload["accounts"]
                           if str(a).strip()]
    elif "accounts_text" in payload and payload["accounts_text"]:
        cfg["accounts"] = parse_accounts(payload["accounts_text"])

    save_config(cfg)
    return {"ok": True, "config": cfg,
            "accounts_count": len(cfg.get("accounts", []))}


@app.post("/api/start")
async def start_test(payload: dict) -> dict:
    global engine, engine_task
    if engine_task and not engine_task.done():
        return {"ok": False, "msg": "测试已在运行中"}

    users = int(payload.get("users", 100))
    observe = float(payload.get("observe", 15))
    event_log.clear()

    # 优先用请求里带的配置，否则用已保存配置
    cfg = load_config()
    if "config" in payload and payload["config"]:
        cfg = {**cfg, **payload["config"]}
        if "accounts_text" in payload["config"]:
            cfg["accounts"] = parse_accounts(payload["config"]["accounts_text"])

    engine = TestEngine(users=users, observe_seconds=observe,
                        emit=broadcast, config=cfg)
    engine_task = asyncio.create_task(engine.run())
    return {"ok": True, "msg": f"已启动 {users} 用户并发测试"}


@app.post("/api/stop")
async def stop_test() -> dict:
    global engine
    if engine:
        engine.stop()
    return {"ok": True, "msg": "已请求停止"}


@app.get("/api/status")
async def status() -> dict:
    running = bool(engine_task and not engine_task.done())
    return {"running": running, "events": len(event_log)}


@app.get("/api/report")
async def report() -> dict:
    """生成可审计报告：每个用户的账号、独立会话 token、精确时间戳。"""
    if not engine:
        return {"ok": False, "msg": "尚未运行测试"}
    states = engine._snapshot()
    # 并发度分析：登录时间戳的极差、阅片时间戳的极差
    login_ts = [s["login_ts"] for s in states if s["login_ts"] > 0]
    viewer_ts = [s["viewer_ts"] for s in states if s["viewer_ts"] > 0]
    first_ts = [s["first_frame_ts"] for s in states if s["first_frame_ts"] > 0]

    def spread(vals):
        if not vals:
            return 0.0
        return round(max(vals) - min(vals), 3)

    tokens = [s["session_token"] for s in states if s["session_token"]]
    return {
        "ok": True,
        "users": len(states),
        "unique_tokens": len(set(tokens)),
        "login_spread_seconds": spread(login_ts),
        "viewer_spread_seconds": spread(viewer_ts),
        "first_frame_spread_seconds": spread(first_ts),
        "states": states,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000, log_level="warning")
