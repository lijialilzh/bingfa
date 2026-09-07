#!/usr/bin/env python3
"""
前端 UI 自动化测试模块：
- 解析 Excel 用例（用例编号、用例名称、操作步骤、预期结果）
- 用 Playwright 打开真实浏览器，按自然语言步骤执行操作
- 验证预期结果，输出每条用例的通过/失败

自然语言步骤支持（中文）：
  打开/访问/进入 <网址>
  点击 <元素>
  在 <元素> 输入 <内容> / 输入 <内容> 到 <元素>
  选择 <下拉框> 为 <选项>
  等待 <N> 秒
  验证/断言/检查 <内容>
  截图
  回车
"""

import asyncio
import re
import time
from typing import Callable, Optional

from playwright.async_api import async_playwright


def split_steps(text: str) -> list:
    """把一段步骤描述拆分成多个步骤。

    支持：
    - 换行分隔
    - 编号分隔：1. 2. 3. 或 1、2、3、 或 1) 2) 3)
    - 句号/分号分隔（当没有编号时）
    - 按动作关键词切分（点击/输入/打开/选择/验证/等待/回车）
    """
    text = (text or "").strip()
    if not text:
        return []
    # 先按换行拆
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) > 1:
        steps = []
        for line in lines:
            steps.extend(split_steps(line))
        return steps

    # 按编号拆分：1. 2. 3. / 1、2、3、 / 1) 2) 3) / （1）（2）
    parts = re.split(r'(?=\d+[\.、\)）]\s*)', text)
    if len(parts) > 1:
        steps = []
        for p in parts:
            p = re.sub(r'^\d+[\.、\)）]\s*', '', p).strip()
            if p:
                steps.extend(_split_by_action(p))
        return steps

    # 按句号/分号拆分
    parts = re.split(r'[。；;]\s*', text)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) > 1:
        steps = []
        for p in parts:
            steps.extend(_split_by_action(p))
        return steps

    return _split_by_action(text)


# 动作关键词（用于把一句话里的多个动作拆开）
_ACTION_WORDS = ["点击", "单击", "双击", "输入", "填写", "填入", "键入",
                 "打开", "访问", "进入", "跳转", "浏览", "转到",
                 "选择", "选中", "验证", "断言", "检查", "确认", "校验",
                 "等待", "回车", "截图", "截屏"]


def _split_by_action(text: str) -> list:
    """按动作关键词把一句话拆成多个步骤。"""
    text = (text or "").strip()
    if not text:
        return []
    # 找到所有动作关键词的位置
    positions = []
    for w in _ACTION_WORDS:
        for m in re.finditer(re.escape(w), text):
            positions.append((m.start(), w))
    if not positions:
        return [text]
    positions.sort()
    # 从每个动作词开始切分
    steps = []
    for i, (pos, w) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        seg = text[pos:end].strip()
        # 去掉段尾的逗号/顿号
        seg = re.sub(r'[，,、]+$', '', seg).strip()
        if seg:
            steps.append(seg)
    return steps


# ---------------- 自然语言步骤解析 ----------------

def parse_step(text: str) -> dict:
    """把自然语言步骤解析成结构化操作。"""
    text = (text or "").strip()
    if not text:
        return {"action": "noop", "raw": text}

    # 打开/访问 URL
    m = re.search(r'(https?://\S+)', text)
    if m and re.match(r'^(打开|访问|进入|跳转|浏览|转到)', text):
        return {"action": "goto", "url": m.group(1), "raw": text}

    # 描述性文字（进入 xxx 页面 / 打开 xxx 页面，无 URL）：跳过
    if re.match(r'^(进入|打开|访问|跳转|浏览|转到)\s*.{0,20}(页面|界面|系统|网站)$', text):
        return {"action": "noop", "raw": text, "desc": "描述性步骤，跳过"}

    # 等待 N 秒
    m = re.match(r'^等待\s*(\d+(?:\.\d+)?)\s*秒?', text)
    if m:
        return {"action": "wait", "seconds": float(m.group(1)), "raw": text}

    # 点击
    m = re.match(r'^(?:点击|点|单击|双击)\s*[:：]?\s*(.+)$', text)
    if m:
        return {"action": "click", "target": m.group(1).strip(), "raw": text}

    # 输入：在 X 输入 Y / 向 X 输入 Y / 在 X 填写 Y
    m = re.match(r'^(?:在|向|往)\s*(.+?)\s*(?:输入|填写|填入|键入)\s*[:：]?\s*(.+)$', text)
    if m:
        return {"action": "fill", "target": m.group(1).strip(),
                "value": m.group(2).strip(), "raw": text}
    # 输入 Y 到 X
    m = re.match(r'^(?:输入|填写|填入|键入)\s*[:：]?\s*(.+?)\s*(?:到|至|进)\s*(.+)$', text)
    if m:
        return {"action": "fill", "target": m.group(2).strip(),
                "value": m.group(1).strip(), "raw": text}
    # 输入 Y（无目标，输入到当前焦点元素）
    m = re.match(r'^(?:输入|填写|填入|键入)\s*[:：]?\s*(.+)$', text)
    if m:
        return {"action": "fill", "target": "", "value": m.group(1).strip(),
                "raw": text}

    # 选择下拉框：选择 X 为 Y / 选择 X = Y / 选择 X 的值为 Y
    m = re.match(r'^(?:选择|选中)\s*[:：]?\s*(.+?)\s*(?:为|的值为|选项为|=)\s*[:：]?\s*(.+)$', text)
    if m:
        return {"action": "select", "target": m.group(1).strip(),
                "value": m.group(2).strip(), "raw": text}

    # 断言/验证/检查
    m = re.match(r'^(?:验证|断言|检查|确认|校验)\s*[:：]?\s*(.+)$', text)
    if m:
        return {"action": "assert", "expect": m.group(1).strip(), "raw": text}

    # 截图
    if re.match(r'^(?:截图|截屏)', text):
        return {"action": "screenshot", "raw": text}

    # 回车
    if re.match(r'^(?:回车|按回车|按Enter|按enter|按确认键)', text):
        return {"action": "press_enter", "raw": text}

    # 角色登录：超级管理员登录 / 管理员登录 / 普通用户登录 / 用XX登录
    m = re.match(r'^(?:用|使用)?\s*(.+?)\s*(?:账号|账户)?\s*登录$', text)
    if m:
        role = m.group(1).strip()
        # 去掉"账号/账户"后缀
        role = re.sub(r'(账号|账户)$', '', role).strip()
        if role:
            return {"action": "login", "role": role, "raw": text}

    # 无法识别
    return {"action": "unknown", "raw": text}


# ---------------- 元素定位 ----------------

def _clean_target(target: str) -> str:
    """去掉目标描述里的后缀词和括号，得到用于文本匹配的名称。"""
    t = (target or "").strip()
    t = re.sub(r'^(?:点击|点|单击|双击)\s*', '', t)
    # 去掉【】「」""'' 等括号
    t = re.sub(r'[【】「」"“”\'‘’]', '', t)
    # 去掉末尾标点
    t = re.sub(r'[。；;，,、\.]+$', '', t)
    t = re.sub(r'(按钮|输入框|文本框|下拉框|下拉列表|链接|菜单|选项|图标|复选框|单选框|标签|页面|弹窗|窗口|区域|栏)$', '', t)
    return t.strip()


async def locate(page, target: str):
    """根据目标描述定位元素，返回 locator 或 None。"""
    target = (target or "").strip()
    if not target:
        return None
    name = _clean_target(target)
    candidates = []
    if name:
        candidates.append(("文本", page.get_by_text(name, exact=False)))
        candidates.append(("按钮", page.get_by_role("button", name=name)))
        candidates.append(("链接", page.get_by_role("link", name=name)))
        candidates.append(("占位符", page.get_by_placeholder(name)))
        candidates.append(("标签", page.get_by_label(name)))
        candidates.append(("标题", page.get_by_title(name)))
    for kind, loc in candidates:
        try:
            if await loc.count() > 0:
                return loc.first
        except Exception:
            continue
    return None


# ---------------- 预期结果验证 ----------------

async def verify_expectation(page, expect: str) -> tuple:
    """验证预期结果，返回 (是否通过, 说明)。"""
    expect = (expect or "").strip()
    if not expect:
        return True, "无预期结果"

    # 等待页面稳定（SPA 渲染）
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass

    # 跳转到 xxx / URL 包含 xxx
    m = re.search(r'(?:跳转|跳转到|进入|url|网址|地址).{0,8}(?:包含|为|是)\s*[:：]?\s*[「"“\']?(\S+?)[」"”\']?$', expect, re.I)
    if m:
        url = page.url
        ok = m.group(1) in url
        return ok, f"当前 URL：{url}"

    # 页面包含/显示/出现/提示 xxx
    m = re.search(r'(?:包含|显示|出现|展示|提示|存在|可见)\s*[:：]?\s*[「"“\']?(.+?)[」"”\']?$', expect)
    if m:
        text = m.group(1).strip()
        try:
            body = await page.inner_text("body")
        except Exception:
            body = ""
        ok = text in body
        if ok:
            return True, f"页面已出现「{text}」"
        # 失败时给出页面文本片段，便于定位
        snippet = body[:200].replace("\n", " ")
        return False, f"页面未找到「{text}」，页面文本开头：{snippet}"

    # 默认：检查页面文本是否包含预期内容
    try:
        body = await page.inner_text("body")
    except Exception:
        body = ""
    ok = expect in body
    if ok:
        return True, f"页面已出现「{expect}」"
    snippet = body[:200].replace("\n", " ")
    return False, f"页面未找到「{expect}」，页面文本开头：{snippet}"


# ---------------- 自动登录 ----------------

async def do_login(page, account: str, password: str,
                   login_url: str = "") -> tuple:
    """打开登录页，输入账号密码，点击登录。返回 (是否成功, 说明)。"""
    if login_url:
        await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
    # 等待登录表单出现
    try:
        await page.wait_for_selector("#login_name, input[placeholder='用户名'], input[placeholder*='用户名']",
                                     timeout=10000)
    except Exception:
        pass
    # 定位用户名输入框
    name_input = None
    for sel in ("#login_name", "input[placeholder='用户名']",
                "input[placeholder*='用户名']", "input[type='text']"):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                name_input = loc
                break
        except Exception:
            continue
    if name_input is None:
        return False, "找不到用户名输入框"
    await name_input.fill(account)

    # 定位密码输入框
    pwd_input = None
    for sel in ("#login_password", "input[placeholder='密码']",
                "input[placeholder*='密码']", "input[type='password']"):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                pwd_input = loc
                break
        except Exception:
            continue
    if pwd_input is None:
        return False, "找不到密码输入框"
    await pwd_input.fill(password)

    # 点击登录按钮
    btn = None
    for sel in ("button:has-text('登 录')", "button:has-text('登录')",
                "button[type='submit']"):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                btn = loc
                break
        except Exception:
            continue
    if btn is None:
        return False, "找不到登录按钮"
    await btn.click()
    # 等待跳转
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    await asyncio.sleep(1)
    return True, f"已用 {account} 登录"


# ---------------- 用例执行器 ----------------

class UITestRunner:
    """前端 UI 自动化测试执行器。"""

    def __init__(self, emit: Optional[Callable[[dict], None]] = None):
        self.emit = emit
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _emit(self, event: dict) -> None:
        if self.emit:
            self.emit(event)

    async def run_case(self, browser, case: dict, base_url: str = "",
                       headless: bool = True, accounts: dict = None,
                       login_url: str = "", step_interval: float = 0.5) -> dict:
        """执行单条用例，返回结果 dict。

        accounts: {"角色名": {"account": ..., "password": ...}}
        login_url: 登录页地址（自动登录时打开）
        step_interval: 每步之间的间隔秒数（有头模式下便于观察）
        """
        case_id = case.get("id", "")
        name = case.get("name", "")
        steps = case.get("steps", [])
        expect = case.get("expect", "")
        accounts = accounts or {}

        result = {
            "id": case_id,
            "name": name,
            "结果": "❌ 失败",
            "步骤日志": [],
            "预期": expect,
            "实际": "",
            "耗时(秒)": 0,
        }
        t0 = time.perf_counter()

        context = await browser.new_context(
            viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        page.set_default_timeout(15000)

        try:
            # 若配置了 base_url 且第一步不是打开网址，先打开 base_url
            if base_url and steps and not re.match(r'^(打开|访问|进入|跳转|浏览|转到)', steps[0]):
                await page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
                result["步骤日志"].append({"步骤": f"打开 {base_url}", "结果": "✅"})

            for step_text in steps:
                if self._stop:
                    result["步骤日志"].append({"步骤": step_text, "结果": "⏹ 已停止"})
                    break
                step = parse_step(step_text)
                log = {"步骤": step_text, "结果": "✅", "说明": ""}
                try:
                    action = step["action"]
                    if action == "goto":
                        await page.goto(step["url"], wait_until="domcontentloaded",
                                        timeout=30000)
                        log["说明"] = f"已打开 {step['url']}"
                    elif action == "login":
                        role = step["role"]
                        cred = accounts.get(role)
                        if not cred:
                            # 尝试模糊匹配：角色名包含关系
                            for k, v in accounts.items():
                                if role in k or k in role:
                                    cred = v
                                    break
                        if not cred:
                            raise RuntimeError(f"未配置角色「{role}」的账号，请在页面上添加")
                        ok, desc = await do_login(page, cred.get("account", ""),
                                                  cred.get("password", ""), login_url)
                        if not ok:
                            raise RuntimeError(desc)
                        log["说明"] = desc
                    elif action == "wait":
                        await asyncio.sleep(step["seconds"])
                        log["说明"] = f"等待 {step['seconds']} 秒"
                    elif action == "click":
                        loc = await locate(page, step["target"])
                        if loc is None:
                            raise RuntimeError(f"找不到元素：{step['target']}")
                        await loc.click()
                        log["说明"] = f"已点击 {step['target']}"
                    elif action == "fill":
                        value = step["value"]
                        if step["target"]:
                            loc = await locate(page, step["target"])
                            if loc is None:
                                raise RuntimeError(f"找不到输入框：{step['target']}")
                            await loc.fill(value)
                            log["说明"] = f"在 {step['target']} 输入 {value}"
                        else:
                            # 输入到当前焦点元素
                            await page.keyboard.type(value)
                            log["说明"] = f"输入 {value}"
                    elif action == "select":
                        loc = await locate(page, step["target"])
                        if loc is None:
                            raise RuntimeError(f"找不到下拉框：{step['target']}")
                        await loc.select_option(label=step["value"])
                        log["说明"] = f"选择 {step['target']} = {step['value']}"
                    elif action == "assert":
                        ok, desc = await verify_expectation(page, step["expect"])
                        if not ok:
                            raise AssertionError(desc)
                        log["说明"] = desc
                    elif action == "press_enter":
                        await page.keyboard.press("Enter")
                        log["说明"] = "已按回车"
                    elif action == "screenshot":
                        log["说明"] = "已截图（略）"
                    elif action == "noop":
                        log["结果"] = "⚠️"
                        log["说明"] = step.get("desc", "空步骤")
                    else:
                        log["结果"] = "⚠️"
                        log["说明"] = f"无法识别的步骤：{step_text}"
                except Exception as e:
                    log["结果"] = "❌"
                    log["说明"] = f"{type(e).__name__}: {e}"
                    result["步骤日志"].append(log)
                    result["实际"] = log["说明"]
                    break
                result["步骤日志"].append(log)
                # 步骤间隔（有头模式下便于观察）
                if step_interval > 0:
                    await asyncio.sleep(step_interval)

            # 所有步骤执行完后，验证预期结果
            if result["步骤日志"] and result["步骤日志"][-1]["结果"] != "❌":
                ok, desc = await verify_expectation(page, expect)
                result["实际"] = desc
                if ok:
                    result["结果"] = "✅ 通过"
                else:
                    result["结果"] = "❌ 失败"
            elif not result["步骤日志"]:
                ok, desc = await verify_expectation(page, expect)
                result["实际"] = desc
                result["结果"] = "✅ 通过" if ok else "❌ 失败"
        except Exception as e:
            result["实际"] = f"{type(e).__name__}: {e}"
        finally:
            await context.close()

        result["耗时(秒)"] = round(time.perf_counter() - t0, 1)
        return result

    async def run(self, cases: list, base_url: str = "",
                  headless: bool = True, accounts: dict = None,
                  login_url: str = "", step_interval: float = 0.5) -> list:
        """顺序执行所有用例，返回结果列表。"""
        results = []
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=headless)
            except Exception as e:
                # 无图形界面时自动回退到无头模式
                self._emit({"type": "ui_test_warning",
                            "msg": f"有头模式启动失败，已自动切换为无头模式：{type(e).__name__}",
                            "ts": time.time()})
                browser = await p.chromium.launch(headless=True)
            for i, case in enumerate(cases):
                if self._stop:
                    break
                self._emit({"type": "ui_case_start", "index": i,
                            "total": len(cases), "name": case.get("name", ""),
                            "ts": time.time()})
                r = await self.run_case(browser, case, base_url, headless,
                                        accounts, login_url, step_interval)
                results.append(r)
                self._emit({"type": "ui_case_result", "index": i,
                            "total": len(cases), "result": r, "ts": time.time()})
            await browser.close()
        self._emit({"type": "ui_test_done", "results": results,
                    "ts": time.time()})
        return results


# ---------------- Excel 用例解析 ----------------

def parse_excel_cases(content: bytes) -> list:
    """解析 Excel 用例，返回用例列表。

    支持列：用例编号/编号/ID、用例名称/名称/标题、操作步骤/步骤、预期结果/预期/期望。
    操作步骤单元格内可用换行分隔多个步骤。
    """
    import io
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    cases = []
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        header = [str(c).strip() if c is not None else "" for c in rows[0]]
        col_map = {}
        for idx, h in enumerate(header):
            hl = h.lower()
            if any(k in hl for k in ("编号", "id", "序号", "用例编号")):
                col_map.setdefault("id", idx)
            elif any(k in hl for k in ("名称", "标题", "用例名", "name")):
                col_map.setdefault("name", idx)
            elif any(k in hl for k in ("步骤", "操作", "操作步骤", "测试步骤")):
                col_map.setdefault("steps", idx)
            elif any(k in hl for k in ("预期", "期望", "结果", "预期结果")):
                col_map.setdefault("expect", idx)

        for row in rows[1:]:
            if row is None or all(c is None or str(c).strip() == "" for c in row):
                continue
            cells = [str(c).strip() if c is not None else "" for c in row]
            if not col_map:
                # 无表头：按 编号|名称|步骤|预期 顺序
                case_id = cells[0] if len(cells) > 0 else ""
                name = cells[1] if len(cells) > 1 else ""
                steps_text = cells[2] if len(cells) > 2 else ""
                expect = cells[3] if len(cells) > 3 else ""
            else:
                case_id = cells[col_map["id"]] if "id" in col_map else ""
                name = cells[col_map["name"]] if "name" in col_map else ""
                steps_text = cells[col_map["steps"]] if "steps" in col_map else ""
                expect = cells[col_map["expect"]] if "expect" in col_map else ""
            if not name and not steps_text:
                continue
            # 步骤按换行/编号/标点拆分
            steps = split_steps(steps_text)
            cases.append({
                "id": case_id,
                "name": name or case_id or f"用例{len(cases) + 1}",
                "steps": steps,
                "expect": expect,
            })
    return cases
