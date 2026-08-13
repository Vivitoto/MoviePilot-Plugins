import asyncio
import concurrent.futures
import hashlib
import html as html_lib
import inspect
import json
import random
import re
import threading
import time
import traceback
import unicodedata
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode, urljoin, urlparse

import pytz
import requests
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
    fs_browser_get_text,
    fs_browser_post,
    fs_browser_session_destroy,
    fs_browser_session_get_text,
    fs_browser_session_post,
    fs_browser_session_start,
    fs_create_session,
    fs_destroy_session,
    fs_get,
    get_answer,
    get_solved_at,
    init_session,
    is_expired,
    is_requested,
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
    plugin_version = "1.1.6"
    plugin_author = "Vivitoto"
    author_url = "https://github.com/Vivitoto"
    plugin_config_prefix = "sehuatang_signin_"
    plugin_order = 22
    auth_level = 1

    # ── Config defaults ──────────────────────────────────
    _enabled = False
    _notify = True
    _refresh_profile = True
    _onlyonce = False
    _cron = ""
    _timeout = 30
    _timezone = "Asia/Shanghai"

    # Multi-account config. New UI stores account/cookie in fixed slots;
    # accounts_text is kept for backward compatibility with older versions.
    _account_slots = 20
    _account_names: list = []
    _account_cookies: list = []
    _account_count = 1
    _random_account_order = True
    _accounts_text = ""
    _accounts: list = []

    # Target site / FlareSolverr
    _base_url = "https://sehuatang.net"
    _flaresolverr_url = "http://127.0.0.1:8191/v1"
    _use_flaresolverr = True

    # Proxy（访问 98 需要）
    _proxy_url = ""

    # Captcha relay
    _captcha_port = 5099
    _captcha_timeout = 300
    _captcha_fetch_timeout = 300
    _captcha_check_retries = 2
    _captcha_site_ttl = 30
    _public_base_url = ""

    # Independent reminder notification. It only nudges the user when not all
    # accounts have a successful local sign-in record for today; it never runs
    # the sign-in flow itself.
    _reminder_enabled = False
    _reminder_cron = "0 21 * * *"
    _reminder_text = "98 签到提醒：今天还有账号未确认签到，请打开 MoviePilot 执行 /sht_signin。"

    # Automatic forum reply. This is intentionally independent from the manual
    # captcha sign-in flow; it never triggers sign-in or captcha relay helpers.
    _auto_reply_enabled = False
    _auto_reply_onlyonce = False
    _auto_reply_window_start = "09:00"
    _auto_reply_window_end = "12:00"
    _auto_reply_forum_ids = "141,166"
    _auto_reply_templates = "感谢分享，辛苦了。\n内容不错，支持一下。\n感谢楼主分享。"
    _auto_reply_custom_prompt = ""
    _auto_reply_title_blacklist = ""
    _auto_reply_content_blacklist = ""
    _auto_reply_author_blacklist = ""
    _auto_reply_max_candidates = 0  # legacy compatibility; auto-reply now scans all page candidates.
    _auto_reply_max_thread_age_days = 7
    _auto_reply_min_interval_minutes = 10
    _auto_reply_max_attempts_per_day = 1
    _auto_reply_ai_timeout = 45
    _auto_reply_polish_deadline_seconds = 360

    # Global lock for site captcha endpoint operations across all accounts.
    # It serializes both fetch and check calls to reduce site-wide 429 risk.
    _captcha_fetch_lock = threading.Lock()
    _signin_lock = threading.Lock()
    _auto_reply_lock = threading.Lock()
    _auto_reply_running_keys: set = set()
    _signin_active = False
    _captcha_site_ttl_buffer = 2

    _scheduler: Optional[BackgroundScheduler] = None
    _history_key = "history"
    _last_result_key = "last_result"
    _user_info_key = "user_info_by_account"
    _money_history_key = "money_history"
    _auto_reply_plan_key = "auto_reply_plan"
    _auto_reply_history_key = "auto_reply_history"
    _auto_replied_threads_key = "auto_replied_threads"
    _auto_reply_success_key = "auto_reply_success_by_day"
    _auto_reply_status_labels = {"success": "成功", "failed": "失败", "skipped": "跳过"}

    def init_plugin(self, config: dict = None):
        self.stop_service()
        try:
            if config:
                self._enabled = self._parse_config_bool(config.get("enabled", False), default=False)
                self._notify = self._parse_config_bool(config.get("notify", True), default=True)
                self._refresh_profile = self._parse_config_bool(config.get("refresh_profile", True), default=True)
                self._onlyonce = self._parse_config_bool(config.get("onlyonce", False), default=False)
                # Main scheduled sign-in is intentionally disabled: this plugin
                # now runs by manual command / one-shot save action only.
                self._cron = ""
                self._timeout = max(1, int(config.get("timeout") or 30))
                self._random_account_order = self._parse_config_bool(config.get("random_account_order", True), default=True)
                self._reminder_enabled = self._parse_config_bool(config.get("reminder_enabled", False), default=False)
                self._reminder_cron = str(config.get("reminder_cron") or "0 21 * * *").strip()
                self._reminder_text = str(config.get("reminder_text") or self._reminder_text).strip()
                self._auto_reply_enabled = self._parse_config_bool(config.get("auto_reply_enabled", False), default=False)
                self._auto_reply_onlyonce = self._parse_config_bool(config.get("auto_reply_onlyonce", False), default=False)
                auto_reply_forum_ids = config.get("auto_reply_forum_ids", "141,166")
                auto_reply_templates = config.get("auto_reply_templates", self._auto_reply_templates)
                self._auto_reply_window_start = str(config.get("auto_reply_window_start") or "09:00").strip()
                self._auto_reply_window_end = str(config.get("auto_reply_window_end") or "12:00").strip()
                self._auto_reply_forum_ids = str(auto_reply_forum_ids if auto_reply_forum_ids is not None else "").strip()
                self._auto_reply_templates = str(auto_reply_templates if auto_reply_templates is not None else "").strip()
                self._auto_reply_custom_prompt = str(config.get("auto_reply_custom_prompt") or "").strip()
                self._auto_reply_title_blacklist = str(config.get("auto_reply_title_blacklist") or "").strip()
                self._auto_reply_content_blacklist = str(config.get("auto_reply_content_blacklist") or "").strip()
                self._auto_reply_author_blacklist = str(config.get("auto_reply_author_blacklist") or "").strip()
                # Legacy compatibility: old versions exposed a hard candidate cap.
                # Keep loading/saving the key but do not use it to stop early.
                try:
                    self._auto_reply_max_candidates = max(0, int(config.get("auto_reply_max_candidates") or 0))
                except (TypeError, ValueError):
                    self._auto_reply_max_candidates = 0
                try:
                    self._auto_reply_max_attempts_per_day = max(
                        1,
                        min(10, int(config.get("auto_reply_max_attempts_per_day") or 1))
                    )
                except (TypeError, ValueError):
                    self._auto_reply_max_attempts_per_day = 1
                try:
                    max_thread_age_days = config.get("auto_reply_max_thread_age_days", 7)
                    self._auto_reply_max_thread_age_days = max(
                        0,
                        min(365, int(7 if max_thread_age_days in (None, "") else max_thread_age_days))
                    )
                except (TypeError, ValueError):
                    self._auto_reply_max_thread_age_days = 7
                try:
                    self._auto_reply_min_interval_minutes = max(
                        0,
                        min(240, int(config.get("auto_reply_min_interval_minutes") or 10))
                    )
                except (TypeError, ValueError):
                    self._auto_reply_min_interval_minutes = 10
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
                self._flaresolverr_url = str(config.get("flaresolverr_url") or "http://127.0.0.1:8191/v1").rstrip("/")
                # FlareSolverr is mandatory for both sign-in and auto-reply requests in this plugin.
                # Keep the legacy config key for compatibility, but do not let it disable the request path.
                self._use_flaresolverr = True
                self._proxy_url = str(config.get("proxy_url") or "").strip()
                self._captcha_port = max(1, int(config.get("captcha_port") or 5099))
                self._captcha_timeout = max(60, int(config.get("captcha_timeout") or 300))
                self._captcha_fetch_timeout = max(30, int(config.get("captcha_fetch_timeout") or 300))
                captcha_check_retries = config.get("captcha_check_retries", 2)
                self._captcha_check_retries = max(0, int(2 if captcha_check_retries in (None, "") else captcha_check_retries))
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
            self._parse_accounts()

            if self._auto_reply_onlyonce and not self._enabled:
                logger.info("[SehuatangSignin] 自动回帖立即执行跳过：插件未启用")
                self._auto_reply_onlyonce = False
                self._update_config()

            if self._onlyonce or (self._enabled and (self._auto_reply_enabled or self._auto_reply_onlyonce)):
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._onlyonce:
                logger.info("[SehuatangSignin] 保存配置后执行一次签到流程")
                self._scheduler.add_job(
                    func=self._run_once,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    id=f"{self.plugin_config_prefix}signin_onlyonce",
                    replace_existing=True,
                )
                self._onlyonce = False
                self._update_config()
            if self._enabled and self._auto_reply_enabled:
                self._register_auto_reply_schedule()
            if self._auto_reply_onlyonce:
                logger.info("[SehuatangSignin] 保存配置后执行一次自动回帖流程")
                self._scheduler.add_job(
                    func=self._run_auto_reply_once,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    id=f"{self.plugin_config_prefix}auto_reply_onlyonce",
                    replace_existing=True,
                )
                self._auto_reply_onlyonce = False
                self._update_config()
            if self._scheduler and self._scheduler.get_jobs():
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
            "enabled": self._enabled, "notify": self._notify, "refresh_profile": self._refresh_profile, "onlyonce": self._onlyonce,
            "cron": "", "timeout": self._timeout,
            "account_count": self._account_count,
            "random_account_order": self._random_account_order,
            "reminder_enabled": self._reminder_enabled,
            "reminder_cron": self._reminder_cron,
            "reminder_text": self._reminder_text,
            "auto_reply_enabled": self._auto_reply_enabled,
            "auto_reply_onlyonce": self._auto_reply_onlyonce,
            "auto_reply_window_start": self._auto_reply_window_start,
            "auto_reply_window_end": self._auto_reply_window_end,
            "auto_reply_forum_ids": self._auto_reply_forum_ids,
            "auto_reply_templates": self._auto_reply_templates,
            "auto_reply_custom_prompt": self._auto_reply_custom_prompt,
            "auto_reply_title_blacklist": self._auto_reply_title_blacklist,
            "auto_reply_content_blacklist": self._auto_reply_content_blacklist,
            "auto_reply_author_blacklist": self._auto_reply_author_blacklist,
            "auto_reply_max_candidates": self._auto_reply_max_candidates,
            "auto_reply_max_attempts_per_day": self._auto_reply_max_attempts_per_day,
            "auto_reply_max_thread_age_days": self._auto_reply_max_thread_age_days,
            "auto_reply_min_interval_minutes": self._auto_reply_min_interval_minutes,
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
    def _parse_config_bool(value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "y", "on", "启用", "开启", "是"}:
            return True
        if text in {"0", "false", "no", "n", "off", "disabled", "disable", "关闭", "否", ""}:
            return False
        return default

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
        services = []
        if self._enabled and self._reminder_enabled and self._reminder_cron:
            services.append({
                "id": "SehuatangSigninReminder",
                "name": "98签到提醒",
                "trigger": CronTrigger.from_crontab(self._reminder_cron),
                "func": self._run_reminder,
                "kwargs": {},
            })
        return services

    def get_page(self) -> List[dict]:
        """Detail page - show account cards, money trend and history."""
        history = self.get_data(self._history_key) or []
        last_results = self.get_data(self._last_result_key) or []
        user_info_map = self.get_data(self._user_info_key) or {}
        money_history = self.get_data(self._money_history_key) or []
        auto_reply_plan = self.get_data(self._auto_reply_plan_key) or {}
        auto_reply_history = self.get_data(self._auto_reply_history_key) or []

        page = []
        account_ids = []
        result_by_account = {}
        for r in last_results if isinstance(last_results, list) else []:
            if r.get("account"):
                result_by_account[r.get("account")] = r

        for idx, acct in enumerate(self._accounts):
            account_id = self._get_account_id(acct, idx)
            suffix = 1
            unique_id = account_id
            while unique_id in account_ids:
                suffix += 1
                unique_id = f"{account_id}_{suffix}"
            account_ids.append(unique_id)

        today = datetime.now().strftime("%Y-%m-%d")
        today_results = [h for h in history if str(h.get("time", "")).startswith(today)]
        today_success_accounts = {h.get("account") for h in today_results if h.get("success")}
        profile_count = sum(
            1 for aid in account_ids
            if isinstance(user_info_map, dict) and isinstance(user_info_map.get(aid), dict)
            and not user_info_map.get(aid, {}).get("error")
        )
        latest_refresh = "-"
        for info in user_info_map.values() if isinstance(user_info_map, dict) else []:
            refresh = info.get("last_refresh") if isinstance(info, dict) else ""
            if refresh and (latest_refresh == "-" or refresh > latest_refresh):
                latest_refresh = refresh

        def stat_card(label: str, value: str, color: str) -> Dict[str, Any]:
            color_map = {
                "primary": ("rgba(25,118,210,.08)", "#1565C0"),
                "success": ("rgba(46,125,50,.08)", "#2E7D32"),
                "warning": ("rgba(245,124,0,.10)", "#E65100"),
                "secondary": ("rgba(123,31,162,.08)", "#6A1B9A"),
            }
            bg, text_color = color_map.get(color, color_map["primary"])
            return {
                'component': 'VCol',
                'props': {'cols': 6, 'md': 3},
                'content': [{
                    'component': 'div',
                    'props': {'style': f'background:{bg};border-radius:12px;padding:10px 12px;min-height:64px;'},
                    'content': [
                        {'component': 'div', 'props': {'class': 'text-caption text-medium-emphasis'}, 'text': label},
                        {'component': 'div', 'props': {'class': 'text-h6 font-weight-bold text-truncate', 'style': f'color:{text_color};'}, 'text': value},
                    ]
                }]
            }

        page.append({
            'component': 'VCard',
            'props': {'variant': 'flat', 'class': 'mb-3'},
            'content': [{
                'component': 'VCardText',
                'props': {'class': 'py-3'},
                'content': [
                    {'component': 'div', 'props': {'class': 'd-flex align-center mb-3'}, 'content': [
                        {'component': 'VIcon', 'props': {'color': 'primary', 'class': 'mr-2'}, 'text': 'mdi-view-dashboard-outline'},
                        {'component': 'div', 'props': {'class': 'text-subtitle-1 font-weight-bold'}, 'text': '执行总览'},
                    ]},
                    {'component': 'VRow', 'props': {'dense': True}, 'content': [
                        stat_card('配置账号', str(len(self._accounts)), 'primary'),
                        stat_card('今日成功', f'{len(today_success_accounts)}/{len(self._accounts)}', 'success'),
                        stat_card('资料已刷新', f'{profile_count}/{len(self._accounts)}', 'secondary'),
                        stat_card('最近刷新', latest_refresh, 'warning'),
                    ]},
                ]
            }]
        })

        auto_reply_card = self._auto_reply_summary_card(auto_reply_plan, auto_reply_history)
        if auto_reply_card:
            page.append(auto_reply_card)

        if self._accounts:
            cards = []
            for idx, acct in enumerate(self._accounts):
                account_id = account_ids[idx] if idx < len(account_ids) else self._get_account_id(acct, idx)
                info = user_info_map.get(account_id) or user_info_map.get(acct.get("name")) or {}
                latest = result_by_account.get(account_id) or next((r for r in history if r.get("account") == account_id), None) or {}
                success = bool(latest.get("success"))
                status_text = '已签到' if success else (latest.get('message') or '未执行')
                status_color = 'success' if success else ('warning' if latest else 'secondary')
                account_path = quote(account_id, safe="")
                captcha_url = f"{self._public_base_url}/{account_path}" if self._public_base_url else f"http://localhost:{self._captcha_port}/{account_path}"
                profile_error = info.get('error') if isinstance(info, dict) else ''

                def metric(label: str, value: Any, color: str = '#1565C0') -> Dict[str, Any]:
                    return {'component': 'VCol', 'props': {'cols': 6}, 'content': [{
                        'component': 'div',
                        'props': {'style': 'background:rgba(0,0,0,.035);border:1px solid rgba(0,0,0,.04);border-radius:10px;padding:8px 10px;min-height:56px;'},
                        'content': [
                            {'component': 'div', 'props': {'class': 'text-caption text-medium-emphasis'}, 'text': label},
                            {'component': 'div', 'props': {'class': 'text-subtitle-2 font-weight-bold text-truncate', 'style': f'color:{color};'}, 'text': str(value or '-')},
                        ]
                    }]}

                cards.append({
                    'component': 'VCol',
                    'props': {'cols': 12, 'sm': 6, 'md': 4, 'lg': 3},
                    'content': [{
                        'component': 'VCard',
                        'props': {'variant': 'flat', 'class': 'h-100', 'style': 'border:1px solid rgba(0,0,0,.08);border-radius:14px;'},
                        'content': [{
                            'component': 'VCardText',
                            'props': {'class': 'py-3'},
                            'content': [
                                {'component': 'div', 'props': {'class': 'd-flex align-start justify-space-between ga-2 mb-2'}, 'content': [
                                    {'component': 'div', 'props': {'class': 'text-subtitle-2 font-weight-bold text-truncate'}, 'text': account_id},
                                    {'component': 'VChip', 'props': {'size': 'x-small', 'variant': 'tonal', 'color': status_color}, 'text': status_text},
                                ]},
                                {'component': 'div', 'props': {'class': 'text-caption text-medium-emphasis mb-2 text-truncate'},
                                 'text': f"等级：{info.get('user_group') or '-'}" if isinstance(info, dict) else '等级：-'},
                                {'component': 'VRow', 'props': {'dense': True, 'class': 'mb-1'}, 'content': [
                                    metric('积分', info.get('credits') if isinstance(info, dict) else '-', '#1565C0'),
                                    metric('金钱', info.get('money') if isinstance(info, dict) else '-', '#E65100'),
                                ]},
                                {'component': 'div', 'props': {'class': 'text-caption text-medium-emphasis mt-2'}, 'text': f"注册：{info.get('register_time') or '-'}" if isinstance(info, dict) else '注册：-'},
                                {'component': 'div', 'props': {'class': 'text-caption text-medium-emphasis'}, 'text': f"刷新：{info.get('last_refresh') or '-'}" if isinstance(info, dict) else '刷新：-'},
                                {'component': 'div', 'props': {'class': 'text-caption text-error mt-1', 'show': bool(profile_error)}, 'text': f"资料：{profile_error}" if profile_error else ''},
                                {'component': 'div', 'props': {'class': 'd-flex justify-end mt-2'}, 'content': [
                                    {'component': 'a', 'props': {'href': captcha_url, 'target': '_blank', 'class': 'text-caption'}, 'text': '验证码链接'}
                                ]},
                            ]
                        }]
                    }]
                })
            page.append({
                'component': 'VCard',
                'props': {'variant': 'flat', 'class': 'mb-3'},
                'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'text-subtitle-1 py-2'}, 'text': '👤 账号状态'},
                    {'component': 'VCardText', 'props': {'class': 'pt-0'}, 'content': [
                        {'component': 'VRow', 'props': {'dense': True}, 'content': cards},
                    ]},
                ]
            })
        else:
            page.append({
                'component': 'VCard',
                'props': {'variant': 'tonal', 'class': 'mb-4'},
                'content': [{'component': 'VCardItem', 'content': [{'component': 'div', 'text': '尚未配置账号，请先在设置中填写账号列表'}]}]
            })

        chart_card = self._money_chart_card(money_history, account_ids)
        if chart_card:
            page.append(chart_card)

        if history:
            rows = []
            for h in history[:30]:
                rows.append([
                    {'component': 'td', 'props': {'style': 'white-space:nowrap;'}, 'text': h.get('account', '-')},
                    {'component': 'td', 'text': '✅' if h.get('success') else '❌'},
                    {'component': 'td', 'props': {'style': 'white-space:nowrap;'}, 'text': h.get('time', '-')},
                    {'component': 'td', 'text': h.get('message', '-')},
                ])
            page.append({
                'component': 'VCard',
                'props': {'variant': 'flat'},
                'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'text-subtitle-1 py-2'}, 'text': '📋 执行记录'},
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
                    'props': {'class': 'py-3'},
                    'content': [{
                        'component': 'VRow',
                        'props': {'align': 'center', 'dense': True, 'class': 'gy-3'},
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 1, 'class': 'py-3'}, 'content': [{'component': 'div', 'props': {'class': 'text-caption text-medium-emphasis'}, 'text': f'账号 {idx}'}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3, 'class': 'py-3'}, 'content': [{'component': 'VTextField', 'props': {'model': f'account_{idx}_name', 'label': '账号名称', 'placeholder': f'账号{idx}', 'density': 'comfortable', 'hide-details': True}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 7, 'class': 'py-3'}, 'content': [{'component': 'VTextField', 'props': {'model': f'account_{idx}_cookie', 'label': 'Cookie', 'placeholder': '_safe=xxx; cPNj_2132_auth=yyy; cPNj_2132_saltkey=zzz; cPNj_2132_sid=0', 'density': 'comfortable', 'hide-details': True}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 1, 'class': 'd-flex justify-end align-center py-3'}, 'content': [{'component': 'VBtn', 'props': {'size': 'small', 'variant': 'text', 'color': 'error', 'onClick': delete_script}, 'text': '删除'}]},
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
                                    'props': {'dense': True, 'align': 'center', 'class': 'gy-3'},
                                    'content': [
                                        {'component': 'VCol', 'props': {'cols': 12, 'sm': 6, 'md': 4, 'class': 'py-3'}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件', 'hide-details': True}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'sm': 6, 'md': 4, 'class': 'py-3'}, 'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '保存后执行一次签到', 'hide-details': True}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'sm': 6, 'md': 4, 'class': 'py-3'}, 'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知', 'hide-details': True}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'sm': 6, 'md': 4, 'class': 'py-3'}, 'content': [{'component': 'VSwitch', 'props': {'model': 'random_account_order', 'label': '随机账号顺序', 'hide-details': True}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'sm': 6, 'md': 4, 'class': 'py-3'}, 'content': [{'component': 'VSwitch', 'props': {'model': 'refresh_profile', 'label': '签到后刷新个人资料', 'hide-details': True}}]},
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
                                    'props': {'dense': True, 'class': 'gy-4'},
                                    'content': [
                                        {'component': 'VCol', 'props': {'cols': 12, 'class': 'py-3'}, 'content': [{'component': 'VTextField', 'props': {'model': 'base_url', 'label': '98 站点网址', 'placeholder': 'https://sehuatang.net', 'hint': '用于签到页、验证码接口、资料页和积分页；域名变更时修改，不要填写末尾 /', 'persistent-hint': True}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6, 'class': 'py-3'}, 'content': [{'component': 'VTextField', 'props': {'model': 'flaresolverr_url', 'label': 'FlareSolverr API 地址（必需）', 'placeholder': 'http://127.0.0.1:8191/v1', 'hint': '签到与自动回帖都通过 FlareSolverr 访问受保护页面，必须填写完整 /v1 路径。', 'persistent-hint': True}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6, 'class': 'py-3'}, 'content': [{'component': 'VTextField', 'props': {'model': 'proxy_url', 'label': '代理地址（访问 98）', 'placeholder': 'http://192.168.31.216:7890'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3, 'class': 'py-3'}, 'content': [{'component': 'VTextField', 'props': {'model': 'captcha_port', 'label': '验证码端口', 'type': 'number', 'placeholder': '5099'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3, 'class': 'py-3'}, 'content': [{'component': 'VTextField', 'props': {'model': 'captcha_timeout', 'label': '人工验证超时(秒)', 'type': 'number', 'placeholder': '300'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3, 'class': 'py-3'}, 'content': [{'component': 'VTextField', 'props': {'model': 'captcha_fetch_timeout', 'label': '获取验证码超时(秒)', 'type': 'number', 'placeholder': '300'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3, 'class': 'py-3'}, 'content': [{'component': 'VTextField', 'props': {'model': 'captcha_check_retries', 'label': '验证失败重试次数', 'type': 'number', 'placeholder': '2'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'class': 'py-3'}, 'content': [{'component': 'VTextField', 'props': {'model': 'public_base_url', 'label': '验证码公网地址（可选）', 'placeholder': 'https://captcha.example.com', 'hint': '用于通知里的人工验证码链接；留空时使用本机端口地址', 'persistent-hint': True}}]},
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
                                {'component': 'div', 'props': {'class': 'text-subtitle-2 font-weight-bold mb-3'}, 'text': '🔔 签到通知'},
                                {
                                    'component': 'VRow',
                                    'props': {'dense': True, 'align': 'center', 'class': 'gy-4'},
                                    'content': [
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 4, 'class': 'py-3'}, 'content': [{'component': 'VSwitch', 'props': {'model': 'reminder_enabled', 'label': '启用签到提醒', 'hide-details': True}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 4, 'class': 'py-3'}, 'content': [{'component': cron_component, 'props': {'model': 'reminder_cron', 'label': '提醒 Cron'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 12, 'class': 'py-3'}, 'content': [{'component': 'VTextField', 'props': {'model': 'reminder_text', 'label': '提醒通知内容', 'placeholder': '98 签到提醒：今天还有账号未确认签到。'}}]},
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
                                {'component': 'div', 'props': {'class': 'text-subtitle-2 font-weight-bold mb-3'}, 'text': '自动回帖'},
                                {
                                    'component': 'VRow',
                                    'props': {'dense': True, 'align': 'center', 'class': 'gy-4'},
                                    'content': [
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3, 'class': 'py-3'}, 'content': [{'component': 'VSwitch', 'props': {'model': 'auto_reply_enabled', 'label': '启用每日自动回帖', 'hide-details': True}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3, 'class': 'py-3'}, 'content': [{'component': 'VSwitch', 'props': {'model': 'auto_reply_onlyonce', 'label': '保存后执行一次回帖', 'hide-details': True}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3, 'class': 'py-3'}, 'content': [{'component': 'VTextField', 'props': {'model': 'auto_reply_window_start', 'label': '窗口开始', 'placeholder': '09:00', 'hint': 'HH:MM；必须早于结束时间。', 'persistent-hint': True}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3, 'class': 'py-3'}, 'content': [{'component': 'VTextField', 'props': {'model': 'auto_reply_window_end', 'label': '窗口结束', 'placeholder': '12:00', 'hint': '仅支持同日窗口，不支持跨午夜。', 'persistent-hint': True}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3, 'class': 'py-3'}, 'content': [{'component': 'VTextField', 'props': {'model': 'auto_reply_forum_ids', 'label': '版块 ID', 'placeholder': '141,166', 'hint': '逗号分隔；留空或无有效 ID 时不会回帖。', 'persistent-hint': True}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3, 'class': 'py-3'}, 'content': [{'component': 'VTextField', 'props': {'model': 'auto_reply_max_attempts_per_day', 'label': '每日最大回帖尝试次数', 'type': 'number', 'placeholder': '1', 'hint': '每账号每天最多尝试回帖次数；失败/跳过才在窗口内重试，成功一次后当天后续尝试自动跳过。', 'persistent-hint': True}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3, 'class': 'py-3'}, 'content': [{'component': 'VTextField', 'props': {'model': 'auto_reply_max_thread_age_days', 'label': '主题最大天数', 'type': 'number', 'placeholder': '7', 'hint': '默认只回复 7 天内主题；填 0 关闭时间过滤。', 'persistent-hint': True}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3, 'class': 'py-3'}, 'content': [{'component': 'VTextField', 'props': {'model': 'auto_reply_min_interval_minutes', 'label': '账号最小回帖间隔(分钟)', 'type': 'number', 'placeholder': '10'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'class': 'py-3'}, 'content': [{'component': 'VTextarea', 'props': {'model': 'auto_reply_templates', 'label': '回复参考模板', 'rows': 3, 'auto-grow': True, 'placeholder': '每行一条，AI 只作为参考，不会在 AI 失败时直接套用。'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'class': 'py-3'}, 'content': [{'component': 'VTextarea', 'props': {'model': 'auto_reply_custom_prompt', 'label': 'AI 补充提示', 'rows': 2, 'auto-grow': True, 'placeholder': '可补充回复风格或规避规则；不要填写 Cookie、API Key。'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 4, 'class': 'py-3'}, 'content': [{'component': 'VTextarea', 'props': {'model': 'auto_reply_title_blacklist', 'label': '标题黑名单', 'rows': 2, 'auto-grow': True, 'placeholder': '每行一个关键词'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 4, 'class': 'py-3'}, 'content': [{'component': 'VTextarea', 'props': {'model': 'auto_reply_content_blacklist', 'label': '内容黑名单', 'rows': 2, 'auto-grow': True, 'placeholder': '每行一个关键词'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 4, 'class': 'py-3'}, 'content': [{'component': 'VTextarea', 'props': {'model': 'auto_reply_author_blacklist', 'label': '作者黑名单', 'rows': 2, 'auto-grow': True, 'placeholder': '每行一个用户名或关键词'}}]},
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
                                {'component': 'div', 'props': {'class': 'text-body-2 text-medium-emphasis mb-2'}, 'text': '① 98 站点网址用于所有站内请求：签到页、验证码、资料页、积分页；站点换域名时改这里即可。'},
                                {'component': 'div', 'props': {'class': 'text-body-2 text-medium-emphasis mb-2'}, 'text': '② FlareSolverr 负责访问受保护页面；如果当前网络访问 98 不稳定，再填写代理地址。'},
                                {'component': 'div', 'props': {'class': 'text-body-2 text-medium-emphasis mb-2'}, 'text': '③ 验证码公网地址用于通知链接，需要能反代到本插件验证码端口；留空只适合本机/内网访问。'},
                                {'component': 'div', 'props': {'class': 'text-body-2 text-medium-emphasis mb-2'}, 'text': '④ Cookie 从浏览器登录后复制，过期、safe_gate 或资料获取失败时重新填写。'},
                                {'component': 'div', 'props': {'class': 'text-body-2 text-medium-emphasis mb-2'}, 'text': '⑤ 个人资料刷新只影响账号卡片和金钱趋势；关闭后不影响签到主流程。'},
                                {'component': 'div', 'props': {'class': 'text-body-2 text-medium-emphasis mb-2'}, 'text': '⑥ 验证码图片只临时保存在会话中，提交后会清理；签到流程结束会清理会话。'},
                                {'component': 'div', 'props': {'class': 'text-body-2 text-medium-emphasis mb-2'}, 'text': '⑦ “发送通知”只控制验证码通知和签到汇总；签到提醒是独立通知，由“启用签到提醒”单独控制。'},
                                {'component': 'div', 'props': {'class': 'text-body-2 text-medium-emphasis'}, 'text': '⑧ 签到提醒只看插件本地当天成功记录，不会为了提醒额外访问 98。'},
                            ]
                        }]
                    },
                ]
            }
        ], {
            "enabled": False, "notify": True, "refresh_profile": True, "onlyonce": False, "cron": "",
            "account_count": 1,
            "random_account_order": True,
            "reminder_enabled": False,
            "reminder_cron": "0 21 * * *",
            "reminder_text": "98 签到提醒：今天还有账号未确认签到，请打开 MoviePilot 执行 /sht_signin。",
            "auto_reply_enabled": False,
            "auto_reply_onlyonce": False,
            "auto_reply_window_start": "09:00",
            "auto_reply_window_end": "12:00",
            "auto_reply_forum_ids": "141,166",
            "auto_reply_templates": "感谢分享，辛苦了。\n内容不错，支持一下。\n感谢楼主分享。",
            "auto_reply_custom_prompt": "",
            "auto_reply_title_blacklist": "",
            "auto_reply_content_blacklist": "",
            "auto_reply_author_blacklist": "",
            "auto_reply_max_candidates": 0,
            "auto_reply_max_attempts_per_day": 1,
            "auto_reply_max_thread_age_days": 7,
            "auto_reply_min_interval_minutes": 10,
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
            "flaresolverr_url": "http://127.0.0.1:8191/v1",
            "use_flaresolverr": True,
            "proxy_url": "", "captcha_port": 5099, "captcha_timeout": 300,
            "captcha_fetch_timeout": 300, "captcha_check_retries": 2,
            "public_base_url": "",
        }

    # ── Scheduler callbacks ───────────────────────────────
    def _run_once(self):
        self._parse_accounts()  # Re-parse in case config changed
        self._do_signin()

    def _run_reminder(self):
        logger.info("[SehuatangSignin] 签到提醒任务触发")
        if self._signin_active or self._signin_lock.locked():
            logger.info("[SehuatangSignin] 签到提醒跳过：签到流程正在进行中")
            return
        self._parse_accounts()
        if not self._accounts:
            logger.info("[SehuatangSignin] 签到提醒跳过：未配置账号")
            return
        if self._all_accounts_signed_today():
            logger.info("[SehuatangSignin] 签到提醒跳过：今日所有账号已签到成功")
            return
        text = self._reminder_text or "98 签到提醒：今天还有账号未确认签到。"
        logger.info(f"[SehuatangSignin] 签到提醒通知内容:\n{text}")
        self.post_message(mtype=NotificationType.Plugin, title="98签到提醒", text=text)

    def _run_auto_reply_once(self):
        logger.info("[SehuatangSignin] 保存配置后立即执行一次自动回帖")
        if not self._enabled:
            logger.info("[SehuatangSignin] 自动回帖立即执行跳过：插件未启用")
            return
        self._parse_accounts()
        forum_ids = self._parse_forum_ids(self._auto_reply_forum_ids)
        today = self._auto_reply_now().strftime("%Y-%m-%d")
        if not forum_ids:
            logger.info("[SehuatangSignin] 自动回帖立即执行跳过：无有效版块 ID")
            return
        if not self._accounts:
            logger.info("[SehuatangSignin] 自动回帖立即执行跳过：未配置账号")
            return

        for _, account, account_id in self._indexed_accounts():
            claimed, reason = self._claim_auto_reply_run(account_id, today)
            if not claimed:
                result = self._auto_reply_result("skipped", reason or "自动回帖正在进行中")
                self._record_auto_reply_result(account_id, result)
                self._notify_auto_reply_result(account_id, result)
                continue
            try:
                result = self._normalize_auto_reply_result(self._auto_reply_single(account, account_id, forum_ids))
                self._record_auto_reply_result(account_id, result)
                self._notify_auto_reply_result(account_id, result)
            finally:
                self._release_auto_reply_run(account_id, today)

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

    # ── Independent auto-reply flow ───────────────────────
    def _register_auto_reply_schedule(self):
        if not self._scheduler:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        self._scheduler.add_job(
            func=self._refresh_auto_reply_plan,
            trigger=CronTrigger(hour=0, minute=5),
            id=f"{self.plugin_config_prefix}auto_reply_refresh",
            replace_existing=True,
        )
        self._schedule_auto_reply_jobs_for_today(force=False)

    def _refresh_auto_reply_plan(self):
        logger.info("[SehuatangSignin] 自动回帖刷新今日计划")
        self._parse_accounts()
        self._schedule_auto_reply_jobs_for_today(force=True)

    def _schedule_auto_reply_jobs_for_today(self, force: bool = False):
        if not self._enabled or not self._auto_reply_enabled:
            return
        if not self._scheduler:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

        forum_ids = self._parse_forum_ids(self._auto_reply_forum_ids)
        now = self._auto_reply_now()
        window = self._parse_auto_reply_window(
            self._auto_reply_window_start,
            self._auto_reply_window_end,
            now=now,
        )
        account_ids = [account_id for _, _, account_id in self._indexed_accounts()]
        if not forum_ids or not window or not account_ids:
            message = "无有效版块 ID" if not forum_ids else ("时间窗口无效" if not window else "未配置账号")
            logger.info(f"[SehuatangSignin] 自动回帖不创建计划：{message}")
            self.save_data(self._auto_reply_plan_key, {
                "date": now.strftime("%Y-%m-%d"),
                "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "enabled": self._auto_reply_enabled,
                "forum_ids": forum_ids,
                "window_start": self._auto_reply_window_start,
                "window_end": self._auto_reply_window_end,
                "account_ids": account_ids,
                "jobs": [],
                "message": message,
            })
            return

        plan = self.get_data(self._auto_reply_plan_key) or {}
        reusable = (
            not force
            and isinstance(plan, dict)
            and plan.get("date") == now.strftime("%Y-%m-%d")
            and plan.get("forum_ids") == forum_ids
            and plan.get("window_start") == self._auto_reply_window_start
            and plan.get("window_end") == self._auto_reply_window_end
            and plan.get("max_attempts_per_day") == max(1, int(self._auto_reply_max_attempts_per_day or 1))
            and plan.get("min_interval_minutes") == int(self._auto_reply_min_interval_minutes or 0)
            and plan.get("account_ids") == account_ids
            and isinstance(plan.get("jobs"), list)
            and plan.get("jobs")
        )
        if not reusable:
            effective_start = max(window[0], now + timedelta(seconds=5))
            if effective_start >= window[1]:
                plan = {
                    "date": now.strftime("%Y-%m-%d"),
                    "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "enabled": self._auto_reply_enabled,
                    "forum_ids": forum_ids,
                    "window_start": self._auto_reply_window_start,
                    "window_end": self._auto_reply_window_end,
                    "max_attempts_per_day": max(1, int(self._auto_reply_max_attempts_per_day or 1)),
                    "min_interval_minutes": int(self._auto_reply_min_interval_minutes or 0),
                    "account_ids": account_ids,
                    "jobs": [],
                    "message": "今日自动回帖窗口已结束",
                }
            else:
                plan = self._generate_auto_reply_plan(effective_start, window[1], forum_ids)
            self.save_data(self._auto_reply_plan_key, plan)

        changed = False
        for job in plan.get("jobs") or []:
            if not isinstance(job, dict):
                continue
            run_at = self._parse_auto_reply_datetime(job.get("run_at"))
            if not run_at:
                job["status"] = "invalid"
                job["message"] = "运行时间无效"
                changed = True
                continue
            if run_at <= now:
                if job.get("status") in ("scheduled", "pending"):
                    job["status"] = "missed"
                    job["message"] = "计划已过期，重启后不补跑"
                    changed = True
                continue
            if job.get("status") not in ("scheduled", "pending", ""):
                continue
            attempt_index = int(job.get("attempt_index") or 1)
            job_id = self._auto_reply_job_id(str(plan.get("date") or ""), str(job.get("account") or ""), attempt_index)
            self._scheduler.add_job(
                func=self._run_auto_reply_for_account,
                trigger="date",
                run_date=run_at,
                args=[int(job.get("account_index", -1)), str(job.get("account") or ""), plan.get("date"), attempt_index],
                id=job_id,
                replace_existing=True,
            )
            job["status"] = job.get("status") or "scheduled"
            changed = True
        if changed:
            self.save_data(self._auto_reply_plan_key, plan)

    def _generate_auto_reply_plan(self, start_at: datetime, end_at: datetime, forum_ids: List[str]) -> Dict[str, Any]:
        indexed_accounts = self._indexed_accounts()
        now = self._auto_reply_now()
        min_gap = int(self._auto_reply_min_interval_minutes or 0) * 60
        max_attempts = max(1, int(self._auto_reply_max_attempts_per_day or 1))
        span_seconds = max(0, int((end_at - start_at).total_seconds()))
        jobs = []

        for account_index, _, account_id in indexed_accounts:
            account_times: List[datetime] = []
            for _ in range(max_attempts):
                run_at = start_at
                if span_seconds > 0:
                    for _ in range(120):
                        candidate = start_at + timedelta(seconds=random.randint(0, span_seconds))
                        if not min_gap or all(abs((candidate - item).total_seconds()) >= min_gap for item in account_times):
                            run_at = candidate
                            break
                    else:
                        run_at = start_at + timedelta(seconds=random.randint(0, span_seconds))
                account_times.append(run_at)
            account_times.sort()
            for attempt_index, run_at in enumerate(account_times, start=1):
                jobs.append({
                    "account": account_id,
                    "account_index": account_index,
                    "attempt_index": attempt_index,
                    "run_at": run_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "scheduled" if run_at > now else "missed",
                    "message": "" if run_at > now else "计划已过期，重启后不补跑",
                })

        jobs.sort(key=lambda item: item.get("run_at", ""))
        return {
            "date": now.strftime("%Y-%m-%d"),
            "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "enabled": self._auto_reply_enabled,
            "forum_ids": forum_ids,
            "window_start": self._auto_reply_window_start,
            "window_end": self._auto_reply_window_end,
            "max_attempts_per_day": max_attempts,
            "min_interval_minutes": int(self._auto_reply_min_interval_minutes or 0),
            "account_ids": [account_id for _, _, account_id in indexed_accounts],
            "jobs": jobs,
            "message": "",
        }

    def _auto_reply_job_id(self, plan_date: str, account_id: str, attempt_index: int) -> str:
        raw_account = str(account_id or "account")
        safe_account = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_account).strip("_")[:48] or "account"
        digest = hashlib.md5(raw_account.encode("utf-8")).hexdigest()[:10]
        return f"{self.plugin_config_prefix}auto_reply_{plan_date}_{safe_account}_{digest}_{int(attempt_index or 1)}"

    def _run_auto_reply_for_account(self, account_index: int, account_id: str, plan_date: str, attempt_index: int = 1):
        if not self._enabled:
            logger.info(f"[SehuatangSignin] [{account_id}] 自动回帖跳过：插件未启用")
            self._update_auto_reply_plan_status(account_id, plan_date, "skipped", "插件未启用", attempt_index=attempt_index)
            return
        if not self._auto_reply_enabled:
            logger.info(f"[SehuatangSignin] [{account_id}] 自动回帖跳过：功能已关闭")
            self._update_auto_reply_plan_status(account_id, plan_date, "skipped", "功能已关闭", attempt_index=attempt_index)
            return
        today = self._auto_reply_now().strftime("%Y-%m-%d")
        if plan_date != today:
            logger.info(f"[SehuatangSignin] [{account_id}] 自动回帖跳过：非今日计划 {plan_date}")
            self._update_auto_reply_plan_status(account_id, plan_date, "skipped", "非今日计划", attempt_index=attempt_index)
            return
        self._parse_accounts()
        account = self._find_indexed_account(account_index, account_id)
        if not account:
            logger.warning(f"[SehuatangSignin] [{account_id}] 自动回帖跳过：账号不存在")
            self._update_auto_reply_plan_status(account_id, plan_date, "skipped", "账号不存在", attempt_index=attempt_index)
            return

        forum_ids = self._parse_forum_ids(self._auto_reply_forum_ids)
        if not forum_ids:
            result = self._auto_reply_result("skipped", "无有效版块 ID")
        else:
            claimed, reason = self._claim_auto_reply_run(account_id, today)
            if not claimed:
                logger.info(f"[SehuatangSignin] [{account_id}] 自动回帖跳过：{reason}")
                self._update_auto_reply_plan_status(account_id, plan_date, "skipped", reason, attempt_index=attempt_index)
                return
            try:
                result = self._auto_reply_single(account, account_id, forum_ids)
            finally:
                self._release_auto_reply_run(account_id, today)
        result = self._normalize_auto_reply_result(result)
        result["attempt_index"] = attempt_index
        result_status = self._auto_reply_result_status(result)
        self._record_auto_reply_result(account_id, result)
        self._update_auto_reply_plan_status(
            account_id,
            plan_date,
            "done" if result_status == "success" else result_status,
            result.get("reason") or result.get("message", ""),
            attempt_index=attempt_index,
        )
        if result_status == "success":
            self._skip_remaining_auto_reply_plan_jobs(account_id, plan_date, attempt_index)
        self._notify_auto_reply_result(account_id, result)

    def _claim_auto_reply_run(self, account_id: str, day: str) -> Tuple[bool, str]:
        key = f"{day}:{account_id}"
        with self._auto_reply_lock:
            if self._has_auto_reply_success_for_day(account_id, day):
                return False, "今日已成功回帖"
            if key in self._auto_reply_running_keys:
                return False, "自动回帖正在进行中"
            self._auto_reply_running_keys.add(key)
        return True, ""

    def _release_auto_reply_run(self, account_id: str, day: str):
        key = f"{day}:{account_id}"
        with self._auto_reply_lock:
            self._auto_reply_running_keys.discard(key)

    def _auto_reply_single(self, account: dict, account_id: str, forum_ids: List[str]) -> Dict[str, Any]:
        fs_sid = ""
        browser_session_key = ""
        request_state = {"requested": False}
        try:
            cookies = self._build_cookies(account)
            logger.info(
                f"[SehuatangSignin] [{account_id}] 自动回帖诊断："
                f"proxy={'yes' if self._proxy_url else 'no'}, "
                f"cookie_names={','.join(self._auto_reply_cookie_names(cookies)) or '-'}"
            )
            fs_sid = fs_create_session()
            if not fs_sid:
                logger.warning(f"[SehuatangSignin] [{account_id}] 自动回帖失败：FS 会话创建失败")
                return self._auto_reply_result("failed", "无法创建 FlareSolverr 会话")

            warmup_url = f"{self._base_url}/plugin.php?id=dd_sign"
            try:
                warmup_html = fs_get(fs_sid, warmup_url, cookies)
                warmup_diag = self._auto_reply_page_diag(warmup_html)
                if self._is_auto_reply_blocked_page(warmup_html):
                    logger.warning(f"[SehuatangSignin] [{account_id}] 自动回帖 FlareSolverr 预热仍返回安全页/权限页，继续尝试持久浏览器 settle：{warmup_diag}")
                elif str(warmup_html or "").strip():
                    logger.info(f"[SehuatangSignin] [{account_id}] 自动回帖已通过 FlareSolverr 预热会话：{warmup_diag}")
                else:
                    logger.warning(f"[SehuatangSignin] [{account_id}] 自动回帖 FlareSolverr 预热未返回页面，继续尝试浏览器流程：{warmup_diag}")
            except Exception as e:
                logger.warning(f"[SehuatangSignin] [{account_id}] 自动回帖 FlareSolverr 预热失败，继续尝试浏览器流程：{e}")

            browser_session_key = f"auto-reply-{account_id}-{uuid.uuid4().hex[:8]}"
            try:
                if fs_browser_session_start(browser_session_key, fs_sid, cookies):
                    logger.info(f"[SehuatangSignin] [{account_id}] 自动回帖已创建持久浏览器会话：{browser_session_key}")
                else:
                    logger.warning(f"[SehuatangSignin] [{account_id}] 自动回帖持久浏览器会话不可用，降级为短浏览器/FS 请求")
                    browser_session_key = ""
            except Exception as e:
                logger.warning(f"[SehuatangSignin] [{account_id}] 自动回帖持久浏览器会话创建失败，降级为短浏览器/FS 请求：{e}")
                browser_session_key = ""

            all_candidates = []
            seen_tids = set()
            skipped_candidates = 0
            blocked_forum_fids = []
            for fid in forum_ids:
                forum_url = f"{self._base_url}/forum.php?mod=forumdisplay&fid={fid}"
                html = self._auto_reply_browser_primary_get(
                    fs_sid,
                    forum_url,
                    cookies,
                    request_state,
                    account_id,
                    f"版块 {fid}",
                    browser_session_key=browser_session_key,
                )
                if self._is_auto_reply_blocked_page(html):
                    logger.warning(f"[SehuatangSignin] [{account_id}] 自动回帖版块 {fid} 被安全页/权限页拦截")
                    if str(fid) not in blocked_forum_fids:
                        blocked_forum_fids.append(str(fid))
                    continue
                for candidate in self._extract_thread_candidates(html, fid, self._base_url):
                    tid = str(candidate.get("tid") or "")
                    if not tid or tid in seen_tids:
                        continue
                    seen_tids.add(tid)
                    ok, reason = self._hard_filter_auto_reply_candidate(candidate, forum_ids, account_id=account_id)
                    if ok:
                        all_candidates.append(candidate)
                    else:
                        skipped_candidates += 1
                        logger.info(f"[SehuatangSignin] [{account_id}] 自动回帖候选 {tid} 跳过：{reason}")

            if not all_candidates:
                message = "无可用候选帖"
                if blocked_forum_fids:
                    blocked_fids = ",".join(blocked_forum_fids)
                    message = f"版块安全页/权限页拦截（blocked_fids={blocked_fids}），未获取到可用候选帖"
                    if skipped_candidates:
                        message = f"{message}，其他候选已跳过 {skipped_candidates} 个"
                    return self._auto_reply_result("failed", message)
                if skipped_candidates:
                    message = f"候选帖均不合适（已跳过 {skipped_candidates} 个）"
                return self._auto_reply_result("skipped", message)

            all_candidates = self._sort_auto_reply_candidates_by_newness(all_candidates)
            last_skip_message = "候选帖均未通过安全评估"
            for candidate in all_candidates:
                tid = str(candidate.get("tid") or "")
                detail_url = str(candidate.get("url") or "")
                detail_html = self._auto_reply_browser_primary_get(
                    fs_sid,
                    detail_url,
                    cookies,
                    request_state,
                    account_id,
                    f"详情 {tid}",
                    browser_session_key=browser_session_key,
                )
                detail = self._extract_auto_reply_thread_detail(detail_html, candidate, forum_ids)
                ok, reason = self._hard_filter_auto_reply_candidate(
                    detail,
                    forum_ids,
                    account_id=account_id,
                    require_detail=True,
                )
                if not ok:
                    logger.info(f"[SehuatangSignin] [{account_id}] 自动回帖详情 {tid} 跳过：{reason}")
                    last_skip_message = reason or last_skip_message
                    continue

                try:
                    assessment = self._assess_auto_reply_with_ai(detail)
                except RuntimeError as e:
                    if str(e).startswith("AI调用失败"):
                        logger.warning(f"[SehuatangSignin] [{account_id}] 自动回帖详情 {tid} 失败：{e}")
                        return self._auto_reply_result(
                            "failed",
                            str(e),
                            fid=detail.get("fid"),
                            tid=tid,
                            title=detail.get("title"),
                        )
                    raise
                if not assessment:
                    logger.info(f"[SehuatangSignin] [{account_id}] 自动回帖详情 {tid} 跳过：AI 评估未通过")
                    last_skip_message = "AI 评估未通过"
                    continue

                try:
                    reply = self._polish_auto_reply_with_ai(detail, assessment)
                except RuntimeError as e:
                    if str(e).startswith("AI调用失败"):
                        logger.warning(f"[SehuatangSignin] [{account_id}] 自动回帖详情 {tid} 失败：{e}")
                        return self._auto_reply_result(
                            "failed",
                            str(e),
                            fid=detail.get("fid"),
                            tid=tid,
                            title=detail.get("title"),
                            risk_reasons=assessment.get("risk_reasons", []),
                        )
                    raise
                if not reply:
                    logger.info(f"[SehuatangSignin] [{account_id}] 自动回帖详情 {tid} 跳过：AI 未返回合格短回复")
                    last_skip_message = "AI 未返回合格短回复"
                    continue

                ok, reason = self._preflight_auto_reply_submit(detail, reply, forum_ids, account_id)
                if not ok:
                    logger.info(f"[SehuatangSignin] [{account_id}] 自动回帖详情 {tid} 提交前跳过：{reason}")
                    last_skip_message = reason or "提交前安全复核未通过"
                    continue

                logger.info(
                    f"[SehuatangSignin] [{account_id}] 自动回帖最终选择："
                    f"fid={detail.get('fid') or '-'} tid={tid} "
                    f"标题={detail.get('title') or '-'} 回复={reply}"
                )
                post_result = self._submit_auto_reply(fs_sid, cookies, detail, reply, browser_session_key=browser_session_key)
                result = self._auto_reply_result(
                    self._auto_reply_result_status(post_result),
                    post_result.get("reason") or post_result.get("message") or
                    ("回帖成功" if post_result.get("success") else "回帖失败"),
                    fid=detail.get("fid"),
                    tid=tid,
                    title=detail.get("title"),
                    reply_summary=reply[:30],
                    risk_reasons=assessment.get("risk_reasons", []),
                )
                if self._auto_reply_result_status(result) == "success":
                    self._mark_auto_replied_thread(account_id, detail)
                return result

            return self._auto_reply_result("skipped", last_skip_message)
        except Exception as e:
            logger.error(f"[SehuatangSignin] [{account_id}] 自动回帖异常：{traceback.format_exc()}")
            return self._auto_reply_result("failed", f"异常：{str(e)}")
        finally:
            if browser_session_key:
                fs_browser_session_destroy(browser_session_key)
            if fs_sid:
                fs_destroy_session(fs_sid)

    def _auto_reply_pace_request(self, request_state: Dict[str, Any]):
        if request_state.get("requested"):
            delay = random.uniform(8, 12)
            logger.info(f"[SehuatangSignin] 自动回帖请求限速等待 {delay:.1f} 秒")
            time.sleep(delay)
        request_state["requested"] = True

    def _auto_reply_paced_get(self, fs_sid: str, url: str, cookies: list, request_state: Dict[str, Any]) -> str:
        self._auto_reply_pace_request(request_state)
        return fs_get(fs_sid, url, cookies)

    @staticmethod
    def _auto_reply_cookie_names(cookies: list) -> List[str]:
        names = []
        for item in cookies or []:
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item.get("name")))
        return sorted(set(names))

    @classmethod
    def _auto_reply_page_classes(cls, html_text: str) -> List[str]:
        text = str(html_text or "")
        low = text.lower()
        classes = []
        if any(marker in low for marker in ("static/safe/js/web.js", "safeid=", "enter-btn", "请完成安全验证", "安全检查")):
            classes.append("safe_gate")
        if any(marker in low for marker in ("cf-challenge", "cf-turnstile", "challenge-platform", "cloudflare", "just a moment")):
            classes.append("cloudflare")
        if any(marker in low for marker in ("您没有权限", "抱歉，您没有权限", "无权访问")):
            classes.append("permission")
        if any(marker in low for marker in ("需要登录",)):
            classes.append("login_required")
        if any(marker in low for marker in ("forum.php?mod=viewthread", "threadlist", "normalthread")):
            classes.append("forum")
        if any(marker in low for marker in ('id="thread_subject"', "fastpostmessage", "replysubmit", "formhash")):
            classes.append("thread")
        if "dd_sign" in low or "signin-btn" in low:
            classes.append("signin")
        return classes or ["empty" if not text.strip() else "unknown"]

    @classmethod
    def _auto_reply_page_diag(cls, html_text: str) -> str:
        text = str(html_text or "")
        return f"len={len(text)}, class={','.join(cls._auto_reply_page_classes(text))}"

    def _auto_reply_browser_primary_get(self, fs_sid: str, url: str, cookies: list,
                                        request_state: Dict[str, Any],
                                        account_id: str = "", page_label: str = "",
                                        browser_session_key: str = "") -> str:
        label = page_label or "页面"
        self._auto_reply_pace_request(request_state)

        if browser_session_key:
            try:
                html = fs_browser_session_get_text(browser_session_key, url, cookies)
            except Exception as e:
                logger.warning(f"[SehuatangSignin] [{account_id}] 自动回帖{label}持久浏览器获取失败：{e}")
            else:
                if str(html or "").strip():
                    if self._is_auto_reply_blocked_page(html):
                        logger.warning(f"[SehuatangSignin] [{account_id}] 自动回帖{label}持久浏览器返回安全页/权限页，重试一次：{self._auto_reply_page_diag(html)}")
                        try:
                            retry_html = fs_browser_session_get_text(browser_session_key, url, cookies, wait_seconds=6)
                        except Exception as e:
                            logger.warning(f"[SehuatangSignin] [{account_id}] 自动回帖{label}持久浏览器重试失败：{e}")
                        else:
                            if str(retry_html or "").strip():
                                html = retry_html
                        if self._is_auto_reply_blocked_page(html):
                            logger.warning(f"[SehuatangSignin] [{account_id}] 自动回帖{label}持久浏览器重试后仍为安全页/权限页，改用短浏览器/FS 备用")
                        else:
                            logger.info(f"[SehuatangSignin] [{account_id}] 自动回帖{label}已通过持久浏览器获取：{self._auto_reply_page_diag(html)}")
                            return html
                    else:
                        logger.info(f"[SehuatangSignin] [{account_id}] 自动回帖{label}已通过持久浏览器获取：{self._auto_reply_page_diag(html)}")
                        return html
                else:
                    logger.warning(f"[SehuatangSignin] [{account_id}] 自动回帖{label}持久浏览器未返回页面，改用短浏览器/FS 备用")

        blocked_html = ""
        try:
            html = fs_browser_get_text(fs_sid, url, cookies)
        except Exception as e:
            logger.warning(f"[SehuatangSignin] [{account_id}] 自动回帖{label}短浏览器获取失败：{e}")
        else:
            if str(html or "").strip():
                if self._is_auto_reply_blocked_page(html):
                    logger.warning(f"[SehuatangSignin] [{account_id}] 自动回帖{label}短浏览器返回安全页/权限页，改用 FS GET 备用：{self._auto_reply_page_diag(html)}")
                    blocked_html = html
                else:
                    logger.info(f"[SehuatangSignin] [{account_id}] 自动回帖{label}已通过短浏览器获取：{self._auto_reply_page_diag(html)}")
                    return html
            else:
                logger.warning(f"[SehuatangSignin] [{account_id}] 自动回帖{label}短浏览器未返回页面，改用 FS GET 备用")

        html = fs_get(fs_sid, url, cookies)
        if self._is_auto_reply_blocked_page(html):
            logger.warning(f"[SehuatangSignin] [{account_id}] 自动回帖{label}FS GET 备用后仍为安全页/权限页：{self._auto_reply_page_diag(html)}")
        elif str(html or "").strip():
            logger.info(f"[SehuatangSignin] [{account_id}] 自动回帖{label}已通过 FS GET 备用获取：{self._auto_reply_page_diag(html)}")
            return html
        else:
            logger.warning(f"[SehuatangSignin] [{account_id}] 自动回帖{label}FS GET 备用未返回页面：{self._auto_reply_page_diag(html)}")

        if not str(html or "").strip() and self._is_auto_reply_blocked_page(blocked_html):
            return blocked_html
        return html

    @staticmethod
    def _auto_reply_now() -> datetime:
        return datetime.now(tz=pytz.timezone(settings.TZ))

    @classmethod
    def _normalize_auto_reply_status_value(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"success", "succeeded", "done", "ok", "成功"}:
            return "success"
        if text in {"failed", "failure", "fail", "error", "failed_error", "失败"}:
            return "failed"
        if text in {"skipped", "skip", "ignored", "跳过"}:
            return "skipped"
        return ""

    @classmethod
    def _auto_reply_result_status(cls, result: Any) -> str:
        if isinstance(result, dict):
            for key in ("result_status", "result_category", "status"):
                status = cls._normalize_auto_reply_status_value(result.get(key))
                if status:
                    return status
            if result.get("success"):
                return "success"
            if result.get("skipped"):
                return "skipped"
            return "failed"
        return cls._normalize_auto_reply_status_value(result) or "failed"

    @classmethod
    def _auto_reply_status_label(cls, result: Any) -> str:
        return cls._auto_reply_status_labels.get(cls._auto_reply_result_status(result), "失败")

    @classmethod
    def _normalize_auto_reply_result(cls, result: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(result or {})
        status = cls._auto_reply_result_status(data)
        label = cls._auto_reply_status_label(status)
        reason = str(data.get("reason") or data.get("message") or label).strip()
        data.update({
            "success": status == "success",
            "skipped": status == "skipped",
            "failed": status == "failed",
            "status": status,
            "result_status": status,
            "result_category": status,
            "result": label,
            "message": reason,
            "reason": reason,
        })
        return data

    @classmethod
    def _auto_reply_result(cls, status: str, reason: str, **fields: Any) -> Dict[str, Any]:
        data = dict(fields)
        data["status"] = status
        data["message"] = reason
        data["reason"] = reason
        return cls._normalize_auto_reply_result(data)

    @staticmethod
    def _parse_forum_ids(value: Any) -> List[str]:
        seen = set()
        forum_ids = []
        for part in re.split(r"[\s,，;；]+", str(value or "")):
            part = part.strip()
            if part.isdigit() and int(part) > 0 and part not in seen:
                seen.add(part)
                forum_ids.append(part)
        return forum_ids

    @staticmethod
    def _parse_line_list(value: Any) -> List[str]:
        seen = set()
        items = []
        for line in str(value or "").replace("\r", "\n").split("\n"):
            item = line.strip()
            key = item.lower()
            if item and key not in seen:
                seen.add(key)
                items.append(item)
        return items

    @classmethod
    def _parse_auto_reply_window(cls, start_value: Any, end_value: Any,
                                 now: Optional[datetime] = None) -> Optional[Tuple[datetime, datetime]]:
        now = now or cls._auto_reply_now()

        def parse_clock(value: Any) -> Optional[Tuple[int, int]]:
            match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(value or ""))
            if not match:
                return None
            hour = int(match.group(1))
            minute = int(match.group(2))
            if hour > 23 or minute > 59:
                return None
            return hour, minute

        start_clock = parse_clock(start_value)
        end_clock = parse_clock(end_value)
        if not start_clock or not end_clock:
            return None
        start_at = now.replace(hour=start_clock[0], minute=start_clock[1], second=0, microsecond=0)
        end_at = now.replace(hour=end_clock[0], minute=end_clock[1], second=0, microsecond=0)
        if end_at <= start_at:
            return None
        if (end_at - start_at).total_seconds() <= 0:
            return None
        return start_at, end_at

    @staticmethod
    def _parse_auto_reply_datetime(value: Any) -> Optional[datetime]:
        if not value:
            return None
        tz = pytz.timezone(settings.TZ)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(str(value), fmt)
                return tz.localize(parsed) if parsed.tzinfo is None else parsed.astimezone(tz)
            except ValueError:
                continue
        return None

    @classmethod
    def _parse_auto_reply_thread_time(cls, value: Any, now: Optional[datetime] = None) -> Optional[datetime]:
        text = cls._strip_html(str(value or ""))
        text = html_lib.unescape(text).replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return None

        tz = pytz.timezone(settings.TZ)
        now = now or cls._auto_reply_now()
        now = tz.localize(now) if now.tzinfo is None else now.astimezone(tz)

        timestamp_match = re.fullmatch(r"\s*(1[0-9]{9}|2[0-9]{9})\s*", text)
        if timestamp_match:
            try:
                parsed = datetime.fromtimestamp(int(timestamp_match.group(1)), tz=tz)
                if 2000 <= parsed.year <= 2100:
                    return parsed
            except (OSError, ValueError, OverflowError):
                pass

        if re.search(r"(刚刚|刚才)", text):
            return now

        relative_patterns = [
            (r"(\d+)\s*(?:分钟|分鐘|分)\s*前", "minutes"),
            (r"(\d+)\s*(?:小时|小時|时)\s*前", "hours"),
            (r"(\d+)\s*天\s*前", "days"),
        ]
        for pattern, unit in relative_patterns:
            match = re.search(pattern, text)
            if match:
                amount = int(match.group(1))
                return now - timedelta(**{unit: amount})

        day_match = re.search(r"(今天|今日|昨天|昨日|前天)\s*(\d{1,2}:\d{2}(?::\d{2})?)?", text)
        if day_match:
            day_offsets = {"今天": 0, "今日": 0, "昨天": 1, "昨日": 1, "前天": 2}
            target_date = (now - timedelta(days=day_offsets.get(day_match.group(1), 0))).date()
            hour, minute, second = cls._parse_auto_reply_clock_fragment(day_match.group(2))
            try:
                return tz.localize(datetime(target_date.year, target_date.month, target_date.day, hour, minute, second))
            except ValueError:
                return None

        absolute_match = re.search(
            r"(?<!\d)((?:19|20)\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?"
            r"(?:\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?",
            text,
        )
        if absolute_match:
            try:
                return tz.localize(datetime(
                    int(absolute_match.group(1)),
                    int(absolute_match.group(2)),
                    int(absolute_match.group(3)),
                    int(absolute_match.group(4) or 0),
                    int(absolute_match.group(5) or 0),
                    int(absolute_match.group(6) or 0),
                ))
            except ValueError:
                return None

        month_day_match = re.search(
            r"(?:^|[^\d-])(\d{1,2})[-/.月](\d{1,2})(?:日)?"
            r"(?:\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?",
            text,
        )
        if month_day_match:
            try:
                parsed = tz.localize(datetime(
                    now.year,
                    int(month_day_match.group(1)),
                    int(month_day_match.group(2)),
                    int(month_day_match.group(3) or 0),
                    int(month_day_match.group(4) or 0),
                    int(month_day_match.group(5) or 0),
                ))
                if parsed > now + timedelta(days=1):
                    parsed = parsed.replace(year=parsed.year - 1)
                return parsed
            except ValueError:
                return None
        return None

    @staticmethod
    def _parse_auto_reply_clock_fragment(value: Any) -> Tuple[int, int, int]:
        parts = [int(part) for part in str(value or "00:00").split(":") if part != ""]
        while len(parts) < 3:
            parts.append(0)
        return parts[0], parts[1], parts[2]

    @classmethod
    def _collect_auto_reply_time_values(cls, html_text: str) -> List[Tuple[str, str]]:
        values: List[Tuple[str, str]] = []
        seen = set()
        raw_html = html_text or ""
        text = cls._strip_html(raw_html)
        time_value = (
            r"(?:刚刚|刚才|(?:今天|今日|昨天|昨日|前天)(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?|"
            r"\d+\s*(?:分钟|分鐘|分|小时|小時|时|天)\s*前|"
            r"(?:19|20)\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}(?:日)?(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?|"
            r"\d{1,2}[-/.月]\d{1,2}(?:日)?(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)"
        )

        def add(raw: Any, source: str):
            raw_text = cls._strip_html(str(raw or ""))
            raw_text = html_lib.unescape(raw_text).replace("\xa0", " ")
            raw_text = re.sub(r"\s+", " ", raw_text).strip()
            key = (source, raw_text)
            if raw_text and key not in seen:
                seen.add(key)
                values.append((raw_text, source))

        for match in re.finditer(r"\b(?:data-time|data-dateline|dateline|timestamp)=[\"'](\d{10})[\"']", raw_html, re.I):
            add(match.group(1), "timestamp")
        for match in re.finditer(r"\b(?:title|datetime)=[\"']([^\"']{1,80})[\"']", raw_html, re.I):
            if cls._looks_like_auto_reply_time_value(match.group(1)):
                add(match.group(1), "attribute")

        published_labels = r"(?:发表于|发布于|发帖时间|发布时间|主题发表于)"
        last_activity_labels = r"(?:最后发表|最后回复|最新回复|回复于|最后更新|更新时间)"
        for match in re.finditer(fr"{published_labels}\s*[:：]?\s*({time_value})", text, re.I):
            add(match.group(1), "published")
        for match in re.finditer(fr"{last_activity_labels}\s*[:：]?\s*({time_value})", text, re.I):
            add(match.group(1), "last_activity")

        generic_patterns = [
            r"(?<!\d)((?:19|20)\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}(?:日)?(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)",
            r"((?:今天|今日|昨天|昨日|前天)(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)",
            r"(\d+\s*(?:分钟|分鐘|分|小时|小時|时|天)\s*前)",
            r"(?<![\d-])(\d{1,2}[-/.月]\d{1,2}(?:日)?\s+\d{1,2}:\d{2}(?::\d{2})?)(?!\d)",
        ]
        for pattern in generic_patterns:
            for match in re.finditer(pattern, text, re.I):
                add(match.group(1), "generic")
        return values

    @classmethod
    def _looks_like_auto_reply_time_value(cls, value: Any) -> bool:
        text = cls._strip_html(str(value or ""))
        text = html_lib.unescape(text).replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return False
        patterns = [
            r"(?:刚刚|刚才)",
            r"(?:今天|今日|昨天|昨日|前天)(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?",
            r"\d+\s*(?:分钟|分鐘|分|小时|小時|时|天)\s*前",
            r"(?:19|20)\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}(?:日)?(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?",
            r"\d{1,2}[-/.月]\d{1,2}(?:日)?(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?",
            r"\d{10}",
        ]
        return any(re.fullmatch(pattern, text, re.I) for pattern in patterns)

    @classmethod
    def _extract_auto_reply_time_metadata(cls, html_text: str, now: Optional[datetime] = None) -> Dict[str, Any]:
        tz = pytz.timezone(settings.TZ)
        now = now or cls._auto_reply_now()
        now = tz.localize(now) if now.tzinfo is None else now.astimezone(tz)
        parsed_items = []
        for raw_value, source in cls._collect_auto_reply_time_values(html_text):
            parsed = cls._parse_auto_reply_thread_time(raw_value, now=now)
            if parsed:
                parsed_items.append({"time": parsed, "raw": raw_value, "source": source})
        if not parsed_items:
            return {}

        published = next((item for item in parsed_items if item["source"] == "published"), None)
        if not published:
            published = next((item for item in parsed_items if item["source"] in ("generic", "attribute", "timestamp")), None)
        last_activity_items = [item for item in parsed_items if item["source"] == "last_activity"]
        last_activity = max(last_activity_items, key=lambda item: item["time"]) if last_activity_items else max(
            parsed_items,
            key=lambda item: item["time"],
        )
        fresh = published or last_activity
        metadata: Dict[str, Any] = {}

        def add(prefix: str, item: Optional[Dict[str, Any]]):
            if not item:
                return
            age_days = max(0.0, (now - item["time"]).total_seconds() / 86400)
            metadata[f"{prefix}_at"] = item["time"].strftime("%Y-%m-%d %H:%M:%S")
            metadata[f"{prefix}_at_ts"] = item["time"].timestamp()
            metadata[f"{prefix}_age_days"] = round(age_days, 3)
            metadata[f"{prefix}_time_raw"] = str(item["raw"])[:80]
            metadata[f"{prefix}_time_source"] = item["source"]

        add("published", published)
        add("last_activity", last_activity)
        add("fresh", fresh)
        return metadata

    @staticmethod
    def _auto_reply_candidate_context(html_text: str, start: int, end: int) -> str:
        starts = [
            html_text.rfind("<tbody", 0, start),
            html_text.rfind("<tr", 0, start),
            html_text.rfind("<li", 0, start),
        ]
        context_start = max([idx for idx in starts if idx >= 0] or [max(0, start - 800)])
        ends = [
            html_text.find("</tbody>", end),
            html_text.find("</tr>", end),
            html_text.find("</li>", end),
        ]
        valid_ends = [idx for idx in ends if idx >= 0]
        context_end = (min(valid_ends) + 8) if valid_ends else min(len(html_text), end + 800)
        return html_text[context_start:context_end]

    @staticmethod
    def _auto_reply_detail_time_context(html_text: str) -> str:
        text = html_text or ""
        strip_patterns = [
            r"(?is)<title\b[^>]*>.*?</title>",
            r"(?is)<[^>]+\bid=[\"']thread_subject[\"'][^>]*>.*?</[^>]+>",
            r"(?is)<td[^>]+\bid=[\"']postmessage_\d+[\"'][^>]*>.*?</td>",
            r"(?is)<div[^>]+\bclass=[\"'][^\"']*t_fsz[^\"']*[\"'][^>]*>.*?</div>",
        ]
        for pattern in strip_patterns:
            text = re.sub(pattern, " ", text)
        return text

    @staticmethod
    def _auto_reply_newness_timestamp(item: Dict[str, Any]) -> Optional[float]:
        for key in ("fresh_at_ts", "published_at_ts", "last_activity_at_ts"):
            try:
                value = float(item.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    @classmethod
    def _sort_auto_reply_candidates_by_newness(cls, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        def sort_key(item: Dict[str, Any]) -> Tuple[int, float, int]:
            timestamp = cls._auto_reply_newness_timestamp(item)
            try:
                tid = int(item.get("tid") or 0)
            except (TypeError, ValueError):
                tid = 0
            return (1 if timestamp is not None else 0, timestamp or 0.0, tid)

        return sorted(candidates, key=sort_key, reverse=True)

    def _indexed_accounts(self) -> List[Tuple[int, dict, str]]:
        indexed_accounts = []
        seen_ids = {}
        for idx, account in enumerate(self._accounts):
            raw_account_id = self._get_account_id(account, idx)
            seen_ids[raw_account_id] = seen_ids.get(raw_account_id, 0) + 1
            account_id = raw_account_id if seen_ids[raw_account_id] == 1 else f"{raw_account_id}_{seen_ids[raw_account_id]}"
            indexed_accounts.append((idx, account, account_id))
        return indexed_accounts

    def _find_indexed_account(self, account_index: int, account_id: str) -> Optional[dict]:
        indexed_accounts = self._indexed_accounts()
        for idx, account, indexed_account_id in indexed_accounts:
            if idx == account_index and indexed_account_id == account_id:
                return account
        for _, account, indexed_account_id in indexed_accounts:
            if indexed_account_id == account_id:
                return account
        return None

    @staticmethod
    def _strip_html(html_text: str) -> str:
        if not html_text:
            return ""
        text = re.sub(r"(?is)<script[^>]*>.*?</script>", "\n", html_text)
        text = re.sub(r"(?is)<style[^>]*>.*?</style>", "\n", text)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</(?:p|div|li|tr|td|th|dd|dt|em|span|strong|a|h\d)>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html_lib.unescape(text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text)
        return text.strip()

    @classmethod
    def _extract_thread_candidates(cls, html_text: str, fid: str, base_url: str) -> List[Dict[str, Any]]:
        candidates = []
        seen = set()
        if not html_text:
            return candidates
        for match in re.finditer(r"<a\b(?P<attrs>[^>]*\bhref=[\"'][^\"']+[\"'][^>]*)>(?P<title>.*?)</a>", html_text, re.I | re.S):
            attrs = match.group("attrs")
            href_match = re.search(r"\bhref=[\"']([^\"']+)[\"']", attrs, re.I)
            if not href_match:
                continue
            href = html_lib.unescape(href_match.group(1))
            tid_match = re.search(r"(?:[?&]tid=|thread-)(\d+)", href, re.I)
            if not tid_match:
                continue
            tid = tid_match.group(1)
            title = cls._strip_html(match.group("title"))
            if not title or tid in seen:
                continue
            if title in {"上一页", "下一页", "最后发表", "新窗"}:
                continue
            context = cls._auto_reply_candidate_context(html_text, match.start(), match.end())
            time_context = context.replace(match.group(0), " ")
            seen.add(tid)
            candidate = {
                "fid": str(fid),
                "tid": tid,
                "title": title[:160],
                "url": f"{base_url.rstrip('/')}/forum.php?mod=viewthread&tid={tid}",
                "source_url": urljoin(base_url.rstrip("/") + "/", href),
                "context": cls._strip_html(context)[:500],
                "sticky_like": cls._is_auto_reply_sticky_context(context),
                "risky_link": cls._has_auto_reply_risky_link(context, base_url),
            }
            candidate.update(cls._extract_auto_reply_time_metadata(time_context))
            candidates.append(candidate)
        return candidates

    @staticmethod
    def _extract_formhash(html_text: str) -> str:
        if not html_text:
            return ""
        patterns = [
            r'name=[\"\']formhash[\"\']\s+value=[\"\']([a-f0-9]{8})[\"\']',
            r'value=[\"\']([a-f0-9]{8})[\"\']\s+name=[\"\']formhash[\"\']',
            r'formhash[=\"\'\s:]+([a-f0-9]{8})',
        ]
        for pattern in patterns:
            match = re.search(pattern, html_text, re.I)
            if match:
                return match.group(1)
        return ""

    @classmethod
    def _extract_auto_reply_thread_detail(cls, html_text: str, candidate: Dict[str, Any],
                                          allowed_forum_ids: List[str]) -> Dict[str, Any]:
        title = cls._first_nonempty([
            cls._first_match(html_text, [r'id=[\"\']thread_subject[\"\'][^>]*>(.*?)<']),
            candidate.get("title", ""),
            cls._first_match(html_text, [r"<title>\s*(.*?)\s*</title>"]),
        ])
        author = cls._first_nonempty([
            cls._first_match(html_text, [r'class=[\"\'][^\"\']*authi[^\"\']*[\"\'][^>]*>.*?<a[^>]*>(.*?)</a>']),
            cls._first_match(html_text, [r'<a[^>]+class=[\"\']xw1[\"\'][^>]*>(.*?)</a>']),
            cls._first_match(html_text, [r'authorid=\d+[^>]*>(.*?)</a>']),
        ])
        content_html = cls._extract_auto_reply_first_post_html(html_text)
        content = cls._strip_html(content_html or html_text)
        fids = re.findall(r'forum\.php\?mod=forumdisplay&fid=(\d+)', html_text or "", re.I)
        fid = ""
        for item in fids:
            if item in allowed_forum_ids:
                fid = item
                break
        fid = fid or str(candidate.get("fid") or "")
        formhash = cls._extract_formhash(html_text)
        can_reply = bool(formhash) and cls._has_auto_reply_fast_reply_form(html_text)
        post_author_refs = cls._extract_auto_reply_post_author_refs(html_text)
        post_authors = [ref.get("username", "") for ref in post_author_refs if ref.get("username")]
        if not post_authors:
            post_authors = cls._extract_auto_reply_post_authors(html_text)
        detail = dict(candidate)
        for key in list(detail.keys()):
            if key.startswith(("published_", "last_activity_", "fresh_")):
                detail.pop(key, None)
        time_context = cls._auto_reply_detail_time_context(html_text)
        detail.update({
            "fid": fid,
            "title": cls._strip_html(title)[:160],
            "author": cls._strip_html(author)[:80],
            "content": content,
            "account_identity": cls._extract_auto_reply_logged_in_identity(html_text),
            "post_author_refs": post_author_refs[:50],
            "post_authors": post_authors[:20],
            "thread_subject_found": bool(cls._first_match(html_text, [r'id=[\"\']thread_subject[\"\'][^>]*>(.*?)<'])),
            "content_found": bool(content_html),
            "sticky_like": bool(candidate.get("sticky_like")) or cls._is_auto_reply_sticky_context(f"{title}\n{content}"),
            "risky_link": bool(candidate.get("risky_link")) or cls._has_auto_reply_risky_link(content_html or html_text, candidate.get("url") or ""),
            "formhash": formhash,
            "can_reply": can_reply,
            "blocked_page": cls._is_auto_reply_blocked_page(html_text),
        })
        detail.update(cls._extract_auto_reply_time_metadata(time_context))
        return detail

    @staticmethod
    def _first_nonempty(values: List[Any]) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    @staticmethod
    def _first_match_raw(text: str, patterns: List[str]) -> str:
        for pattern in patterns:
            m = re.search(pattern, text or "", re.I | re.S)
            if m:
                value = html_lib.unescape(m.group(1)).strip()
                if value:
                    return value
        return ""

    @classmethod
    def _extract_auto_reply_first_post_html(cls, html_text: str) -> str:
        """Extract the first-post body conservatively without stopping at nested table cells."""
        html = html_text or ""
        post_match = re.search(
            r"<div\b[^>]+id=[\"']post_\d+[\"'][^>]*>[\s\S]*?(?=<div\b[^>]+id=[\"']post_\d+[\"']|<div\b[^>]+id=[\"']postlistreply[\"']|</body>|$)",
            html,
            re.I,
        )
        search_area = post_match.group(0) if post_match else html
        start_match = re.search(
            r"<td\b[^>]+id=[\"']postmessage_\d+[\"'][^>]*>|<div\b[^>]+class=[\"'][^\"']*t_fsz[^\"']*[\"'][^>]*>",
            search_area,
            re.I,
        )
        if not start_match:
            return html_lib.unescape(search_area).strip()
        body = search_area[start_match.start():]
        end_match = re.search(
            r"(?=<div\b[^>]+id=[\"']post_rate_div_|<div\b[^>]+class=[\"'][^\"']*pob[^\"']*[\"']|<div\b[^>]+id=[\"']comment_|<div\b[^>]+id=[\"']postlistreply[\"'])",
            body,
            re.I,
        )
        if end_match:
            body = body[:end_match.start()]
        return html_lib.unescape(body).strip()

    @classmethod
    def _extract_auto_reply_post_authors(cls, html_text: str) -> List[str]:
        authors: List[str] = []
        seen = set()
        for pattern in [
            r'class=[\"\'][^\"\']*authi[^\"\']*[\"\'][^>]*>.*?<a[^>]*>(.*?)</a>',
            r'<a[^>]+class=[\"\']xw1[\"\'][^>]*>(.*?)</a>',
            r'authorid=\d+[^>]*>(.*?)</a>',
        ]:
            for match in re.finditer(pattern, html_text or "", re.I | re.S):
                author = cls._strip_html(match.group(1))[:80]
                key = author.lower()
                if author and key not in seen:
                    seen.add(key)
                    authors.append(author)
        return authors

    @classmethod
    def _extract_auto_reply_logged_in_identity(cls, html_text: str) -> Dict[str, str]:
        """Best-effort parse of the current logged-in Discuz account from page chrome."""
        html = html_text or ""
        info: Dict[str, str] = {}

        uid_patterns = [
            r"\bdiscuz_uid\s*=\s*['\"]?(\d+)",
            r"\bmember_uid\s*=\s*['\"]?(\d+)",
            r"id=[\"']um[\"'][\s\S]{0,2000}?home\.php\?mod=space(?:&amp;|&)uid=(\d+)",
            r"class=[\"'][^\"']*vwmy[^\"']*[\"'][\s\S]{0,800}?home\.php\?mod=space(?:&amp;|&)uid=(\d+)",
        ]
        for pattern in uid_patterns:
            uid = cls._first_match(html, [pattern])
            if uid and uid != "0":
                info["uid"] = uid
                break

        username_patterns = [
            r"class=[\"'][^\"']*vwmy[^\"']*[\"'][^>]*>\s*(?:<a[^>]*>)?(.*?)(?:</a>)?\s*</",
            r"id=[\"']um[\"'][\s\S]{0,2000}?home\.php\?mod=space[^>]*>(.*?)</a>",
            r"欢迎您回来[，,\s]*(?:<[^>]+>)*([^<\s]{1,40})",
        ]
        for pattern in username_patterns:
            username = cls._strip_html(cls._first_match_raw(html, [pattern]))[:80]
            if username and username not in {"访问我的空间", "我的空间", "设置", "退出"}:
                info["username"] = username
                break
        return info

    @classmethod
    def _extract_auto_reply_post_author_refs(cls, html_text: str) -> List[Dict[str, str]]:
        """Parse ordered post authors from Discuz post blocks, preserving uid when available."""
        html = html_text or ""
        refs: List[Dict[str, str]] = []
        seen = set()

        post_blocks = [match.group(0) for match in re.finditer(
            r"<div\b[^>]+id=[\"']post_\d+[\"'][^>]*>[\s\S]*?(?=<div\b[^>]+id=[\"']post_\d+[\"']|</body>|$)",
            html,
            re.I,
        )]
        if not post_blocks:
            post_blocks = [html]

        for block in post_blocks:
            uid = cls._first_match(block, [
                r"authorid=(\d+)",
                r"home\.php\?mod=space(?:&amp;|&)uid=(\d+)",
            ])
            username = cls._first_nonempty([
                cls._first_match(block, [r"class=[\"'][^\"']*authi[^\"']*[\"'][^>]*>.*?<a[^>]*>(.*?)</a>"]),
                cls._first_match(block, [r"<a[^>]+class=[\"']xw1[\"'][^>]*>(.*?)</a>"]),
                cls._first_match(block, [r'authorid=\d+[^>]*>(.*?)</a>']),
            ])
            username = cls._strip_html(username)[:80]
            if not uid and not username:
                continue
            key = (uid or "", username.lower())
            if key in seen:
                continue
            seen.add(key)
            ref: Dict[str, str] = {}
            if uid:
                ref["uid"] = uid
            if username:
                ref["username"] = username
            refs.append(ref)
        return refs

    @staticmethod
    def _has_auto_reply_fast_reply_form(html_text: str) -> bool:
        text = html_text or ""
        fastpost_form = re.search(
            r'<form\b[^>]*(?:id=[\"\']fastpostform[\"\']|name=[\"\']fastpostform[\"\'])[^>]*>.*?</form>',
            text,
            re.I | re.S,
        )
        if not fastpost_form:
            return False
        form_html = fastpost_form.group(0)
        has_message_box = re.search(
            r'<textarea\b[^>]*(?:id=[\"\']fastpostmessage[\"\']|name=[\"\']message[\"\'])',
            form_html,
            re.I,
        )
        has_reply_submit = "replysubmit=yes" in form_html.lower() or re.search(
            r'name=[\"\']replysubmit[\"\']',
            form_html,
            re.I,
        )
        return bool(has_message_box and has_reply_submit)

    @staticmethod
    def _is_auto_reply_blocked_page(html_text: str) -> bool:
        text = html_text or ""
        markers = [
            "static/safe/js/web.js",
            "safeid=",
            "enter-btn",
            "cf-challenge",
            "cf-turnstile",
            "challenge-platform",
            "cf-browser-verification",
            "Cloudflare",
            "Just a moment",
            "请完成安全验证",
            "安全检查",
            "访问过于频繁",
            "您没有权限",
            "需要登录",
            "抱歉，指定的主题不存在",
        ]
        return any(marker.lower() in text.lower() for marker in markers)

    @staticmethod
    def _is_auto_reply_sticky_context(html_text: str) -> bool:
        text = html_text or ""
        markers = [
            "displayorder",
            "stickthread",
            "置顶",
            "全局置顶",
            "分类置顶",
            "本版置顶",
            "公告",
            "版规",
            "规则",
        ]
        return any(marker.lower() in text.lower() for marker in markers)

    @staticmethod
    def _has_auto_reply_risky_link(html_text: str, base_url: str = "") -> bool:
        text = html_text or ""
        base_host = urlparse(base_url or "https://sehuatang.net").hostname or "sehuatang.net"
        base_host = base_host.lower().lstrip(".")
        url_patterns = [
            r'\b(?:https?|ftp)://[^\s\"\'<>()]+',
            r'\b(?:magnet|ed2k|thunder)[:：][^\s\"\'<>()]+',
        ]
        urls = []
        for pattern in url_patterns:
            urls.extend(re.findall(pattern, text, re.I))
        for match in re.finditer(r'\b(?:href|src)=[\"\']([^\"\']+)[\"\']', text, re.I):
            urls.append(html_lib.unescape(match.group(1)))
        for raw_url in urls:
            parsed = urlparse(raw_url.strip())
            scheme = (parsed.scheme or "").lower()
            if scheme in {"magnet", "ed2k", "thunder", "ftp"}:
                return True
            host = (parsed.hostname or "").lower().lstrip(".")
            if host and host != base_host and not host.endswith(f".{base_host}"):
                return True

        risky_domains = [
            "t.me", "telegram.me", "discord.gg", "discord.com", "line.me",
            "whatsapp.com", "mega.nz", "pan.baidu.com", "aliyundrive.com",
            "alipan.com", "quark.cn", "115.com", "pikpak", "bit.ly", "tinyurl.com",
        ]
        lower = text.lower()
        return any(domain in lower for domain in risky_domains)

    @staticmethod
    def _normalize_auto_reply_risk_text(text: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
        return "".join(ch for ch in normalized if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")

    @classmethod
    def _has_auto_reply_contact_or_diversion_text(cls, text: str) -> bool:
        normalized = str(text or "")
        patterns = [
            r"\b(?:telegram|discord|whatsapp|line|twitter|x\.com|tg群?|群号|QQ群|微信群|微信|VX|私信|私聊|PM)\b",
            r"(?:加群|进群|入群|私信|私聊|联系我|联系方式|站外|外站|跳转|最新地址|备用网址|访问方法|回家地址)",
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
            r"(?<!\d)(?:1[3-9]\d{9}|\d{6,12})(?!\d)",
        ]
        if any(re.search(pattern, normalized, re.I) for pattern in patterns):
            return True
        compact = cls._normalize_auto_reply_risk_text(normalized)
        compact_markers = [
            "telegram", "discord", "whatsapp", "twitter", "xcom", "tg群", "qq群", "微信群",
            "微信", "vx", "私信", "私聊", "加群", "进群", "入群", "联系我", "联系方式",
            "站外", "外站", "跳转", "最新地址", "备用网址", "访问方法", "回家地址", "永久地址",
            "防丢地址", "防失联", "邮箱", "群号",
        ]
        return any(marker in compact for marker in compact_markers)

    def _hard_filter_auto_reply_candidate(self, item: Dict[str, Any], forum_ids: List[str],
                                          account_id: str = "", require_detail: bool = False) -> Tuple[bool, str]:
        tid = str(item.get("tid") or "")
        title = str(item.get("title") or "")
        content = str(item.get("content") or "")
        author = str(item.get("author") or "")
        fid = str(item.get("fid") or "")
        haystack_title = title.lower()
        haystack_content = f"{title}\n{content}\n{item.get('context') or ''}".lower()

        if not tid:
            return False, "缺少 tid"
        if account_id and self._is_auto_thread_replied(account_id, tid):
            return False, "账号已回复过该主题"
        if fid and fid not in forum_ids:
            return False, f"实际版块 {fid} 不在允许列表"
        if item.get("blocked_page"):
            return False, "安全入口或权限页面"
        if item.get("sticky_like"):
            return False, "置顶/公告/规则上下文"
        if item.get("risky_link"):
            return False, "外链/下载协议风险"
        if self._has_auto_reply_contact_or_diversion_text(f"{title}\n{content}"):
            return False, "联系方式/站外引流风险"
        if account_id and require_detail:
            if self._auto_reply_current_account_has_replied_in_detail(item, account_id):
                return False, "账号已在详情页回复过该主题"
        if require_detail and not item.get("thread_subject_found"):
            return False, "详情页缺少主题标题标记"
        if require_detail and not item.get("content_found"):
            return False, "详情页缺少首楼正文标记"
        if require_detail and not item.get("formhash"):
            return False, "缺少 formhash"
        if require_detail and not item.get("can_reply"):
            return False, "页面没有可用回复表单"
        age_reason = self._auto_reply_thread_age_filter_reason(item, require_detail=require_detail)
        if age_reason:
            return False, age_reason

        for keyword in self._parse_line_list(self._auto_reply_title_blacklist):
            if keyword.lower() in haystack_title:
                return False, f"标题黑名单：{keyword[:20]}"
        for keyword in self._parse_line_list(self._auto_reply_content_blacklist):
            if keyword.lower() in haystack_content:
                return False, f"内容黑名单：{keyword[:20]}"
        for keyword in self._parse_line_list(self._auto_reply_author_blacklist):
            if keyword.lower() in author.lower():
                return False, f"作者黑名单：{keyword[:20]}"

        management_author_keywords = ["admin", "管理员", "版主", "管理组"]
        if author and any(keyword.lower() in author.lower() for keyword in management_author_keywords):
            return False, "管理账号作者"

        trap_keywords = [
            "自动回复检测", "机器人检测", "回帖检测", "回复检测", "回帖识别", "回复识别",
            "回帖后识别", "回复后识别", "回帖后验证", "回复后验证", "回帖后登记", "回复后登记",
            "识别账号", "检测账号", "验证账号", "真人验证", "机器人验证", "自动回复", "bot检测",
        ]
        if any(keyword.lower() in haystack_content for keyword in trap_keywords):
            return False, "钓鱼/诱捕/自动回复检测风险"

        moderation_keywords = [
            "版规", "规则", "公告", "置顶", "投诉", "举报", "管理", "删帖", "封禁",
            "永久访问", "访问本站", "访问方法", "发布器", "白名单", "报毒", "安装",
            "备用网址", "最新地址", "防丢", "二次验证", "申诉", "官方", "通知", "邀请码",
            "教程", "版务", "站务", "安全入口", "验证入口", "加群", "私信",
            "联系方式", "Telegram", "TG", "QQ", "微信", "群号", "必看", "新手须知",
            "申请", "悬赏", "求助", "建议", "声明", "处罚", "招募", "招聘", "高薪",
            "兼职", "接单", "合作", "破解", "破解高手", "safe_gate",
        ]
        if any(keyword.lower() in haystack_content for keyword in moderation_keywords):
            return False, "规则/公告/管理类关键词"
        return True, ""

    def _preflight_auto_reply_submit(self, detail: Dict[str, Any], reply: str, forum_ids: List[str],
                                     account_id: str) -> Tuple[bool, str]:
        ok, reason = self._hard_filter_auto_reply_candidate(
            detail,
            forum_ids,
            account_id=account_id,
            require_detail=True,
        )
        if not ok:
            return False, reason
        if self._validate_auto_reply_text(reply, detail) != reply:
            return False, "回复文本提交前校验失败"
        return True, ""

    def _auto_reply_thread_age_filter_reason(self, item: Dict[str, Any], require_detail: bool = False) -> str:
        try:
            max_age_days = int(self._auto_reply_max_thread_age_days or 0)
        except (TypeError, ValueError):
            max_age_days = 7
        if max_age_days <= 0:
            return ""

        if require_detail:
            if str(item.get("published_time_source") or "") != "published":
                return "详情未解析到可信主题发布时间，按新鲜度策略保守跳过"
            timestamp = self._auto_reply_thread_timestamp(item, prefer_published=True)
        else:
            timestamp = self._auto_reply_thread_timestamp(item, prefer_published=False)
        if timestamp is None:
            return "详情未解析到主题时间，按新鲜度策略保守跳过" if require_detail else ""

        age_days = max(0.0, (self._auto_reply_now().timestamp() - timestamp) / 86400)
        if age_days > max_age_days:
            return f"主题时间超过 {max_age_days} 天（约 {age_days:.1f} 天）"
        return ""

    @staticmethod
    def _auto_reply_thread_timestamp(item: Dict[str, Any], prefer_published: bool = False) -> Optional[float]:
        keys = ("published_at_ts", "fresh_at_ts", "last_activity_at_ts") if prefer_published else (
            "fresh_at_ts",
            "published_at_ts",
            "last_activity_at_ts",
        )
        for key in keys:
            try:
                value = float(item.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    def _assess_auto_reply_with_ai(self, detail: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        prompt = self._build_auto_reply_assessment_prompt(detail)
        raw_response = self._call_auto_reply_llm_with_timeout(prompt, "评估")
        if not raw_response:
            return None

        assessment = self._extract_auto_reply_ai_json(raw_response)
        validated = self._validate_auto_reply_assessment(assessment)
        if not validated:
            logger.info("[SehuatangSignin] 自动回帖 AI 评估未通过低风险校验")
        return validated

    def _polish_auto_reply_with_ai(self, detail: Dict[str, Any], assessment: Dict[str, Any]) -> Optional[str]:
        rejected_reply = ""
        rejection_reason = ""
        validation_failures = 0
        started_at = time.monotonic()
        try:
            polish_deadline_seconds = max(
                180,
                int(self._auto_reply_polish_deadline_seconds or 360),
            )
        except (TypeError, ValueError):
            polish_deadline_seconds = 360

        # Keep asking the AI to repair the same candidate after local validation
        # failures. Only AI/system errors or the generous wall-clock deadline stop
        # polishing, so a good candidate is not discarded by a small attempt cap.
        while True:
            elapsed_seconds = time.monotonic() - started_at
            if elapsed_seconds >= polish_deadline_seconds:
                logger.info(
                    f"[SehuatangSignin] 自动回帖 AI 回复润色超过 {polish_deadline_seconds} 秒仍未通过本地校验，"
                    f"停止润色当前候选；本地校验失败次数={validation_failures}"
                )
                return None
            prompt = self._build_auto_reply_polish_prompt(
                detail,
                assessment,
                rejected_reply=rejected_reply,
                rejection_reason=rejection_reason,
            )
            raw_response = self._call_auto_reply_llm_with_timeout(prompt, "润色")
            reply = self._extract_auto_reply_reply(raw_response)
            validated, reason = self._validate_auto_reply_text_with_reason(reply, detail)
            if validated:
                return validated

            rejected_reply = reply
            rejection_reason = reason or "回复格式不合规"
            validation_failures += 1
            logger.info(
                f"[SehuatangSignin] 自动回帖 AI 回复第 {validation_failures} 次未通过本地校验："
                f"{rejection_reason}；继续修正当前候选，不按次数上限跳过"
            )
        return None

    def _call_auto_reply_llm_with_timeout(self, prompt: str, stage_name: str) -> str:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(lambda: asyncio.run(self._call_auto_reply_llm(prompt)))
        try:
            return future.result(timeout=max(5, int(self._auto_reply_ai_timeout or 45)))
        except concurrent.futures.TimeoutError as e:
            logger.warning(f"[SehuatangSignin] 自动回帖 AI {stage_name}调用超时")
            raise RuntimeError(f"AI调用失败：{stage_name}调用超时") from e
        except Exception as e:
            message = str(e) or e.__class__.__name__
            if message.startswith("AI调用失败"):
                logger.warning(f"[SehuatangSignin] 自动回帖 AI {stage_name}调用失败：{message}")
                raise
            logger.warning(f"[SehuatangSignin] 自动回帖 AI {stage_name}不可用或调用失败：{message}")
            raise RuntimeError(f"AI调用失败：{stage_name}不可用或调用失败：{message}") from e
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    async def _call_auto_reply_llm(self, prompt: str) -> str:
        try:
            from app.agent.llm import LLMHelper
        except Exception as e:
            logger.warning(f"[SehuatangSignin] 自动回帖 AI调用失败：系统 LLM 不可用：{e}")
            raise RuntimeError(f"AI调用失败：系统 LLM 不可用：{e}") from e

        llm = LLMHelper.get_llm(streaming=False)
        if inspect.isawaitable(llm):
            llm = await llm
        if not llm:
            raise RuntimeError("AI调用失败：系统 LLM 不可用")
        if hasattr(llm, "ainvoke"):
            response = llm.ainvoke(prompt)
            if inspect.isawaitable(response):
                response = await response
        elif hasattr(llm, "invoke"):
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(None, llm.invoke, prompt)
        else:
            raise RuntimeError("AI调用失败：系统 LLM 不支持调用")
        return self._extract_llm_response_text(response, LLMHelper)

    @classmethod
    def _extract_llm_response_text(cls, response: Any, llm_helper: Any = None) -> str:
        if response is None:
            return ""

        extractor = getattr(llm_helper, "extract_text_content", None) if llm_helper else None
        if callable(extractor):
            try:
                try:
                    content = response.content
                except AttributeError:
                    content = response
                try:
                    text = extractor(content, fallback_to_string=True)
                except TypeError:
                    text = extractor(content)
                return str(text or "")
            except Exception:
                pass

        return cls._llm_response_to_text(response)

    @staticmethod
    def _llm_response_to_text(response: Any) -> str:
        if response is None:
            return ""
        if isinstance(response, str):
            return response
        content = getattr(response, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
                else:
                    parts.append(str(item))
            return "\n".join(part for part in parts if part)
        return str(response)

    def _build_auto_reply_assessment_prompt(self, detail: Dict[str, Any]) -> str:
        custom_prompt = self._auto_reply_custom_prompt.strip()
        content = str(detail.get("content") or "")
        return (
            "你是论坛自动回帖安全评估器，只判断帖子是否适合普通低调短回复，不生成回复。\n"
            "必须只输出 JSON，不要输出 Markdown、解释、思考过程或额外文本。\n"
            "字段只允许 should_reply、risk_level、risk_reasons，不要输出 reply 字段。\n"
            "只有明确是普通低风险分享或轻量交流帖时，should_reply 才能为 true 且 risk_level 为 low。\n"
            "成人/敏感/资源内容本身不是风险理由，不得仅因这类内容提高 risk_level 或拒绝。\n"
            "Adult/sensitive/resource content alone is not a rejection reason and must not raise risk.\n"
            "普通资源分享/预览帖如未命中强制拒绝项，可评估为 should_reply=true 且 risk_level=low。\n"
            "Ordinary resource share/preview posts may be should_reply=true with risk_level=low unless they hit mandatory exclusions.\n"
            "回复可见、预览播放器、番号列表、资源说明本身不等同于诱导下载；只有外链跳转、联系方式、加群、私信、站点访问方法或明确下载诱导才必须拒绝。\n"
            "钓鱼/诱捕/反自动回复检测必须极度保守：如果帖子暗示回帖后会识别、验证、筛选、登记、检测账号/真人/机器人/自动回复，或回帖后才暴露外链、联系方式、群组、最新地址、访问方法，必须 should_reply=false。\n"
            "Normal reply-to-view content alone is allowed, but any trap where replying reveals risk, identifies the account, tests automation, verifies humans/bots, or exposes offsite/contact/access information must be rejected.\n"
            "必须拒绝 rules/announcements/moderation/site-admin/safe-gate/access-method/latest-address/whitelist/publisher/help/tutorials/complaints/appeals/recruitment/cracking-tutorial/external/contact/group/private-message/traffic-diversion/link-risk/download-inducement/phishing/scam/ads/soft-ad/disputes/inadequate-info。\n"
            "对应中文包括规则、公告、版务、站务、站点管理、安全入口、访问方法、最新地址、白名单、发布器、求助、教程、投诉、申诉、招募、破解教程、外部渠道、联系方式、群组、私信、引流、外链风险、诱导下载、钓鱼、诈骗、广告、软广、争议、信息不足。\n"
            "JSON 格式：{\"should_reply\": true, \"risk_level\": \"low\", \"risk_reasons\": []}\n"
            f"补充要求（只能收紧，不能放宽上述拒绝规则）：{custom_prompt or '无'}\n"
            f"版块 ID：{detail.get('fid') or '-'}\n"
            f"标题：{detail.get('title') or '-'}\n"
            f"作者：{detail.get('author') or '-'}\n"
            f"首楼正文完整内容：{content or '-'}"
        )

    def _build_auto_reply_polish_prompt(self, detail: Dict[str, Any], assessment: Dict[str, Any],
                                        rejected_reply: str = "", rejection_reason: str = "") -> str:
        templates = self._parse_line_list(self._auto_reply_templates)
        custom_prompt = self._auto_reply_custom_prompt.strip()
        content = str(detail.get("content") or "")
        risk_reasons = assessment.get("risk_reasons") or []
        rejected_reply = re.sub(r"\s+", " ", self._strip_html(str(rejected_reply or ""))).strip()
        rejection_reason = re.sub(r"\s+", " ", str(rejection_reason or "")).strip()
        retry_guidance = ""
        if rejected_reply or rejection_reason:
            retry_guidance = (
                "上一轮被本地校验拒绝，请修正后重新生成，不要解释。\n"
                f"被拒回复：{json.dumps(rejected_reply[:80] or '-', ensure_ascii=False)}\n"
                f"拒绝原因：{rejection_reason[:80] or '未说明'}\n"
                "本轮必须避开该问题，换一种自然短评，只输出新回复。\n"
            )
        return (
            "你只负责为已通过低风险评估的论坛帖子生成一条回帖，不再做适合性判断。\n"
            "必须只输出最终要提交的纯文本回复，不要输出 JSON、字段名、引号、Markdown、解释或思考过程。\n"
            "回复要求：自然中文，像普通用户随手回复，低调短句，短优先，6-18 个中文字符最佳，最长 30 个字符。\n"
            "禁止 emoji、Markdown、URL、联系方式、AI/机器人/模型自称、道歉拒绝话术，不要重复标题或照抄标题。\n"
            "电影/视频/影视/资源分享类帖子：优先写像刚看到标题/简介后的轻口语短评，语气松弛，不要像客服或模板。\n"
            "这类回复可以轻轻贴合标题或正文透露出的氛围：简介是否清楚、题材/主题、画质/版本、演员/人物/主体、画风/风格；只根据已出现的信息，不编造剧情、演员、清晰度、评价。\n"
            "明确不要写论坛套话：感谢分享、支持一下、路过看看、顶一下、楼主辛苦、辛苦了、前排支持。\n"
            "不要在回复里出现 下载、链接、地址、资源 等索取或交付资源词。\n"
            "不要复述片名、标题、番号或专名；需要贴合内容时，用泛化说法，不照搬原文名词。\n"
            "所有回复必须只使用标题和首楼正文中的可见线索，不要引入帖子里没出现的信息。\n"
            "不得编造剧情、演员/人物、导演、字幕、画质、版本、时长、评分、资源质量或观看体验。\n"
            "必须从标题/正文中挑一个可见线索再泛化成短评；没有对应线索就不要写该方向。\n"
            "如果标题和正文内容太薄，选择中性、内容有依据的短句，例如标题信息挺直观、看介绍还算清楚，不要假装看过细节。\n"
            "避免空洞泛泛的库存短语，即使未命中禁用词，也不要输出不错不错、看起来不错、可以可以、很棒、收藏了这类无可见依据的话。\n"
            "影视/视频/资源分享帖的合格方向示例（只看方向，禁止逐字套用）：简介看着挺清楚 / 这个题材挺有意思 / 预览感觉还可以 / 画面风格挺顺眼 / 介绍写得蛮直观。\n"
            "这些示例不是模板；请根据帖子已出现的信息换一种说法。\n"
            "无效示例（绝对不要输出）：感谢分享 / 支持一下 / 路过看看 / 顶一下 / 楼主辛苦了 / 求个资源 / 有下载吗 / 看看链接 / 地址发下 / 磁力有吗 / 网盘在哪。\n"
            "禁止出现的套话和资源词：感谢分享、谢谢分享、感谢楼主、支持一下、路过看看、顶一下、帮顶、前排支持、楼主辛苦、辛苦了、下载、链接、地址、资源、网盘、磁力、私发、发我、求资源。\n"
            "标点根据回复内容自然选择：可以不加句末标点，也可以用。！？~等少量常见标点或一个普通空格作停顿，但不要固定套用某一种。\n"
            "仍然要短、低调、不刷屏，不添加奇怪符号或颜文字。\n"
            "尽量避免直接照抄常见模板，参考模板只用于把握语气。\n"
            f"{retry_guidance}"
            f"参考模板：{json.dumps(templates, ensure_ascii=False)}\n"
            f"补充要求：{custom_prompt or '无'}\n"
            f"低风险原因：{json.dumps(risk_reasons, ensure_ascii=False)}\n"
            f"版块 ID：{detail.get('fid') or '-'}\n"
            f"标题：{detail.get('title') or '-'}\n"
            f"作者：{detail.get('author') or '-'}\n"
            f"首楼正文完整内容：{content or '-'}"
        )

    @classmethod
    def _extract_auto_reply_ai_json(cls, response_text: Any) -> Optional[Dict[str, Any]]:
        text = re.sub(r"(?is)<think>.*?</think>", "", str(response_text or "")).strip()
        if not text:
            return None
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.I | re.S)
        candidates = [text]
        if fenced:
            candidates.insert(0, fenced.group(1))
        json_object = cls._extract_first_json_object(text)
        if json_object:
            candidates.insert(0, json_object)
        for candidate in candidates:
            try:
                data = json.loads(candidate)
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                continue
        return None

    @classmethod
    def _extract_auto_reply_reply(cls, response_text: Any) -> str:
        text = re.sub(r"(?is)<think>.*?</think>", "", str(response_text or "")).strip()
        if not text:
            return ""
        data = cls._extract_auto_reply_ai_json(text)
        if isinstance(data, dict) and data.get("reply") is not None:
            return str(data.get("reply") or "")
        fenced = re.search(r"```(?:json|text)?\s*(.*?)\s*```", text, re.I | re.S)
        if fenced:
            text = fenced.group(1).strip()
        reply_match = re.search(r'["“]?reply["”]?\s*[:：]\s*["“]?([^"”\n{}]+)', text, re.I)
        if reply_match:
            return reply_match.group(1)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return lines[0] if lines else text

    @staticmethod
    def _extract_first_json_object(text: str) -> str:
        start = -1
        depth = 0
        in_string = False
        escaped = False
        for idx, char in enumerate(text or ""):
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
                continue
            if char == "{":
                if depth == 0:
                    start = idx
                depth += 1
            elif char == "}":
                if depth:
                    depth -= 1
                    if depth == 0 and start >= 0:
                        return text[start:idx + 1]
        return ""

    @staticmethod
    def _validate_auto_reply_assessment(assessment: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not isinstance(assessment, dict):
            return None
        allowed_keys = {"should_reply", "risk_level", "risk_reasons"}
        if any(key not in allowed_keys for key in assessment.keys()):
            return None
        if assessment.get("should_reply") is not True:
            return None
        risk_level = str(assessment.get("risk_level") or "").strip().lower()
        if risk_level != "low":
            return None
        risk_reasons = assessment.get("risk_reasons") or []
        if not isinstance(risk_reasons, list):
            risk_reasons = [str(risk_reasons)]
        return {
            "should_reply": True,
            "risk_level": "low",
            "risk_reasons": [str(item)[:80] for item in risk_reasons],
        }

    def _validate_auto_reply_text(self, reply_text: Any, detail: Dict[str, Any]) -> Optional[str]:
        validated, _ = self._validate_auto_reply_text_with_reason(reply_text, detail)
        return validated

    def _validate_auto_reply_text_with_reason(self, reply_text: Any,
                                              detail: Dict[str, Any]) -> Tuple[Optional[str], str]:
        reply = re.sub(r"(?is)<think>.*?</think>", "", str(reply_text or "")).strip()
        reply = self._strip_html(reply)
        reply = html_lib.unescape(reply).replace("\xa0", " ")
        reply = re.sub(r"\s+", " ", reply)
        reply = reply.strip(" \"'“”‘’`")
        lower_reply = reply.lower()
        if reply.count(" ") > 1:
            return None, "空格过多"
        if len(reply) < 2 or len(reply) > 30:
            return None, "回复长度必须为 2-30 个字符"
        if not re.search(r"[\u4e00-\u9fff]", reply):
            return None, "缺少中文内容"
        if re.search(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", reply):
            return None, "包含 emoji 或特殊符号"
        if re.search(r"[*_#>\[\]`]", reply):
            return None, "包含 Markdown 标记"
        if re.search(r"(?:https?://|www\.|[a-z0-9][a-z0-9.-]{1,}\.(?:com|net|org|cn|cc|tv|me|io|xyz)\b)", lower_reply):
            return None, "包含 URL 或域名"
        blocked_fragments = [
            "<think", "</think", "```", "{", "}", "我是ai", "作为ai", "作为一个ai",
            "ai助手", "语言模型", "机器人", "抱歉", "无法回复", "不能回复",
            "http://", "https://", "www.", "telegram", "tg", "qq", "微信", "群号",
            "下载", "链接", "地址", "资源", "网盘", "磁力", "私发", "发我", "求资源",
        ]
        for fragment in blocked_fragments:
            if fragment in lower_reply:
                return None, f"包含禁用词：{fragment[:20]}"
        compact_reply = re.sub(r"[\s，,。.!！?？~～、]+", "", reply).lower()
        stock_reply_fragments = [
            "感谢分享", "谢谢分享", "感谢楼主", "支持一下", "路过看看", "顶一下",
            "帮顶", "前排支持", "楼主辛苦", "辛苦了", "不错支持",
            "内容不错", "不错不错", "看起来不错", "可以可以", "很棒",
            "好东西", "收藏了", "收藏一下", "马克一下", "学习了",
        ]
        for fragment in stock_reply_fragments:
            if fragment in compact_reply:
                return None, f"命中论坛套话：{fragment}"
        if self._has_auto_reply_contact_or_diversion_text(reply):
            return None, "包含联系方式或引流内容"
        title = re.sub(r"\s+", "", self._strip_html(str(detail.get("title") or "")))
        if title and (reply == title or title in reply or (len(reply) >= 6 and reply in title)):
            return None, "重复标题或照抄标题"
        title_for_repeat_check = unicodedata.normalize("NFKC", title)
        for marker in [
            "影视资源分享", "电影资源分享", "视频资源分享", "影片资源分享",
            "资源分享", "影视分享", "电影分享", "视频分享", "影片分享",
        ]:
            if marker not in title_for_repeat_check:
                continue
            title_prefix = title_for_repeat_check.split(marker, 1)[0]
            title_prefix = re.sub(r"^(?:[\[【(（][^\]】)）]{1,30}[\]】)）])+", "", title_prefix)
            title_prefix = title_prefix.strip(" -_·.。,，:：|/\\[]【】()（）")
            if len(title_prefix) >= 2 and title_prefix in reply:
                return None, f"重复标题片名或专名：{title_prefix[:20]}"
            break
        reply_norm = reply.rstrip("。.!！")
        for template in self._parse_line_list(self._auto_reply_templates):
            template_norm = re.sub(r"\s+", "", self._strip_html(template)).strip(" \"'“”‘’`").rstrip("。.!！")
            if reply_norm and reply_norm == template_norm:
                return None, "照抄参考模板"
        return reply, ""

    def _submit_auto_reply(self, fs_sid: str, cookies: list, detail: Dict[str, Any], reply: str,
                           browser_session_key: str = "") -> Dict[str, Any]:
        fid = str(detail.get("fid") or "")
        tid = str(detail.get("tid") or "")
        formhash = str(detail.get("formhash") or "")
        if not fid or not tid or not formhash or not reply:
            return self._auto_reply_result("failed", "缺少回帖参数")
        post_url = (
            f"{self._base_url}/forum.php?mod=post&action=reply&fid={fid}&tid={tid}"
            "&extra=&replysubmit=yes&infloat=yes&handlekey=fastpost&inajax=1"
        )
        post_data = urlencode({
            "formhash": formhash,
            "message": reply,
            "usesig": "1",
            "subject": "",
            "posttime": str(int(time.time())),
        })
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": str(detail.get("url") or f"{self._base_url}/forum.php?mod=viewthread&tid={tid}"),
            "Origin": self._base_url,
        }
        response: Dict[str, Any] = {}
        if browser_session_key:
            try:
                response = fs_browser_session_post(
                    browser_session_key,
                    post_url,
                    post_data,
                    cookies,
                    headers=headers,
                    referer_url=headers["Referer"],
                )
            except Exception as e:
                logger.warning(f"[SehuatangSignin] 自动回帖持久浏览器 POST 失败，改用短浏览器/FS request.post 备用：{e}")
            else:
                if str((response or {}).get("html") or "").strip():
                    logger.info(f"[SehuatangSignin] 自动回帖已通过持久浏览器 POST 提交，via={response.get('via') or 'browser'}")
                    return self._parse_auto_reply_post_result(response.get("html", ""))
                logger.warning("[SehuatangSignin] 自动回帖持久浏览器 POST 未返回页面，改用短浏览器/FS request.post 备用")
        try:
            response = fs_browser_post(
                fs_sid,
                post_url,
                post_data,
                cookies,
                headers=headers,
                referer_url=headers["Referer"],
            )
        except Exception as e:
            logger.warning(f"[SehuatangSignin] 自动回帖浏览器 POST 失败，改用 FS request.post 备用：{e}")
        else:
            if str((response or {}).get("html") or "").strip():
                logger.info(f"[SehuatangSignin] 自动回帖已通过浏览器 POST 提交，via={response.get('via') or 'browser'}")
                return self._parse_auto_reply_post_result(response.get("html", ""))
            logger.warning("[SehuatangSignin] 自动回帖浏览器 POST 未返回页面，改用 FS request.post 备用")
        try:
            response = self._fs_post(fs_sid, post_url, post_data, cookies, headers=headers)
        except Exception as e:
            logger.warning(f"[SehuatangSignin] 自动回帖 POST 异常：{e}")
            return self._auto_reply_result("failed", f"回帖 POST 异常：{e}")
        return self._parse_auto_reply_post_result(response.get("html", ""))

    def _fs_post(self, fs_sid: str, url: str, post_data: str, cookies: list,
                 headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "cmd": "request.post",
            "session": fs_sid,
            "url": url,
            "postData": post_data,
            "maxTimeout": 60000,
        }
        if cookies:
            payload["cookies"] = cookies
        if headers:
            payload["headers"] = headers
        if self._proxy_url:
            payload["proxy"] = {"url": self._proxy_url}
        try:
            response = requests.post(self._flaresolverr_url, json=payload, timeout=max(self._timeout * 4, 90))
            response.raise_for_status()
            data = response.json()
        except (RequestException, ValueError) as e:
            raise RuntimeError(f"FlareSolverr request.post 失败：{e}") from e
        if data.get("status") != "ok":
            raise RuntimeError(data.get("message") or "FlareSolverr request.post 返回失败")
        solution = data.get("solution") or {}
        return {
            "html": solution.get("response", ""),
            "status": solution.get("status", 0),
            "cookies": solution.get("cookies", []),
        }

    @classmethod
    def _parse_auto_reply_post_result(cls, html_text: str) -> Dict[str, Any]:
        text = cls._strip_html(html_text)
        compact = re.sub(r"\s+", " ", text)
        lower = (html_text or "").lower()
        success_markers = ["post_reply_succeed", "succeedhandle_fastpost", "回复发布成功", "发表回复成功"]
        failure_markers = [
            "需要登录", "您没有权限", "验证码", "安全", "safe_gate", "抱歉", "失败",
            "未定义操作", "请不要重复发帖", "两次发表间隔", "包含不良内容", "审核",
        ]
        if any(marker.lower() in lower or marker in compact for marker in failure_markers):
            return cls._auto_reply_result("failed", compact[:120] or "回帖失败")
        if any(marker.lower() in lower or marker in compact for marker in success_markers):
            return cls._auto_reply_result("success", "回帖成功")
        unknown_message = compact[:120]
        if unknown_message:
            unknown_message = f"回帖结果未知：{unknown_message}"
        return cls._auto_reply_result("failed", unknown_message or "回帖结果未知")

    def _has_auto_reply_record_for_day(self, account_id: str, day: str) -> bool:
        history = self.get_data(self._auto_reply_history_key) or []
        if not isinstance(history, list):
            return False
        return any(item.get("account") == account_id and item.get("date") == day for item in history if isinstance(item, dict))

    def _has_auto_reply_success_for_day(self, account_id: str, day: str) -> bool:
        success_map = self.get_data(self._auto_reply_success_key) or {}
        if isinstance(success_map, dict):
            day_records = success_map.get(day) or {}
            if isinstance(day_records, dict) and day_records.get(account_id):
                return True

        plan = self.get_data(self._auto_reply_plan_key) or {}
        if isinstance(plan, dict) and plan.get("date") == day:
            for job in plan.get("jobs") or []:
                if isinstance(job, dict) and job.get("account") == account_id and job.get("status") == "done":
                    return True

        history = self.get_data(self._auto_reply_history_key) or []
        if not isinstance(history, list):
            return False
        return any(
            item.get("account") == account_id and item.get("date") == day and item.get("success")
            for item in history
            if isinstance(item, dict)
        )

    def _is_auto_thread_replied(self, account_id: str, tid: str) -> bool:
        tid = str(tid or "")
        if not tid:
            return False
        data = self.get_data(self._auto_replied_threads_key) or {}
        if isinstance(data, dict):
            for owner, records in data.items():
                if isinstance(records, dict):
                    records = records.values()
                if any(str(item.get("tid") or "") == tid for item in records if isinstance(item, dict)):
                    return True
        history = self.get_data(self._auto_reply_history_key) or []
        if isinstance(history, list):
            for item in history:
                if isinstance(item, dict) and item.get("success") and str(item.get("tid") or "") == tid:
                    return True
        return False

    def _auto_reply_account_aliases(self, account_id: str) -> set:
        aliases = {str(account_id or "").strip().lower()}
        user_info_map = self.get_data(self._user_info_key) or {}
        info = user_info_map.get(account_id) if isinstance(user_info_map, dict) else None
        if isinstance(info, dict):
            for key in ("username", "account"):
                value = str(info.get(key) or "").strip().lower()
                if value:
                    aliases.add(value)
        return {item for item in aliases if item}

    def _auto_reply_current_account_has_replied_in_detail(self, item: Dict[str, Any], account_id: str) -> bool:
        """Detect whether the current logged-in account has already replied in this thread.

        This is intentionally based on the live detail page, not only on this
        plugin's local reply history, so it also skips threads Vito replied to
        manually outside the plugin.
        """
        account_uids = set()
        account_names = self._auto_reply_account_aliases(account_id)

        identity = item.get("account_identity") or {}
        if isinstance(identity, dict):
            uid = str(identity.get("uid") or "").strip()
            username = str(identity.get("username") or "").strip().lower()
            if uid and uid != "0":
                account_uids.add(uid)
            if username:
                account_names.add(username)

        user_info_map = self.get_data(self._user_info_key) or {}
        info = user_info_map.get(account_id) if isinstance(user_info_map, dict) else None
        if isinstance(info, dict):
            uid = str(info.get("user_id") or "").strip()
            if uid and uid != "0":
                account_uids.add(uid)

        for ref in (item.get("post_author_refs") or []):
            if not isinstance(ref, dict):
                continue
            uid = str(ref.get("uid") or "").strip()
            username = str(ref.get("username") or "").strip().lower()
            if uid and uid in account_uids:
                return True
            if username and username in account_names:
                return True

        reply_authors = [str(author or "").strip().lower() for author in (item.get("post_authors") or [])]
        return bool(account_names and any(author in account_names for author in reply_authors))

    def _mark_auto_replied_thread(self, account_id: str, detail: Dict[str, Any]):
        data = self.get_data(self._auto_replied_threads_key) or {}
        if not isinstance(data, dict):
            data = {}
        records = data.get(account_id) or []
        if not isinstance(records, list):
            records = []
        tid = str(detail.get("tid") or "")
        records = [item for item in records if str(item.get("tid") or "") != tid]
        records.insert(0, {
            "time": self._auto_reply_now().strftime("%Y-%m-%d %H:%M:%S"),
            "fid": str(detail.get("fid") or ""),
            "tid": tid,
            "title": str(detail.get("title") or "")[:160],
        })
        data[account_id] = records[:500]
        self.save_data(self._auto_replied_threads_key, data)

    def _mark_auto_reply_success_for_day(self, account_id: str, result: Dict[str, Any]):
        success_map = self.get_data(self._auto_reply_success_key) or {}
        if not isinstance(success_map, dict):
            success_map = {}
        now = self._auto_reply_now()
        day = now.strftime("%Y-%m-%d")
        day_records = success_map.get(day) or {}
        if not isinstance(day_records, dict):
            day_records = {}
        day_records[account_id] = {
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "fid": str(result.get("fid") or ""),
            "tid": str(result.get("tid") or ""),
            "title": str(result.get("title") or "")[:160],
        }
        success_map[day] = day_records
        keep_days = sorted(str(key) for key in success_map.keys())[-30:]
        self.save_data(self._auto_reply_success_key, {day_key: success_map[day_key] for day_key in keep_days})

    def _record_auto_reply_result(self, account_id: str, result: Dict[str, Any]):
        history = self.get_data(self._auto_reply_history_key) or []
        if not isinstance(history, list):
            history = []
        now = self._auto_reply_now()
        result = self._normalize_auto_reply_result(result)
        status = self._auto_reply_result_status(result)
        reason = str(result.get("reason") or result.get("message") or "")[:160]
        history.insert(0, {
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "date": now.strftime("%Y-%m-%d"),
            "account": account_id,
            "attempt_index": int(result.get("attempt_index") or 1),
            "success": status == "success",
            "skipped": status == "skipped",
            "failed": status == "failed",
            "status": status,
            "result_status": status,
            "result_category": status,
            "result": self._auto_reply_status_label(status),
            "fid": str(result.get("fid") or ""),
            "tid": str(result.get("tid") or ""),
            "title": str(result.get("title") or "")[:160],
            "message": reason,
            "reason": reason,
            "reply_summary": str(result.get("reply_summary") or "")[:60],
            "risk_reasons": result.get("risk_reasons") or [],
        })
        self.save_data(self._auto_reply_history_key, history[:100])
        if status == "success":
            self._mark_auto_reply_success_for_day(account_id, result)

    def _update_auto_reply_plan_status(self, account_id: str, plan_date: str, status: str, message: str = "",
                                      attempt_index: Optional[int] = None):
        plan = self.get_data(self._auto_reply_plan_key) or {}
        if not isinstance(plan, dict) or plan.get("date") != plan_date:
            return
        changed = False
        for job in plan.get("jobs") or []:
            if not isinstance(job, dict) or job.get("account") != account_id:
                continue
            if attempt_index is not None and int(job.get("attempt_index") or 1) != int(attempt_index):
                continue
            job["status"] = status
            job["message"] = str(message or "")[:160]
            job["executed_at"] = self._auto_reply_now().strftime("%Y-%m-%d %H:%M:%S")
            changed = True
        if changed:
            self.save_data(self._auto_reply_plan_key, plan)

    def _skip_remaining_auto_reply_plan_jobs(self, account_id: str, plan_date: str, completed_attempt_index: int):
        plan = self.get_data(self._auto_reply_plan_key) or {}
        if not isinstance(plan, dict) or plan.get("date") != plan_date:
            return
        changed = False
        for job in plan.get("jobs") or []:
            if not isinstance(job, dict) or job.get("account") != account_id:
                continue
            attempt_index = int(job.get("attempt_index") or 1)
            if attempt_index == int(completed_attempt_index):
                continue
            if job.get("status") in ("scheduled", "pending", ""):
                job["status"] = "skipped"
                job["message"] = "今日已成功回帖，后续尝试跳过"
                if self._scheduler:
                    job_id = self._auto_reply_job_id(plan_date, account_id, attempt_index)
                    try:
                        self._scheduler.remove_job(job_id)
                    except Exception:
                        pass
                changed = True
        if changed:
            self.save_data(self._auto_reply_plan_key, plan)

    def _notify_auto_reply_result(self, account_id: str, result: Dict[str, Any]):
        if not self._notify:
            return
        result = self._normalize_auto_reply_result(result)
        status = self._auto_reply_result_status(result)
        label = self._auto_reply_status_label(status)
        lines = [
            f"账号：{account_id}",
            f"结果：{label}",
            f"原因：{result.get('reason') or result.get('message') or '-'}",
        ]
        if result.get("attempt_index") is not None:
            lines.append(f"attempt_index：{result.get('attempt_index')}")
        if result.get("fid"):
            lines.append(f"fid：{result.get('fid')}")
        if result.get("tid"):
            lines.append(f"tid：{result.get('tid')}")
        if result.get("title"):
            lines.append(f"标题：{result.get('title')}")
        if result.get("reply_summary"):
            lines.append(f"回复摘要：{result.get('reply_summary')}")
        self.post_message(
            mtype=NotificationType.Plugin,
            title=f"98自动回帖{label}",
            text="\n".join(lines),
        )

    def _auto_reply_summary_card(self, plan: Dict[str, Any], history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not self._auto_reply_enabled and not plan and not history:
            return None
        if not isinstance(plan, dict):
            plan = {}
        jobs = plan.get("jobs") if isinstance(plan, dict) else []
        jobs = jobs if isinstance(jobs, list) else []
        recent_history = history[:5] if isinstance(history, list) else []
        rows = []
        for job in jobs[:10]:
            if not isinstance(job, dict):
                continue
            rows.append([
                {'component': 'td', 'text': job.get('account', '-')},
                {'component': 'td', 'props': {'style': 'white-space:nowrap;'}, 'text': job.get('run_at', '-')},
                {'component': 'td', 'text': job.get('status', '-')},
                {'component': 'td', 'text': job.get('message', '') or '-'},
            ])
        history_lines = []
        for item in recent_history:
            if not isinstance(item, dict):
                continue
            icon = item.get("result") or self._auto_reply_status_label(item)
            history_lines.append({
                'component': 'div',
                'props': {'class': 'text-caption text-medium-emphasis text-truncate'},
                'text': f"{item.get('time', '-')} {item.get('account', '-')} {icon}：{item.get('reason') or item.get('message', '-')}",
            })
        content = [
            {'component': 'VCardTitle', 'props': {'class': 'text-subtitle-1 py-2'}, 'text': '自动回帖'},
            {'component': 'VCardText', 'props': {'class': 'pt-0'}, 'content': [
                {'component': 'div', 'props': {'class': 'text-caption text-medium-emphasis mb-2'}, 'text': f"版块：{','.join(plan.get('forum_ids') or []) or '-'}｜窗口：{plan.get('window_start') or '-'}-{plan.get('window_end') or '-'}｜{plan.get('message') or '今日计划'}"},
            ]},
        ]
        if rows:
            content.append({
                'component': 'VTable',
                'props': {'density': 'compact', 'hover': True},
                'content': [
                    {'component': 'thead', 'content': [{'component': 'tr', 'content': [
                        {'component': 'th', 'text': '账号'}, {'component': 'th', 'text': '时间'},
                        {'component': 'th', 'text': '状态'}, {'component': 'th', 'text': '说明'},
                    ]}]},
                    {'component': 'tbody', 'content': [{'component': 'tr', 'content': row} for row in rows]},
                ]
            })
        if history_lines:
            content.append({'component': 'VCardText', 'props': {'class': 'pt-2'}, 'content': history_lines})
        return {
            'component': 'VCard',
            'props': {'variant': 'flat', 'class': 'mb-3'},
            'content': content,
        }

    # ── Core sign-in logic (multi-account loop) ───────────
    def _do_signin(self):
        acquired = self._signin_lock.acquire(blocking=False)
        if not acquired:
            logger.warning("[SehuatangSignin] 签到流程正在执行，跳过重复触发")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="98签到执行中",
                    text="已有签到流程正在执行，本次触发已跳过。"
                )
            return
        try:
            self._signin_active = True
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

            if self._random_account_order and len(indexed_accounts) > 1:
                random.shuffle(indexed_accounts)
                logger.info(
                    "[SehuatangSignin] 串行随机账号顺序: "
                    + ", ".join(account_id for _, _, account_id in indexed_accounts)
                )
            all_results = self._do_signin_serial(indexed_accounts)

            self._notify_summary(all_results)
            self._save_results(all_results)
        finally:
            self._signin_active = False
            self._signin_lock.release()

    def _do_signin_serial(self, indexed_accounts: list) -> list:
        all_results = []
        for pos, (idx, account, account_id) in enumerate(indexed_accounts):
            logger.info(f"[SehuatangSignin] [{pos+1}/{len(indexed_accounts)}] 串行处理账号: {account_id}")
            result = self._signin_single(account, account_id)
            all_results.append({"account": account_id, **result})
        return all_results

    def _signin_single(self, account: dict, account_id: str) -> dict:
        steps = []
        result = {"success": False, "message": "", "steps": steps}
        fs_sid = ""
        captcha_session_id = ""
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
                    f"[SehuatangSignin] [{account_id}] 发验证码链接前等待全局验证码获取锁，"
                    f"最长等待 {self._captcha_fetch_timeout} 秒"
                )
                with self._captcha_fetch_lock, site_captcha_lock():
                    pass

                captcha_session_id = f"{account_id}-{uuid.uuid4().hex[:8]}"
                init_session(captcha_session_id, account_id)
                captcha_session_active = True
                account_path = quote(captcha_session_id, safe="")
                captcha_url = f"{self._public_base_url}/{account_path}" if self._public_base_url else f"http://localhost:{self._captcha_port}/{account_path}"
                self._send_captcha_notification("打开后获取", captcha_url, account_id)
                logger.info(f"[SehuatangSignin] [{account_id}] 已发送验证码准备通知，等待用户打开页面: {captcha_session_id}")

                open_deadline = time.time() + self._captcha_timeout
                while time.time() < open_deadline:
                    if is_requested(captcha_session_id):
                        logger.info(f"[SehuatangSignin] [{account_id}] 用户已打开验证码页面，开始现场获取验证码")
                        break
                    if is_expired(captcha_session_id, self._captcha_timeout):
                        logger.warning(f"[SehuatangSignin] [{account_id}] 验证码准备会话已过期")
                        break
                    time.sleep(2)

                if not is_requested(captcha_session_id):
                    destroy_session(captcha_session_id, destroy_fs=False)
                    captcha_session_active = False
                    result["message"] = f"验证码页面未在 {self._captcha_timeout} 秒内打开"
                    return result

                logger.info(
                    f"[SehuatangSignin] [{account_id}] 开始现场获取验证码，"
                    f"最长获取 {self._captcha_fetch_timeout} 秒"
                )
                with self._captcha_fetch_lock, site_captcha_lock():
                    captcha_data = fetch_captcha_for_account(
                        fs_sid,
                        cookies,
                        max_wait_seconds=self._captcha_fetch_timeout,
                        browser_session_key=captcha_session_id,
                    )
                if not captcha_data:
                    result["message"] = "无法获取支持的验证码（slide/rotate/click），或接口限流/超时"
                    logger.warning(f"[SehuatangSignin] [{account_id}] 获取验证码失败")
                    return result
                if captcha_data.get("error"):
                    result["message"] = captcha_data.get("message") or "验证码获取失败"
                    logger.warning(f"[SehuatangSignin] [{account_id}] {result['message']}")
                    return result

                cap_type = captcha_data["type"]
                captcha_data["site_ttl_seconds"] = self._captcha_site_ttl
                steps.append(f"验证码类型：{cap_type}")
                logger.info(
                    f"[SehuatangSignin] [{account_id}] 验证码: {cap_type} "
                    f"display=({captcha_data.get('display_x')},{captcha_data.get('display_y')}) "
                    f"master={captcha_data.get('master_width')}x{captcha_data.get('master_height')} "
                    f"thumb={captcha_data.get('thumb_width')}x{captcha_data.get('thumb_height')}"
                )

                set_captcha_data(captcha_session_id, captcha_data, fs_sid)
                captcha_started_at = time.time()

                # 站点验证码实际有效期很短（日志显示约 30 秒），本地 5 分钟只会让过期答案被提交。
                # 因此每张验证码从用户打开页面后现场获取，并按站点 TTL 等待。
                captcha_safe_window = max(10, self._captcha_site_ttl - self._captcha_site_ttl_buffer)
                answer_deadline = min(
                    time.time() + self._captcha_timeout,
                    captcha_started_at + captcha_safe_window,
                )
                while time.time() < answer_deadline:
                    if is_solved(captcha_session_id):
                        logger.info(f"[SehuatangSignin] [{account_id}] 用户已完成验证码")
                        break
                    if is_expired(captcha_session_id, self._captcha_timeout):
                        logger.warning(f"[SehuatangSignin] [{account_id}] 验证码会话已过期")
                        break
                    time.sleep(2)

                if not is_solved(captcha_session_id):
                    destroy_session(captcha_session_id, destroy_fs=False)
                    captcha_session_active = False
                    msg = f"验证码超时（站点有效期约 {self._captcha_site_ttl} 秒）"
                    steps.append(msg)
                    logger.warning(f"[SehuatangSignin] [{account_id}] {msg}")
                    if round_no < max_rounds:
                        continue
                    result["message"] = msg
                    return result

                solved_at = get_solved_at(captcha_session_id) or time.time()
                answer_age = solved_at - captcha_started_at
                if answer_age > captcha_safe_window:
                    destroy_session(captcha_session_id, destroy_fs=False)
                    captcha_session_active = False
                    msg = f"验证码提交过慢（{answer_age:.1f}s），已超过站点有效期，刷新下一轮"
                    steps.append(msg)
                    logger.warning(f"[SehuatangSignin] [{account_id}] {msg}")
                    if round_no < max_rounds:
                        continue
                    result["message"] = f"验证码失败：提交超过站点有效期约 {self._captcha_site_ttl} 秒"
                    return result

                answer = get_answer(captcha_session_id)
                steps.append(f"用户提交：{answer}")
                logger.info(f"[SehuatangSignin] [{account_id}] 等待全局验证码接口锁，提交 check: {answer}")
                with self._captcha_fetch_lock, site_captcha_lock():
                    ok, check_result = submit_check(
                        fs_sid,
                        answer,
                        cap_type,
                        cookies,
                        browser_session_key=captcha_session_id,
                    )

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
                    logger.info(f"[SehuatangSignin] [{account_id}] 验证码通过，提交签到...")
                    sign_result = complete_signin(fs_sid, cookies, browser_session_key=captcha_session_id)
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
                        elif "验证超时" in str(msg) and final_btn == "N/A":
                            result["success"] = True
                            result["message"] = "签到成功：验证码已通过，sign_v2 返回验证超时（最终状态未取到）"
                            logger.info(
                                f"[SehuatangSignin] [{account_id}] check 已 OK，sign_v2 返回验证超时且最终状态 N/A，按成功兜底"
                            )
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
            if self._refresh_profile and fs_sid and 'cookies' in locals():
                profile = self._refresh_account_profile(fs_sid, cookies, account_id)
                if profile:
                    result["user_info"] = profile
                    steps.append(
                        f"资料刷新：等级={profile.get('user_group') or '-'} "
                        f"积分={profile.get('credits') or '-'} 金钱={profile.get('money') or '-'}"
                    )
            if captcha_session_active:
                destroy_session(captcha_session_id or account_id, destroy_fs=False)
            if fs_sid:
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

    @staticmethod
    def _strip_html_text(html_text: str) -> str:
        """Convert a small HTML document to line-oriented text for profile parsing."""
        if not html_text:
            return ""
        text = re.sub(r"(?is)<script[^>]*>.*?</script>", "\n", html_text)
        text = re.sub(r"(?is)<style[^>]*>.*?</style>", "\n", text)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</(?:p|div|li|tr|td|th|dd|dt|em|span|strong|a|h\d)>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html_lib.unescape(text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text)
        return text.strip()

    @staticmethod
    def _first_match(text: str, patterns: List[str]) -> str:
        for pattern in patterns:
            m = re.search(pattern, text, re.I | re.S)
            if m:
                value = html_lib.unescape(m.group(1)).strip(" ：:\n\t")
                value = re.sub(r"\s+", " ", value)
                if value:
                    return value[:80]
        return ""

    @staticmethod
    def _to_number(value: Any) -> Optional[float]:
        if value is None:
            return None
        m = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
        return float(m.group(0)) if m else None

    @classmethod
    def _parse_profile_info(cls, html_text: str) -> Dict[str, Any]:
        """Parse sehuatang profile/credits HTML into normalized fields."""
        text = cls._strip_html_text(html_text)
        compact = re.sub(r"\s+", " ", text)
        info = {
            "user_group": cls._first_match(compact, [
                r"用户组\s*[:：]?\s*(Lv\.?\d+\s+[^\s|<]{1,24})",
                r"(Lv\.?\d+\s+[^\s|<]{1,24})",
            ]),
            "credits": cls._first_match(compact, [
                r"积分\s*[:：]\s*([\-\d,\.]+)",
                r"总积分\s*[:：]?\s*([\-\d,\.]+)",
            ]),
            "money": cls._first_match(compact, [
                r"金钱\s*[:：]\s*([\-\d,\.]+)",
                r"金钱\s+([\-\d,\.]+)",
            ]),
            "register_time": cls._first_match(compact, [
                r"注册时间\s*[:：]?\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2})?)",
            ]),
        }
        uid = cls._first_match(html_text, [r"home\.php\?mod=space&uid=(\d+)", r"uid['\"]?\s*[:=]\s*['\"]?(\d+)"])
        if uid:
            info["user_id"] = uid
        username = cls._first_match(html_text, [r"<title>\s*([^<\-]+?)\s*(?:-|的个人资料)", r"用户名\s*[:：]?\s*([^\n<]{1,40})"])
        if username:
            info["username"] = username
        return {k: v for k, v in info.items() if v not in (None, "")}

    def _refresh_account_profile(self, fs_sid: str, cookies: list, account_id: str) -> Dict[str, Any]:
        """Refresh level/credits/money for one account. Never raises to caller."""
        try:
            profile_html = fs_get(fs_sid, f"{self._base_url}/home.php?mod=space", cookies)
            credit_html = fs_get(fs_sid, f"{self._base_url}/home.php?mod=spacecp&ac=credit&showcredit=1", cookies)
            combined = f"{profile_html}\n{credit_html}"
            if "static/safe/js/web.js" in combined or "safeid=" in combined or "enter-btn" in combined:
                return {"error": "safe_gate", "last_refresh": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            info = self._parse_profile_info(combined)
            info["account"] = account_id
            info["last_refresh"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if not any(info.get(k) for k in ("user_group", "credits", "money", "register_time")):
                info["error"] = "页面未匹配"
            return info
        except Exception as e:
            logger.warning(f"[SehuatangSignin] [{account_id}] 用户资料刷新失败：{e}")
            return {"account": account_id, "error": str(e), "last_refresh": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    def _money_chart_card(self, money_history: List[Dict[str, Any]], account_ids: List[str]) -> Optional[Dict[str, Any]]:
        if not money_history:
            return None
        recent = money_history[-30:]
        ids = account_ids or sorted({aid for item in recent for aid in (item.get("values") or {}).keys()})
        series = []
        for aid in ids:
            data = []
            has_value = False
            for item in recent:
                value = (item.get("values") or {}).get(aid)
                if value is not None:
                    has_value = True
                data.append(value)
            if has_value:
                series.append({"name": aid, "data": data})
        if not series:
            return None
        return {
            'component': 'VCard',
            'props': {'variant': 'flat', 'class': 'mb-3'},
            'content': [
                {'component': 'VCardTitle', 'props': {'class': 'text-subtitle-1 py-2'}, 'text': '📈 金钱趋势（近30天）'},
                {'component': 'VApexChart', 'props': {
                    'height': 280,
                    'options': {
                        'chart': {'type': 'line', 'toolbar': {'show': True}},
                        'stroke': {'curve': 'smooth', 'width': 3},
                        'xaxis': {'categories': [item.get('day', '') for item in recent]},
                        'legend': {'show': True},
                        'markers': {'size': 3},
                        'noData': {'text': '暂无金钱趋势数据'},
                    },
                    'series': series,
                }}
            ]
        }

    def _all_accounts_signed_today(self) -> bool:
        """Return True if all currently configured accounts have a successful local record today."""
        if not self._accounts:
            return False
        indexed_accounts = []
        seen_ids = {}
        for idx, account in enumerate(self._accounts):
            raw_account_id = self._get_account_id(account, idx)
            seen_ids[raw_account_id] = seen_ids.get(raw_account_id, 0) + 1
            account_id = raw_account_id if seen_ids[raw_account_id] == 1 else f"{raw_account_id}_{seen_ids[raw_account_id]}"
            indexed_accounts.append(account_id)
        today = datetime.now().strftime("%Y-%m-%d")
        history = self.get_data(self._history_key) or []
        signed_accounts = {
            item.get("account") for item in history
            if item.get("success") and str(item.get("time", "")).startswith(today)
        }
        return all(account_id in signed_accounts for account_id in indexed_accounts)

    def _send_captcha_notification(self, cap_type: str, url: str, account_id: str):
        """Send WeChat notification with captcha relay URL."""
        if not self._notify:
            return
        title = f"🔐 98验证码 - {account_id}"
        if cap_type == "打开后获取":
            captcha_line = "验证码：打开页面后现场获取"
        else:
            captcha_line = f"验证码类型：{cap_type}"
        text = (
            f"账号：{account_id}\n"
            f"{captcha_line}\n\n"
            f"人工操作地址：\n{url}\n\n"
            f"请先打开页面；后台会在页面打开后现场获取验证码。\n"
            f"验证码显示后约 {self._captcha_site_ttl} 秒内完成，过期会自动刷新下一轮。"
        )
        logger.info(f"[SehuatangSignin] 验证码通知内容:\n{title}\n{text}")
        self.post_message(mtype=NotificationType.Plugin, title=title, text=text)

    def _notify_summary(self, results: list):
        """Send summary notification for all accounts."""
        if not self._notify:
            return
        success_count = sum(1 for r in results if r.get("success"))
        total = len(results)
        title = f"98签到完成：{success_count}/{total} 成功"
        lines = []
        for r in results:
            icon = "✅" if r.get("success") else "❌"
            lines.append(f"{icon} {r['account']}：{r['message']}")
            info = r.get("user_info") or {}
            if info.get("error"):
                lines.append(f"资料：{info.get('error')}")
            elif any(info.get(k) for k in ("user_group", "credits", "money")):
                lines.append(
                    f"{info.get('user_group') or '-'}｜"
                    f"积分 {info.get('credits') or '-'}｜金钱 {info.get('money') or '-'}"
                )
        text = "\n".join(lines)
        logger.info(f"[SehuatangSignin] 汇总通知内容:\n{title}\n{text}")
        self.post_message(mtype=NotificationType.Plugin, title=title, text=text)

    def _merge_user_info(self, results: list) -> Dict[str, Any]:
        user_info_map = self.get_data(self._user_info_key) or {}
        if not isinstance(user_info_map, dict):
            user_info_map = {}
        for r in results:
            account = r.get("account")
            info = r.get("user_info") or {}
            if account and isinstance(info, dict) and info:
                merged = dict(user_info_map.get(account) or {})
                merged.update(info)
                if not info.get("error"):
                    merged.pop("error", None)
                user_info_map[account] = merged
        self.save_data(self._user_info_key, user_info_map)
        return user_info_map

    def _save_money_history(self, results: list):
        history = self.get_data(self._money_history_key) or []
        if not isinstance(history, list):
            history = []
        day = datetime.now().strftime("%Y-%m-%d")
        today = next((item for item in history if item.get("day") == day), None)
        if not today:
            today = {"day": day, "values": {}}
            history.append(today)
        values = today.setdefault("values", {})
        for r in results:
            account = r.get("account")
            money = (r.get("user_info") or {}).get("money")
            numeric = self._to_number(money)
            if account and numeric is not None:
                values[account] = numeric
        history = sorted(history, key=lambda x: x.get("day", ""))[-90:]
        self.save_data(self._money_history_key, history)

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
        self._merge_user_info(results)
        self._save_money_history(results)

    def stop_service(self):
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                self._scheduler.shutdown(wait=False)
            self._scheduler = None
        stop_server()
