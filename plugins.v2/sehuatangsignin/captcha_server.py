"""
Sehuatang captcha relay UI server - embedded Flask app for MP plugin.
Started on-demand, supports multi-account via URL path.
"""
import base64
import json
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
<title>Sehuatang 验证码 - {{ account }}</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, sans-serif; background: #1a1a2e; color: #e0e0e0; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
  .container { width: 100%; max-width: 420px; padding: 12px; }
  h1 { color: #e94560; font-size: 1.2em; text-align: center; margin-bottom: 4px; }
  .account-tag { text-align: center; color: #888; font-size: 0.8em; margin-bottom: 12px; }
  .card { background: #16213e; border-radius: 12px; padding: 14px; margin: 8px 0; }
  .info { color: #888; font-size: 0.8em; margin: 4px 0; text-align: center; }
  .expire-warn { color: #e94560; font-size: 0.8em; text-align: center; }
  .success-box { background: #16213e; border-radius: 12px; padding: 20px; margin: 10px 0; text-align: center; }
  .success-text { color: #2ecc71; font-weight: bold; font-size: 1.2em; }
  .captcha-area { position: relative; width: 300px; height: 220px; margin: 8px auto; border-radius: 8px; overflow: hidden; border: 2px solid #0f3460; }
  .captcha-bg { width: 300px; height: 220px; display: block; min-width: 300px; pointer-events: none; }
  .captcha-thumb { position: absolute; cursor: grab; user-select: none; -webkit-user-select: none; touch-action: none; }
  .captcha-thumb:active { cursor: grabbing; }
  .slider-track { width: 300px; height: 44px; margin: 8px auto 0; background: #0f3460; border-radius: 22px; position: relative; overflow: hidden; }
  .slider-fill { height: 100%; background: #e94560; border-radius: 22px 0 0 22px; width: 0; }
  .slider-handle { position: absolute; top: 0; width: 60px; height: 44px; background: #e94560; border-radius: 22px; cursor: grab; display: flex; align-items: center; justify-content: center; font-size: 20px; color: white; user-select: none; left: 0; }
  .slider-handle:active { cursor: grabbing; }
  .status-bar { text-align: center; margin: 8px 0; font-size: 0.9em; }
  .coord { color: #e94560; font-weight: bold; }
  .btn { display: block; width: 100%; padding: 14px; border-radius: 10px; border: none; font-size: 16px; cursor: pointer; text-align: center; margin: 8px 0; background: #e94560; color: white; font-weight: bold; }
  .btn:disabled { background: #555; cursor: not-allowed; }
  .btn-subtle { background: #0f3460; color: #e0e0e0; }
</style>
</head>
<body>
<div class="container">
<h1>🔐 Sehuatang 验证码</h1>
<p class="account-tag">账号：{{ account }}</p>

{% if solved %}
<div class="success-box">
  <p class="success-text">✅ 验证码已提交！</p>
  <p class="info">坐标：{{ answer }}</p>
  <p class="info">正在完成签到，请稍候...</p>
</div>

{% elif error %}
<div class="card"><p style="color:#e94560;text-align:center;">{{ error }}</p>
  <button class="btn btn-subtle" onclick="location.reload()">🔄 重新获取</button>
</div>

{% elif captcha_ready %}
  <p class="info">类型：<strong>{{ captcha_type | upper }}</strong> | 显示位置：({{ dx }},{{ dy }}) | 拇指：{{ tw }}×{{ th }}</p>
  <p class="info" style="color:#e94560;">👆 <strong>拖动红色滑块/拼图块</strong>到缺口位置</p>

  {% if captcha_type == 'slide' %}
  <div class="captcha-area">
    <img class="captcha-bg" src="data:image/png;base64,{{ master_b64 }}" alt="captcha">
  </div>
  <div class="slider-track" id="slider-track">
    <div class="slider-fill" id="slider-fill"></div>
    <div class="slider-handle" id="slider-handle">▶</div>
  </div>
  {% else %}
  <div class="captcha-area" style="touch-action:none;">
    <img class="captcha-bg" src="data:image/png;base64,{{ master_b64 }}" alt="captcha">
    <img class="captcha-thumb" id="captcha-thumb" src="data:image/png;base64,{{ thumb_b64 }}" alt="thumb"
         style="left:{{ dx }}px; top:{{ dy }}px; width:{{ tw }}px; height:{{ th }}px;">
  </div>
  {% endif %}

  <div class="status-bar">
    拖动距离：<span class="coord" id="drag-info">0px</span><br>
    提交坐标：<span class="coord" id="coord-info">({{ dx }},{{ dy }})</span>
  </div>

  <button class="btn" id="submit-btn" onclick="submitAnswer()">✅ 提交坐标</button>
  <p class="expire-warn" id="expire-timer"></p>

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
const account = "{{ account }}";
let hasDragged = false, dragDist = 0, dragDistY = 0;

function showTimer(sec) {
  const el = document.getElementById('expire-timer');
  if (!el) return;
  const tick = () => {
    if (sec <= 0) { el.textContent = '⚠️ 验证码可能已过期，请刷新'; return; }
    const m = Math.floor(sec / 60), s = sec % 60;
    el.textContent = `⏰ 剩余 ${m}:${String(s).padStart(2,'0')} 有效`;
    sec--;
    setTimeout(tick, 1000);
  };
  tick();
}
showTimer(240);

if (capType === 'slide') {
  const handle = document.getElementById('slider-handle');
  const track = document.getElementById('slider-track');
  const fill = document.getElementById('slider-fill');
  const dragInfo = document.getElementById('drag-info');
  const coordInfo = document.getElementById('coord-info');
  const maxDist = 300;
  let currentLeft = 0, startX = 0;

  function updateUI(left) {
    handle.style.left = left + 'px';
    fill.style.width = (left / (maxDist - 60) * 100) + '%';
    const gapX = dx + Math.round(left);
    dragDist = Math.round(left);
    dragInfo.textContent = dragDist + 'px';
    coordInfo.textContent = '(gap_x=' + gapX + ', y=' + dy + ')';
  }

  function onStart(e) {
    e.preventDefault();
    e.stopPropagation();
    startX = (e.touches ? e.touches[0].clientX : e.clientX) - currentLeft;
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onEnd);
    document.addEventListener('touchmove', onMove, {passive: false});
    document.addEventListener('touchend', onEnd);
  }
  function onMove(e) {
    e.preventDefault();
    const cx = e.touches ? e.touches[0].clientX : e.clientX;
    currentLeft = Math.max(0, Math.min(cx - startX, maxDist - 60));
    hasDragged = true;
    updateUI(currentLeft);
  }
  function onEnd() {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onEnd);
    document.removeEventListener('touchmove', onMove);
    document.removeEventListener('touchend', onEnd);
  }
  handle.addEventListener('mousedown', onStart);
  handle.addEventListener('touchstart', onStart, {passive: false});
  track.addEventListener('click', function(e) {
    if (e.target === track || e.target === fill) {
      currentLeft = Math.max(0, Math.min(e.offsetX - 30, maxDist - 60));
      hasDragged = true;
      updateUI(currentLeft);
    }
  });
}

if (capType === 'drag') {
  const thumb = document.getElementById('captcha-thumb');
  const dragInfo = document.getElementById('drag-info');
  const coordInfo = document.getElementById('coord-info');
  let origLeft = dx, origTop = dy, startX = 0, startY = 0;
  const maxX = 300 - tw, maxY = 220 - th;

  function updateUI(left, top) {
    thumb.style.left = left + 'px';
    thumb.style.top = top + 'px';
    dragDist = left - dx;
    dragDistY = top - dy;
    dragInfo.textContent = 'Δ(' + dragDist + ',' + dragDistY + ')';
    coordInfo.textContent = '(gap=' + left + ',' + top + ')';
  }
  function onStart(e) {
    e.preventDefault();
    e.stopPropagation();
    startX = (e.touches ? e.touches[0].clientX : e.clientX) - origLeft;
    startY = (e.touches ? e.touches[0].clientY : e.clientY) - origTop;
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onEnd);
    document.addEventListener('touchmove', onMove, {passive: false});
    document.addEventListener('touchend', onEnd);
  }
  function onMove(e) {
    e.preventDefault();
    origLeft = Math.max(0, Math.min((e.touches ? e.touches[0].clientX : e.clientX) - startX, maxX));
    origTop = Math.max(0, Math.min((e.touches ? e.touches[0].clientY : e.clientY) - startY, maxY));
    hasDragged = true;
    updateUI(origLeft, origTop);
  }
  function onEnd() {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onEnd);
    document.removeEventListener('touchmove', onMove);
    document.removeEventListener('touchend', onEnd);
  }
  thumb.addEventListener('mousedown', onStart);
  thumb.addEventListener('touchstart', onStart, {passive: false});
}

async function submitAnswer() {
  const btn = document.getElementById('submit-btn');
  btn.disabled = true;
  btn.textContent = '提交中...';

  let gapX, gapY;
  if (capType === 'slide') {
    gapX = dx + dragDist;
    gapY = dy;
  } else {
    gapX = dx + dragDist;
    gapY = dy + dragDistY;
  }
  const ans = gapX + ',' + gapY;

  try {
    const r = await fetch('/' + account + '/submit', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: 'gap_x=' + gapX + '&gap_y=' + gapY
    });
    if (r.ok) {
      const t = await r.text();
      document.body.innerHTML = t;
    } else {
      btn.textContent = '提交失败，重试';
      btn.disabled = false;
    }
  } catch(e) {
    btn.textContent = '网络错误，重试';
    btn.disabled = false;
  }
}
</script>
</body>
</html>"""

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
            return render_template_string(CAPTCHA_HTML, account=account_id, captcha_ready=False,
                                          solved=False, error="会话不存在或已过期，请重新获取验证码")
        if session.get("solved"):
            return render_template_string(CAPTCHA_HTML, account=account_id, captcha_ready=False,
                                          solved=True, answer=session.get("answer", ""))
        if not session.get("captcha_data"):
            return render_template_string(CAPTCHA_HTML, account=account_id, captcha_ready=False, solved=False)

        with _sessions_lock:
            data = session.get("captcha_data", {})
        return render_template_string(
            CAPTCHA_HTML,
            account=account_id,
            captcha_ready=True,
            solved=False,
            captcha_type=data.get("type", "?"),
            dx=data.get("display_x", 0),
            dy=data.get("display_y", 0),
            tw=data.get("thumb_width", 64),
            th=data.get("thumb_height", 64),
            master_b64=data.get("master_b64", ""),
            thumb_b64=data.get("thumb_b64", ""),
        )

    @app.route("/<path:account_id>/submit", methods=["POST"])
    def captcha_submit(account_id):
        with _sessions_lock:
            session = _captcha_sessions.get(account_id)
            if not session or session.get("solved"):
                return render_template_string(CAPTCHA_HTML, account=account_id, solved=True,
                                              error="会话已过期")

            gap_x = int(request.form.get("gap_x", 0))
            gap_y = int(request.form.get("gap_y", 0))
            answer = f"{gap_x},{gap_y}"
            session["answer"] = answer
            session["solved"] = True
            session["solved_at"] = time.time()

        logger.info(f"[SehuatangCaptcha] Account {account_id}: user submitted {answer}")
        return render_template_string(CAPTCHA_HTML, account=account_id, solved=True, answer=answer)

    return app


# ─── Session management ───────────────────────────────────
def init_session(account_id: str):
    """Initialize a captcha session for an account."""
    with _sessions_lock:
        _captcha_sessions[account_id] = {
            "solved": False, "answer": None, "captcha_data": None,
            "fs_sid": None, "created_at": time.time(),
        }


def destroy_session(account_id: str):
    """Clean up a captcha session."""
    with _sessions_lock:
        session = _captcha_sessions.pop(account_id, None)
    if session and session.get("fs_sid"):
        try:
            requests.post(
                FS_URL_TEMPLATE.format(flaresolverr_url=_get_fs_url()),
                json={"cmd": "sessions.destroy", "session": session["fs_sid"]},
                timeout=5,
            )
        except Exception:
            pass


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


def get_answer(account_id: str) -> tuple:
    """Get the user's answer. Returns (gap_x, gap_y)."""
    with _sessions_lock:
        session = _captcha_sessions.get(account_id)
        if session and session.get("answer"):
            parts = session["answer"].split(",")
            return int(parts[0]), int(parts[1])
    return 0, 0


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


def _proxy_param() -> dict | None:
    if _proxy_url_cache:
        return {"proxy": {"url": _proxy_url_cache}}
    return None


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
def fetch_captcha_for_account(fs_sid: str, cookies: list, max_retries: int = 8) -> dict | None:
    """Fetch a slide or drag captcha from sehuatang."""
    for attempt in range(max_retries):
        if attempt > 0:
            time.sleep(3)
        html = fs_get(fs_sid, f"{BASE_URL}/misc.php?mod=captcha", cookies)
        cap = extract_json(html)
        data = cap.get("data", {})
        if not data or not data.get("type"):
            continue
        cap_type = data["type"]
        if cap_type in ("slide", "drag"):
            # Extract base64 images
            master_b64 = data.get("master_image_base64", "")
            if "," in master_b64:
                master_b64 = master_b64.split(",", 1)[1]
            thumb_b64 = data.get("thumb_image_base64", "")
            if thumb_b64 and "," in thumb_b64:
                thumb_b64 = thumb_b64.split(",", 1)[1]
            data["master_b64"] = master_b64
            data["thumb_b64"] = thumb_b64
            logger.debug(f"[SehuatangCaptcha] Got {cap_type} after {attempt+1} attempt(s)")
            return data
        logger.debug(f"[SehuatangCaptcha] Attempt {attempt + 1}: got {cap_type}, retrying...")
    return None


def check_sign_status(fs_sid: str, cookies: list) -> tuple:
    """Check if already signed in. Returns (is_signed, button_text)."""
    html = fs_get(fs_sid, f"{BASE_URL}/plugin.php?id=dd_sign", cookies)
    btn = re.search(r'id="signin-btn"[^>]*>([^<]+)<', html) if html else None
    if btn:
        return "已签到" in btn.group(1), btn.group(1)
    return False, "N/A"


def submit_check(fs_sid: str, gap_x: int, gap_y: int, cap_type: str,
                 display_y: int, cookies: list) -> tuple:
    """Submit captcha check. Returns (ok, result_dict)."""
    if cap_type == "slide":
        answer = f"{gap_x},{display_y}"
    else:
        answer = f"{gap_x},{gap_y}"
    result = fs_post(fs_sid, f"{BASE_URL}/misc.php?mod=captcha&action=check", answer, cookies)
    ok = result.get("data") == "ok"
    logger.info(f"[SehuatangCaptcha] Check {answer}: {'OK' if ok else result.get('data','?')}")
    return ok, result


def complete_signin(fs_sid: str, cookies: list) -> dict:
    """Complete the sign-in after captcha passes."""
    html = fs_get(fs_sid, f"{BASE_URL}/plugin.php?id=dd_sign&ac=sign_v2", cookies)
    return extract_json(html)


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