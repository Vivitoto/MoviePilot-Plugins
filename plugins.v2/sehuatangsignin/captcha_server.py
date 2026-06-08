"""
Sehuatang captcha relay UI server - embedded Flask app for MP plugin.
Started on-demand, supports multi-account via URL path.
"""
import base64
import contextlib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
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
FS_URL_TEMPLATE = "{flaresolverr_url}"

# ─── Session state ────────────────────────────────────────
# Keyed by account_id: {fs_sid, captcha_data, solved, answer, ...}
# Keep a disk-backed copy because MP/plugin reloads or reverse-proxy targets can
# make the notification producer and HTTP relay handler run in different Python
# memory spaces. The store contains captcha images/session ids, not account cookies.
_captcha_sessions: dict = {}
_sessions_lock = threading.Lock()
_DEFAULT_SESSION_STORE_PATH = os.path.join("/var/tmp", "sehuatangsignin", "captcha_sessions.json")
_SESSION_STORE_PATH = os.environ.get("SEHUATANG_CAPTCHA_SESSION_STORE", _DEFAULT_SESSION_STORE_PATH)
# Optional compatibility bridge for explicitly configured legacy relay stores.
# Do not default to /tmp: MoviePilot/nginx may use /tmp for cache files and will
# complain about arbitrary JSON files there ("cache file ... is too small").
_LEGACY_SESSION_STORE_PATH = os.environ.get("SEHUATANG_CAPTCHA_LEGACY_SESSION_STORE", "").strip()
_SESSION_LOCK_PATH = f"{_SESSION_STORE_PATH}.lock"
_SITE_CAPTCHA_LOCK_PATH = f"{_SESSION_STORE_PATH}.site.lock"
_SITE_CAPTCHA_THROTTLE_PATH = f"{_SESSION_STORE_PATH}.site.next"
try:
    _SESSION_MAX_AGE_SECONDS = max(
        3600,
        int(os.environ.get("SEHUATANG_CAPTCHA_SESSION_MAX_AGE_SECONDS", "86400")),
    )
except ValueError:
    _SESSION_MAX_AGE_SECONDS = 86400

try:
    _DEFAULT_CAPTCHA_SITE_TTL_SECONDS = max(10, int(os.environ.get("SEHUATANG_CAPTCHA_SITE_TTL_SECONDS", "30")))
except ValueError:
    _DEFAULT_CAPTCHA_SITE_TTL_SECONDS = 30

try:
    _SITE_CAPTCHA_MIN_INTERVAL_SECONDS = max(
        0.0,
        float(os.environ.get("SEHUATANG_CAPTCHA_MIN_INTERVAL_SECONDS", "8")),
    )
except ValueError:
    _SITE_CAPTCHA_MIN_INTERVAL_SECONDS = 8.0

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Linux fallback
    fcntl = None


@contextlib.contextmanager
def _file_lock(path: str):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    lock_file = open(path, "a+", encoding="utf-8")
    try:
        if fcntl:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


@contextlib.contextmanager
def _session_store_lock():
    """Cross-process lock for the disk-backed captcha session store."""
    with _file_lock(_SESSION_LOCK_PATH):
        yield


@contextlib.contextmanager
def site_captcha_lock():
    """Cross-process short lock + minimum interval for captcha fetch endpoints."""
    with _file_lock(_SITE_CAPTCHA_LOCK_PATH):
        _wait_site_throttle_unlocked()
        try:
            yield
        finally:
            _save_site_throttle_unlocked()


@contextlib.contextmanager
def site_captcha_submit_lock():
    """Cross-process mutex for captcha check submits; never wait throttle before user answer check."""
    with _file_lock(_SITE_CAPTCHA_LOCK_PATH):
        yield


def wait_for_site_captcha_ready() -> float:
    """Wait until a site captcha request can be sent, without consuming a new throttle slot."""
    with _file_lock(_SITE_CAPTCHA_LOCK_PATH):
        return _wait_site_throttle_unlocked()


def get_site_captcha_wait_seconds() -> float:
    """Return seconds until the next site captcha request is allowed."""
    with _file_lock(_SITE_CAPTCHA_LOCK_PATH):
        return max(0.0, _read_site_throttle_unlocked() - time.time())


def defer_site_captcha_requests(seconds: float) -> float:
    """Push the next allowed site captcha request into the future without sleeping."""
    try:
        delay = max(0.0, float(seconds or 0))
    except (TypeError, ValueError):
        delay = 0.0
    if delay <= 0:
        return 0.0
    with _file_lock(_SITE_CAPTCHA_LOCK_PATH):
        existing = _read_site_throttle_unlocked()
        next_allowed = max(existing, time.time() + delay)
        _write_site_throttle_unlocked(next_allowed)
        return max(0.0, next_allowed - time.time())


def _read_site_throttle_unlocked() -> float:
    try:
        with open(_SITE_CAPTCHA_THROTTLE_PATH, "r", encoding="utf-8") as f:
            return float((f.read() or "0").strip() or 0)
    except FileNotFoundError:
        return 0.0
    except Exception:
        return 0.0


def _write_site_throttle_unlocked(next_allowed: float):
    directory = os.path.dirname(_SITE_CAPTCHA_THROTTLE_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(_SITE_CAPTCHA_THROTTLE_PATH, "w", encoding="utf-8") as f:
        f.write(str(float(next_allowed or 0)))


def _wait_site_throttle_unlocked() -> float:
    """Sleep until the next site captcha request is allowed. Caller holds site lock."""
    total_slept = 0.0
    while True:
        next_allowed = _read_site_throttle_unlocked()
        delay = next_allowed - time.time()
        if delay <= 0:
            return total_slept
        logger.debug(f"[SehuatangCaptcha] Site captcha throttle sleep {delay:.1f}s")
        slept = min(delay, 60)
        time.sleep(slept)
        total_slept += slept


def _save_site_throttle_unlocked():
    """Record the next allowed site captcha request time. Caller holds site lock."""
    if _SITE_CAPTCHA_MIN_INTERVAL_SECONDS <= 0:
        return
    try:
        jitter = random.uniform(0, min(3.0, _SITE_CAPTCHA_MIN_INTERVAL_SECONDS / 2))
        next_allowed = time.time() + _SITE_CAPTCHA_MIN_INTERVAL_SECONDS + jitter
        _write_site_throttle_unlocked(next_allowed)
    except Exception as e:
        logger.debug(f"[SehuatangCaptcha] Failed to save site throttle state: {e}")


def _session_store_paths() -> list[str]:
    paths = []
    for path in (_SESSION_STORE_PATH, _LEGACY_SESSION_STORE_PATH):
        if path and path not in paths:
            paths.append(path)
    return paths


def _merge_session(target: dict, key: str, incoming: dict):
    current = target.get(key)
    if not isinstance(current, dict):
        target[key] = incoming
        return
    # Prefer whichever side has the user's answer; this lets a stale/default relay
    # write solved=True while the plugin polls the primary plugin-data store.
    if incoming.get("solved") and not current.get("solved"):
        target[key] = incoming
        return
    if float(incoming.get("solved_at") or 0) > float(current.get("solved_at") or 0):
        target[key] = incoming
        return
    if float(incoming.get("created_at") or 0) > float(current.get("created_at") or 0) and not current.get("solved"):
        target[key] = incoming


def _prune_sessions_unlocked(now: float | None = None) -> bool:
    """Drop stale sessions left behind by crashes/reloads to keep the store bounded."""
    now = now or time.time()
    changed = False
    for account_id, session in list(_captcha_sessions.items()):
        created_at = 0
        if isinstance(session, dict):
            created_at = float(session.get("created_at") or 0)
        if not created_at or now - created_at > _SESSION_MAX_AGE_SECONDS:
            fs_sid = session.get("fs_sid") if isinstance(session, dict) else None
            _captcha_sessions.pop(account_id, None)
            _destroy_fs_session_later(fs_sid)
            changed = True
    return changed


def _destroy_fs_session_later(fs_sid: str | None):
    """Best-effort cleanup for stale FlareSolverr sessions without blocking store locks."""
    if not fs_sid:
        return
    try:
        if not globals().get("_fs_url_cache"):
            return
        threading.Thread(target=fs_destroy_session, args=(fs_sid,), daemon=True).start()
    except Exception:
        pass


def _load_sessions_unlocked() -> dict:
    global _captcha_sessions
    loaded = {}
    changed = False
    for path in _session_store_paths():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(value, dict):
                        before = loaded.get(key)
                        _merge_session(loaded, key, value)
                        changed = changed or before != loaded.get(key)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"[SehuatangCaptcha] Failed to load session store {path}: {e}")
    if loaded:
        changed = changed or loaded != _captcha_sessions
        _captcha_sessions = loaded
    if _prune_sessions_unlocked():
        changed = True
    if changed:
        _save_sessions_unlocked()
    return _captcha_sessions


def _save_sessions_unlocked():
    _prune_sessions_unlocked()
    for path in _session_store_paths():
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp_path = f"{path}.{os.getpid()}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(_captcha_sessions, f, ensure_ascii=False)
            os.replace(tmp_path, path)
        except Exception as e:
            logger.warning(f"[SehuatangCaptcha] Failed to save session store {path}: {e}")

# ─── HTML Template ─────────────────────────────────────────
CAPTCHA_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>98 验证码 - {{ display_account or account }}</title>
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
  .thumb-preview { box-sizing: content-box; display:block; max-width:none; margin:8px auto; border-radius:6px; border:1px solid #0f3460; background:#fff; padding:6px; object-fit: fill; image-rendering: auto; }
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
<p class="account-tag">账号：{{ display_account or account }}</p>

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
  {% if not request_started %}
  <p>准备获取实时验证码</p>
  <p class="info">为避免微信/反代/链接预览提前打开页面导致验证码过期，请在确认本人已打开页面后点击下面按钮。</p>
  <form method="post" action="/{{ route_path or route_account or account }}/request">
    <button class="btn" type="submit">▶️ 开始获取验证码</button>
  </form>
  {% else %}
  <p>正在获取验证码...</p>
  <p class="info">后台正在现场获取验证码，避免通知延迟吃掉有效期。</p>
  <p class="info">页面会自动刷新，请保持打开。</p>
  <button class="btn btn-subtle" onclick="location.reload()">🔄 立即刷新</button>
  <script>setTimeout(function(){ location.reload(); }, 2000);</script>
  {% endif %}
</div>
{% endif %}
</div>

<script>
const capType = "{{ captcha_type }}";
const dx = {{ dx }}, dy = {{ dy }}, tw = {{ tw }}, th = {{ th }};
const masterW = {{ master_w }}, masterH = {{ master_h }};
const account = "{{ route_account or account }}";
let answer = "";
let expired = false;

function setAnswer(value, label) {
  answer = String(value || "");
  const btn = document.getElementById('submit-btn');
  const ans = document.getElementById('answer-info');
  const act = document.getElementById('action-info');
  if (ans) ans.textContent = answer || '-';
  if (act) act.textContent = label || answer || '尚未操作';
  if (btn) {
    btn.disabled = expired || !answer;
    btn.textContent = expired ? '验证码已过期' : (answer ? '✅ 提交答案' : '先完成验证码操作');
  }
}

function showTimer(sec) {
  const el = document.getElementById('expire-timer');
  if (!el) return;
  const tick = () => {
    if (sec <= 0) {
      expired = true;
      el.textContent = '⚠️ 验证码已过期，请等待下一轮新链接';
      const btn = document.getElementById('submit-btn');
      if (btn) { btn.disabled = true; btn.textContent = '验证码已过期'; }
      return;
    }
    const m = Math.floor(sec / 60), s = sec % 60;
    el.textContent = `⏰ 剩余 ${m}:${String(s).padStart(2,'0')} 有效`;
    sec--;
    setTimeout(tick, 1000);
  };
  tick();
}
{% if captcha_ready %}
showTimer({{ expire_seconds }});
{% endif %}

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
  if (expired || !answer) return;
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
        "display_account": "",
        "route_account": "",
        "route_path": "",
        "captcha_type": "",
        "dx": 0,
        "dy": 0,
        "tw": 64,
        "th": 64,
        "master_w": 300,
        "master_h": 220,
        "master_b64": "",
        "thumb_b64": "",
        "expire_seconds": _DEFAULT_CAPTCHA_SITE_TTL_SECONDS,
        "debug_json": "{}",
    }
    defaults.update(kwargs)
    return render_template_string(CAPTCHA_HTML, **defaults)


# ─── Flask App Factory ────────────────────────────────────
def create_app():
    if Flask is None:
        raise RuntimeError("Flask is not installed. Run: pip install flask")
    app = Flask(__name__)

    @app.route("/__sht_health")
    def relay_health():
        with _sessions_lock, _session_store_lock():
            session_count = len(_load_sessions_unlocked())
        return {
            "ok": True,
            "plugin": "SehuatangSignin",
            "version": "1.0.9",
            "sessionCount": session_count,
        }

    @app.route("/<path:account_id>")
    def captcha_page(account_id):
        if account_id in ("favicon.ico", "robots.txt"):
            return "", 404
        with _sessions_lock, _session_store_lock():
            session = _load_sessions_unlocked().get(account_id)
        if not session:
            return _render_captcha_template(account=account_id, route_path=quote(account_id, safe=""), captcha_ready=False,
                                            solved=False, error="会话不存在或已过期，请重新获取验证码")
        display_account = session.get("account_id") or account_id
        if session.get("solved"):
            return _render_captcha_template(account=account_id, route_account=account_id,
                                            route_path=quote(account_id, safe=""),
                                            display_account=display_account, captcha_ready=False,
                                            solved=True, answer=session.get("answer", ""))
        if not session.get("captcha_data"):
            return _render_captcha_template(account=account_id, route_account=account_id,
                                            route_path=quote(account_id, safe=""),
                                            display_account=display_account, captcha_ready=False,
                                            solved=False, request_started=bool(session.get("requested")),
                                            expire_seconds=0)

        with _sessions_lock, _session_store_lock():
            session = _load_sessions_unlocked().get(account_id, session)
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
            route_account=account_id,
            route_path=quote(account_id, safe=""),
            display_account=display_account,
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
            expire_seconds=max(0, int(float(session.get("site_expires_at") or (created_at + _DEFAULT_CAPTCHA_SITE_TTL_SECONDS)) - time.time())),
            debug_json=json.dumps(debug, ensure_ascii=False, indent=2),
        )

    @app.route("/<path:account_id>/request", methods=["POST"])
    def captcha_request(account_id):
        with _sessions_lock, _session_store_lock():
            sessions = _load_sessions_unlocked()
            session = sessions.get(account_id)
            display_account = session.get("account_id") if session else account_id
            if not session:
                return _render_captcha_template(account=account_id, route_account=account_id,
                                                route_path=quote(account_id, safe=""),
                                                display_account=display_account, solved=False,
                                                error="会话不存在或已过期，请重新获取验证码")
            first_request = False
            if not session.get("requested"):
                session["requested"] = True
                session["requested_at"] = time.time()
                _captcha_sessions[account_id] = session
                _save_sessions_unlocked()
                first_request = True
        if first_request:
            logger.info(f"[SehuatangCaptcha] Account {account_id}: user confirmed captcha fetch request")
        return redirect(f"/{quote(account_id, safe='')}", code=303)

    @app.route("/<path:account_id>/submit", methods=["POST"])
    def captcha_submit(account_id):
        with _sessions_lock, _session_store_lock():
            session = _load_sessions_unlocked().get(account_id)
            display_account = session.get("account_id") if session else account_id
            if not session:
                return _render_captcha_template(account=account_id, route_account=account_id,
                                                display_account=display_account, solved=False,
                                                error="会话已过期")
            if session.get("solved"):
                return _render_captcha_template(account=account_id, route_account=account_id,
                                                display_account=display_account, solved=True,
                                                answer=session.get("answer", ""))

            site_expires_at = float(session.get("site_expires_at") or 0)
            if site_expires_at and time.time() > site_expires_at:
                return _render_captcha_template(account=account_id, route_account=account_id,
                                                display_account=display_account, solved=False,
                                                error="验证码已过期，请等待下一轮新链接")

            answer = str(request.form.get("answer") or "").strip()
            if not answer:
                return "missing answer", 400
            session["answer"] = answer
            session["solved"] = True
            session["solved_at"] = time.time()
            # The plugin only needs the answer after submit. Drop embedded base64
            # captcha images immediately so solved sessions do not keep image data
            # on disk while the plugin is still polling or after a hot reload.
            captcha_data = session.get("captcha_data")
            if isinstance(captcha_data, dict):
                session["captcha_data"] = {"type": captcha_data.get("type")}
            _captcha_sessions[account_id] = session
            _save_sessions_unlocked()

        logger.info(f"[SehuatangCaptcha] Account {account_id}: user submitted {answer}")
        return _render_captcha_template(account=account_id, route_account=account_id,
                                        display_account=display_account, solved=True, answer=answer)

    return app


# ─── Session management ───────────────────────────────────
def init_session(account_id: str, display_account: str | None = None):
    """Initialize a captcha session for an account/session key."""
    with _sessions_lock, _session_store_lock():
        _load_sessions_unlocked()
        _captcha_sessions[account_id] = {
            "account_id": display_account or account_id,
            "solved": False, "answer": None, "captcha_data": None,
            "requested": False, "requested_at": None,
            "fs_sid": None, "created_at": time.time(),
        }
        _save_sessions_unlocked()
    logger.info(f"[SehuatangCaptcha] Initialized captcha session for account {account_id}")


def destroy_session(account_id: str, destroy_fs: bool = True):
    """Clean up a captcha session."""
    with _sessions_lock, _session_store_lock():
        _load_sessions_unlocked()
        session = _captcha_sessions.pop(account_id, None)
        _save_sessions_unlocked()
    if destroy_fs and session and session.get("fs_sid"):
        fs_destroy_session(session["fs_sid"])


def set_captcha_data(account_id: str, data: dict, fs_sid: str):
    """Store captcha data for display."""
    with _sessions_lock, _session_store_lock():
        _load_sessions_unlocked()
        session = _captcha_sessions.get(account_id)
        if session:
            ttl = int(data.get("site_ttl_seconds") or _DEFAULT_CAPTCHA_SITE_TTL_SECONDS)
            ttl = max(10, ttl)
            session["captcha_data"] = data
            session["fs_sid"] = fs_sid
            session["site_expires_at"] = time.time() + ttl
            session["solved"] = False
            _captcha_sessions[account_id] = session
            _save_sessions_unlocked()
    logger.info(f"[SehuatangCaptcha] Stored captcha data for account {account_id}: type={data.get('type')}")


def is_solved(account_id: str) -> bool:
    """Check if user has submitted the captcha."""
    with _sessions_lock, _session_store_lock():
        session = _load_sessions_unlocked().get(account_id)
        return bool(session and session.get("solved"))


def get_answer(account_id: str) -> str:
    """Get the user's raw answer string."""
    with _sessions_lock, _session_store_lock():
        session = _load_sessions_unlocked().get(account_id)
        if session and session.get("answer"):
            return str(session["answer"])
    return ""


def get_solved_at(account_id: str) -> float:
    """Get the timestamp when user submitted the captcha answer."""
    with _sessions_lock, _session_store_lock():
        session = _load_sessions_unlocked().get(account_id)
        if session and session.get("solved_at"):
            try:
                return float(session["solved_at"])
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def is_requested(account_id: str) -> bool:
    """Check if user has opened the relay page and requested a live captcha fetch."""
    with _sessions_lock, _session_store_lock():
        session = _load_sessions_unlocked().get(account_id)
        return bool(session and session.get("requested"))


def is_expired(account_id: str, timeout: int = 300) -> bool:
    """Check if the session has expired."""
    with _sessions_lock, _session_store_lock():
        session = _load_sessions_unlocked().get(account_id)
        if not session:
            return True
        return time.time() - session.get("created_at", 0) > timeout


# ─── FS helpers ───────────────────────────────────────────
_fs_url_cache: str = ""
_proxy_url_cache: str = ""
_fs_user_agents: dict = {}


def _get_fs_url() -> str:
    return _fs_url_cache


def _looks_like_flaresolverr_api_url(url: str) -> bool:
    clean = (url or "").strip().rstrip("/").lower()
    return clean.endswith("/v1")


def set_fs_url(url: str):
    global _fs_url_cache
    _fs_url_cache = str(url or "").strip().rstrip("/")


def set_proxy_url(url: str):
    global _proxy_url_cache
    _proxy_url_cache = url.strip()


def set_site_captcha_min_interval(seconds: float):
    """Set cross-account minimum interval between site captcha fetch/check requests."""
    global _SITE_CAPTCHA_MIN_INTERVAL_SECONDS
    try:
        _SITE_CAPTCHA_MIN_INTERVAL_SECONDS = max(0.0, float(seconds or 0))
    except (TypeError, ValueError):
        _SITE_CAPTCHA_MIN_INTERVAL_SECONDS = 8.0


def set_session_store_path(path: str):
    """Set captcha session store path, preferably under MoviePilot plugin data dir."""
    global _SESSION_STORE_PATH, _SESSION_LOCK_PATH, _SITE_CAPTCHA_LOCK_PATH, _SITE_CAPTCHA_THROTTLE_PATH
    clean = str(path or "").strip()
    if not clean:
        return
    directory = os.path.dirname(clean)
    if directory:
        os.makedirs(directory, exist_ok=True)
    _SESSION_STORE_PATH = clean
    _SESSION_LOCK_PATH = f"{_SESSION_STORE_PATH}.lock"
    _SITE_CAPTCHA_LOCK_PATH = f"{_SESSION_STORE_PATH}.site.lock"
    _SITE_CAPTCHA_THROTTLE_PATH = f"{_SESSION_STORE_PATH}.site.next"
    logger.info(f"[SehuatangCaptcha] Session store path: {_SESSION_STORE_PATH}")


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
    fs_url = _get_fs_url()
    if not _looks_like_flaresolverr_api_url(fs_url):
        return {"error": "FlareSolverr API 地址必须填写完整 /v1 路径"}
    if session_id:
        payload["session"] = session_id
    if cookies:
        payload["cookies"] = cookies
    proxy = _proxy_param()
    if proxy:
        payload.update(proxy)
    try:
        r = requests.post(
            FS_URL_TEMPLATE.format(flaresolverr_url=fs_url),
            json=payload,
            timeout=timeout + 10,
        )
        d = r.json()
    except Exception as e:
        return {"error": f"FlareSolverr 调用失败: {e}"}
    if d.get("status") != "ok":
        return {"error": d.get("message", "unknown")}
    sol = d.get("solution", {})
    user_agent = sol.get("userAgent") or sol.get("user_agent")
    if session_id and user_agent:
        _fs_user_agents[session_id] = user_agent
    _merge_solution_cookies(cookies, sol.get("cookies", []))
    return {"html": sol.get("response", ""), "cookies": sol.get("cookies", []),
            "status": sol.get("status", 0), "userAgent": user_agent}


def fs_create_session() -> str:
    fs_url = _get_fs_url()
    if not _looks_like_flaresolverr_api_url(fs_url):
        logger.warning("[SehuatangCaptcha] FlareSolverr API 地址必须填写完整 /v1 路径")
        return ""
    payload = {"cmd": "sessions.create", "maxTimeout": 90000}
    proxy = _proxy_param()
    if proxy:
        payload.update(proxy)
    try:
        r = requests.post(
            FS_URL_TEMPLATE.format(flaresolverr_url=fs_url),
            json=payload,
            timeout=15,
        )
        d = r.json()
    except Exception as e:
        logger.warning(f"[SehuatangCaptcha] FlareSolverr 会话创建失败: {e}")
        return ""
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


def fs_get(fs_sid: str, url: str, cookies: list, headers: dict | None = None) -> str:
    payload = {"cmd": "request.get", "url": url, "maxTimeout": 60000}
    if headers:
        payload["headers"] = {k: v for k, v in headers.items() if v}
    return fs_call(fs_sid, payload, cookies).get("html", "")


def _browser_fetch_headers(fs_sid: str, *, content_type: str | None = None,
                           include_origin: bool = False) -> dict:
    headers = {
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": f"{BASE_URL}/plugin.php?id=dd_sign",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        **({"User-Agent": _fs_user_agents.get(fs_sid)} if _fs_user_agents.get(fs_sid) else {}),
    }
    if include_origin:
        headers["Origin"] = BASE_URL
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _captcha_headers(fs_sid: str) -> dict:
    # HAR-confirmed: both the first captcha load and the refresh icon call the
    # same loadCaptcha() function, i.e. browser fetch('misc.php?mod=captcha')
    # from dd_sign. For same-origin GET fetch Chrome did not send Origin, but it
    # did send no-cache + Sec-Fetch cors/empty headers.
    return _browser_fetch_headers(fs_sid, include_origin=False)


def _check_headers(fs_sid: str) -> dict:
    # HAR-confirmed: check is same-origin POST text/plain and does include Origin.
    return _browser_fetch_headers(fs_sid, content_type="text/plain", include_origin=True)


def _is_cf_challenge_html(html: str) -> bool:
    if not html:
        return False
    lower = html.lower()
    return any(marker in lower for marker in (
        "enable javascript and cookies to continue",
        "challenge-error-text",
        "_cf_chl_opt",
        "cf-challenge",
        "cf_chl_",
    ))


def _parse_check_html(html: str) -> dict:
    if _is_cf_challenge_html(html):
        return {"code": 403, "message": "Cloudflare challenge returned", "data": "cf_challenge", "raw": html[:300]}
    if "static/safe/js/web.js" in html or "enter-btn" in html or "safeid=" in html:
        return {"code": 403, "message": "safe gate returned", "data": "safe_gate", "raw": html[:300]}
    m = re.search(r"<body>(.+?)</body>", html, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return {"raw": m.group(1)[:300]}
    try:
        return json.loads(html)
    except json.JSONDecodeError:
        return {"raw": html[:300]}


def _merge_response_cookiejar(cookies: list, jar):
    returned = []
    for c in jar:
        returned.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain or ".sehuatang.net",
            "path": c.path or "/",
        })
    _merge_solution_cookies(cookies, returned)


def _direct_check_post(fs_sid: str, url: str, body: str, cookies: list) -> dict:
    """Browser-like direct POST for captcha check using current cookies/proxy/UA."""
    session = requests.Session()
    proxy = _proxy_url_cache.strip()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    headers = _check_headers(fs_sid)
    session.headers.update({k: v for k, v in headers.items() if v})
    for c in cookies or []:
        if not c.get("name"):
            continue
        session.cookies.set(
            c.get("name"), c.get("value", ""),
            domain=c.get("domain") or ".sehuatang.net",
            path=c.get("path") or "/",
        )
    try:
        resp = session.post(url, data=body.encode("utf-8"), timeout=30)
        _merge_response_cookiejar(cookies, session.cookies)
        result = _parse_check_html(resp.text)
        result.setdefault("http_status", resp.status_code)
        result.setdefault("via", "direct")
        return result
    except Exception as e:
        return {"code": 599, "message": f"direct check failed: {e}", "data": "direct_error"}


def fs_post(fs_sid: str, url: str, body: str, cookies: list) -> dict:
    # Check endpoint is sensitive to browser/XHR context. Direct requests with
    # FS-updated cookies has proven closer than FlareSolverr request.post, which
    # can trigger safe_gate and invalidate the current captcha state.
    direct_result = _direct_check_post(fs_sid, url, body, cookies)
    if direct_result.get("data") != "direct_error":
        logger.info(f"[SehuatangCaptcha] Direct check result: {direct_result.get('data') or direct_result.get('message')}")
        return direct_result

    r = fs_call(fs_sid, {
        "cmd": "request.post",
        "url": url,
        "postData": body,
        "headers": _check_headers(fs_sid),
        "maxTimeout": 30000,
    }, cookies)
    html = r.get("html", "")
    result = _parse_check_html(html)
    result.setdefault("via", "flaresolverr")
    logger.info(f"[SehuatangCaptcha] Direct check failed; FlareSolverr check result: {result.get('data')}")
    return result


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

    Supported manual relay types: slide, rotate, click. Drag is unsupported for
    manual relay, so keep refreshing with a slower backoff until a supported
    captcha appears, the endpoint rate-limits, or the total wait cap expires.
    """
    supported_types = {"slide", "rotate", "click"}
    if max_retries is None:
        max_retries = 4
    attempt = 0
    deadline = time.time() + max(1, int(max_wait_seconds or 300))
    while (max_retries is None or attempt < max_retries) and time.time() < deadline:
        attempt += 1
        if attempt > 1:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            # Keep retries deliberately paced. The captcha endpoint is site-wide
            # sensitive: unsupported drag captchas should be refreshed, but not
            # hammered into 429 while other accounts are waiting behind the lock.
            delay = min(random.uniform(15, 30), remaining)
            logger.debug(f"[SehuatangCaptcha] Waiting {delay:.1f}s before captcha retry")
            time.sleep(delay)
        with site_captcha_lock():
            html = fs_get(fs_sid, f"{BASE_URL}/misc.php?mod=captcha", cookies, headers=_captcha_headers(fs_sid))
        cap = extract_json(html)
        code = cap.get("code")
        if code == 429:
            logger.warning("[SehuatangCaptcha] Captcha endpoint returned 429; stop retrying this account")
            return {"error": "rate_limited", "message": "验证码接口 429 限流，本账号本轮停止获取"}
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
        if cap_type == "drag":
            logger.info(f"[SehuatangCaptcha] Attempt {attempt}: got unsupported drag, refreshing slowly")
        else:
            logger.info(f"[SehuatangCaptcha] Attempt {attempt}: got unsupported {cap_type}, retrying with jitter")
    logger.warning(f"[SehuatangCaptcha] Captcha fetch timed out after {max_wait_seconds}s")
    return None


def check_sign_status(fs_sid: str, cookies: list) -> tuple:
    """Check if already signed in. Returns (is_signed, button_text)."""
    html = fs_get(fs_sid, f"{BASE_URL}/plugin.php?id=dd_sign", cookies)
    btn = re.search(r'id="signin-btn"[^>]*>([^<]+)<', html) if html else None
    if btn:
        return "已签到" in btn.group(1), btn.group(1)
    if html:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        if "已签到" in text:
            return True, "已签到"
        if "今日未签到" in text or "点击签到" in text:
            return False, "今日未签到，点击签到"
        if "static/safe/js/web.js" in html or "safeid=" in html or "enter-btn" in html:
            return False, "safe_gate"
    return False, "N/A"


def submit_check(fs_sid: str, answer: str, cap_type: str, cookies: list) -> tuple:
    """Submit raw captcha answer. Returns (ok, result_dict)."""
    result = fs_post(fs_sid, f"{BASE_URL}/misc.php?mod=captcha&action=check", answer, cookies)
    ok = result.get("data") == "ok"
    logger.info(f"[SehuatangCaptcha] Check {cap_type} answer={answer}: {'OK' if ok else result.get('data','?')}")
    return ok, result


def complete_signin(fs_sid: str, cookies: list) -> dict:
    """Complete the sign-in after captcha passes."""
    # HAR-confirmed: page JS calls sign_v2 via same-origin browser fetch, not a
    # top-level navigation. Match that shape to avoid subtle session/timing drift.
    html = fs_get(
        fs_sid,
        f"{BASE_URL}/plugin.php?id=dd_sign&ac=sign_v2",
        cookies,
        headers=_browser_fetch_headers(fs_sid, include_origin=False),
    )
    return extract_json(html)


app = create_app() if Flask is not None else None


# ─── Embedded server ──────────────────────────────────────
_server_thread = None
_server_port = 5099
_server = None


def start_server(port: int = 5099):
    """Start the embedded captcha relay HTTP server in a background thread."""
    global _server_thread, _server_port, _server
    if _server_thread and _server_thread.is_alive():
        return
    if Flask is None:
        logger.error("[SehuatangCaptcha] Cannot start server: Flask not installed")
        return

    _server_port = port

    def _run():
        global _server
        try:
            from werkzeug.serving import make_server
            _server = make_server("0.0.0.0", port, app, threaded=True)
            _server.serve_forever()
        except Exception:
            logger.error(f"[SehuatangCaptcha] Server error: {traceback.format_exc()}")
        finally:
            _server = None

    _server_thread = threading.Thread(target=_run, daemon=True)
    _server_thread.start()
    logger.info(f"[SehuatangCaptcha] Captcha relay server started on port {port}")


def stop_server():
    """Stop the embedded captcha relay server so plugin reloads do not leave stale listeners."""
    global _server_thread, _server
    server = _server
    if server:
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            logger.warning(f"[SehuatangCaptcha] Server shutdown error: {traceback.format_exc()}")
    if _server_thread and _server_thread.is_alive():
        _server_thread.join(timeout=3)
    _server_thread = None
    _server = None
