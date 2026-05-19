import concurrent.futures
import json
import random
import re
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

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
    fs_destroy_session,
    fs_get,
    get_answer,
    init_session,
    is_expired,
    is_solved,
    set_captcha_data,
    set_base_url,
    set_fs_url,
    set_proxy_url,
    set_session_store_path,
    site_captcha_lock,
    start_server,
    stop_server,
    submit_check,
)


class SehuatangSignin(_PluginBase):
    plugin_name = "98签到自用"
    plugin_desc = "98签到自用辅助：推送验证码链接，手动验证后继续提交签到。"
    plugin_icon = "https://raw.githubusercontent.com/Vivitoto/MoviePilot-Plugins/main/icons/shtsignin.png"
    plugin_version = "0.1.17"
    plugin_author = "Vivitoto"
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

    # Multi-account config. New UI stores account/cookie in fixed slots;
    # accounts_text is kept for backward compatibility with older versions.
    _account_slots = 20
    _account_names: list = []
    _account_cookies: list = []
    _account_count = 1
    _account_interval_seconds = 3
    _parallel_accounts = False
    _accounts_text = ""
    _accounts: list = []

    # Target site / FlareSolverr
    _base_url = "https://sehuatang.net"
    _flaresolverr_url = "http://127.0.0.1:8191"
    _use_flaresolverr = True

    # Proxy（访问 98 需要）
    _proxy_url = ""

    # Captcha relay
    _captcha_port = 5099
    _captcha_timeout = 300
    _captcha_fetch_timeout = 300
    _captcha_check_retries = 2
    _public_base_url = ""

    # Global lock for site captcha endpoint operations across all accounts.
    # It serializes both fetch and check calls to reduce site-wide 429 risk.
    _captcha_fetch_lock = threading.Lock()

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
                self._account_interval_seconds = max(0, int(config.get("account_interval_seconds") or 3))
                self._parallel_accounts = bool(config.get("parallel_accounts", False))
                self._accounts_text = str(config.get("accounts_text") or "").strip()
                self._account_names = []
                self._account_cookies = []
                for idx in range(1, self._account_slots + 1):
                    self._account_names.append(str(config.get(f"account_{idx}_name") or "").strip())
                    self._account_cookies.append(str(config.get(f"account_{idx}_cookie") or "").strip())

                legacy_accounts = self._parse_accounts_text(self._accounts_text)
                # Migrate legacy textarea config into the new slot UI on first load.
                if legacy_accounts and not any(self._account_cookies):
                    for idx, account in enumerate(legacy_accounts[:self._account_slots]):
                        self._account_names[idx] = account.get("name", "")
                        self._account_cookies[idx] = account.get("cookie_str", "")

                saved_count = int(config.get("account_count") or 0)
                inferred_count = max(
                    [idx + 1 for idx, cookie in enumerate(self._account_cookies) if cookie] or
                    [min(len(legacy_accounts), self._account_slots) or 1]
                )
                self._account_count = min(self._account_slots, max(1, saved_count, inferred_count))

                self._base_url = str(config.get("base_url") or "https://sehuatang.net").strip().rstrip("/")
                self._flaresolverr_url = str(config.get("flaresolverr_url") or "http://127.0.0.1:8191").rstrip("/")
                self._use_flaresolverr = config.get("use_flaresolverr", True)
                self._proxy_url = str(config.get("proxy_url") or "").strip()
                self._captcha_port = max(1, int(config.get("captcha_port") or 5099))
                self._captcha_timeout = max(60, int(config.get("captcha_timeout") or 300))
                self._captcha_fetch_timeout = max(30, int(config.get("captcha_fetch_timeout") or 300))
                self._captcha_check_retries = max(0, int(config.get("captcha_check_retries") or 2))
                self._public_base_url = str(config.get("public_base_url") or "").strip().rstrip("/")
                self._parse_accounts()

            # Start embedded captcha server. Stop first to avoid keeping a stale
            # relay thread after plugin config saves / hot reloads.
            stop_server()
            data_path = self.get_data_path()
            set_session_store_path(str(data_path / "captcha_sessions.json"))
            set_base_url(self._base_url)
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
        account_lines = []
        for idx in range(self._account_slots):
            name = (self._account_names[idx] if idx < len(self._account_names) else "").strip()
            cookie = (self._account_cookies[idx] if idx < len(self._account_cookies) else "").strip()
            if cookie:
                account_lines.append(f"{name or f'账号{idx + 1}'} | {cookie}")

        config = {
            "enabled": self._enabled, "notify": self._notify, "onlyonce": self._onlyonce,
            "cron": self._cron, "timeout": self._timeout,
            "account_count": self._account_count,
            "account_interval_seconds": self._account_interval_seconds,
            "parallel_accounts": self._parallel_accounts,
            "accounts_text": "\n".join(account_lines),
            "base_url": self._base_url,
            "flaresolverr_url": self._flaresolverr_url,
            "use_flaresolverr": self._use_flaresolverr,
            "proxy_url": self._proxy_url,
            "captcha_port": self._captcha_port, "captcha_timeout": self._captcha_timeout,
            "captcha_fetch_timeout": self._captcha_fetch_timeout,
            "captcha_check_retries": self._captcha_check_retries,
            "public_base_url": self._public_base_url,
        }
        for idx in range(1, self._account_slots + 1):
            config[f"account_{idx}_name"] = self._account_names[idx - 1] if idx - 1 < len(self._account_names) else ""
            config[f"account_{idx}_cookie"] = self._account_cookies[idx - 1] if idx - 1 < len(self._account_cookies) else ""
        self.update_config(config)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/sht_signin",
            "event": EventType.PluginAction,
            "desc": "执行98签到自用",
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
            "name": "98签到自用",
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
            for idx, acct in enumerate(self._accounts):
                name = acct.get("name", "?")
                account_id = self._get_account_id(acct, idx)
                account_path = quote(account_id, safe="")
                latest = next((r for r in history if r.get("account") in (name, account_id)), None)
                status = "✅" if latest and latest.get("success") else "❓"
                last_time = latest.get("time", "-") if latest else "-"
                last_msg = latest.get("message", "-") if latest else "未执行"
                url = f"{self._public_base_url}/{account_path}" if self._public_base_url else f"http://localhost:{self._captcha_port}/{account_path}"
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
        account_cards = []
        for idx in range(1, self._account_slots + 1):
            delete_actions = []
            for move_idx in range(idx, self._account_slots):
                delete_actions.extend([
                    f"account_{move_idx}_name = account_{move_idx + 1}_name",
                    f"account_{move_idx}_cookie = account_{move_idx + 1}_cookie",
                ])
            delete_actions.extend([
                f"account_{self._account_slots}_name = ''",
                f"account_{self._account_slots}_cookie = ''",
                "account_count = Math.max(1, (account_count || 1) - 1)",
            ])
            delete_script = "function(event) { " + "; ".join(delete_actions) + "; }"
            account_cards.append({
                'component': 'VCard',
                'props': {'variant': 'tonal', 'class': 'mb-2', 'show': f'{{{{ account_count >= {idx} }}}}'},
                'content': [{
                    'component': 'VCardText',
                    'props': {'class': 'py-2'},
                    'content': [{
                        'component': 'VRow',
                        'props': {'align': 'center', 'dense': True},
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'div', 'props': {'class': 'text-caption text-medium-emphasis'}, 'text': f'账号 {idx}'}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': f'account_{idx}_name', 'label': '账号名称', 'placeholder': f'账号{idx}', 'density': 'compact', 'hide-details': True}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': f'account_{idx}_cookie', 'label': 'Cookie', 'placeholder': '_safe=xxx; cPNj_2132_auth=yyy; cPNj_2132_saltkey=zzz; cPNj_2132_sid=0', 'density': 'compact', 'hide-details': True}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 1, 'class': 'text-md-right'}, 'content': [{'component': 'VBtn', 'props': {'size': 'small', 'variant': 'text', 'color': 'error', 'onClick': delete_script}, 'text': '删除'}]},
                        ]
                    }]
                }]
            })
        add_account_btn = {
            'component': 'VBtn',
            'props': {
                'variant': 'tonal',
                'color': 'primary',
                'prepend-icon': 'mdi-plus',
                'class': 'mt-1',
                'show': '{{ account_count < 20 }}',
                'onClick': 'function(event) { account_count = Math.min(20, (account_count || 1) + 1); }',
            },
            'text': '添加账号',
        }
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
                                {'component': 'div', 'props': {'class': 'text-subtitle-2 font-weight-bold mb-2'}, 'text': '👤 多账号配置'},
                                {'component': 'div', 'props': {'class': 'text-body-2 text-medium-emphasis mb-3'}, 'text': '每个账号单独填写 Cookie。当前表单最多 20 个；如需无限数量需改远程 Vue 配置组件。'},
                                *account_cards,
                                add_account_btn
                            ]
                        }]
                    },
                    {
                        'component': 'VCard',
                        'props': {'variant': 'flat', 'class': 'mb-4'},
                        'content': [{
                            'component': 'VCardItem',
                            'content': [
                                {'component': 'div', 'props': {'class': 'text-subtitle-2 font-weight-bold mb-3'}, 'text': '🖥️ 访问与验证码'},
                                {
                                    'component': 'VRow',
                                    'content': [
                                        {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VTextField', 'props': {'model': 'base_url', 'label': '98 站点网址', 'placeholder': 'https://sehuatang.net', 'hint': '域名变更时修改；不要填写末尾 /'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'flaresolverr_url', 'label': 'FlareSolverr 地址', 'placeholder': 'http://127.0.0.1:8191'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'proxy_url', 'label': '代理地址（访问 98）', 'placeholder': 'http://192.168.31.216:7890'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'captcha_port', 'label': '验证码端口', 'type': 'number', 'placeholder': '5099'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'captcha_timeout', 'label': '人工验证超时(秒)', 'type': 'number', 'placeholder': '300'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'captcha_fetch_timeout', 'label': '获取验证码超时(秒)', 'type': 'number', 'placeholder': '300'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'captcha_check_retries', 'label': '验证失败重试次数', 'type': 'number', 'placeholder': '2'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VTextField', 'props': {'model': 'public_base_url', 'label': '公网地址（可选）', 'placeholder': 'https://captcha.example.com'}}]},
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
                                {'component': 'div', 'props': {'class': 'text-subtitle-2 font-weight-bold mb-3'}, 'text': '⏰ 定时与多账号'},
                                {
                                    'component': 'VRow',
                                    'content': [
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': cron_component, 'props': {'model': 'cron', 'label': '定时任务'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'account_interval_seconds', 'label': '账号启动间隔(秒)', 'type': 'number', 'placeholder': '3'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'parallel_accounts', 'label': '并行处理多账号'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'div', 'props': {'class': 'text-body-2 text-medium-emphasis mt-1'}, 'text': '💡 串行：前一个账号完成后再处理下一个；并行：按间隔依次启动，验证码链接互不混淆。'}]},
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
                                {'component': 'div', 'props': {'class': 'text-subtitle-2 font-weight-bold mb-3'}, 'text': '📋 简要说明'},
                                {'component': 'div', 'props': {'class': 'text-body-2 text-medium-emphasis mb-2'}, 'text': '① 先部署 FlareSolverr；访问受限时填写代理。'},
                                {'component': 'div', 'props': {'class': 'text-body-2 text-medium-emphasis mb-2'}, 'text': '② Cookie 从浏览器登录后复制，过期后重新填写。'},
                                {'component': 'div', 'props': {'class': 'text-body-2 text-medium-emphasis'}, 'text': '③ 有验证码会推送链接，打开后拖动提交。'},
                            ]
                        }]
                    },
                ]
            }
        ], {
            "enabled": False, "notify": True, "onlyonce": False, "cron": "30 9 * * *",
            "account_count": 1,
            "account_interval_seconds": 3,
            "parallel_accounts": False,
            "accounts_text": "",
            "account_1_name": "", "account_1_cookie": "",
            "account_2_name": "", "account_2_cookie": "",
            "account_3_name": "", "account_3_cookie": "",
            "account_4_name": "", "account_4_cookie": "",
            "account_5_name": "", "account_5_cookie": "",
            "account_6_name": "", "account_6_cookie": "",
            "account_7_name": "", "account_7_cookie": "",
            "account_8_name": "", "account_8_cookie": "",
            "account_9_name": "", "account_9_cookie": "",
            "account_10_name": "", "account_10_cookie": "",
            "account_11_name": "", "account_11_cookie": "",
            "account_12_name": "", "account_12_cookie": "",
            "account_13_name": "", "account_13_cookie": "",
            "account_14_name": "", "account_14_cookie": "",
            "account_15_name": "", "account_15_cookie": "",
            "account_16_name": "", "account_16_cookie": "",
            "account_17_name": "", "account_17_cookie": "",
            "account_18_name": "", "account_18_cookie": "",
            "account_19_name": "", "account_19_cookie": "",
            "account_20_name": "", "account_20_cookie": "",
            "base_url": "https://sehuatang.net",
            "flaresolverr_url": "http://127.0.0.1:8191",
            "use_flaresolverr": True,
            "proxy_url": "", "captcha_port": 5099, "captcha_timeout": 300,
            "captcha_fetch_timeout": 300, "captcha_check_retries": 2,
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
        if event.event_data.get("action") not in ("98_signin", "sht_signin"):
            return
        logger.info("[SehuatangSignin] 收到手动触发指令")
        self._parse_accounts()
        self._do_signin()

    # ── Core sign-in logic (multi-account loop) ───────────
    def _do_signin(self):
        if not self._accounts:
            logger.warning("[SehuatangSignin] 未配置账号，请在插件设置中填写账号")
            return

        indexed_accounts = []
        seen_ids = {}
        for idx, account in enumerate(self._accounts):
            raw_account_id = self._get_account_id(account, idx)
            seen_ids[raw_account_id] = seen_ids.get(raw_account_id, 0) + 1
            account_id = raw_account_id if seen_ids[raw_account_id] == 1 else f"{raw_account_id}_{seen_ids[raw_account_id]}"
            indexed_accounts.append((idx, account, account_id))

        if self._parallel_accounts and len(indexed_accounts) > 1:
            all_results = self._do_signin_parallel(indexed_accounts)
        else:
            all_results = self._do_signin_serial(indexed_accounts)

        self._notify_summary(all_results)
        self._save_results(all_results)

    def _do_signin_serial(self, indexed_accounts: list) -> list:
        all_results = []
        for pos, (idx, account, account_id) in enumerate(indexed_accounts):
            logger.info(f"[SehuatangSignin] [{idx+1}/{len(indexed_accounts)}] 串行处理账号: {account_id}")
            result = self._signin_single(account, account_id)
            all_results.append({"account": account_id, **result})
            if pos < len(indexed_accounts) - 1 and self._account_interval_seconds > 0:
                time.sleep(self._account_interval_seconds)
        return all_results

    def _do_signin_parallel(self, indexed_accounts: list) -> list:
        all_results = []
        futures = []
        logger.info(
            f"[SehuatangSignin] 并行处理 {len(indexed_accounts)} 个账号，启动间隔 {self._account_interval_seconds} 秒"
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(indexed_accounts)) as executor:
            for idx, account, account_id in indexed_accounts:
                logger.info(f"[SehuatangSignin] [{idx+1}/{len(indexed_accounts)}] 并行启动账号: {account_id}")
                future = executor.submit(self._signin_single, account, account_id)
                futures.append((idx, account_id, future))
                if idx < len(indexed_accounts) - 1 and self._account_interval_seconds > 0:
                    time.sleep(self._account_interval_seconds)

            for idx, account_id, future in futures:
                try:
                    result = future.result()
                except Exception as e:
                    logger.error(f"[SehuatangSignin] [{account_id}] 并行任务异常：{traceback.format_exc()}")
                    result = {"success": False, "message": f"异常：{str(e)}", "steps": []}
                all_results.append({"account": account_id, **result})

        all_results.sort(key=lambda item: next(
            (idx for idx, _, aid in indexed_accounts if aid == item.get("account")), 0
        ))
        return all_results

    def _signin_single(self, account: dict, account_id: str) -> dict:
        steps = []
        result = {"success": False, "message": "", "steps": steps}
        fs_sid = ""
        captcha_session_active = False
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

            max_rounds = self._captcha_check_retries + 1
            for round_no in range(1, max_rounds + 1):
                if round_no > 1:
                    steps.append(f"验证码重试：第 {round_no}/{max_rounds} 轮")
                    logger.info(f"[SehuatangSignin] [{account_id}] 验证码失败后重试，第 {round_no}/{max_rounds} 轮")

                logger.info(
                    f"[SehuatangSignin] [{account_id}] 等待全局验证码获取锁，"
                    f"最长获取 {self._captcha_fetch_timeout} 秒"
                )
                with self._captcha_fetch_lock, site_captcha_lock():
                    captcha_data = fetch_captcha_for_account(
                        fs_sid,
                        cookies,
                        max_wait_seconds=self._captcha_fetch_timeout,
                    )
                if not captcha_data:
                    result["message"] = "无法获取支持的验证码（slide/rotate/click），或接口限流/超时"
                    logger.warning(f"[SehuatangSignin] [{account_id}] 获取验证码失败")
                    return result

                cap_type = captcha_data["type"]
                steps.append(f"验证码类型：{cap_type}")
                logger.info(
                    f"[SehuatangSignin] [{account_id}] 验证码: {cap_type} "
                    f"display=({captcha_data.get('display_x')},{captcha_data.get('display_y')}) "
                    f"master={captcha_data.get('master_width')}x{captcha_data.get('master_height')} "
                    f"thumb={captcha_data.get('thumb_width')}x{captcha_data.get('thumb_height')}"
                )

                captcha_session_id = f"{account_id}-{uuid.uuid4().hex[:8]}"
                init_session(captcha_session_id, account_id)
                captcha_session_active = True
                set_captcha_data(captcha_session_id, captcha_data, fs_sid)
                account_path = quote(captcha_session_id, safe="")
                captcha_url = f"{self._public_base_url}/{account_path}" if self._public_base_url else f"http://localhost:{self._captcha_port}/{account_path}"
                self._send_captcha_notification(cap_type, captcha_url, account_id)
                logger.info(f"[SehuatangSignin] [{account_id}] 已发送验证码通知，等待用户操作: {captcha_session_id}")

                deadline = time.time() + self._captcha_timeout
                while time.time() < deadline:
                    if is_solved(captcha_session_id):
                        logger.info(f"[SehuatangSignin] [{account_id}] 用户已完成验证码")
                        break
                    if is_expired(captcha_session_id, self._captcha_timeout):
                        logger.warning(f"[SehuatangSignin] [{account_id}] 验证码会话已过期")
                        break
                    time.sleep(2)

                if not is_solved(captcha_session_id):
                    result["message"] = f"验证码超时（{self._captcha_timeout}秒）"
                    return result

                answer = get_answer(captcha_session_id)
                steps.append(f"用户提交：{answer}")
                logger.info(f"[SehuatangSignin] [{account_id}] 等待全局验证码接口锁，提交 check: {answer}")
                with self._captcha_fetch_lock, site_captcha_lock():
                    ok, check_result = submit_check(fs_sid, answer, cap_type, cookies)

                    if not ok and check_result.get("data") != "safe_gate" and round_no < max_rounds:
                        cooldown = random.uniform(10, 15)
                        steps.append(f"验证码失败全局冷却：{cooldown:.1f}秒后重试")
                        logger.info(
                            f"[SehuatangSignin] [{account_id}] 验证码 check 失败后全局冷却 {cooldown:.1f} 秒，"
                            f"暂停其他账号验证码 fetch/check 以降低 429 风险"
                        )
                        time.sleep(cooldown)

                if ok:
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
                        # Some runs return "验证超时" from sign_v2 even though the site state
                        # has already changed to signed after captcha check. Trust the final page state.
                        final_signed, final_btn = check_sign_status(fs_sid, cookies)
                        steps.append(f"最终状态复查：{final_btn}")
                        logger.info(f"[SehuatangSignin] [{account_id}] 最终状态复查: {final_btn}")
                        if final_signed:
                            result["success"] = True
                            result["message"] = "签到成功：最终状态已签到"
                        else:
                            result["message"] = f"签到异常：{msg}"
                    return result

                destroy_session(captcha_session_id, destroy_fs=False)
                captcha_session_active = False
                fail_msg = check_result.get('data', '?')
                steps.append(f"验证码失败：{fail_msg}")
                logger.warning(f"[SehuatangSignin] [{account_id}] 验证码 check 失败: {check_result}")
                if fail_msg == "safe_gate":
                    result["message"] = "验证码 check 被站点安全入口拦截，请更新账号 Cookie（尤其 _safe/cf_clearance）或稍后重试"
                    return result
                if round_no >= max_rounds:
                    result["message"] = f"验证码失败：{fail_msg}"
                    return result

            return result
        except Exception as e:
            logger.error(f"[SehuatangSignin] [{account_id}] 异常：{traceback.format_exc()}")
            result["message"] = f"异常：{str(e)}"
            return result
        finally:
            if captcha_session_active:
                destroy_session(captcha_session_id if 'captcha_session_id' in locals() else account_id)
            elif fs_sid:
                fs_destroy_session(fs_sid)

    # ── Helpers ────────────────────────────────────────────
    def _parse_accounts_text(self, text: str) -> list:
        """Parse legacy accounts_text into list of dicts. Format: name | cookie_string per line."""
        accounts = []
        if not text:
            return accounts
        for line in text.strip().split("\n"):
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
        return accounts

    def _parse_accounts(self):
        """Parse account slots first, fallback to legacy accounts_text."""
        accounts = []
        for idx in range(self._account_slots):
            name = (self._account_names[idx] if idx < len(self._account_names) else "").strip()
            cookie_str = (self._account_cookies[idx] if idx < len(self._account_cookies) else "").strip()
            if not cookie_str:
                continue
            if not name:
                name = f"账号{idx + 1}"
            accounts.append({"name": name, "cookie_str": cookie_str})

        if not accounts:
            accounts = self._parse_accounts_text(self._accounts_text)

        self._accounts = accounts
        self._accounts_text = "\n".join([f"{a['name']} | {a['cookie_str']}" for a in accounts])
        logger.info(f"[SehuatangSignin] 解析到 {len(self._accounts)} 个账号: {[a['name'] for a in accounts]}")

    def _get_account_id(self, account: dict, idx: int) -> str:
        """Get a URL-safe-ish account identifier while keeping Chinese readable."""
        name = str(account.get("name", "")).strip()
        if name:
            # Keep Chinese/English/numbers readable; replace path/query-breaking characters.
            safe_name = re.sub(r"[\\/\?#%]+", "_", name)
            safe_name = re.sub(r"\s+", "_", safe_name).strip("_ .")
            if safe_name:
                return safe_name[:48]
        import hashlib
        return hashlib.md5(str(account.get("cookie_str", "")).encode()).hexdigest()[:12]

    def _build_cookies(self, account: dict) -> list:
        """Build cookie list from account config."""
        cookies = []
        cookie_str = str(account.get("cookie_str", "")).strip()
        host = urlparse(self._base_url or "https://sehuatang.net").hostname or "sehuatang.net"
        cookie_domain = f".{host.lstrip('.')}"
        if cookie_str:
            for part in cookie_str.split(";"):
                part = part.strip()
                if "=" in part:
                    name, value = part.split("=", 1)
                    cookies.append({"name": name.strip(), "value": value.strip(),
                                    "domain": cookie_domain, "path": "/"})
        return cookies

    def _send_captcha_notification(self, cap_type: str, url: str, account_id: str):
        """Send WeChat notification with captcha relay URL."""
        if not self._notify:
            return
        self.post_message(
            title=f"🔐 98验证码 - {account_id}",
            text=(
                f"账号：{account_id}\n"
                f"验证码类型：{cap_type}\n\n"
                f"人工操作地址：\n{url}\n\n"
                f"请在 {self._captcha_timeout // 60} 分钟内打开并完成验证。\n"
                f"按页面提示完成 slide/rotate/click 后提交。"
            ),
        )

    def _notify_summary(self, results: list):
        """Send summary notification for all accounts."""
        if not self._notify:
            return
        success_count = sum(1 for r in results if r.get("success"))
        total = len(results)
        lines = [f"98签到自用完成：{success_count}/{total} 成功"]
        for r in results:
            icon = "✅" if r.get("success") else "❌"
            lines.append(f"  {icon} {r['account']}: {r['message']}")
        self.post_message(title="98签到自用汇总", text="\n".join(lines))

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
        stop_server()
