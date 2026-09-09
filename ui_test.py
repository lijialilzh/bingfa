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
    - 换行分隔（含 <br> 标签）
    - 编号分隔：1. 2. 3. 或 1、2、3、 或 1) 2) 3)
    - 句号/分号分隔（当没有编号时）
    - 逗号/顿号 + 动作词分隔（如「进入登录页面，输入用户名，点击登录」）
    """
    text = (text or "").strip()
    if not text:
        return []
    # 把 <br> / <br/> / <br /> 等 HTML 换行标签转成真实换行
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.I)
    # 先按换行拆
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) > 1:
        steps = []
        for line in lines:
            steps.extend(split_steps(line))
        return steps

    # 按编号拆分：1. 2. 3. / 1、2、3、 / 1) 2) 3) / （1）（2）
    # (?<!\d) 避免在「10.」的 1 和 0 之间误切
    parts = re.split(r'(?<!\d)(?=\d+[\.、\)）]\s*)', text)
    if len(parts) > 1:
        steps = []
        for p in parts:
            p = re.sub(r'^\d+[\.、\)）]\s*', '', p).strip()
            if p:
                steps.append(p)
        return steps

    # 按句号/分号拆分
    parts = re.split(r'[。；;]\s*', text)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) > 1:
        return parts

    # 按「逗号/顿号 + 动作词」拆分（动作词需在逗号/顿号后或句首）
    action_words = ["点击", "单击", "双击", "输入", "填写", "填入", "键入",
                    "打开", "访问", "进入", "跳转", "浏览", "转到",
                    "选择", "选中", "验证", "断言", "检查", "确认", "校验",
                    "等待", "回车", "截图", "截屏"]
    # 找到所有「逗号/顿号后紧跟动作词」或「句首动作词」的位置
    positions = []
    for w in action_words:
        # 句首
        if text.startswith(w):
            positions.append((0, w))
        # 逗号/顿号后
        for m in re.finditer(r'[，,、]\s*' + re.escape(w), text):
            positions.append((m.end() - len(w), w))
    if positions:
        positions.sort()
        # 去重（同一位置）
        seen = set()
        unique = []
        for pos, w in positions:
            if pos not in seen:
                seen.add(pos)
                unique.append((pos, w))
        if len(unique) > 1:
            steps = []
            # 第一个动作词之前的前缀（如「进入登录页面」）也保留
            if unique[0][0] > 0:
                prefix = text[:unique[0][0]].strip()
                prefix = re.sub(r'[，,、]+$', '', prefix).strip()
                if prefix:
                    steps.append(prefix)
            for i, (pos, w) in enumerate(unique):
                end = unique[i + 1][0] if i + 1 < len(unique) else len(text)
                seg = text[pos:end].strip()
                seg = re.sub(r'[，,、]+$', '', seg).strip()
                if seg:
                    steps.append(seg)
            return steps

    return [text]


# ---------------- 自然语言步骤解析 ----------------

def parse_step(text: str) -> dict:
    """把自然语言步骤解析成结构化操作。"""
    text = (text or "").strip()
    if not text:
        return {"action": "noop", "raw": text}

    # 去掉开头的带圈数字前缀（① ② ③ 等）
    text = re.sub(r'^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]\s*', '', text).strip()
    if not text:
        return {"action": "noop", "raw": text}

    # 打开/访问 URL
    m = re.search(r'(https?://\S+)', text)
    if m and re.match(r'^(打开|访问|进入|跳转|浏览|转到)', text):
        return {"action": "goto", "url": m.group(1), "raw": text}

    # 描述性文字（进入 xxx 页面 / 打开 xxx 页面，无 URL）：跳过
    if re.match(r'^(进入|打开|访问|跳转|浏览|转到|入)\s*.{0,20}(页面|界面|系统|网站)$', text):
        return {"action": "noop", "raw": text, "desc": "描述性步骤，跳过"}
    # 输入登录地址 / 输入网址 等描述性文字：跳过
    if re.match(r'^(输入登录地址|输入网址|输入地址|打开登录地址)$', text):
        return {"action": "noop", "raw": text, "desc": "描述性步骤，跳过"}
    # 无操作 / 不做任何操作：跳过
    if re.match(r'^(无操作|不做任何操作|不进行任何操作)', text):
        return {"action": "noop", "raw": text, "desc": "无操作，跳过"}
    # 描述性校验场景（无法确定具体输入值）：跳过
    if re.match(r'^(密码限制校验|长度限制校验|格式校验|边界值校验)', text):
        return {"action": "noop", "raw": text, "desc": "描述性校验场景，跳过"}
    # 标题（冒号结尾且不含动作词，如「用户首次登录修改密码验证：」）：跳过
    if text.rstrip().endswith(("：", ":")):
        _action_words = ["点击", "单击", "双击", "输入", "填写", "填入", "键入",
                         "打开", "访问", "进入", "跳转", "浏览", "转到",
                         "选择", "选中", "断言", "检查", "确认", "校验",
                         "等待", "回车", "截图", "截屏"]
        if not any(w in text for w in _action_words):
            return {"action": "noop", "raw": text, "desc": "标题，跳过"}

    # 复合步骤：描述前缀 + 点击【XX】按钮（如「内容不做输入，点击【确认修改】按钮」）
    m = re.search(r'点击\s*[:：]?\s*[【\[](.+?)[】\]]\s*(?:按钮|链接|菜单|图标)?', text)
    if m and re.search(r'点击', text):
        target = m.group(1).strip()
        if re.search(r'按钮', text):
            target = target + "按钮"
        return {"action": "click", "target": target, "raw": text}

    # 等待 N 秒
    m = re.match(r'^等待\s*(\d+(?:\.\d+)?)\s*秒?', text)
    if m:
        return {"action": "wait", "seconds": float(m.group(1)), "raw": text}

    # 点击【XX】按钮 / 点击 XX 按钮 / 点击 XX
    m = re.match(r'^(?:点击|点|单击|双击)\s*[:：]?\s*[【\[]?(.+?)[】\]]?\s*(?:按钮|链接|菜单|图标)?\s*[。；;]?$', text)
    if m:
        target = m.group(1).strip()
        target = re.sub(r'[。；;]+$', '', target).strip()
        # 若原文有「按钮」后缀，保留
        if re.search(r'按钮', text):
            target = target + "按钮"
        return {"action": "click", "target": target, "raw": text}

    # 输入：在 X 输入 Y / 向 X 输入 Y / 在 X 填写 Y
    # X 不能包含逗号/顿号（避免把「在进入登录页面，输入...」误解析成 fill）
    m = re.match(r'^(?:在|向|往)\s*([^，,、]+?)\s*(?:输入|填写|填入|键入)\s*[:：]?\s*(.+)$', text)
    if m:
        return {"action": "fill", "target": m.group(1).strip(),
                "value": m.group(2).strip(), "raw": text}
    # 输入 Y 到 X
    m = re.match(r'^(?:输入|填写|填入|键入)\s*[:：]?\s*(.+?)\s*(?:到|至|进)\s*(.+)$', text)
    if m:
        return {"action": "fill", "target": m.group(2).strip(),
                "value": m.group(1).strip(), "raw": text}
    # 输入正确的用户名，正确密码 → 用账号配置登录
    if re.search(r'输入正确的用户名|输入用户名.*密码|输入.*用户名.*密码', text):
        return {"action": "login_auto", "raw": text}
    # 用户名或密码输入错误 → 输入错误密码
    if re.search(r'用户名或密码输入错误|密码输入错误|用户名输入错误', text):
        return {"action": "login_wrong", "raw": text}
    # 用户名或密码未输入 → 直接点击登录（不输入）
    if re.search(r'用户名或密码未输入|未输入.*点击|不输入.*点击', text):
        return {"action": "login_empty", "raw": text}
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
    """根据目标描述定位元素，返回 locator 或 None。

    优先匹配可点击元素（按钮/菜单/链接），避免匹配到面包屑等纯文本。
    """
    target = (target or "").strip()
    if not target:
        return None
    name = _clean_target(target)
    candidates = []
    if name:
        # 优先可点击元素
        candidates.append(("按钮", page.get_by_role("button", name=name)))
        candidates.append(("链接", page.get_by_role("link", name=name)))
        candidates.append(("菜单项", page.locator(f".ant-menu-item:has-text('{name}')")))
        candidates.append(("菜单标题", page.locator(f".ant-menu-submenu-title:has-text('{name}')")))
        candidates.append(("占位符", page.get_by_placeholder(name)))
        candidates.append(("标签", page.get_by_label(name)))
        candidates.append(("标题", page.get_by_title(name)))
        # 最后才用纯文本（可能匹配到面包屑等不可点击元素）
        candidates.append(("文本", page.get_by_text(name, exact=False)))
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

async def fill_login_form(page, account: str, password: str,
                          login_url: str = "") -> tuple:
    """打开登录页，只输入账号密码（不点击登录）。返回 (是否成功, 说明)。"""
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
    return True, f"已输入 {account} 的账号密码"


async def do_login(page, account: str, password: str,
                   login_url: str = "") -> tuple:
    """打开登录页，输入账号密码，点击登录。返回 (是否成功, 说明)。"""
    ok, desc = await fill_login_form(page, account, password, login_url)
    if not ok:
        return False, desc
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
    # 等待跳转：轮询 URL 变化（登录跳转可能需要 3-5 秒）
    login_url_before = page.url
    for _ in range(15):
        await asyncio.sleep(0.5)
        if page.url != login_url_before and "login" not in page.url:
            break
    # 等待页面内容加载（SPA 渲染）
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    await asyncio.sleep(1)
    return True, f"已用 {account} 登录"


async def _click_login_button(page) -> None:
    """点击登录按钮（不输入账号密码）。"""
    for sel in ("button:has-text('登 录')", "button:has-text('登录')",
                "button[type='submit']"):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await loc.click()
                await asyncio.sleep(1)
                return
        except Exception:
            continue
    raise RuntimeError("找不到登录按钮")


# ---------------- 自动探索 ----------------

async def explore_page(page, max_items: int = 50) -> list:
    """自动探索页面：发现所有可点击的菜单/按钮/链接，逐个点击验证。

    返回 [{"名称": ..., "结果": "✅"/"❌", "说明": ...}]
    """
    results = []
    # 等待页面稳定
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    await asyncio.sleep(1)

    # 收集可点击元素：菜单项、导航链接、按钮（含 iframe 内）
    items = []
    seen = set()

    def is_meaningful(text: str) -> bool:
        """过滤无关元素：纯数字、日期、图标、危险按钮等。"""
        t = text.strip()
        if not t or len(t) > 30:
            return False
        # 纯数字/日期/时间
        if re.fullmatch(r'[\d\-/:\.\s]+', t):
            return False
        # 单个字符
        if len(t) <= 1:
            return False
        # 危险按钮：退出登录、返回、关闭、删除等
        if re.search(r'(退出|登出|注销|返回|关闭|删除|移除|清空|重置)', t):
            return False
        return True

    async def collect(locator, kind):
        try:
            n = await locator.count()
            for i in range(min(n, max_items)):
                try:
                    el = locator.nth(i)
                    text = (await el.inner_text()).strip()
                    if not is_meaningful(text) or text in seen:
                        continue
                    seen.add(text)
                    items.append({"text": text, "kind": kind, "loc": el})
                except Exception:
                    continue
        except Exception:
            pass

    await collect(page.locator("menuitem"), "菜单")
    await collect(page.locator(".ant-menu-item, .ant-menu-submenu-title"), "菜单")
    await collect(page.locator("nav a, .nav-item, .menu-item"), "导航")
    await collect(page.locator("button"), "按钮")

    # 收集 iframe 内的元素
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            await collect(frame.locator("menuitem"), "菜单")
            await collect(frame.locator(".ant-menu-item, .ant-menu-submenu-title"), "菜单")
            await collect(frame.locator("nav a, .nav-item, .menu-item"), "导航")
            await collect(frame.locator("button"), "按钮")
        except Exception:
            pass

    for item in items:
        try:
            # 先关闭可能的弹窗/遮罩
            await _close_modals(page)
            # 记录点击前的 URL
            url_before = page.url
            # 用 JS 点击，绕过遮罩拦截
            try:
                await item["loc"].click(timeout=3000)
            except Exception:
                # 点击失败时用 JS 强制点击
                await item["loc"].evaluate("el => el.click()")
            await asyncio.sleep(1)
            # 若点击后跳转到了登录页（可能误点了退出），停止探索
            if "login" in page.url and url_before != page.url:
                results.append({"名称": item["text"], "结果": "⚠️",
                                "说明": "点击后跳转到登录页，停止探索"})
                break
            # 若弹出弹窗，记录并关闭弹窗（避免遮罩挡住后续点击）
            modal_text = await _get_modal_text(page)
            if modal_text:
                results.append({"名称": item["text"], "结果": "✅",
                                "说明": f"点击后弹出弹窗：{modal_text[:50]}"})
                await _close_modals(page)
                continue
            # 检查页面是否正常（有内容、无崩溃）
            body = await page.inner_text("body")
            if body.strip():
                results.append({"名称": item["text"], "结果": "✅",
                                "说明": f"点击后页面正常"})
            else:
                results.append({"名称": item["text"], "结果": "❌",
                                "说明": "点击后页面空白"})
        except Exception as e:
            results.append({"名称": item["text"], "结果": "❌",
                            "说明": f"{type(e).__name__}"})
    return results


async def _get_modal_text(page) -> str:
    """检测当前是否有弹窗，返回弹窗文本（无弹窗返回空字符串）。"""
    for sel in (".ant-modal", ".ant-modal-content", "[role='dialog']",
                ".modal", ".tx_modal__body"):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                if await loc.is_visible():
                    return (await loc.inner_text()).strip()
        except Exception:
            pass
    return ""


async def _close_modals(page) -> None:
    """尝试关闭页面上的弹窗/遮罩。"""
    # 常见关闭按钮
    for sel in (".ant-modal-close", ".ant-modal .ant-modal-close",
                "button[aria-label='Close']", "button[aria-label='close']",
                ".modal-close", ".close", "button:has-text('关 闭')",
                "button:has-text('关闭')", "button:has-text('取 消')",
                "button:has-text('取消')"):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await loc.click(timeout=2000)
                await asyncio.sleep(0.3)
        except Exception:
            pass
    # 按 ESC 关闭
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.3)
    except Exception:
        pass


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
        # 对每个步骤先拆分（支持「进入登录页面，输入...，点击...」这种一句话多步骤）
        expanded_steps = []
        for s in steps:
            expanded_steps.extend(split_steps(s))
        steps = expanded_steps

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
                # 自动探索：遍历页面所有功能
                if re.search(r'(自动探索|遍历所有功能|探索所有功能|自动遍历|遍历功能)', step_text):
                    log = {"步骤": step_text, "结果": "✅", "说明": ""}
                    try:
                        explored = await explore_page(page)
                        passed = sum(1 for x in explored if x["结果"] == "✅")
                        log["说明"] = f"探索 {len(explored)} 个功能，{passed} 个正常"
                        result["步骤日志"].append(log)
                        # 把探索明细追加到结果
                        result.setdefault("探索明细", []).extend(explored)
                        continue
                    except Exception as e:
                        log["结果"] = "❌"
                        log["说明"] = f"{type(e).__name__}: {e}"
                        result["步骤日志"].append(log)
                        result["实际"] = log["说明"]
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
                    elif action == "login_auto":
                        # 输入正确的用户名密码 → 只填表单，不点击登录（后续步骤会点击）
                        if accounts:
                            first_role = list(accounts.keys())[0]
                            cred = accounts[first_role]
                            ok, desc = await fill_login_form(page, cred.get("account", ""),
                                                             cred.get("password", ""), login_url)
                            if not ok:
                                raise RuntimeError(desc)
                            log["说明"] = desc
                        else:
                            raise RuntimeError("未配置账号，请在页面上添加")
                    elif action == "login_wrong":
                        # 用户名或密码错误 → 输入错误密码
                        ok, desc = await do_login(page, "wronguser", "wrongpass", login_url)
                        log["说明"] = f"已输入错误账号密码（{desc}）"
                    elif action == "login_empty":
                        # 用户名或密码未输入 → 直接点击登录
                        await _click_login_button(page)
                        log["说明"] = "未输入账号密码，直接点击登录"
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

def split_cases_by_number(steps_text: str, mode: str = "steps") -> list:
    """把包含多个编号场景的步骤文本拆分成多个独立场景。

    mode:
    - "steps"：操作步骤。数字编号+带圈数字（如「2. ① 点击...」）→ 去掉圈数字，
      作为子步骤合并到当前场景（数字编号和圈数字重复时去掉圈）。
    - "expect"：预期结果。数字编号+带圈数字（如「5. ①修改密码成功」）→ 父级，
      吞并后续所有内容。

    层级规则：
    - 数字编号（1. 2. 3. / 1、2、3、 / 1) 2) 3) / (1) (2)）→ 顶级场景
    - 纯带圈数字行（① ② ③，无数字前缀）→ 子步骤，合并到当前场景
    - 父级标题（冒号结尾）→ 吞并后续所有内容

    返回 [{"编号": ..., "内容": ...}]
    """
    text = (steps_text or "").strip()
    if not text:
        return []
    # 把 <br> 转成换行
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.I)

    # 顶级编号：1. 1、 1) (1)
    top_pattern = re.compile(r'^(\d+[\.、\)）]\s*|\(\d+\)\s*)')
    # 子编号：① ② ③
    sub_pattern = re.compile(r'^([①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]\s*)')
    # 去掉开头的带圈数字
    sub_strip = re.compile(r'^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]\s*')

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    scenes = []
    current = None  # 当前场景
    swallow = False  # 当前场景是否为「吞并一切」的父级
    for line in lines:
        # 父级吞并模式：所有行都合并到父级
        if swallow:
            current["内容"] += "\n" + line
            continue
        # 纯带圈数字行（无数字前缀）→ 子步骤，合并到当前场景
        m_sub = sub_pattern.match(line)
        if m_sub:
            if current is None:
                current = {"编号": "", "内容": ""}
            sub_num = m_sub.group(1).strip()
            content = line[m_sub.end():].strip()
            if current["内容"]:
                current["内容"] += "\n" + sub_num + " " + content
            else:
                current["内容"] = sub_num + " " + content
            continue
        # 数字编号 → 新场景
        m_top = top_pattern.match(line)
        if m_top:
            num = m_top.group(1).strip()
            content = line[m_top.end():].strip()
            # 内容带圈数字开头（数字编号和圈数字重复）
            if sub_pattern.match(content):
                if mode == "steps":
                    # 去掉圈数字，作为子步骤合并到当前场景
                    content = sub_strip.sub('', content).strip()
                    if current is not None:
                        current["内容"] += "\n" + content
                    else:
                        current = {"编号": num, "内容": content}
                    continue
                else:
                    # expect 模式：父级，吞并后续
                    if current is not None:
                        scenes.append(current)
                    current = {"编号": num, "内容": content}
                    swallow = True
                    continue
            # 新场景
            if current is not None:
                scenes.append(current)
            current = {"编号": num, "内容": content}
            # 父级标题：冒号结尾
            if content.rstrip().endswith(("：", ":")):
                swallow = True
            continue
        # 无编号续行
        if current is not None:
            if current["内容"]:
                current["内容"] += "\n" + line
            else:
                current["内容"] = line
        else:
            current = {"编号": "", "内容": line}
    if current is not None:
        scenes.append(current)

    # 如果没有识别到多个顶级编号，返回空（表示不需要拆分）
    numbered = [s for s in scenes if s["编号"]]
    if len(numbered) < 2:
        return []
    return scenes


def extract_roles(precondition: str) -> list:
    """从前置条件里提取多个角色名。

    如「超级管理员、医院管理员、普通用户已存在」→ ['超级管理员', '医院管理员', '普通用户']
    「普通用户已登录」→ ['普通用户']
    """
    if not precondition:
        return []
    text = precondition
    # 去掉状态词
    text = re.sub(r'(已存在|已登录|已注册|已创建|已配置|已添加|已开通)', '', text)
    # 按顿号/逗号/分号/空格/换行分隔
    parts = re.split(r'[、，,;；\s]+', text)
    roles = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if p not in roles:
            roles.append(p)
    return roles


def split_by_roles(case: dict) -> list:
    """按前置条件里的多个角色拆分成多条用例，每条用例用对应角色登录。

    前置条件如「超级管理员、医院管理员、普通用户已存在」→ 拆成 3 条用例，
    每条用例步骤开头插入对应角色的登录步骤。
    """
    roles = extract_roles(case.get("precondition", ""))
    if len(roles) <= 1:
        return [case]
    result = []
    for role in roles:
        steps = list(case.get("steps", []))
        # 找到登录步骤，替换为对应角色；否则在开头插入
        login_idx = None
        for i, s in enumerate(steps):
            if re.search(r'登录\s*$', s.strip()):
                login_idx = i
                break
        if login_idx is not None:
            steps[login_idx] = f"{role}登录"
        else:
            steps = [f"{role}登录"] + steps
        result.append({
            **case,
            "name": f"{case['name']}（{role}）",
            "steps": steps,
        })
    return result


def parse_excel_cases(content: bytes) -> list:
    """解析 Excel 用例，返回用例列表。

    支持列：用例编号/编号/ID、用例名称/名称/标题、操作步骤/步骤、预期结果/预期/期望。
    操作步骤单元格内可用换行分隔多个步骤。
    若操作步骤包含多个编号场景（1. 2. 3. 或 ①②③），自动拆分成多条独立用例。
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
            if any(k in hl for k in ("用例号", "编号", "id", "序号", "用例编号")):
                col_map.setdefault("id", idx)
            elif any(k in hl for k in ("功能模块", "模块", "名称", "标题", "用例名", "name")):
                col_map.setdefault("name", idx)
            elif any(k in hl for k in ("前置条件", "前置", "前提", "precondition")):
                col_map.setdefault("precondition", idx)
            elif any(k in hl for k in ("步骤", "操作", "操作步骤", "测试步骤")):
                col_map.setdefault("steps", idx)
            elif any(k in hl for k in ("预期", "期望", "结果", "预期结果")):
                col_map.setdefault("expect", idx)

        for row in rows[1:]:
            if row is None or all(c is None or str(c).strip() == "" for c in row):
                continue
            cells = [str(c).strip() if c is not None else "" for c in row]
            if not col_map:
                # 无表头：按 编号|名称|前置条件|步骤|预期 顺序
                case_id = cells[0] if len(cells) > 0 else ""
                name = cells[1] if len(cells) > 1 else ""
                precondition = cells[2] if len(cells) > 2 else ""
                steps_text = cells[3] if len(cells) > 3 else ""
                expect = cells[4] if len(cells) > 4 else ""
            else:
                case_id = cells[col_map["id"]] if "id" in col_map else ""
                name = cells[col_map["name"]] if "name" in col_map else ""
                precondition = cells[col_map["precondition"]] if "precondition" in col_map else ""
                steps_text = cells[col_map["steps"]] if "steps" in col_map else ""
                expect = cells[col_map["expect"]] if "expect" in col_map else ""
            if not name and not steps_text:
                continue

            # 先拆预期结果：预期结果能拆出多个场景时，步骤编号才是场景编号
            expect_scenes = split_cases_by_number(expect, mode="expect") if expect else []
            # 只有预期结果拆出多个场景时，才按编号拆步骤场景；
            # 否则步骤编号是「步骤序号」，作为一条用例的多个步骤
            scenes = split_cases_by_number(steps_text, mode="steps") if expect_scenes else []
            if scenes:
                for i, scene in enumerate(scenes):
                    # 每个场景拆成步骤
                    steps = split_steps(scene["内容"])
                    scene_name = name or case_id or f"用例{len(cases) + 1}"
                    if scene["编号"]:
                        scene_name = f"{scene_name} - {scene['编号']}"
                    # 匹配对应的预期结果：按编号匹配，否则按顺序匹配
                    scene_expect = expect
                    if expect_scenes:
                        # 优先按编号匹配
                        matched = None
                        for es in expect_scenes:
                            if es["编号"] and es["编号"] == scene["编号"]:
                                matched = es["内容"]
                                break
                        if matched is None and i < len(expect_scenes):
                            matched = expect_scenes[i]["内容"]
                        if matched:
                            scene_expect = matched
                    # 去掉预期结果开头的编号前缀（如「1. 」）
                    scene_expect = re.sub(r'^\d+[\.、\)）]\s*', '', scene_expect).strip()
                    cases.extend(split_by_roles({
                        "id": case_id,
                        "name": scene_name,
                        "precondition": precondition,
                        "steps": steps,
                        "expect": scene_expect,
                    }))
            else:
                # 步骤按换行/编号/标点拆分
                steps = split_steps(steps_text)
                cases.extend(split_by_roles({
                    "id": case_id,
                    "name": name or case_id or f"用例{len(cases) + 1}",
                    "precondition": precondition,
                    "steps": steps,
                    "expect": expect,
                }))
    return cases
