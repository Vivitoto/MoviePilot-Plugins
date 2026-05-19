import json
import re
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from requests import RequestException

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType

from .captcha_server import (
    check_sign_status,
    complete_signin,
    destroy_session,
    fetch_captcha_for_account,
    fs_create_session,
    fs_get,
    get_answer,
    init_session,
    is_expired,
    is_solved,
    set_captcha_data,
    set_fs_url,
    set_proxy_url,
    start_server,
    submit_check,
    BASE_URL,
)


class SehuatangSignin(_PluginBase):
    plugin_name = "色花堂签到"
    plugin_desc = "FlareSolverr + 人工辅助验证码，支持多账号。遇到验证码时企微通知URL，手动拖动后自动完成签到。"
    plugin_icon = "https://raw.githubusercontent.com/Vivitoto/MoviePilot-Plugins/main/icons/shtsignin.png"
    plugin_version = "0.1.0"
    plugin_author = "Vivitoto"
    author_url = "https://github.com/Vivitoto"
    plugin_config_prefix = "sehuatang_signin_"
    plugin_order = 22
    auth_level = 1

    # ── Config defaults ──────────────────────────────────
    _enabled = False
    _notify = True
    _onlyonce = False
    _cron = "30 9 * * *"
    _timeout = 30
    _timezone = "Asia/Shanghai"

    # Multi-account: one per line, "name | cookie1=val1; cookie2=val2"
    _accounts_text = ""
    _accounts: list = []

    # FlareSolverr
    _flaresolverr_url = "http://127.0.0.1:8191"
    _use_flaresolverr = True

    # Proxy（访问 sehuatang 需要）
    _proxy_url = ""

    # Captcha relay
    _captcha_port = 5099
    _captcha_timeout = 300
    _public_base_url = ""

    _scheduler: Optional[BackgroundScheduler] = None
    _history_key = "history"
    _last_result_key = "last_result"

    def init_plugin(self, config: dict = None):
        self.stop_service()
        try:
            if config:
                self._enabled = config.get("enabled", False)
                self._notify = config.get("notify", True)
                self._onlyonce = config.get("onlyonce", False)
                self._cron = config.get("cron") or "30 9 * * *"
                self._timeout = max(1, int(config.get("timeout") or 30))
                self._accounts_text = str(config.get("accounts_text") or "").strip()
                self._flaresolverr_url = str(config.get("flaresolverr_url") or "http://127.0.0.1:8191").rstrip("/")
                self._use_flaresolverr = config.get("use_flaresolverr", True)
                self._proxy_url = str(config.get("proxy_url") or "").strip()
                self._captcha_port = max(1, int(config.get("captcha_port") or 5099))
                self._captcha_timeout = max(60, int(config.get("captcha_timeout") or 300))
                self._public_base_url = str(config.get("public_base_url") or "").strip().rstrip("/")
                self._parse_accounts()

            # Start embedded captcha server
            set_fs_url(self._flaresolverr_url)
            set_proxy_url(self._proxy_url)
            start_server(self._captcha_port)

            if self._onlyonce:
                logger.info("[SehuatangSignin] 保存配置后执行一次")
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    func=self._run_once,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                )
                self._onlyonce = False
                self._update_config()
                if self._scheduler.get_jobs():
                    self._scheduler.start()
        except Exception as e:
            logger.error(f"[SehuatangSignin] 初始化错误：{str(e)}", exc_info=True)

    def get_state(self) -> bool:
        return self._enabled and bool(self._flaresolverr_url)

    def _update_config(self):
        self.update_config({
            "enabled": self._enabled, "notify": self._notify, "onlyonce": self._onlyonce,
            "cron": self._cron, "timeout": self._timeout,
            "accounts_text": self._accounts_text,
            "flaresolverr_url": self._flaresolverr_url,
            "use_flaresolverr": self._use_flaresolverr,
            "proxy_url": self._proxy_url,
            "captcha_port": self._captcha_port, "captcha_timeout": self._captcha_timeout,
            "public_base_url": self._public_base_url,
        })

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/sht_signin",
            "event": EventType.PluginAction,
            "desc": "执行色花堂签到",
            "category": "站点",
            "data": {"action": "sht_signin"},
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{"path": "/run", "endpoint": self.api_run, "methods": ["GET"], "summary": "执行签到"}]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._cron:
            return []
        return [{
            "id": "SehuatangSignin",
            "name": "色花堂签到",
            "trigger": CronTrigger.from_crontab(self._cron),
            "func": self._run_cron,
            "kwargs": {},
        }]

    def get_page(self) -> List[dict]:
        """Detail page - show accounts and history."""
        history = self.get_data(self._history_key) or []

        page = []

        # ── Account list ──
        if self._accounts:
            account_rows = []
            for acct in self._accounts:
                name = acct.get("name", "?")
                latest = next((r for r in history if r.get("account") == name), None)
                status = "✅" if latest and latest.get("success") else "❓"
                last_time = latest.get("time", "-") if latest else "-"
                last_msg = latest.get("message", "-") if latest else "未执行"
                url = f"{self._public_base_url}/{name}" if self._public_base_url else f"http://localhost:{self._captcha_port}/{name}"
                account_rows.append([
                    {'component': 'td', 'text': name},
                    {'component': 'td', 'text': status},
                    {'component': 'td', 'text': last_time},
                    {'component': 'td', 'text': last_msg},
                    {'component': 'td', 'content': [{'component': 'a', 'props': {'href': url, 'target': '_blank'}, 'text': '🔗'}] if self._public_base_url else {'component': 'td', 'text': url[:40]}},
                ])

            page.append({
                'component': 'VCard',
                'props': {'variant': 'flat', 'class': 'mb-4'},
                'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'text-subtitle-1'}, 'text': f'👤 账号列表 ({len(self._accounts)})'},
                    {'component': 'VTable',
                     'props': {'density': 'compact', 'hover': True},
                     'content': [
                         {'component': 'thead', 'content': [{'component': 'tr', 'content': [
                             {'component': 'th', 'text': '名称'}, {'component': 'th', 'text': '状态'},
                             {'component': 'th', 'text': '最后执行'}, {'component': 'th', 'text': '结果'},
                             {'component': 'th', 'text': '验证码链接'},
                         ]}]},
                         {'component': 'tbody', 'content': [{'component': 'tr', 'content': row} for row in account_rows]},
                     ]}
                ]
            })
        else:
            page.append({
                'component': 'VCard',
                'props': {'variant': 'tonal', 'class': 'mb-4'},
                'content': [{'component': 'VCardItem', 'content': [{'component': 'div', 'text': '尚未配置账号，请先在设置中填写账号列表'}]}]
            })

        # ── History ──
        if history:
            rows = []
            for h in history[:20]:
                rows.append([
                    {'component': 'td', 'text': h.get('account', '-')},
                    {'component': 'td', 'text': '✅' if h.get('success') else '❌'},
                    {'component': 'td', 'text': h.get('time', '-')},
                    {'component': 'td', 'text': h.get('message', '-')},
                ])
            page.append({
                'component': 'VCard',
                'props': {'variant': 'flat'},
                'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'text-subtitle-1'}, 'text': '📋 执行记录'},
                    {'component': 'VTable',
                     'props': {'density': 'compact', 'hover': True},
                     'content': [
                         {'component': 'thead', 'content': [{'component': 'tr', 'content': [
                             {'component': 'th', 'text': '账号'}, {'component': 'th', 'text': '结果'},
                             {'component': 'th', 'text': '时间'}, {'component': 'th', 'text': '详情'},
                         ]}]},
                         {'component': 'tbody', 'content': [{'component': 'tr', 'content': row} for row in rows]},
                     ]}
                ]
            })

        return page

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        version = getattr(settings, "VERSION_FLAG", "v1")
        cron_component = "VCronField" if version == "v2" else "VTextField"
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VCard',
                        'props': {'variant': 'flat', 'class': 'mb-4'},
                        'content': [{
                            'component': 'VCardItem',
                            'content': [
                                {'component': 'div', 'props': {'class': 'text-subtitle-2 font-weight-bold mb-3'}, 'text': '🟢 基本配置'},
                                {
                                    'component': 'VRow',
                                    'content': [
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '保存后执行一次'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'use_flaresolverr', 'label': '使用 FlareSolverr'}}]},
                                    ]
                                }
                            ]
                        }]
                    },
                    {
                        'component': 'VCard',
                        'props': {'variant': 'flat', 'class': 'mb-4'},
                        'content': [{
                            'component': 'VCardItem',
                            'content': [
                                {'component': 'div', 'props': {'class': 'text-subtitle-2 font-weight-bold mb-3'}, 'text': '👤 多账号配置'},
                                {
                                    'component': 'VRow',
                                    'content': [
                                        {'component': 'VCol', 'props': {'cols': 12}, 'content': [
                                            {'component': 'VTextarea', 'props': {
                                                'model': 'accounts_text',
                                                'label': '账号列表（每行一个，格式：名称 | Cookie）',
                                                'placeholder': '账号1 | _safe=xxx; cPNj_2132_auth=yyy; cPNj_2132_saltkey=zzz; cPNj_2132_sid=0\n账号2 | _safe=aaa; cPNj_2132_auth=bbb',
                                                'rows': 4,
                                                'auto-grow': True,
                                                'hint': '名称用于生成独立 URL 路径（如 /账号1）。\n需要添加账号就新增一行，删除账号就删掉对应行。'
                                            }}
                                        ]},
                                    ]
                                }
                            ]
                        }]
                    },
                    {
                        'component': 'VCard',
                        'props': {'variant': 'flat', 'class': 'mb-4'},
                        'content': [{
                            'component': 'VCardItem',
                            'content': [
                                {'component': 'div', 'props': {'class': 'text-subtitle-2 font-weight-bold mb-3'}, 'text': '🖥️ FlareSolverr 与验证码 Relay'},
                                {
                                    'component': 'VRow',
                                    'content': [
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'flaresolverr_url', 'label': 'FlareSolverr 地址', 'placeholder': 'http://127.0.0.1:8191'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'proxy_url', 'label': '代理地址（访问 sehuatang）', 'placeholder': 'http://192.168.31.216:7890'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'captcha_port', 'label': 'Relay 端口', 'type': 'number', 'placeholder': '5099'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'captcha_timeout', 'label': '超时(秒)', 'type': 'number', 'placeholder': '300'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VTextField', 'props': {'model': 'public_base_url', 'label': '公网地址（反代后）', 'placeholder': 'https://captcha.example.com'}}]},
                                    ]
                                }
                            ]
                        }]
                    },
                    {
                        'component': 'VCard',
                        'props': {'variant': 'tonal', 'class': 'mb-2'},
                        'content': [{
                            'component': 'VCardItem',
                            'content': [
                                {'component': 'div', 'props': {'class': 'text-subtitle-2 font-weight-bold mb-3'}, 'text': '⏰ 定时'},
                                {
                                    'component': 'VRow',
                                    'content': [
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': cron_component, 'props': {'model': 'cron', 'label': '定时任务'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'div', 'props': {'class': 'text-body-2 text-medium-emphasis mt-1'}, 'text': '💡 遇到验证码时通过企微通知发送 Relay URL，手动拖动完成后再自动提交签到'}]},
                                    ]
                                }
                            ]
                        }]
                    },
                    # ── 前置说明 ──
                    {
                        'component': 'VCard',
                        'props': {'variant': 'flat', 'class': 'mb-2'},
                        'content': [{
                            'component': 'VCardItem',
                            'content': [
                                {'component': 'div', 'props': {'class': 'text-subtitle-2 font-weight-bold mb-3'}, 'text': '📋 使用说明与注意事项'},
                                {'component': 'div', 'props': {'class': 'text-body-2 text-medium-emphasis'}, 'text': '\n'.join([
                                    '🔹 前置条件：',
                                    '  1. 部署 FlareSolverr（Docker 或独立进程），默认地址 http://127.0.0.1:8191',
                                    '  2. 安装 Flask 依赖：pip install flask',
                                    '  3. 配置反代：将 public_base_url 指向本机 captcha_port 端口',
                                    '  4. 确保 MP 环境能访问 sehuatang（需 proxy_url 或路由器代理）',
                                    '',
                                    '🔹 Cookie 获取方法：',
                                    '  1. 浏览器登录 sehuatang 后，F12 → Application → Cookies',
                                    '  2. 复制 _safe、cPNj_2132_auth、cPNj_2132_saltkey、cPNj_2132_sid 四个 cookie',
                                    '  3. 在账号配置中填入（每行一个账号）',
                                    '',
                                    '🔹 Cookie 必须在有效期内（通常 30 天左右），过期需重新获取',
                                    '🔹 验证码有效时间约 4-5 分钟，超时自动作废，下次触发重新获取',
                                    '🔹 验证码 URL 格式：{public_base_url}/{账号名称}（如 https://captcha.example.com/账号1）',
                                    '🔹 多账号按顺序处理，前一账号完成后自动进入下一账号',
                                    '🔹 遇到验证码时通过企微通知推送链接，不会静默失败',
                                    '🔹 插件会在启动时自动启动内嵌 HTTP Server（端口为 captcha_port），监听验证码提交',
                                ])},
                            ]
                        }]
                    },
                ]
            }
        ], {
            "enabled": False, "notify": True, "onlyonce": False, "cron": "30 9 * * *",
            "accounts_text": "",
            "flaresolverr_url": "http://127.0.0.1:8191",
            "use_flaresolverr": True,
            "proxy_url": "", "captcha_port": 5099, "captcha_timeout": 300,
            "public_base_url": "",
        }

    # ── Scheduler callbacks ───────────────────────────────
    def _run_once(self):
        self._parse_accounts()  # Re-parse in case config changed
        self._do_signin()

    def _run_cron(self):
        logger.info("[SehuatangSignin] 定时任务触发")
        self._parse_accounts()
        self._do_signin()

    def api_run(self):
        """API endpoint to trigger sign-in."""
        self._parse_accounts()
        self._do_signin()
        return {"code": 0, "message": "签到流程已启动"}

    @eventmanager.register(EventType.PluginAction)
    def _plugin_action_handler(self, event: Event):
        """Handle plugin action from command/WeChat menu."""
        if not event or not event.event_data:
            return
        if event.event_data.get("action") != "sht_signin":
            return
        logger.info("[SehuatangSignin] 收到手动触发指令")
        self._parse_accounts()
        self._do_signin()

    # ── Core sign-in logic (multi-account loop) ───────────
    def _do_signin(self):
        if not self._accounts:
            logger.warning("[SehuatangSignin] 未配置账号，请在插件设置中填写 accounts_text")
            return

        all_results = []
        for idx, account in enumerate(self._accounts):
            account_id = self._get_account_id(account, idx)
            logger.info(f"[SehuatangSignin] [{idx+1}/{len(self._accounts)}] 处理账号: {account_id}")
            result = self._signin_single(account, account_id)
            all_results.append({"account": account_id, **result})
            if idx < len(self._accounts) - 1:
                time.sleep(3)
        self._notify_summary(all_results)
        self._save_results(all_results)

    def _signin_single(self, account: dict, account_id: str) -> dict:
        steps = []
        result = {"success": False, "message": "", "steps": steps}
        try:
            cookies = self._build_cookies(account)
            logger.info(f"[SehuatangSignin] [{account_id}] 创建 FS 会话...")
            fs_sid = fs_create_session()
            if not fs_sid:
                result["message"] = "无法创建 FlareSolverr 会话"
                logger.error(f"[SehuatangSignin] [{account_id}] FS 会话创建失败")
                return result
            logger.info(f"[SehuatangSignin] [{account_id}] FS 会话: {fs_sid[:16]}...")
            is_signed, btn_text = check_sign_status(fs_sid, cookies)
            steps.append(f"签到状态：{btn_text}")
            logger.info(f"[SehuatangSignin] [{account_id}] 签到状态: {btn_text}")
            if is_signed:
                result["success"] = True
                result["message"] = "今日已签到"
                return result
            captcha_data = fetch_captcha_for_account(fs_sid, cookies)
            if not captcha_data:
                result["message"] = "无法获取 slide/drag 验证码"
                logger.warning(f"[SehuatangSignin] [{account_id}] 获取验证码失败")
                return result
            cap_type = captcha_data["type"]
            dy = captcha_data.get("display_y", 0)
            steps.append(f"验证码类型：{cap_type}")
            logger.info(f"[SehuatangSignin] [{account_id}] 验证码: {cap_type} display_y={dy}")
            init_session(account_id)
            set_captcha_data(account_id, captcha_data, fs_sid)
            captcha_url = f"{self._public_base_url}/{account_id}" if self._public_base_url else f"http://localhost:{self._captcha_port}/{account_id}"
            self._send_captcha_notification(cap_type, captcha_url, account_id)
            logger.info(f"[SehuatangSignin] [{account_id}] 已发送验证码通知，等待用户操作...")
            deadline = time.time() + self._captcha_timeout
            while time.time() < deadline:
                if is_solved(account_id):
                    logger.info(f"[SehuatangSignin] [{account_id}] 用户已完成验证码")
                    break
                if is_expired(account_id, self._captcha_timeout):
                    logger.warning(f"[SehuatangSignin] [{account_id}] 验证码会话已过期")
                    break
                time.sleep(2)
            if not is_solved(account_id):
                result["message"] = f"验证码超时（{self._captcha_timeout}秒）"
                destroy_session(account_id)
                return result
            gap_x, gap_y = get_answer(account_id)
            steps.append(f"用户提交：({gap_x},{gap_y})")
            logger.info(f"[SehuatangSignin] [{account_id}] 提交验证码 check: ({gap_x},{gap_y})")
            ok, check_result = submit_check(fs_sid, gap_x, gap_y, cap_type, dy, cookies)
            if not ok:
                result["message"] = f"验证码失败：{check_result.get('data', '?')}"
                logger.warning(f"[SehuatangSignin] [{account_id}] 验证码 check 失败: {check_result}")
                destroy_session(account_id)
                return result
            steps.append("验证码通过 ✅")
            logger.info(f"[SehuatangSignin] [{account_id}] 验证码通过，完成签到...")
            sign_result = complete_signin(fs_sid, cookies)
            code = sign_result.get("code", -1)
            msg = sign_result.get("message", "")
            steps.append(f"签到结果：{msg}")
            logger.info(f"[SehuatangSignin] [{account_id}] 签到结果: code={code} msg={msg}")
            if code == 200:
                result["success"] = True
                result["message"] = f"签到成功：{msg}"
            elif code == 201:
                result["success"] = True
                result["message"] = "今日已签到"
            else:
                result["message"] = f"签到异常：{msg}"
            destroy_session(account_id)
        except Exception as e:
            logger.error(f"[SehuatangSignin] [{account_id}] 异常：{traceback.format_exc()}")
            result["message"] = f"异常：{str(e)}"
            destroy_session(account_id)
        return result

    # ── Helpers ────────────────────────────────────────────
    def _parse_accounts(self):
        """Parse accounts_text into list of dicts. Format: name | cookie_string per line."""
        if not self._accounts_text:
            self._accounts = []
            return
        accounts = []
        for line in self._accounts_text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if "|" in line:
                name, cookie_str = line.split("|", 1)
                name = name.strip()
                cookie_str = cookie_str.strip()
            else:
                # No pipe: entire line is cookie, auto-generate name
                cookie_str = line
                import hashlib
                name = hashlib.md5(cookie_str.encode()).hexdigest()[:8]
            if name and cookie_str:
                accounts.append({"name": name, "cookie_str": cookie_str})
        self._accounts = accounts
        logger.info(f"[SehuatangSignin] 解析到 {len(self._accounts)} 个账号: {[a['name'] for a in accounts]}")

    def _get_account_id(self, account: dict, idx: int) -> str:
        """Get unique account identifier."""
        name = str(account.get("name", "")).strip()
        if name:
            return name
        import hashlib
        return hashlib.md5(str(account.get("cookie_str", "")).encode()).hexdigest()[:12]

    def _build_cookies(self, account: dict) -> list:
        """Build cookie list from account config."""
        cookies = []
        cookie_str = str(account.get("cookie_str", "")).strip()
        if cookie_str:
            for part in cookie_str.split(";"):
                part = part.strip()
                if "=" in part:
                    name, value = part.split("=", 1)
                    cookies.append({"name": name.strip(), "value": value.strip(),
                                    "domain": ".sehuatang.net", "path": "/"})
        return cookies

    def _send_captcha_notification(self, cap_type: str, url: str, account_id: str):
        """Send WeChat notification with captcha relay URL."""
        if not self._notify:
            return
        self.post_message(
            title=f"🔐 色花堂验证码 - {account_id}",
            text=f"验证码类型：{cap_type}\n账号：{account_id}\n\n请在 {self._captcha_timeout // 60} 分钟内打开：\n{url}\n\n拖动滑块到缺口位置后提交",
        )

    def _notify_summary(self, results: list):
        """Send summary notification for all accounts."""
        if not self._notify:
            return
        success_count = sum(1 for r in results if r.get("success"))
        total = len(results)
        lines = [f"色花堂签到完成：{success_count}/{total} 成功"]
        for r in results:
            icon = "✅" if r.get("success") else "❌"
            lines.append(f"  {icon} {r['account']}: {r['message']}")
        self.post_message(title="色花堂签到汇总", text="\n".join(lines))

    def _save_results(self, results: list):
        """Save all results to plugin data."""
        history = self.get_data(self._history_key) or []
        for r in results:
            history.insert(0, {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "account": r.get("account", "?"),
                "success": r.get("success", False),
                "message": r.get("message", ""),
            })
        self.save_data(self._history_key, history[:50])
        self.save_data(self._last_result_key, results)

    def stop_service(self):
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                self._scheduler.shutdown(wait=False)
            self._scheduler = None