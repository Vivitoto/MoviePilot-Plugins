"""mox 站点的浏览器侧操作：登录、过 Turnstile、签到。

这个模块只做一件事 —— 用 cloakbrowser 完成那些纯 requests 做不到的动作，
然后把结果（cookie / 签到响应）交回给插件主逻辑。

为什么必须用真浏览器：
    登录接口要求 Cloudflare Turnstile token，这个 token 由 CF 签发给真实浏览器
    环境，无法用 requests 伪造；签到接口要图片验证码，而图片本身也要靠浏览器
    带着 cf_clearance 才能取到。
    MoviePilot 镜像自带 cloakbrowser（98 号签到插件已在用），
    它的指纹伪装（webdriver=false、真实 UA、正确时区/分辨率）能通过 CF 检测。

调用方拿到的 cookie 可以直接喂给 requests.Session，
后续的用户信息刷新、资产统计等请求仍然走 requests，不必再开浏览器。
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

BASE_URL_DEFAULT = "https://mox.moxing.chat"
SITEKEY = "0x4AAAAAAAw2Adjoweo1w6v7"   # 站点页面里写死的 Turnstile sitekey
LOGIN_PATH = "/login"
SIGN_PATH = "/forum/sign"
LOGIN_API = "/api/forum/login"
SIGN_API = "/api/forum/check-in/sign"
CAPTCHA_API = "/api/forum/captcha/generate"

WAIT_TURNSTILE = 45
WAIT_TOKEN = 60
CAPTCHA_MAX_TRIES = 5     # 验证码识别错就换一张重来，超过就放弃


@dataclass
class BrowserResult:
    """浏览器侧动作的结果。"""
    ok: bool = False
    status: str = "unknown"     # ok / already / login / net / token / submit / error
    message: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    token_len: int = 0
    http_status: Optional[int] = None
    response: str = ""
    rewards: list = field(default_factory=list)   # 页面 props.rewardsReal，用于把奖励索引翻译成文本
    steps: list[str] = field(default_factory=list)

    def add(self, msg: str) -> None:
        self.steps.append(msg)


def _noop_log(_msg: str) -> None:
    pass


def _launch_browser(proxy: str = "", log: Callable[[str], None] = _noop_log):
    """启动 cloakbrowser，返回 (browser, context, page)。

    启动参数是实测能通过 CF 的组合；改动任何一项都可能让指纹判断失败。
    代理参数有两种传递方式，cloakbrowser 的不同版本接受的形式不同，两个都试。
    """
    try:
        from cloakbrowser import launch
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise RuntimeError(
            "cloakbrowser 不可用，无法通过 Cloudflare Turnstile。"
            "MoviePilot 镜像应自带该库；本地开发需要 pip install cloakbrowser。"
        ) from exc
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"cloakbrowser 导入失败：{exc}") from exc

    proxy = (proxy or "").strip()
    attempts: list[tuple[str, dict]] = []
    if proxy:
        attempts.append(("proxy str", {"headless": True, "proxy": proxy,
                                       "timezone": "Asia/Shanghai", "locale": "zh-CN"}))
        attempts.append(("proxy dict", {"headless": True, "proxy": {"server": proxy},
                                        "timezone": "Asia/Shanghai", "locale": "zh-CN"}))
        attempts.append(("args", {"headless": True, "args": [f"--proxy-server={proxy}"],
                                  "timezone": "Asia/Shanghai", "locale": "zh-CN"}))
    else:
        attempts.append(("plain", {"headless": True,
                                   "timezone": "Asia/Shanghai", "locale": "zh-CN"}))

    last_err = "no attempt"
    for label, kwargs in attempts:
        try:
            browser = launch(**kwargs)
        except TypeError as exc:
            # 参数形式不被接受，换下一种
            last_err = f"{label}: {exc}"
            continue
        except Exception as exc:
            last_err = f"{label}: {exc}"
            continue

        try:
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
        except Exception as exc:
            last_err = f"{label} context: {exc}"
            try:
                browser.close()
            except Exception:
                pass
            continue

        page = context.new_page()
        log(f"浏览器已启动（{label}）")
        return browser, context, page

    raise RuntimeError(f"cloakbrowser 启动失败：{last_err}")


def _collect_cookies(context) -> dict[str, str]:
    """把浏览器 cookie 导出成 requests 能直接用的 name->value 字典。"""
    out: dict[str, str] = {}
    try:
        for ck in context.cookies():
            name = ck.get("name")
            value = ck.get("value")
            if name and value is not None:
                out[str(name)] = str(value)
    except Exception:
        pass
    return out


def _wait_turnstile(page, limit: int = WAIT_TURNSTILE) -> bool:
    deadline = time.time() + limit
    while time.time() < deadline:
        try:
            if page.evaluate("typeof window.turnstile") == "object":
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def _grab_token(page, limit: int = WAIT_TOKEN) -> str:
    """渲染一个独立控件取 Turnstile token。

    先看页面上有没有已经渲染好的 token（站点自己的 turnstile-box 可能已经完成），
    没有再自己渲染一个。渲染到独立 div，不去碰页面自己的控件，
    避免触发站点自身的提交流程。
    """
    try:
        existing = page.evaluate("""() => {
          const el = document.querySelector('input[name="cf-turnstile-response"]');
          return el ? (el.value || '') : '';
        }""")
    except Exception:
        existing = ""
    if existing:
        return str(existing)

    page.evaluate(f"""() => {{
      const old = document.getElementById('__mox_ts');
      if (old) old.remove();
      const box = document.createElement('div');
      box.id = '__mox_ts';
      box.style.cssText =
        'position:fixed;top:8px;left:8px;z-index:2147483647;background:#fff;padding:8px';
      document.body.appendChild(box);
      window.__mox_tok = '';
      window.__mox_err = '';
      try {{
        window.turnstile.render(box, {{
          sitekey: '{SITEKEY}',
          callback: t => {{ window.__mox_tok = t; }},
          'error-callback': e => {{ window.__mox_err = String(e); }},
        }});
      }} catch (e) {{ window.__mox_err = e.message; }}
    }}""")

    deadline = time.time() + limit
    while time.time() < deadline:
        time.sleep(2)
        try:
            tok = page.evaluate("window.__mox_tok || ''")
        except Exception:
            tok = ""
        if tok:
            return str(tok)
    return ""


def _verify_logged_in(page, base_url: str, timeout: int = 60) -> bool:
    """用页面跳转判断是否真的登录了。

    不能靠 cookie 是否存在来判断 —— 站点对游客也会发 mox_session，
    真正的判据是访问签到页会不会被踢回 /login。
    """
    try:
        page.goto(f"{base_url}{SIGN_PATH}", wait_until="domcontentloaded",
                  timeout=timeout * 1000)
        time.sleep(3)
        return "/login" not in (page.url or "")
    except Exception:
        return False


def _apply_resource_filter(page) -> None:
    """拦掉图片/字体/媒体请求。

    签到和登录只关心 HTML、JS 和 API，图片字体纯属浪费流量。
    站点会对同一出口 IP 限流，一次页面加载几十个请求很容易触发，
    砍掉这些能显著降低被拒的概率。
    """
    try:
        def _route(route):
            try:
                if route.request.resource_type in ("image", "font", "media"):
                    return route.abort()
            except Exception:
                pass
            return route.continue_()
        page.route("**/*", _route)
    except Exception:
        pass


def _read_page_props(page) -> dict:
    """取出 Inertia 页面 data-page 里的 props。

    签到页把奖励表放在 props.rewardsReal，签到接口只返回奖励下标，
    需要拿这张表把下标翻译成奖励名称。
    """
    try:
        props = page.evaluate("""() => {
          const el = document.getElementById('app') || document.querySelector('[data-page]');
          if (!el) return null;
          const raw = el.getAttribute('data-page');
          if (!raw) return null;
          try { return JSON.parse(raw).props || null; } catch (e) { return null; }
        }""")
        return props if isinstance(props, dict) else {}
    except Exception:
        return {}


def _ocr_available() -> bool:
    """本地 OCR 是否可用（ddddocr 未安装时插件仍能跑，只是签到拿不到验证码答案）。"""
    try:
        import ddddocr  # noqa: F401
        return True
    except Exception:
        return False


_OCR_ENGINE = None


def _ocr_classify(png: bytes) -> str:
    """识别验证码图片，返回文字。失败返回空串。

    模型首次加载约 0.3 秒，之后单张识别约 30 毫秒，所以缓存引擎实例。
    """
    global _OCR_ENGINE
    if not png:
        return ""
    try:
        if _OCR_ENGINE is None:
            import ddddocr
            _OCR_ENGINE = ddddocr.DdddOcr(show_ad=False)
        return (_OCR_ENGINE.classification(png) or "").strip()
    except Exception:
        return ""


def _dismiss_age_gate(page, log: Callable[[str], None] = _noop_log) -> None:
    """处理 21+ 年龄声明弹窗。点不到就算了，不影响后续。"""
    try:
        clicked = page.evaluate("""() => {
          const all = [...document.querySelectorAll('button')]
            .filter(b => b.offsetParent !== null);
          const t = all.find(b => /我已满21岁|I am 21|已满21/.test(b.innerText || ''));
          if (t) { t.click(); return true; }
          return false;
        }""")
        if clicked:
            log("已确认年龄声明")
            time.sleep(3)
    except Exception:
        pass


def _enter_login_form(page, log: Callable[[str], None] = _noop_log) -> None:
    """登录页有个前置入口页，点掉它才会显示表单。"""
    try:
        clicked = page.evaluate("""() => {
          const all = [...document.querySelectorAll('button')]
            .filter(b => b.offsetParent !== null);
          const t = all.find(b => /点我开始|进入网站|开始魔性/.test(b.innerText || ''));
          if (t) { t.click(); return true; }
          return false;
        }""")
        if clicked:
            log("已进入登录表单")
            time.sleep(4)
    except Exception:
        pass


def login_with_browser(
    base_url: str = BASE_URL_DEFAULT,
    username: str = "",
    password: str = "",
    proxy: str = "",
    remember: bool = True,
    timeout: int = 60,
    log: Callable[[str], None] = _noop_log,
) -> BrowserResult:
    """用账号密码登录，返回可复用的 cookie。

    登录接口要的是图片验证码，不是 Turnstile：
        POST /api/forum/captcha/generate -> {key, img}
        POST /api/forum/login            -> {captcha, captcha_key, username, password,
                                             sec, secanswer, remember}

    验证码必须自己取、自己提交。登录页加载时自己也会取一张，
    如果交给页面去提交，它用的是它那张的 key，和我们识别的那张对不上，必然失败。
    """
    result = BrowserResult()
    if not username or not password:
        result.status = "login"
        result.message = "未配置账号密码"
        return result

    browser = context = page = None
    try:
        browser, context, page = _launch_browser(proxy, log)
        _apply_resource_filter(page)

        log("打开登录页")
        try:
            page.goto(f"{base_url}{LOGIN_PATH}", wait_until="domcontentloaded",
                      timeout=timeout * 1000)
        except Exception as exc:
            result.status = "net"
            result.message = f"登录页打不开：{type(exc).__name__}: {str(exc)[:100]}"
            return result

        time.sleep(4)
        _enter_login_form(page, log)
        _dismiss_age_gate(page, log)
        time.sleep(2)

        if not _ocr_available():
            result.status = "token"
            result.message = "未安装 ddddocr，无法自动识别登录验证码"
            return result
        result.add("本地 OCR 可用")

        last_code = -1
        last_detail = ""
        solved = False

        for attempt in range(1, CAPTCHA_MAX_TRIES + 1):
            cap = page.evaluate("""async (api) => {
              try {
                const r = await fetch(api, {credentials: 'include',
                  headers: {'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}});
                const j = await r.json();
                return {status: r.status, key: j.key || '', img: j.img || j.image || ''};
              } catch (e) { return {status: -1, key: '', img: '', err: String(e)}; }
            }""", CAPTCHA_API)

            key = str(cap.get("key") or "")
            img = str(cap.get("img") or "")
            if not key or not img:
                result.status = "token"
                result.message = f"取登录验证码失败（HTTP {cap.get('status')}）"
                return result

            try:
                png = base64.b64decode(img.split(",", 1)[-1])
            except Exception as exc:
                result.status = "token"
                result.message = f"验证码图片解码失败：{type(exc).__name__}"
                return result

            answer = _ocr_classify(png)
            if not answer or not (4 <= len(answer) <= 6):
                result.add(f"第 {attempt} 次识别异常：{answer!r}")
                log(f"识别异常：{answer!r}")
                time.sleep(1.0)
                continue

            result.add(f"第 {attempt} 次识别：{answer}")
            log(f"登录验证码识别：{answer}")

            res = page.evaluate("""async (args) => {
              const [api, key, ans, user, pw, remember] = args;
              try {
                const m = document.cookie.match(/XSRF-TOKEN=([^;]+)/);
                const xsrf = m ? decodeURIComponent(m[1]) : '';
                const r = await fetch(api, {
                  method: 'POST',
                  headers: {'Content-Type': 'application/json', 'Accept': 'application/json',
                            'X-XSRF-TOKEN': xsrf, 'X-Requested-With': 'XMLHttpRequest'},
                  body: JSON.stringify({captcha: ans, captcha_key: key,
                                        username: user, password: pw,
                                        sec: null, secanswer: '', remember: !!remember}),
                  credentials: 'include',
                });
                return {status: r.status, body: (await r.text()).slice(0, 600)};
              } catch (e) { return {status: -1, body: 'fetch error: ' + e.message}; }
            }""", [LOGIN_API, key, answer, username, password, remember])

            last_code = int(res.get("status", -1))
            body = str(res.get("body", ""))
            result.http_status = last_code
            result.response = body
            result.add(f"登录 HTTP {last_code}")
            log(f"登录 HTTP {last_code}")

            try:
                parsed = json.loads(body)
                detail = f"{parsed.get('message') or ''} {parsed.get('errors') or ''}"
            except Exception:
                detail = body
            last_detail = detail

            if last_code == 200:
                solved = True
                break
            if "验证码" not in detail:
                break
            time.sleep(1.5)

        result.cookies = _collect_cookies(context)

        if not solved:
            result.status = "login"
            if last_detail and "验证码" in last_detail:
                result.message = f"登录验证码连续 {CAPTCHA_MAX_TRIES} 次未通过"
            else:
                result.message = f"登录失败 HTTP {last_code}：{last_detail[:120]}"
            return result

        # 登录接口返回成功不等于真的登录了，用签到页确认一次
        if not _verify_logged_in(page, base_url, timeout):
            result.status = "login"
            result.message = "登录接口返回成功，但签到页仍被踢回登录页"
            return result

        result.cookies = _collect_cookies(context)
        result.add(f"导出 {len(result.cookies)} 个 cookie：{', '.join(sorted(result.cookies))}")
        log(f"导出 {len(result.cookies)} 个 cookie")

        result.ok = True
        result.status = "ok"
        result.message = "登录成功"
        return result

    except Exception as exc:
        result.status = "error"
        result.message = f"{type(exc).__name__}: {str(exc)[:150]}"
        return result
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass

def _read_sign_button(page) -> dict:
    """读取签到按钮状态。clickable 只在真正能触发签到时为 True。

    页面可能同时有「签到抽奖」和「领取连续签到宝箱」两个含「签到」的按钮，
    因此不能只取第一个匹配项，必须把所有候选列出来再挑真正的签到入口。
    否则宝箱按钮会被误判成「已签到」。
    """
    try:
        return page.evaluate("""() => {
          const all = [...document.querySelectorAll('button,[role=button]')]
            .filter(b => b.offsetParent !== null);
          const hits = all.map(b => (b.innerText || '').trim())
                          .filter(t => t && /签到/.test(t));
          const bodyTxt = document.body.innerText || '';
          // 站点真正的「还能不能签」信号是这行状态文字，按钮文案会随进度变
          // （连续签到 6/7 时按钮是「领取连续签到宝箱」而不是「签到抽奖」）
          const canSign = /可以签到/.test(bodyTxt) && !/今日已签到/.test(bodyTxt);
          const signTxt = hits.find(t => /签到抽奖|立即签到|领取连续签到宝箱/.test(t))
                       || (canSign ? hits[0] || '' : '');
          const doneTxt = hits.find(t => /今日已签到|已经签到/.test(t)) || '';
          const txt = signTxt || doneTxt || hits[0] || '';
          return {found: !!txt, text: txt, clickable: !!signTxt && canSign, all: hits};
        }""")
    except Exception:
        return {"found": False, "text": "", "clickable": False, "all": []}


def checkin_with_browser(
    base_url: str = BASE_URL_DEFAULT,
    cookies: Optional[dict[str, str]] = None,
    proxy: str = "",
    timeout: int = 60,
    log: Callable[[str], None] = _noop_log,
) -> BrowserResult:
    """用给定 cookie 完成签到。

    流程：打开签到页 → 判断今天是否已签 → 取图片验证码 → 本地 OCR 识别
    → 带答案提交 → 识别错就换一张重来（最多 CAPTCHA_MAX_TRIES 次）。

    为什么不用点击按钮：签到按钮的文案会随连续签到进度变化
    （连续 6/7 时是「领取连续签到宝箱」而不是「签到抽奖」），
    点错了还可能触发站点自己的提交逻辑，所以直接调接口更稳。
    """
    result = BrowserResult()
    cookies = cookies or {}

    browser = context = page = None
    try:
        browser, context, page = _launch_browser(proxy, log)
        _apply_resource_filter(page)

        # 注入 cookie（域名用带前导点的形式，子域也能带上）
        try:
            domain = base_url.split("//", 1)[-1].split("/", 1)[0]
            ctx_cookies = [
                {"name": k, "value": v, "domain": f".{domain}", "path": "/"}
                for k, v in cookies.items()
            ]
            if ctx_cookies:
                context.add_cookies(ctx_cookies)
                result.add(f"注入 {len(ctx_cookies)} 个 cookie")
        except Exception as exc:
            log(f"注入 cookie 失败（继续尝试）：{exc}")

        log("打开签到页")
        try:
            page.goto(f"{base_url}{SIGN_PATH}", wait_until="domcontentloaded",
                      timeout=timeout * 1000)
        except Exception as exc:
            result.status = "net"
            result.message = f"签到页打不开：{type(exc).__name__}: {str(exc)[:100]}"
            return result

        time.sleep(4)

        if "/login" in (page.url or ""):
            result.status = "login"
            result.message = "cookie 已失效，被重定向到登录页"
            return result
        result.add("签到页已打开且登录态有效")

        # 先读状态再决定动作
        state = _read_sign_button(page)
        result.add(f"按钮文案：{state.get('text') or '未找到'}")
        log(f"签到按钮：{state.get('text') or '未找到'}")

        # 顺手把奖励表取下来：签到接口只回下标，要靠这张表翻译成奖励名称
        page_props = _read_page_props(page)
        rewards = page_props.get("rewardsReal") or []
        result.rewards = rewards if isinstance(rewards, list) else []
        if result.rewards:
            result.add(f"奖励表 {len(result.rewards)} 项")

        if not state.get("found"):
            result.status = "token"
            result.message = "页面上找不到签到按钮"
            return result

        if not state.get("clickable"):
            result.ok = True
            result.status = "already"
            result.message = f"今日已签到（按钮：{state.get('text')}）"
            return result

        # 站点签到走图片验证码，不是 Turnstile：
        #   POST /api/forum/captcha/generate -> {key, img(base64)}
        #   POST /api/forum/check-in/sign    -> {captcha: <文字答案>, captcha_key: key}
        # 按钮文案会随连续签到进度变（6/7 时是「领取连续签到宝箱」），
        # 所以不去点按钮，直接打接口，避免误触站点自己的提交逻辑。
        if not _ocr_available():
            result.status = "token"
            result.message = "未安装 ddddocr，无法自动识别图片验证码"
            return result
        result.add("本地 OCR 可用")

        last_code = -1
        last_body = ""
        solved = False

        for attempt in range(1, CAPTCHA_MAX_TRIES + 1):
            cap = page.evaluate("""async (api) => {
              try {
                const r = await fetch(api, {credentials: 'include',
                  headers: {'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}});
                const j = await r.json();
                return {status: r.status, key: j.key || '', img: j.img || j.image || ''};
              } catch (e) { return {status: -1, key: '', img: '', err: String(e)}; }
            }""", CAPTCHA_API)

            key = str(cap.get("key") or "")
            img = str(cap.get("img") or "")
            if not key or not img:
                result.status = "token"
                result.message = f"取验证码失败（HTTP {cap.get('status')}）"
                return result

            try:
                png = base64.b64decode(img.split(",", 1)[-1])
            except Exception as exc:
                result.status = "token"
                result.message = f"验证码图片解码失败：{type(exc).__name__}"
                return result

            answer = _ocr_classify(png)
            if not answer:
                result.status = "token"
                result.message = "验证码识别失败"
                return result
            # 站点验证码固定 5 位，长短不对说明识别有问题，直接换一张比提交更划算
            if not (4 <= len(answer) <= 6):
                result.add(f"第 {attempt} 次识别长度异常（{len(answer)}）：{answer}")
                log(f"识别长度异常：{answer}")
                time.sleep(1.0)
                continue

            result.add(f"第 {attempt} 次识别：{answer}")
            log(f"验证码识别：{answer}")

            res = page.evaluate("""async (args) => {
              const [api, key, ans] = args;
              try {
                const m = document.cookie.match(/XSRF-TOKEN=([^;]+)/);
                const xsrf = m ? decodeURIComponent(m[1]) : '';
                const r = await fetch(api, {
                  method: 'POST',
                  headers: {'Content-Type': 'application/json', 'Accept': 'application/json',
                            'X-XSRF-TOKEN': xsrf, 'X-Requested-With': 'XMLHttpRequest'},
                  body: JSON.stringify({captcha: ans, captcha_key: key}),
                  credentials: 'include',
                });
                return {status: r.status, body: (await r.text()).slice(0, 800)};
              } catch (e) { return {status: -1, body: 'fetch error: ' + e.message}; }
            }""", [SIGN_API, key, answer])

            last_code = int(res.get("status", -1))
            last_body = str(res.get("body", ""))
            result.http_status = last_code
            result.response = last_body
            result.add(f"签到 HTTP {last_code}")
            log(f"签到 HTTP {last_code}")

            if last_code == 200:
                solved = True
                break
            if "已签到" in last_body or "已经签到" in last_body:
                result.ok = True
                result.status = "already"
                result.message = "服务端表示今日已签到"
                result.cookies = _collect_cookies(context)
                return result
            # 响应里的中文是 \uXXXX 转义的，必须先反序列化再判断，
            # 直接对原文做中文匹配永远匹配不到。
            try:
                parsed = json.loads(last_body)
                detail = f"{parsed.get('message') or ''} {parsed.get('errors') or ''}"
            except Exception:
                detail = last_body
            # 只有「验证码错误」才值得换一张重试，其他错误重试也没意义
            if "验证码" not in detail:
                break
            time.sleep(1.5)

        result.cookies = _collect_cookies(context)

        if solved:
            result.ok = True
            result.status = "ok"
            result.message = "签到成功"
            return result

        result.status = "submit"
        if last_body and "验证码" in last_body:
            result.message = f"验证码连续 {CAPTCHA_MAX_TRIES} 次识别错误"
        else:
            result.message = f"签到失败 HTTP {last_code}：{last_body[:150]}"
        return result

    except Exception as exc:
        result.status = "error"
        result.message = f"{type(exc).__name__}: {str(exc)[:150]}"
        return result
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass


def read_checkin_state(
    base_url: str = BASE_URL_DEFAULT,
    cookies: Optional[dict[str, str]] = None,
    proxy: str = "",
    timeout: int = 60,
    log: Callable[[str], None] = _noop_log,
) -> dict[str, Any]:
    """只读检查：打开签到页看按钮文案，不点击不提交。

    返回 {ok, logged_in, button_text, already, error}。
    用于「先探路再动手」的场景，成本低且没有副作用。
    """
    out: dict[str, Any] = {"ok": False, "logged_in": False, "button_text": "",
                           "buttons": [], "rewards": [], "already": False, "error": ""}
    browser = context = page = None
    try:
        browser, context, page = _launch_browser(proxy, log)
        _apply_resource_filter(page)
        try:
            domain = base_url.split("//", 1)[-1].split("/", 1)[0]
            ctx_cookies = [
                {"name": k, "value": v, "domain": f".{domain}", "path": "/"}
                for k, v in (cookies or {}).items()
            ]
            if ctx_cookies:
                context.add_cookies(ctx_cookies)
        except Exception:
            pass

        page.goto(f"{base_url}{SIGN_PATH}", wait_until="domcontentloaded",
                  timeout=timeout * 1000)
        time.sleep(4)
        out["logged_in"] = "/login" not in (page.url or "")
        state = _read_sign_button(page)
        out["button_text"] = state.get("text", "")
        out["buttons"] = state.get("all", [])
        page_props = _read_page_props(page)
        out["rewards"] = page_props.get("rewardsReal") or []
        out["already"] = bool(state.get("found")) and not bool(state.get("clickable"))
        out["ok"] = True
        return out
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        return out
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass
