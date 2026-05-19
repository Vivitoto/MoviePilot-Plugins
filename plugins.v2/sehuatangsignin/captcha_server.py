"""
Sehuatang captcha relay UI server - embedded Flask app for MP plugin.
Started on-demand, supports multi-account via URL path.
"""
import base64
import json
import random
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from urllib.parse import quote, unquote

import requests

from app.log import logger

try:
    from flask import Flask, redirect, render_template_string, request
except ImportError:
    Flask = None
    logger.warning("[SehuatangCaptcha] Flask not installed, attempting auto-install via pip...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "flask"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=120
        )
        from flask import Flask, redirect, render_template_string, request
        logger.info("[SehuatangCaptcha] Flask auto-installed successfully")
    except Exception as e:
        logger.warning(f"[SehuatangCaptcha] Auto-install Flask failed: {e}. Run: pip install flask")

# ─── Constants ────────────────────────────────────────────
BASE_URL = "https://sehuatang.net"
FS_URL_TEMPLATE = "{flaresolverr_url}/v1"

# ─── Session state ────────────────────────────────────────
# Keyed by account_id: {fs_sid, captcha_data, solved, answer, ...}
_captcha_sessions: dict = {}
_sessions_lock = threading.Lock()

# ─── HTML Template ─────────────────────────────────────────
CAPTCHA_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>98 验证码 - {{ account }}</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #1a1a2e; color: #e0e0e0; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
  .container { width: 100%; max-width: 440px; padding: 12px; }
  h1 { color: #e94560; font-size: 1.2em; text-align: center; margin-bottom: 4px; }
  .account-tag { text-align: center; color: #888; font-size: 0.8em; margin-bottom: 12px; }
  .card { background: #16213e; border-radius: 12px; padding: 14px; margin: 8px 0; }
  .info { color: #aaa; font-size: 0.82em; margin: 6px 0; text-align: center; line-height: 1.45; }
  .hint { color:#e94560; font-weight: 700; }
  .expire-warn { color: #e94560; font-size: 0.8em; text-align: center; }
  .success-box { background: #16213e; border-radius: 12px; padding: 20px; margin: 10px 0; text-align: center; }
  .success-text { color: #2ecc71; font-weight: bold; font-size: 1.2em; }
  .captcha-wrap { overflow-x: auto; padding-bottom: 4px; }
  .captcha-area { box-sizing: content-box; position: relative; width: {{ master_w }}px; height: {{ master_h }}px; margin: 8px auto; border-radius: 8px; overflow: hidden; border: 2px solid #0f3460; background:#0b1024; touch-action:none; }
  .captcha-bg { width: {{ master_w }}px; height: {{ master_h }}px; display: block; min-width: {{ master_w }}px; user-select: none; -webkit-user-select: none; object-fit: fill; image-rendering: auto; }
  .captcha-thumb { position: absolute; cursor: grab; user-select: none; -webkit-user-select: none; touch-action: none; z-index: 3; object-fit: fill; image-rendering: auto; }
  .captcha-thumb:active { cursor: grabbing; }
  .rotate-stage { box-sizing: content-box; width: {{ master_w }}px; height: {{ master_h }}px; margin: 8px auto; position: relative; border-radius: 8px; overflow:hidden; border:2px solid #0f3460; background:#0b1024; }
  .rotate-master { width: {{ master_w }}px; height: {{ master_h }}px; display:block; object-fit: fill; image-rendering: auto; }
  .rotate-thumb { position:absolute; left:50%; top:50%; width:{{ tw }}px; height:{{ th }}px; margin-left: calc(-{{ tw }}px / 2); margin-top: calc(-{{ th }}px / 2); transform: rotate(0deg); transform-origin: 50% 50%; object-fit: fill; image-rendering: auto; }
  .thumb-preview { box-sizing: content-box; display:block; max-width:none; margin:8px auto; border-radius:6px; border:1px solid #0f3460; background:#0b1024; object-fit: fill; image-rendering: auto; }
  .range { width: 100%; accent-color: #e94560; }
  .status-bar { text-align: center; margin: 8px 0; font-size: 0.9em; line-height:1.6; }
  .coord { color: #e94560; font-weight: bold; word-break: break-all; }
  .click-dot { position:absolute; width:22px; height:22px; border-radius:50%; background:#e94560; color:white; display:flex; align-items:center; justify-content:center; font-size:12px; font-weight:bold; transform:translate(-50%,-50%); pointer-events:none; z-index:5; box-shadow:0 0 0 2px rgba(255,255,255,.75); }
  .btn { display: block; width: 100%; padding: 14px; border-radius: 10px; border: none; font-size: 16px; cursor: pointer; text-align: center; margin: 8px 0; background: #e94560; color: white; font-weight: bold; }
  .btn:disabled { background: #555; cursor: not-allowed; }
  .btn-subtle { background: #0f3460; color: #e0e0e0; }
  details { margin-top: 8px; color:#aaa; font-size:12px; }
  pre { white-space: pre-wrap; word-break: break-all; background:#0b1024; border-radius:8px; padding:8px; margin-top:6px; text-align:left; }
</style>
</head>
<body>
<div class="container">
<h1>🔐 98 验证码</h1>
<p class="account-tag">账号：{{ account }}</p>

{% if solved %}
<div class="success-box">
  <p class="success-text">✅ 验证码已提交！</p>
  <p class="info">答案：{{ answer }}</p>
  <p class="info">正在继续提交签到，请稍候...</p>
</div>

{% elif error %}
<div class="card"><p style="color:#e94560;text-align:center;">{{ error }}</p>
  <button class="btn btn-subtle" onclick="location.reload()">🔄 刷新页面</button>
</div>

{% elif captcha_ready %}
<div class="card">
  <p class="info">类型：<strong>{{ captcha_type | upper }}</strong> | 图片：{{ master_w }}×{{ master_h }} | thumb：{{ tw }}×{{ th }} | 初始：({{ dx }},{{ dy }})</p>

  {% if captcha_type == 'slide' %}
    <p class="info hint">拖动图块到背景缺口位置，然后提交。</p>
    <div class="captcha-wrap"><div class="captcha-area" id="captcha-area">
      <img class="captcha-bg" id="captcha-bg" src="data:image/png;base64,{{ master_b64 }}" alt="captcha" draggable="false">
      {% if thumb_b64 %}<img class="captcha-thumb" id="captcha-thumb" src="data:image/png;base64,{{ thumb_b64 }}" alt="thumb" draggable="false" style="left:{{ dx }}px; top:{{ dy }}px; width:{{ tw }}px; height:{{ th }}px;">{% endif %}
    </div></div>

  {% elif captcha_type == 'rotate' %}
    <p class="info hint">拖动角度滑条，让图形旋转到正确方向，然后提交。</p>
    <div class="captcha-wrap"><div class="rotate-stage">
      <img class="rotate-master" src="data:image/png;base64,{{ master_b64 }}" alt="captcha" draggable="false">
      {% if thumb_b64 %}<img class="rotate-thumb" id="rotate-thumb" src="data:image/png;base64,{{ thumb_b64 }}" alt="thumb" draggable="false">{% endif %}
    </div></div>
    <input class="range" id="rotate-range" type="range" min="0" max="359" value="0" step="1">

  {% elif captcha_type == 'click' %}
    <p class="info hint">按提示图在背景图上点选；支持多点，按点击顺序提交。</p>
    {% if thumb_b64 %}<img class="thumb-preview" src="data:image/png;base64,{{ thumb_b64 }}" alt="click prompt" style="width:{{ tw }}px; height:{{ th }}px;">{% endif %}
    <div class="captcha-wrap"><div class="captcha-area" id="captcha-area">
      <img class="captcha-bg" id="captcha-bg" src="data:image/png;base64,{{ master_b64 }}" alt="captcha" draggable="false">
    </div></div>
    <button class="btn btn-subtle" type="button" onclick="undoClick()">↩️ 撤销上一个点</button>

  {% else %}
    <p class="info hint">当前类型暂不支持，请重新获取。</p>
  {% endif %}

  <div class="status-bar">
    当前操作：<span class="coord" id="action-info">尚未操作</span><br>
    提交答案：<span class="coord" id="answer-info">-</span>
  </div>

  <button class="btn" id="submit-btn" onclick="submitAnswer()" disabled>先完成验证码操作</button>
  <p class="expire-warn" id="expire-timer"></p>

  <details>
    <summary>调试信息（用于核对原始字段）</summary>
    <pre>{{ debug_json }}</pre>
  </details>
</div>

{% else %}
<div class="card" style="text-align:center">
  <p>正在获取验证码...</p>
  <button class="btn btn-subtle" onclick="location.reload()">🔄 重试</button>
</div>
{% endif %}
</div>

<script>
const capType = "{{ captcha_type }}";
const dx = {{ dx }}, dy = {{ dy }}, tw = {{ tw }}, th = {{ th }};
const masterW = {{ master_w }}, masterH = {{ master_h }};
const account = "{{ account }}";
let answer = "";

function setAnswer(value, label) {
  answer = String(value || "");
  const btn = document.getElementById('submit-btn');
  const ans = document.getElementById('answer-info');
  const act = document.getElementById('action-info');
  if (ans) ans.textContent = answer || '-';
  if (act) act.textContent = label || answer || '尚未操作';
  if (btn) {
    btn.disabled = !answer;
    btn.textContent = answer ? '✅ 提交答案' : '先完成验证码操作';
  }
}

function showTimer(sec) {
  const el = document.getElementById('expire-timer');
  if (!el) return;
  const tick = () => {
    if (sec <= 0) { el.textContent = '⚠️ 验证码可能已过期，请重新触发签到'; return; }
    const m = Math.floor(sec / 60), s = sec % 60;
    el.textContent = `⏰ 剩余 ${m}:${String(s).padStart(2,'0')} 有效`;
    sec--;
    setTimeout(tick, 1000);
  };
  tick();
}
showTimer({{ expire_seconds }});

if (capType === 'slide') {
  const area = document.getElementById('captcha-area');
  const bg = document.getElementById('captcha-bg');
  const thumb = document.getElementById('captcha-thumb');
  let left = dx, startX = 0;
  function clamp(v, min, max) { return Math.max(min, Math.min(v, max)); }
  function render() {
    if (!thumb) return;
    thumb.style.left = left + 'px';
    thumb.style.top = dy + 'px';
    const x = Math.round(left), y = Math.round(dy);
    setAnswer(x + ',' + y, '图块位置：(' + x + ',' + y + ')');
  }
  function point(e) {
    const t = e.touches ? e.touches[0] : e;
    const target = bg || area;
    const r = target.getBoundingClientRect();
    return { x: (t.clientX - r.left) * masterW / r.width };
  }
  function onStart(e) {
    if (!thumb) return;
    e.preventDefault();
    const p = point(e); startX = p.x - left;
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onEnd);
    document.addEventListener('touchmove', onMove, {passive:false});
    document.addEventListener('touchend', onEnd);
  }
  function onMove(e) {
    e.preventDefault();
    const p = point(e);
    left = clamp(p.x - startX, 0, masterW - tw);
    render();
  }
  function onEnd() {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onEnd);
    document.removeEventListener('touchmove', onMove);
    document.removeEventListener('touchend', onEnd);
  }
  if (thumb) {
    thumb.addEventListener('mousedown', onStart);
    thumb.addEventListener('touchstart', onStart, {passive:false});
  }
}

if (capType === 'rotate') {
  const range = document.getElementById('rotate-range');
  const thumb = document.getElementById('rotate-thumb');
  function renderAngle(markReady) {
    const angle = parseInt(range.value || '0', 10);
    if (thumb) thumb.style.transform = 'rotate(' + angle + 'deg)';
    if (markReady) setAnswer(String(angle), '旋转角度：' + angle + '°');
  }
  if (range) {
    range.addEventListener('input', function() { renderAngle(true); });
    renderAngle(false);
  }
}

const clickPoints = [];
function renderClickPoints() {
  const area = document.getElementById('captcha-area');
  if (!area) return;
  area.querySelectorAll('.click-dot').forEach(e => e.remove());
  clickPoints.forEach((p, i) => {
    const dot = document.createElement('div');
    dot.className = 'click-dot'; dot.textContent = String(i + 1);
    dot.style.left = p.x + 'px'; dot.style.top = p.y + 'px';
    area.appendChild(dot);
  });
  const ans = clickPoints.map(p => p.x + ',' + p.y).join(',');
  setAnswer(ans, clickPoints.length ? '已点 ' + clickPoints.length + ' 个位置' : '尚未点选');
}
function undoClick() {
  if (capType !== 'click') return;
  clickPoints.pop(); renderClickPoints();
}
if (capType === 'click') {
  const area = document.getElementById('captcha-area');
  if (area) area.addEventListener('click', function(e) {
    const bg = document.getElementById('captcha-bg');
    const target = bg || area;
    const r = target.getBoundingClientRect();
    const x = Math.round((e.clientX - r.left) * masterW / r.width);
    const y = Math.round((e.clientY - r.top) * masterH / r.height);
    clickPoints.push({x, y}); renderClickPoints();
  });
}

async function submitAnswer() {
  if (!answer) return;
  const btn = document.getElementById('submit-btn');
  btn.disabled = true;
  btn.textContent = '提交中...';
  try {
    const r = await fetch('/' + encodeURIComponent(account) + '/submit', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: 'answer=' + encodeURIComponent(answer)
    });
    if (r.ok) {
      const t = await r.text();
      document.body.innerHTML = t;
    } else {
      btn.textContent = '提交失败，重试'; btn.disabled = false;
    }
  } catch(e) {
    btn.textContent = '网络错误，重试'; btn.disabled = false;
  }
}
</script>
</body>
</html>"""

def _render_captcha_template(**kwargs):
    defaults = {
        "captcha_ready": False,
        "solved": False,
        "error": None,
        "answer": "",
        "captcha_type": "",
        "dx": 0,
        "dy": 0,
        "tw": 64,
        "th": 64,
        "master_w": 300,
        "master_h": 220,
        "master_b64": "",
        "thumb_b64": "",
        "expire_seconds": 300,
        "debug_json": "{}",
    }
    defaults.update(kwargs)
    return render_template_string(CAPTCHA_HTML, **defaults)


# ─── Flask App Factory ────────────────────────────────────
def create_app():
    if Flask is None:
        raise RuntimeError("Flask is not installed. Run: pip install flask")
    app = Flask(__name__)

    @app.route("/<path:account_id>")
    def captcha_page(account_id):
        if account_id in ("favicon.ico", "robots.txt"):
            return "", 404
        with _sessions_lock:
            session = _captcha_sessions.get(account_id)
        if not session:
            return _render_captcha_template(account=account_id, captcha_ready=False,
                                            solved=False, error="会话不存在或已过期，请重新获取验证码")
        if session.get("solved"):
            return _render_captcha_template(account=account_id, captcha_ready=False,
                                            solved=True, answer=session.get("answer", ""))
        if not session.get("captcha_data"):
            return _render_captcha_template(account=account_id, captcha_ready=False, solved=False)

        with _sessions_lock:
            data = session.get("captcha_data", {})
            created_at = session.get("created_at", time.time())
        debug = {
            "type": data.get("type"),
            "display_x": data.get("display_x"),
            "display_y": data.get("display_y"),
            "thumb_width": data.get("thumb_width"),
            "thumb_height": data.get("thumb_height"),
            "master_width": data.get("master_width"),
            "master_height": data.get("master_height"),
            "has_master_image_base64": bool(data.get("master_b64")),
            "has_thumb_image_base64": bool(data.get("thumb_b64")),
        }
        return _render_captcha_template(
            account=account_id,
            captcha_ready=True,
            solved=False,
            captcha_type=data.get("type", "?"),
            dx=int(data.get("display_x") or 0),
            dy=int(data.get("display_y") or 0),
            tw=int(data.get("thumb_width") or 64),
            th=int(data.get("thumb_height") or 64),
            master_w=int(data.get("master_width") or 300),
            master_h=int(data.get("master_height") or 220),
            master_b64=data.get("master_b64", ""),
            thumb_b64=data.get("thumb_b64", ""),
            expire_seconds=max(0, int(300 - (time.time() - created_at))),
            debug_json=json.dumps(debug, ensure_ascii=False, indent=2),
        )

    @app.route("/<path:account_id>/submit", methods=["POST"])
    def captcha_submit(account_id):
        with _sessions_lock:
            session = _captcha_sessions.get(account_id)
            if not session or session.get("solved"):
                return _render_captcha_template(account=account_id, solved=True,
                                                error="会话已过期")

            answer = str(request.form.get("answer") or "").strip()
            if not answer:
                return "missing answer", 400
            session["answer"] = answer
            session["solved"] = True
            session["solved_at"] = time.time()

        logger.info(f"[SehuatangCaptcha] Account {account_id}: user submitted {answer}")
        return _render_captcha_template(account=account_id, solved=True, answer=answer)

    return app


# ─── Session management ───────────────────────────────────
def init_session(account_id: str):
    """Initialize a captcha session for an account."""
    with _sessions_lock:
        _captcha_sessions[account_id] = {
            "solved": False, "answer": None, "captcha_data": None,
            "fs_sid": None, "created_at": time.time(),
        }


def destroy_session(account_id: str, destroy_fs: bool = True):
    """Clean up a captcha session."""
    with _sessions_lock:
        session = _captcha_sessions.pop(account_id, None)
    if destroy_fs and session and session.get("fs_sid"):
        fs_destroy_session(session["fs_sid"])


def set_captcha_data(account_id: str, data: dict, fs_sid: str):
    """Store captcha data for display."""
    with _sessions_lock:
        session = _captcha_sessions.get(account_id)
        if session:
            session["captcha_data"] = data
            session["fs_sid"] = fs_sid
            session["solved"] = False


def is_solved(account_id: str) -> bool:
    """Check if user has submitted the captcha."""
    with _sessions_lock:
        session = _captcha_sessions.get(account_id)
        return bool(session and session.get("solved"))


def get_answer(account_id: str) -> str:
    """Get the user's raw answer string."""
    with _sessions_lock:
        session = _captcha_sessions.get(account_id)
        if session and session.get("answer"):
            return str(session["answer"])
    return ""


def is_expired(account_id: str, timeout: int = 300) -> bool:
    """Check if the session has expired."""
    with _sessions_lock:
        session = _captcha_sessions.get(account_id)
        if not session:
            return True
        return time.time() - session.get("created_at", 0) > timeout


# ─── FS helpers ───────────────────────────────────────────
_fs_url_cache: str = ""
_proxy_url_cache: str = ""


def _get_fs_url() -> str:
    return _fs_url_cache


def set_fs_url(url: str):
    global _fs_url_cache
    _fs_url_cache = url.rstrip("/")


def set_proxy_url(url: str):
    global _proxy_url_cache
    _proxy_url_cache = url.strip()


def set_base_url(url: str):
    """Set target site base URL, e.g. https://sehuatang.net."""
    global BASE_URL
    clean = (url or "").strip().rstrip("/")
    BASE_URL = clean or "https://sehuatang.net"


def _proxy_param() -> dict | None:
    if _proxy_url_cache:
        return {"proxy": {"url": _proxy_url_cache}}
    return None


def _merge_solution_cookies(cookies: list, solution_cookies: list):
    """Merge cookies returned by FlareSolverr back into the mutable cookie jar.

    The captcha endpoint may update server-side state cookies. If we keep
    replaying only the original configured cookies, the following check request
    can be evaluated against stale captcha state.
    """
    if not isinstance(cookies, list) or not isinstance(solution_cookies, list):
        return
    index = {}
    for i, item in enumerate(cookies):
        if not isinstance(item, dict) or not item.get("name"):
            continue
        key = (item.get("name"), item.get("domain", ""), item.get("path", "/"))
        index[key] = i
    for item in solution_cookies:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        key = (item.get("name"), item.get("domain", ""), item.get("path", "/"))
        if key in index:
            cookies[index[key]].update(item)
        else:
            cookies.append(dict(item))


def fs_call(session_id: str, payload: dict, cookies: list, timeout: int = 60) -> dict:
    if session_id:
        payload["session"] = session_id
    if cookies:
        payload["cookies"] = cookies
    proxy = _proxy_param()
    if proxy:
        payload.update(proxy)
    r = requests.post(
        FS_URL_TEMPLATE.format(flaresolverr_url=_get_fs_url()),
        json=payload,
        timeout=timeout + 10,
    )
    d = r.json()
    if d.get("status") != "ok":
        return {"error": d.get("message", "unknown")}
    sol = d.get("solution", {})
    _merge_solution_cookies(cookies, sol.get("cookies", []))
    return {"html": sol.get("response", ""), "cookies": sol.get("cookies", []),
            "status": sol.get("status", 0)}


def fs_create_session() -> str:
    payload = {"cmd": "sessions.create", "maxTimeout": 90000}
    proxy = _proxy_param()
    if proxy:
        payload.update(proxy)
    r = requests.post(
        FS_URL_TEMPLATE.format(flaresolverr_url=_get_fs_url()),
        json=payload,
        timeout=15,
    )
    d = r.json()
    return d.get("session", "") if d.get("status") == "ok" else ""


def fs_destroy_session(fs_sid: str):
    """Destroy a FlareSolverr session, ignoring cleanup errors."""
    if not fs_sid:
        return
    try:
        requests.post(
            FS_URL_TEMPLATE.format(flaresolverr_url=_get_fs_url()),
            json={"cmd": "sessions.destroy", "session": fs_sid},
            timeout=5,
        )
    except Exception:
        pass


def fs_get(fs_sid: str, url: str, cookies: list) -> str:
    return fs_call(fs_sid, {"cmd": "request.get", "url": url, "maxTimeout": 60000}, cookies).get("html", "")


def fs_post(fs_sid: str, url: str, body: str, cookies: list) -> dict:
    r = fs_call(fs_sid, {
        "cmd": "request.post",
        "url": url,
        "postData": body,
        "headers": {"Content-Type": "text/plain", "Referer": f"{BASE_URL}/plugin.php?id=dd_sign"},
        "maxTimeout": 30000,
    }, cookies)
    html = r.get("html", "")
    m = re.search(r"<body>(.+?)</body>", html, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return {"raw": m.group(1)[:300]}
    return {"raw": html[:300]}


def extract_json(html: str) -> dict:
    m = re.search(r"<body>(.+?)</body>", html, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return {}
    return {}


# ─── Sign-in flow ─────────────────────────────────────────
def _strip_data_uri(value: str) -> str:
    if value and "," in value:
        return value.split(",", 1)[1]
    return value or ""


def fetch_captcha_for_account(fs_sid: str, cookies: list, max_retries: int | None = None,
                              max_wait_seconds: int = 300) -> dict | None:
    """Fetch a supported captcha from sehuatang.

    Supported manual relay types: slide, rotate, click. Drag is intentionally skipped.
    Retries use a 10–15s jitter to avoid hammering the captcha endpoint, with a total wait cap.
    """
    supported_types = {"slide", "rotate", "click"}
    attempt = 0
    deadline = time.time() + max(1, int(max_wait_seconds or 300))
    while (max_retries is None or attempt < max_retries) and time.time() < deadline:
        attempt += 1
        if attempt > 1:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            delay = min(random.uniform(10, 15), remaining)
            logger.debug(f"[SehuatangCaptcha] Waiting {delay:.1f}s before captcha retry")
            time.sleep(delay)
        html = fs_get(fs_sid, f"{BASE_URL}/misc.php?mod=captcha", cookies)
        cap = extract_json(html)
        code = cap.get("code")
        if code == 429:
            logger.warning("[SehuatangCaptcha] Captcha endpoint returned 429; stop retrying this account")
            return None
        data = cap.get("data", {})
        if not data or not data.get("type"):
            logger.debug(f"[SehuatangCaptcha] Attempt {attempt}: no captcha type, code={code}")
            continue
        cap_type = data["type"]
        if cap_type in supported_types:
            data["master_b64"] = _strip_data_uri(data.get("master_image_base64", ""))
            data["thumb_b64"] = _strip_data_uri(data.get("thumb_image_base64", ""))
            logger.info(
                f"[SehuatangCaptcha] Got supported {cap_type} after {attempt} attempt(s); "
                f"master={data.get('master_width')}x{data.get('master_height')} "
                f"thumb={data.get('thumb_width')}x{data.get('thumb_height')} "
                f"display=({data.get('display_x')},{data.get('display_y')})"
            )
            return data
        logger.info(f"[SehuatangCaptcha] Attempt {attempt}: got unsupported {cap_type}, retrying with jitter")
    logger.warning(f"[SehuatangCaptcha] Captcha fetch timed out after {max_wait_seconds}s")
    return None


def check_sign_status(fs_sid: str, cookies: list) -> tuple:
    """Check if already signed in. Returns (is_signed, button_text)."""
    html = fs_get(fs_sid, f"{BASE_URL}/plugin.php?id=dd_sign", cookies)
    btn = re.search(r'id="signin-btn"[^>]*>([^<]+)<', html) if html else None
    if btn:
        return "已签到" in btn.group(1), btn.group(1)
    return False, "N/A"


def submit_check(fs_sid: str, answer: str, cap_type: str, cookies: list) -> tuple:
    """Submit raw captcha answer. Returns (ok, result_dict)."""
    result = fs_post(fs_sid, f"{BASE_URL}/misc.php?mod=captcha&action=check", answer, cookies)
    ok = result.get("data") == "ok"
    logger.info(f"[SehuatangCaptcha] Check {cap_type} answer={answer}: {'OK' if ok else result.get('data','?')}")
    return ok, result


def complete_signin(fs_sid: str, cookies: list) -> dict:
    """Complete the sign-in after captcha passes."""
    html = fs_get(fs_sid, f"{BASE_URL}/plugin.php?id=dd_sign&ac=sign_v2", cookies)
    return extract_json(html)


app = create_app() if Flask is not None else None


# ─── Embedded server ──────────────────────────────────────
_server_thread = None
_server_port = 5099


def start_server(port: int = 5099):
    """Start the embedded captcha relay HTTP server in a background thread."""
    global _server_thread, _server_port
    if _server_thread and _server_thread.is_alive():
        return
    if Flask is None:
        logger.error("[SehuatangCaptcha] Cannot start server: Flask not installed")
        return

    _server_port = port

    try:
        from waitress import serve
        def _runner(): serve(app, host="0.0.0.0", port=port, threads=6)
    except ImportError:
        logger.warning("[SehuatangCaptcha] waitress not available, using Flask dev server")
        def _runner(): app.run(host="0.0.0.0", port=port, debug=False, threaded=True)

    def _run():
        try:
            _runner()
        except Exception:
            logger.error(f"[SehuatangCaptcha] Server error: {traceback.format_exc()}")

    _server_thread = threading.Thread(target=_run, daemon=True)
    _server_thread.start()
    logger.info(f"[SehuatangCaptcha] Captcha relay server started on port {port}")


def stop_server():
    """No-op: daemon thread stops with parent process. Kept for API completeness."""
    global _server_thread
    _server_thread = None