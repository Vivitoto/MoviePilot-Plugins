import html as html_lib
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
    plugin_version = "1.0.18"
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

    # Global lock for site captcha endpoint operations across all accounts.
    # It serializes both fetch and check calls to reduce site-wide 429 risk.
    _captcha_fetch_lock = threading.Lock()
    _signin_lock = threading.Lock()
    _signin_active = False
    _captcha_site_ttl_buffer = 2

    _scheduler: Optional[BackgroundScheduler] = None
    _history_key = "history"
    _last_result_key = "last_result"
    _user_info_key = "user_info_by_account"
    _money_history_key = "money_history"

    def init_plugin(self, config: dict = None):
        self.stop_service()
        try:
            if config:
                self._enabled = config.get("enabled", False)
                self._notify = config.get("notify", True)
                self._refresh_profile = bool(config.get("refresh_profile", True))
                self._onlyonce = config.get("onlyonce", False)
                # Main scheduled sign-in is intentionally disabled: this plugin
                # now runs by manual command / one-shot save action only.
                self._cron = ""
                self._timeout = max(1, int(config.get("timeout") or 30))
                self._random_account_order = bool(config.get("random_account_order", True))
                self._reminder_enabled = bool(config.get("reminder_enabled", False))
                self._reminder_cron = str(config.get("reminder_cron") or "0 21 * * *").strip()
                self._reminder_text = str(config.get("reminder_text") or self._reminder_text).strip()
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
                self._use_flaresolverr = config.get("use_flaresolverr", True)
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
            "enabled": self._enabled, "notify": self._notify, "refresh_profile": self._refresh_profile, "onlyonce": self._onlyonce,
            "cron": "", "timeout": self._timeout,
            "account_count": self._account_count,
            "random_account_order": self._random_account_order,
            "reminder_enabled": self._reminder_enabled,
            "reminder_cron": self._reminder_cron,
            "reminder_text": self._reminder_text,
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
                                        {'component': 'VCol', 'props': {'cols': 12, 'sm': 6, 'md': 4, 'class': 'py-3'}, 'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '保存后执行一次', 'hide-details': True}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'sm': 6, 'md': 4, 'class': 'py-3'}, 'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知', 'hide-details': True}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'sm': 6, 'md': 4, 'class': 'py-3'}, 'content': [{'component': 'VSwitch', 'props': {'model': 'use_flaresolverr', 'label': '使用 FlareSolverr', 'hide-details': True}}]},
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
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6, 'class': 'py-3'}, 'content': [{'component': 'VTextField', 'props': {'model': 'flaresolverr_url', 'label': 'FlareSolverr API 地址', 'placeholder': 'http://127.0.0.1:8191/v1', 'hint': '必须填写完整 /v1 路径', 'persistent-hint': True}}]},
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
