import hashlib
import html
import json
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

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


class SijisheSignIn(_PluginBase):
    plugin_name = "司机签到自用"
    plugin_desc = "自动登录并完成论坛签到。"
    plugin_icon = "https://raw.githubusercontent.com/Vivitoto/MoviePilot-Plugins/main/icons/sijishe-v2.png"
    plugin_version = "1.0.7"
    plugin_author = "Vivitoto"
    author_url = "https://github.com/Vivitoto"
    plugin_config_prefix = "sijishe_"
    plugin_order = 21
    auth_level = 1

    _enabled = False
    _notify = True
    _onlyonce = False
    _cron = "10 9 * * *"
    _username = ""
    _password = ""
    _proxy_url = ""
    _base_url = "https://xsijishe.net"
    _timeout = 20
    _timezone = "Asia/Shanghai"
    _uid = ""
    _use_flaresolverr = False
    _flaresolverr_url = "http://127.0.0.1:8191/v1"
    _retry_count = 3
    _retry_interval_minutes = 5

    _scheduler: Optional[BackgroundScheduler] = None
    _history_key = "history"
    _last_result_key = "last_result"
    _user_info_key = "user_info"
    _asset_history_key = "asset_history"
    _retry_state_key = "retry_state"

    def init_plugin(self, config: dict = None):
        self.stop_service()
        try:
            if config:
                self._enabled = config.get("enabled", False)
                self._notify = config.get("notify", True)
                self._onlyonce = config.get("onlyonce", False)
                self._cron = config.get("cron") or "10 9 * * *"
                self._username = str(config.get("username") or "").strip()
                self._password = str(config.get("password") or "")
                self._proxy_url = str(config.get("proxy_url") or "").strip()
                self._base_url = str(config.get("base_url") or "https://xsijishe.net").strip().rstrip("/")
                self._timeout = max(1, int(config.get("timeout") or 20))
                self._uid = str(config.get("uid") or "").strip()
                self._use_flaresolverr = config.get("use_flaresolverr", False)
                self._flaresolverr_url = str(config.get("flaresolverr_url") or "http://127.0.0.1:8191/v1").strip()
                retry_count = config.get("retry_count", 3)
                retry_interval = config.get("retry_interval_minutes", 5)
                self._retry_count = max(0, int(3 if retry_count in (None, "") else retry_count))
                self._retry_interval_minutes = max(1, int(5 if retry_interval in (None, "") else retry_interval))

            if self._onlyonce:
                logger.info("司机社签到自用：保存配置后执行一次")
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    func=self.run_by_onlyonce,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="司机社签到自用",
                    kwargs={}
                )
                self._onlyonce = False
                self.__update_config()
                if self._scheduler.get_jobs():
                    self._scheduler.start()
        except Exception as e:
            logger.error(f"司机社签到自用初始化错误：{str(e)}", exc_info=True)

    def get_state(self) -> bool:
        return self._enabled and bool(self._username and self._password)

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "username": self._username,
            "password": self._password,
            "proxy_url": self._proxy_url,
            "base_url": self._base_url,
            "timeout": self._timeout,
            "timezone": self._timezone,
            "uid": self._uid,
            "use_flaresolverr": self._use_flaresolverr,
            "flaresolverr_url": self._flaresolverr_url,
            "retry_count": self._retry_count,
            "retry_interval_minutes": self._retry_interval_minutes,
        })

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/sijishe_signin",
            "event": EventType.PluginAction,
            "desc": "执行司机社签到",
            "data": {"action": "sijishe_signin"}
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/run",
            "endpoint": self.api_run,
            "methods": ["GET"],
            "summary": "执行签到"
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._cron:
            return []
        return [{
            "id": "SijisheSignIn",
            "name": "司机社签到自用",
            "trigger": CronTrigger.from_crontab(self._cron),
            "func": self.run_by_cron,
            "kwargs": {}
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        version = getattr(settings, "VERSION_FLAG", "v1")
        cron_field_component = "VCronField" if version == "v2" else "VTextField"
        return [
            {
                'component': 'VForm',
                'content': [
                    # ── Card 1：基本配置 ──────────────────────────────────
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
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '保存后执行一次'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'use_flaresolverr', 'label': '使用 FlareSolverr'}}]},
                                    ]
                                }
                            ]
                        }]
                    },
                    # ── Card 2：账号与站点 ──────────────────────────────
                    {
                        'component': 'VCard',
                        'props': {'variant': 'flat', 'class': 'mb-4'},
                        'content': [{
                            'component': 'VCardItem',
                            'content': [
                                {'component': 'div', 'props': {'class': 'text-subtitle-2 font-weight-bold mb-3'}, 'text': '👤 账号与站点'},
                                {
                                    'component': 'VRow',
                                    'content': [
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'username', 'label': '用户名'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'password', 'label': '密码', 'type': 'password'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'uid', 'label': 'UID（可选）', 'placeholder': '如 747026'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'base_url', 'label': '站点地址', 'placeholder': 'https://xsijishe.net'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'proxy_url', 'label': '代理地址', 'placeholder': 'http://127.0.0.1:7890'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'timeout', 'label': '请求超时（秒）', 'type': 'number', 'placeholder': '20'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VTextField', 'props': {'model': 'flaresolverr_url', 'label': 'FlareSolverr 地址', 'placeholder': 'http://127.0.0.1:8191/v1'}}]},
                                    ]
                                }
                            ]
                        }]
                    },
                    # ── Card 3：定时与重试 ──────────────────────────────
                    {
                        'component': 'VCard',
                        'props': {'variant': 'tonal', 'class': 'mb-2'},
                        'content': [{
                            'component': 'VCardItem',
                            'content': [
                                {'component': 'div', 'props': {'class': 'text-subtitle-2 font-weight-bold mb-3'}, 'text': '⏰ 定时与重试'},
                                {
                                    'component': 'VRow',
                                    'content': [
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': cron_field_component, 'props': {'model': 'cron', 'label': '定时任务', 'placeholder': '10 9 * * *'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'retry_count', 'label': '失败重试次数', 'type': 'number', 'placeholder': '3'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'retry_interval_minutes', 'label': '重试间隔（分钟）', 'type': 'number', 'placeholder': '5'}}]},
                                        {'component': 'VCol', 'props': {'cols': 12}, 'content': [
                                            {'component': 'div', 'props': {'class': 'text-body-2 text-medium-emphasis mt-1'}, 'text': '💡 定时任务触发失败后自动重试，限当天内；自动定时首轮会随机延时 1-30 分钟，失败重试按配置间隔准时触发。'}
                                        ]},
                                    ]
                                }
                            ]
                        }]
                    },
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cron": "10 9 * * *",
            "username": "",
            "password": "",
            "proxy_url": "",
            "base_url": "https://xsijishe.net",
            "timeout": 20,
            "timezone": "Asia/Shanghai",
            "uid": "",
            "use_flaresolverr": False,
            "flaresolverr_url": "http://127.0.0.1:8191/v1",
            "retry_count": 3,
            "retry_interval_minutes": 5,
        }

    def get_page(self) -> List[dict]:
        history = self.get_data(self._history_key) or []
        user_info = self.get_data(self._user_info_key) or {}
        asset_history = self.get_data(self._asset_history_key) or []
        if not history and not user_info:
            return [{'component': 'div', 'text': '暂无数据', 'props': {'class': 'text-center'}}]

        history = sorted(history, key=lambda x: x.get('executed_at', ''), reverse=True) if history else []
        username = user_info.get('username', self._username or '未知用户')
        user_group = user_info.get('user_group', '-')
        credits = user_info.get('credits', '-')
        prestige = user_info.get('prestige', '-')
        tickets = user_info.get('tickets', '-')
        contribution = user_info.get('contribution', '-')
        reg_time = user_info.get('reg_time', '-')

        def _metric_card(label: str, value: str, color: str, cols: Dict[str, int] = None) -> Dict[str, Any]:
            props = {'cols': 6, 'sm': 3, 'md': 3}
            if cols:
                props.update(cols)
            color_map = {
                'primary': ('rgba(25,118,210,.08)', 'rgba(25,118,210,.22)', '#1565C0'),
                'secondary': ('rgba(123,31,162,.08)', 'rgba(123,31,162,.22)', '#6A1B9A'),
                'warning': ('rgba(245,124,0,.10)', 'rgba(245,124,0,.24)', '#E65100'),
                'success': ('rgba(46,125,50,.08)', 'rgba(46,125,50,.22)', '#2E7D32'),
            }
            bg, border, text_color = color_map.get(color, color_map['primary'])
            return {
                'component': 'VCol',
                'props': props,
                'content': [{
                    'component': 'div',
                    'props': {'style': f'background:{bg};border:1px solid {border};border-radius:10px;padding:7px 10px;min-height:54px;'},
                    'content': [
                        {'component': 'div', 'props': {'class': 'text-caption text-medium-emphasis'}, 'text': label},
                        {'component': 'div', 'props': {'class': 'text-subtitle-2 font-weight-bold text-truncate', 'style': f'color:{text_color};'}, 'text': value or '-'}
                    ]
                }]
            }

        def _chip(text: str, color: str) -> Dict[str, Any]:
            return {'component': 'VChip', 'props': {'size': 'x-small', 'variant': 'tonal', 'color': color}, 'text': text or '-'}

        def _status_chip(text: Any) -> Dict[str, Any]:
            value = str(text or '-')
            if '失败' in value:
                return _chip(value, 'error')
            if '未' in value:
                return _chip(value, 'warning')
            if '成功' in value:
                return _chip(value, 'success')
            return _chip(value, 'primary')

        def _source_chip(text: Any) -> Dict[str, Any]:
            value = str(text or '-')
            return _chip(value, 'info' if '自动' in value else 'warning')

        page = [{
            'component': 'VCard',
            'props': {'variant': 'flat', 'class': 'mb-3'},
            'content': [
                {'component': 'VCardText', 'props': {'class': 'py-3'}, 'content': [
                    {'component': 'div', 'props': {'class': 'd-flex align-center justify-space-between mb-2'}, 'content': [
                        {'component': 'div', 'content': [
                            {'component': 'div', 'props': {'class': 'text-subtitle-1 font-weight-bold'}, 'text': username},
                            {'component': 'div', 'props': {'class': 'text-caption text-medium-emphasis'}, 'text': f'注册：{reg_time}'}
                        ]},
                        {'component': 'VChip', 'props': {'size': 'small', 'variant': 'tonal', 'color': 'primary'}, 'text': user_group}
                    ]},
                    {'component': 'VRow', 'props': {'dense': True}, 'content': [
                        _metric_card('积分', str(credits), 'primary'),
                        _metric_card('威望', str(prestige), 'secondary'),
                        _metric_card('车票', str(tickets), 'success'),
                        _metric_card('贡献', str(contribution), 'warning'),
                    ]}
                ]}
            ]
        }]

        # 资产趋势图：按天聚合展示，兼容旧版 date(含时分秒) 记录
        def _asset_day(item: Dict[str, Any]) -> str:
            return str(item.get('day') or item.get('date') or '')[:10]

        if asset_history and len(asset_history) >= 2:
            asset_history = sorted(asset_history, key=_asset_day)[-30:]
            dates = [_asset_day(item) for item in asset_history]
            credits_data = [self._parse_num(item.get('credits')) or 0 for item in asset_history]
            tickets_data = [self._parse_num(item.get('tickets')) or 0 for item in asset_history]
            page.append({
                'component': 'VCard',
                'props': {'variant': 'flat', 'class': 'mb-3'},
                'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'text-subtitle-1 py-2'}, 'text': '📈 资产趋势图'},
                    {'component': 'VApexChart',
                     'props': {
                         'height': 220,
                         'options': {
                             'chart': {'type': 'line', 'toolbar': {'show': True}},
                             'stroke': {'curve': 'smooth', 'width': 3},
                             'xaxis': {'categories': dates},
                             'colors': ['#3B82F6', '#F59E0B'],
                             'legend': {'show': True},
                             'noData': {'text': '暂无资产趋势数据'},
                         },
                         'series': [
                             {'name': '积分', 'data': credits_data},
                             {'name': '车票', 'data': tickets_data},
                         ]
                     }}
                ]
            })

        # 执行记录表格
        if history:
            page.append({
                'component': 'VCard',
                'props': {'variant': 'flat', 'class': 'mb-3'},
                'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'text-subtitle-1 py-2'}, 'text': f'🗂️ 执行记录（共 {len(history)} 条）'},
                    {'component': 'VTable', 'props': {'density': 'compact', 'hover': True}, 'content': [
                        {'component': 'thead', 'content': [{
                            'component': 'tr', 'content': [
                                {'component': 'th', 'props': {'style': 'text-align:center; width: 26%;'}, 'text': '时间'},
                                {'component': 'th', 'props': {'style': 'text-align:center; width: 14%;'}, 'text': '触发方式'},
                                {'component': 'th', 'props': {'style': 'text-align:center; width: 12%;'}, 'text': '登录'},
                                {'component': 'th', 'props': {'style': 'text-align:center; width: 16%;'}, 'text': '签到'},
                                {'component': 'th', 'props': {'style': 'text-align:center; width: 20%;'}, 'text': '奖励'},
                                {'component': 'th', 'props': {'style': 'text-align:center; width: 12%;'}, 'text': '消息'},
                            ]
                        }]},
                        {'component': 'tbody', 'content': [{
                            'component': 'tr', 'content': [
                                {'component': 'td', 'props': {'style': 'text-align:center;'}, 'text': item.get('executed_at', '-')},
                                {'component': 'td', 'props': {'style': 'text-align:center;'}, 'content': [_source_chip(item.get('source_text') or self._source_text(item.get('source')))]},
                                {'component': 'td', 'props': {'style': 'text-align:center;'}, 'content': [_status_chip(item.get('login_status', '-'))]},
                                {'component': 'td', 'props': {'style': 'text-align:center;'}, 'content': [_status_chip(item.get('signin_status', '-'))]},
                                {'component': 'td', 'props': {'style': 'text-align:center;'}, 'text': item.get('reward', '-') or '-'},
                                {'component': 'td', 'props': {'style': 'text-align:center;'}, 'text': (item.get('message', '-') or '-')[:30]},
                            ]
                        } for item in history[:30]]}
                    ]}
                ]
            })

        return page

    def api_run(self):
        return self.run_once(source="api")

    def run_by_cron(self):
        self._log_step("【run_by_cron】被主调度器调用，source=cron")
        today = datetime.now().strftime("%Y-%m-%d")
        retry_state = self.get_data(self._retry_state_key)
        if isinstance(retry_state, dict):
            if retry_state.get("date") == today and retry_state.get("attempt", 0) >= self._retry_count:
                self._log_step(f"当日重试次数已耗尽（已尝试 {retry_state['attempt']} 次），跳过本次定时触发")
                return
            if retry_state.get("date") != today:
                self.save_data(self._retry_state_key, None)
        return self.run_once(source="cron")

    def run_by_onlyonce(self):
        self._log_step("【run_by_onlyonce】被初始化调度器调用，source=onlyonce")
        return self.run_once(source="onlyonce")

    @eventmanager.register(EventType.PluginAction)
    def remote_run(self, event: Event):
        if not event or not event.event_data:
            return
        if event.event_data.get("action") != "sijishe_signin":
            return
        self.run_once(source="command")

    def _session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        })
        if self._proxy_url:
            session.proxies.update({"http": self._proxy_url, "https": self._proxy_url})
        return session

    def _log_step(self, message: str):
        logger.info(f"{self.plugin_name}：{message}")

    def _source_text(self, source: Any) -> str:
        source_value = str(source or '').strip().lower()
        if source_value in {'cron', 'scheduler', 'schedule', 'service', 'auto', 'automatic', 'retry'}:
            return '自动触发'
        if source_value in {'command', 'api', 'manual', 'onlyonce'}:
            return '手动触发'
        return '自动触发' if 'cron' in source_value or 'sched' in source_value else '手动触发'

    def _now_text(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _save_result(self, result: Dict[str, Any]):
        history = self.get_data(self._history_key) or []
        if not isinstance(history, list):
            history = []
        result_day = str(result.get('executed_at') or '')[:10]
        result_source = str(result.get('source') or '').strip().lower()
        result_label = str(result.get('result_label') or '')
        auto_sources = {'cron', 'scheduler', 'schedule', 'service', 'auto', 'automatic', 'retry'}
        if result_day and result_source == 'retry' and result_label in {'成功', '已签到'}:
            history = [
                item for item in history
                if not (
                    str(item.get('executed_at') or '').startswith(result_day)
                    and str(item.get('source') or '').strip().lower() in auto_sources
                    and str(item.get('result_label') or '') == '失败'
                )
            ]
        history.append(result)
        history = sorted(history, key=lambda x: x.get('executed_at', ''), reverse=True)[:30]
        self.save_data(self._history_key, history)
        self.save_data(self._last_result_key, result)

    def _notify_text(self, result: Dict[str, Any]) -> str:
        trigger = self._source_text(result.get('source'))
        lines = [
            '✨ 司机社签到结果',
            '━━━━━━━━━━',
            f"🕒 执行时间：{result.get('executed_at', '-')}",
            f"🚦 触发方式：{trigger}",
            f"🔐 登录状态：{result.get('login_status', '-')}",
            f"✍️ 签到状态：{result.get('signin_status', '-')}",
        ]
        reward = result.get('reward')
        if reward:
            lines.append(f"🎁 签到奖励：{reward}")
        lines.extend([
            f"🧭 执行模式：{'FlareSolverr' if result.get('use_flaresolverr') else 'requests'}",
            f"📝 结果说明：{result.get('message', '-')}",
        ])
        return "\n".join(lines)

    def _md5(self, text: str) -> str:
        return hashlib.md5(text.encode('utf-8')).hexdigest()

    def _get_formhash(self, text: str) -> Optional[str]:
        """从HTML中提取formhash"""
        match = re.search(r'name=["\']formhash["\']\s*value=["\']([a-f0-9]{8})["\']', text)
        if match:
            return match.group(1)
        match = re.search(r'formhash[=\'"\s]*([a-f0-9]{8})', text)
        if match:
            return match.group(1)
        return None

    def _get_loginhash(self, text: str) -> Optional[str]:
        """从HTML中提取loginhash"""
        match = re.search(r'loginhash=([a-zA-Z0-9]+)', text)
        if match:
            return match.group(1)
        return None

    def _extract_uid(self, session: requests.Session) -> Optional[str]:
        """从 cookie 中提取 UID；失败时回落到手动配置的 UID"""
        import urllib.parse
        for cookie in session.cookies:
            if "creditnotice" in cookie.name:
                decoded = urllib.parse.unquote(cookie.value)
                match = re.search(r'(\d+)', decoded)
                if match:
                    return match.group(1)
        for cookie in session.cookies:
            if "lastcheckfeed" in cookie.name:
                parts = cookie.value.split("|")
                if parts and parts[0].isdigit():
                    return parts[0]
        return self._uid or None
    def _extract_uid_from_fs_cookies(self, cookies: List[dict]) -> Optional[str]:
        import urllib.parse
        for cookie in cookies or []:
            name = cookie.get("name", "")
            value = str(cookie.get("value", ""))
            if "creditnotice" in name:
                decoded = urllib.parse.unquote(value)
                match = re.search(r'(\d+)', decoded)
                if match:
                    return match.group(1)
            if "lastcheckfeed" in name:
                parts = urllib.parse.unquote(value).split("|")
                if parts and parts[0].isdigit():
                    return parts[0]
        return self._uid or None

    def _fs_call(self, payload: Dict[str, Any], timeout: Optional[int] = None) -> Dict[str, Any]:
        r = requests.post(self._flaresolverr_url, json=payload, timeout=timeout or max(self._timeout * 4, 90))
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            raise RuntimeError(data.get("message") or "FlareSolverr 调用失败")
        return data

    def _fs_proxy(self) -> Optional[Dict[str, Any]]:
        return {"url": self._proxy_url} if self._proxy_url else None

    def _fs_create_session(self) -> str:
        sid = f"sijishe-{int(time.time())}-{random.randint(1000,9999)}"
        self._fs_call({"cmd": "sessions.create", "session": sid}, timeout=30)
        return sid

    def _fs_destroy_session(self, sid: str):
        try:
            self._fs_call({"cmd": "sessions.destroy", "session": sid}, timeout=30)
        except Exception:
            pass

    def _fs_get(self, sid: str, url: str, max_timeout: int = 90000) -> Dict[str, Any]:
        payload = {"cmd": "request.get", "session": sid, "url": url, "maxTimeout": max_timeout}
        proxy = self._fs_proxy()
        if proxy:
            payload["proxy"] = proxy
        return self._fs_call(payload)

    def _fs_post(self, sid: str, url: str, post_data: str, headers: Optional[Dict[str, str]] = None, max_timeout: int = 90000) -> Dict[str, Any]:
        payload = {"cmd": "request.post", "session": sid, "url": url, "postData": post_data, "maxTimeout": max_timeout}
        if headers:
            payload["headers"] = headers
        proxy = self._fs_proxy()
        if proxy:
            payload["proxy"] = proxy
        return self._fs_call(payload)

    def _parse_user_info_html(self, html_text: str, uid: Optional[str] = None) -> Dict[str, Any]:
        raw_html = html.unescape(html_text or "")
        # 司机社页面会把资产写成多种结构，例如：
        #   <em>积分</em><span>123</span>
        #   积分：123
        #   积分</em> 123
        # 旧规则只允许 label 后面跟闭合标签，遇到 <span>/<a> 等开放标签会解析成 '-'。
        text_for_search = re.sub(r'<!--.*?-->', ' ', raw_html, flags=re.S)
        text_for_search = re.sub(r'<br\s*/?>', '\n', text_for_search, flags=re.I)
        text_for_search = re.sub(r'</(?:li|p|div|tr|td|th|span|em|dd|dt)>', ' ', text_for_search, flags=re.I)
        text_for_search = re.sub(r'<[^>]+>', ' ', text_for_search)
        text_for_search = re.sub(r'\s+', ' ', text_for_search)

        def pick(pattern: str, source: Optional[str] = None) -> Optional[str]:
            m = re.search(pattern, source if source is not None else raw_html, re.S | re.I)
            return html.unescape(m.group(1)).strip().strip(',') if m else None

        def pick_any(patterns):
            for p in patterns:
                v = pick(p)
                if v:
                    return v
            return None

        def pick_labeled_number(label: str) -> Optional[str]:
            # label 后允许冒号、空白、任意 HTML 标签，再取第一个数字；积分要避开“积分等级”。
            html_label = r'积分(?!等级)' if label == '积分' else re.escape(label)
            text_label = r'积分(?!等级)' if label == '积分' else re.escape(label)
            patterns = [
                rf'{html_label}\s*(?:[：:=]|&nbsp;|\s|</?[^>]+>)*([+-]?\d[\d,]*)',
                rf'{text_label}\s*[：:=\s]+([+-]?\d[\d,]*)',
            ]
            for pattern in patterns:
                source = text_for_search if '<' not in pattern else raw_html
                v = pick(pattern, source=source)
                if v:
                    return v.lstrip('+')
            return None

        info = {
            "username": self._username,
            "uid": uid,
            "credits": pick_labeled_number('积分'),
            "prestige": pick_labeled_number('威望'),
            "tickets": pick_labeled_number('车票'),
            "contribution": pick_labeled_number('贡献'),
            "reg_time": pick_any([
                r'注册时间\s*(?:[：:]|</?[^>]+>|\s)*([^<\n]{4,40})',
                r'注册时间\s*([\d\-:\s]+)',
            ]),
            "last_visit": pick_any([
                r'最后访问\s*(?:[：:]|</?[^>]+>|\s)*([^<\n]{4,40})',
                r'最后访问\s*([\d\-:\s]+)',
            ]),
        }
        group_match = re.search(r'Lv\.[^<\s]+[^<]{0,20}', raw_html)
        info["user_group"] = html.unescape(group_match.group(0)).strip() if group_match else pick(r'用户组\s*(?:[：:]|</?[^>]+>|\s)*([^<\n]+)')
        self._log_step(f"解析用户信息结果：积分={info.get('credits') or '-'} 威望={info.get('prestige') or '-'} 车票={info.get('tickets') or '-'} 贡献={info.get('contribution') or '-'} 用户组={info.get('user_group') or '-'}")
        return info

    def _has_asset_info(self, info: Optional[Dict[str, Any]]) -> bool:
        return bool(info and any(info.get(field) for field in ("credits", "prestige", "tickets", "contribution")))

    def _merge_user_info(self, base: Dict[str, Any], extra: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not extra:
            return base
        for key, val in extra.items():
            if val and not base.get(key):
                base[key] = val
        return base

    def _save_asset_point(self, user_info: Dict[str, Any]):
        today = datetime.now().strftime("%Y-%m-%d")
        point = {
            "day": today,
            "credits": self._parse_num(user_info.get("credits")) or 0,
            "prestige": self._parse_num(user_info.get("prestige")) or 0,
            "tickets": self._parse_num(user_info.get("tickets")) or 0,
            "contribution": self._parse_num(user_info.get("contribution")) or 0,
        }
        history = self.get_data(self._asset_history_key) or []
        if not isinstance(history, list):
            history = []

        def _item_day(item: Dict[str, Any]) -> str:
            return str(item.get("day") or item.get("date") or "")[:10]

        history = [item for item in history if _item_day(item) != today]
        history.append(point)
        history = sorted(history, key=_item_day, reverse=True)[:30]
        self.save_data(self._asset_history_key, list(reversed(history)))

    def _refresh_user_info_requests(self, session: requests.Session, uid: Optional[str] = None):
        uid = uid or self._extract_uid(session) or self._uid
        if not uid:
            self._log_step("刷新用户信息失败：无法确定 UID")
            return None

        urls = [
            f"{self._base_url}/home.php?mod=space&uid={uid}",
            f"{self._base_url}/home.php?mod=space&uid={uid}&do=profile",
            f"{self._base_url}/home.php?mod=spacecp&ac=credit&showcredit=1",
        ]
        info: Optional[Dict[str, Any]] = None
        for index, url in enumerate(urls):
            self._log_step(f"刷新用户资料页: {url}" if index == 0 else f"资料页未解析到资产，尝试备用页面: {url}")
            resp = session.get(url, timeout=self._timeout)
            resp.raise_for_status()
            parsed = self._parse_user_info_html(resp.text, uid=uid)
            info = self._merge_user_info(info or parsed, parsed)
            if self._has_asset_info(info):
                break

        if not info:
            return None
        self.save_data(self._user_info_key, info)
        self._save_asset_point(info)
        self._log_step(f"用户信息已刷新：积分={info.get('credits') or '-'} 威望={info.get('prestige') or '-'} 贡献={info.get('contribution') or '-'}")
        return info

    def _parse_num(self, val) -> Optional[int]:
        """把带逗号的数字字符串转成 int"""
        if val is None:
            return None
        try:
            return int(str(val).replace(",", "").strip())
        except (ValueError, TypeError):
            return None

    def _reward_to_map(self, reward: Optional[str]) -> Dict[str, int]:
        """Parse reward text into {field: delta}, e.g. 积分 +1，车票 +1."""
        result: Dict[str, int] = {}
        if not reward:
            return result
        text = html.unescape(str(reward))
        for field in ["积分", "威望", "车票", "贡献"]:
            aliases = [field]
            if field == "车票":
                aliases.append("車票")
            for alias in aliases:
                m = re.search(rf'{re.escape(alias)}\s*(?:[+＋:]|：)?\s*(\d+)', text)
                if m:
                    result[field] = int(m.group(1))
                    break
        return result

    def _format_reward_map(self, reward_map: Dict[str, int]) -> Optional[str]:
        """Format reward map in stable user-facing order."""
        parts = []
        for field in ["积分", "威望", "车票", "贡献"]:
            value = reward_map.get(field)
            if value and value > 0:
                parts.append(f"{field} +{value}")
        return "，".join(parts) if parts else None

    def _asset_delta_reward_map(self, pre_info: Optional[Dict[str, Any]], post_info: Optional[Dict[str, Any]]) -> Dict[str, int]:
        """Calculate positive asset deltas from before/after snapshots."""
        if not pre_info or not post_info:
            return {}
        result: Dict[str, int] = {}
        for field, label in [("credits", "积分"), ("prestige", "威望"), ("tickets", "车票"), ("contribution", "贡献")]:
            pre = self._parse_num(pre_info.get(field))
            post = self._parse_num(post_info.get(field))
            if pre is not None and post is not None and post > pre:
                result[label] = post - pre
        return result

    def _merge_reward_with_asset_delta(self, reward: Optional[str], pre_info: Optional[Dict[str, Any]], post_info: Optional[Dict[str, Any]]) -> Tuple[Optional[str], bool]:
        """Use asset delta to fill missing reward fields, without losing parsed response reward."""
        delta_map = self._asset_delta_reward_map(pre_info, post_info)
        if not delta_map:
            return reward, False
        reward_map = self._reward_to_map(reward)
        if reward_map:
            changed = False
            for field, value in delta_map.items():
                if field not in reward_map:
                    reward_map[field] = value
                    changed = True
            merged = self._format_reward_map(reward_map)
            return merged or reward, changed
        delta_reward = self._format_reward_map(delta_map)
        return delta_reward or reward, bool(delta_reward)

    def _extract_reward(self, text: str) -> Optional[str]:
        """从签到响应文本中提取奖励信息"""
        if not text:
            return None
        text = html.unescape(text)
        visible_text = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', text, flags=re.S)
        visible_text = re.sub(r'<[^>]+>', ' ', visible_text)
        visible_text = re.sub(r'\s+', ' ', visible_text).strip()
        if not visible_text:
            self._log_step("签到响应未包含奖励文本，将尝试通过签到前后资产对比兜底")
            return None
        patterns = [
            r'获得\s*([^<\n]{1,40})',
            r'奖励[：:]\s*([^<\n]{1,30})',
            r'签到成功[，,]\s*([^<\n]{1,40})',
            r'恭喜[^<\n]{0,20}获得\s*([^<\n]{1,30})',
            r'积分\s*[\+:]\s*(\d+)',
            r'威望\s*[\+:]\s*(\d+)',
            r'车票\s*[\+:]\s*(\d+)',
            r'积分\s*(\d+)',
            r'威望\s*(\d+)',
            r'车票\s*(\d+)',
        ]
        for p in patterns:
            m = re.search(p, text) or re.search(p, visible_text)
            if m:
                reward = m.group(1).strip()
                if reward.isdigit():
                    field = "积分" if "积分" in p else ("威望" if "威望" in p else "车票")
                    reward = f"{field} +{reward}"
                return reward if len(reward) < 50 else reward[:50]
        # 兜底：直接找 +数字 形式
        m = re.search(r'[\+＋](\d+)', text)
        if m:
            num = m.group(1)
            # 看前后文确定字段
            idx = m.start()
            ctx = text[max(0, idx-20):idx+20]
            field = "积分"
            if "车票" in ctx or "車票" in ctx:
                field = "车票"
            elif "威望" in ctx:
                field = "威望"
            return f"{field} +{num}"
        self._log_step(f"奖励文本未匹配，响应片段：{text[:200].replace(chr(10),' ').replace(chr(13),' ')}")
        return None

    def _refresh_user_info_fs(self, sid: str, uid: Optional[str] = None):
        target_uid = uid or self._uid or '747026'
        urls = [
            f"{self._base_url}/home.php?mod=space&uid={target_uid}",
            f"{self._base_url}/home.php?mod=space&uid={target_uid}&do=profile",
            f"{self._base_url}/home.php?mod=spacecp&ac=credit&showcredit=1",
        ]
        info: Optional[Dict[str, Any]] = None
        for index, url in enumerate(urls):
            if index > 0:
                self._log_step(f"资料页未解析到资产，尝试备用页面: {url}")
            data = self._fs_get(sid, url)
            sol = data.get("solution", {})
            html_text = sol.get("response") or ""
            if not uid:
                uid = self._extract_uid_from_fs_cookies(sol.get("cookies") or []) or self._uid or target_uid
            parsed = self._parse_user_info_html(html_text, uid=uid)
            info = self._merge_user_info(info or parsed, parsed)
            if self._has_asset_info(info):
                break
        if not info:
            return None
        self.save_data(self._user_info_key, info)
        self._save_asset_point(info)
        return info

    def _login_fs(self, sid: str) -> Tuple[bool, str]:
        try:
            data = self._fs_get(sid, f"{self._base_url}/member.php?mod=logging&action=login&infloat=yes&handlekey=login&inajax=1&ajaxtarget=fwin_content_login")
            sol = data.get("solution", {})
            html_text = sol.get("response") or ""
            formhash = self._get_formhash(html_text)
            loginhash = self._get_loginhash(html_text)
            if not formhash:
                return False, "无法获取 formhash"
            if not loginhash:
                return False, "无法获取 loginhash"
            post_data = (
                f"formhash={formhash}&referer={requests.utils.quote(self._base_url + '/', safe='')}&"
                f"username={requests.utils.quote(self._username, safe='')}&password={self._md5(self._password)}&questionid=0&answer="
            )
            data = self._fs_post(sid, f"{self._base_url}/member.php?mod=logging&action=login&loginsubmit=yes&handlekey=login&loginhash={loginhash}&inajax=1", post_data, headers={"Content-Type": "application/x-www-form-urlencoded"})
            cookies = data.get("solution", {}).get("cookies") or []
            if any(c.get("name") == "SgL6_2132_auth" or "auth" in c.get("name", "").lower() for c in cookies):
                return True, "登录成功"
            return False, "登录失败，未获得 auth cookie"
        except Exception as e:
            return False, f"FlareSolverr 登录异常: {str(e)}"

    def _signin_fs(self, sid: str) -> Tuple[bool, str, Optional[str]]:
        """FlareSolverr 签到，返回 (是否成功, 消息, 奖励)"""
        reward = None
        try:
            self._fs_get(sid, f"{self._base_url}/k_misign-sign.html")
            target_uid = self._uid or '747026'
            data = self._fs_get(sid, f"{self._base_url}/home.php?mod=space&uid={target_uid}")
            sol = data.get("solution", {})
            html_text = sol.get("response") or ""
            cookies = sol.get("cookies") or []
            uid = self._extract_uid_from_fs_cookies(cookies) or self._uid or target_uid
            if uid != target_uid:
                data = self._fs_get(sid, f"{self._base_url}/home.php?mod=space&uid={uid}")
                sol = data.get("solution", {})
                html_text = sol.get("response") or html_text
            formhash = self._get_formhash(html_text)
            if not formhash:
                return False, "无法获取签到 formhash", None
            data = self._fs_get(sid, f"{self._base_url}/plugin.php?id=k_misign:sign&operation=qiandao&formhash={formhash}&format=empty&inajax=1&ajaxtarget=JD_sign")
            resp = data.get("solution", {}).get("response") or ""
            cookies = data.get("solution", {}).get("cookies") or []

            # 提取奖励
            reward = self._extract_reward(resp)
            self._log_step(f"签到响应片段（前200字）：{resp[:200].replace(chr(10),' ').replace(chr(13),' ')}")
            if reward:
                self._log_step(f"提取到奖励：{reward}")

            # 判据：文本优先（更明确，能区分"今日已签到"和"首次签到成功"）
            if any(k in resp for k in ["今日已签到", "已经签到", "已完成签到", "请勿重复签到", "重复签到"]):
                return True, "今日已签到", None
            if "非法字符" in resp or "已经被系统拒绝" in resp:
                return True, "今日已签到（非法字符：重复请求）", None
            if "签到成功" in resp:
                if reward:
                    return True, f"签到成功：{reward}", reward
                return True, "签到成功", None

            # cookie 判据兜底
            if any("misigntime" in c.get("name", "").lower() for c in cookies):
                if reward:
                    return True, f"签到成功：{reward}", reward
                return True, "签到完成", None

            # 兜底
            if reward:
                return True, f"签到完成：{reward}", reward
            return True, "签到完成", None
        except Exception as e:
            return False, f"FlareSolverr 签到异常: {str(e)}", None

    def _login(self, session: requests.Session) -> Tuple[bool, str]:
        """登录并返回(是否成功, 消息)"""
        try:
            # 0. 先访问首页获取初始 cookie
            self._log_step("访问首页获取初始会话...")
            session.get(f"{self._base_url}/", timeout=self._timeout, allow_redirects=True)
            
            # 1. 获取登录框，提取 formhash 和 loginhash
            login_form_url = f"{self._base_url}/member.php?mod=logging&action=login&infloat=yes&handlekey=login&inajax=1&ajaxtarget=fwin_content_login"
            self._log_step(f"获取登录框: {login_form_url}")
            resp = session.get(login_form_url, timeout=self._timeout)
            resp.raise_for_status()
            
            formhash = self._get_formhash(resp.text)
            loginhash = self._get_loginhash(resp.text)
            
            if not formhash:
                return False, "无法获取 formhash"
            if not loginhash:
                return False, "无法获取 loginhash"
            
            self._log_step(f"formhash={formhash}, loginhash={loginhash}")
            
            # 2. 提交登录
            login_url = f"{self._base_url}/member.php?mod=logging&action=login&loginsubmit=yes&handlekey=login&loginhash={loginhash}&inajax=1"
            password_md5 = self._md5(self._password)
            
            post_data = {
                "formhash": formhash,
                "referer": f"{self._base_url}/",
                "username": self._username,
                "password": password_md5,
                "questionid": "0",
                "answer": "",
            }
            
            self._log_step(f"提交登录: {login_url}")
            resp = session.post(login_url, data=post_data, timeout=self._timeout)
            resp.raise_for_status()
            
            # 优先检查 auth cookie（HAR 中登录成功后返回空响应但设置 auth cookie）
            for cookie in session.cookies:
                if "auth" in cookie.name.lower():
                    return True, "登录成功"
            
            # 其次检查响应文本
            if "登录成功" in resp.text or "欢迎您回来" in resp.text:
                return True, "登录成功"
            elif "密码错误" in resp.text or ("密码" in resp.text and "错误" in resp.text):
                return False, "密码错误"
            elif "用户不存在" in resp.text:
                return False, "用户不存在"
            elif "登录失败" in resp.text:
                return False, "登录失败"
            else:
                return False, f"登录结果未知: {resp.text[:200]}"
                
        except RequestException as e:
            return False, f"登录请求失败: {str(e)}"
        except Exception as e:
            return False, f"登录异常: {str(e)}"

    def _signin(self, session: requests.Session) -> Tuple[bool, str, Optional[str]]:
        """签到并返回(是否成功, 消息, 奖励)"""
        try:
            sign_page_url = f"{self._base_url}/k_misign-sign.html"
            self._log_step(f"访问签到页: {sign_page_url}")
            sign_resp = session.get(sign_page_url, timeout=self._timeout)

            # 先尝试直接从签到页取 formhash；失败再回退到用户空间页
            formhash = self._get_formhash(sign_resp.text or "")
            uid = self._extract_uid(session) or self._uid
            if not formhash and uid:
                space_url = f"{self._base_url}/home.php?mod=space&uid={uid}"
                self._log_step(f"签到页未取到 formhash，回退到用户空间: {space_url}")
                resp = session.get(space_url, timeout=self._timeout)
                formhash = self._get_formhash(resp.text or "")

            if not formhash:
                # 最后再试首页，防止表单散落在其他页面
                self._log_step("用户空间仍未取到 formhash，回退首页再试一次")
                home_resp = session.get(f"{self._base_url}/", timeout=self._timeout)
                formhash = self._get_formhash(home_resp.text or "")

            if not formhash:
                return False, f"无法获取签到 formhash（uid={uid or '-'}）", None

            self._log_step(f"签到 formhash={formhash}")
            sign_url = f"{self._base_url}/plugin.php?id=k_misign:sign&operation=qiandao&formhash={formhash}&format=empty&inajax=1&ajaxtarget=JD_sign"
            self._log_step(f"执行签到: {sign_url}")
            resp = session.get(sign_url, timeout=self._timeout)

            # 提取奖励
            reward = self._extract_reward(resp.text or "")
            self._log_step(f"签到响应片段（前200字）：{resp.text[:200].replace(chr(10),' ').replace(chr(13),' ') if resp.text else '(空)'}")
            if reward:
                self._log_step(f"提取到奖励：{reward}")

            # 以文本判据优先
            if any(k in resp.text for k in ["今日已签到", "已经签到", "已完成签到", "请勿重复签到", "重复签到"]):
                return True, "今日已签到" + (f"：{reward}" if reward else ""), reward
            if "非法字符" in resp.text or "已经被系统拒绝" in resp.text:
                return True, "今日已签到（非法字符：重复请求）" + (f"：{reward}" if reward else ""), reward
            if "签到成功" in resp.text:
                return True, f"签到成功：{reward}" if reward else "签到成功", reward

            # cookie 兜底判据
            if any("misigntime" in c.name.lower() for c in session.cookies):
                return True, f"签到完成：{reward}" if reward else "签到完成", reward

            if resp.status_code == 200:
                return True, f"签到完成：{reward}" if reward else "签到完成", reward
            else:
                return False, f"签到响应异常: {resp.text[:200]}", None

        except Exception as e:
            return False, f"签到异常: {str(e)}", None

    def run_once(self, source: str = "manual"):
        steps: List[str] = []
        trigger_text = self._source_text(source)
        
        # 定时任务随机延时
        if str(source).strip().lower() == 'cron':
            delay_seconds = random.randint(60, 1800)
            self._log_step(f"随机延时 {delay_seconds // 60} 分 {delay_seconds % 60} 秒后开始执行（{source}）")
            steps.append(f"⏳ 定时任务随机延时 {delay_seconds // 60} 分 {delay_seconds % 60} 秒")
            time.sleep(delay_seconds)

        result = {
            'executed_at': self._now_text(),
            'source': source,
            'source_text': trigger_text,
            'login_status': '未开始',
            'signin_status': '未开始',
            'reward': None,
            'result_label': '执行中',
            'message': '',
            'finished': False,
            'proxy_used': self._proxy_url or '未配置',
            'steps': steps,
        }

        # 检查配置
        if not self._username or not self._password:
            steps.append('❌ 未配置账号密码，终止执行')
            result.update({
                'login_status': '失败', 
                'signin_status': '未执行', 
                'result_label': '失败', 
                'message': '未配置账号密码', 
                'finished': True
            })
            self._save_result(result)
            if self._notify:
                self.post_message(mtype=NotificationType.Plugin, title=f"【{self.plugin_name}】", text=self._notify_text(result))
            return result

        session = None
        fs_session = None
        result['use_flaresolverr'] = self._use_flaresolverr
        steps.append(f"🌐 代理：{self._proxy_url or '未配置'}")
        self._log_step(f"开始执行签到流程（{trigger_text}）")

        try:
            if self._use_flaresolverr:
                fs_session = self._fs_create_session()
                steps.append(f"🛡️ 已创建 FlareSolverr 会话：{self._flaresolverr_url}")
            else:
                session = self._session()
                steps.append("🌐 已创建 requests 会话")

            # 1. 登录
            steps.append('🔐 开始登录')
            self._log_step("开始登录")
            if self._use_flaresolverr:
                login_success, login_msg = self._login_fs(fs_session)
            else:
                login_success, login_msg = self._login(session)
            result['login_status'] = '成功' if login_success else '失败'
            steps.append(f"🔐 登录{'成功' if login_success else '失败'}: {login_msg}")
            self._log_step(f"登录{'成功' if login_success else '失败'}: {login_msg}")

            if not login_success:
                result.update({
                    'signin_status': '未执行',
                    'result_label': '失败',
                    'message': f'登录失败: {login_msg}',
                    'finished': True
                })
                self._save_result(result)
                if self._notify:
                    self.post_message(mtype=NotificationType.Plugin, title=f"【{self.plugin_name}】", text=self._notify_text(result))
                if fs_session:
                    self._fs_destroy_session(fs_session)
                self._handle_retry_after_result(result, "登录失败")
                return result

            # 1.5 签到前资产快照（用于后续对比算奖励）
            pre_info = None
            try:
                pre_info = self._refresh_user_info_fs(fs_session, uid=self._uid or None) if self._use_flaresolverr else self._refresh_user_info_requests(session, uid=self._uid or None)
                self._log_step(f"签到前资产快照：积分 {pre_info.get('credits') or '-'} / 威望 {pre_info.get('prestige') or '-'} / 车票 {pre_info.get('tickets') or '-'} / 贡献 {pre_info.get('contribution') or '-'}")
            except Exception as e:
                self._log_step(f"签到前资产快照失败：{str(e)}")

            # 2. 签到
            steps.append('✍️ 开始签到')
            self._log_step("开始签到")
            if self._use_flaresolverr:
                sign_success, sign_msg, reward = self._signin_fs(fs_session)
            else:
                sign_success, sign_msg, reward = self._signin(session)
            result['signin_status'] = '成功' if sign_success else '失败'
            result['reward'] = reward
            if reward:
                steps.append(f"🎁 签到奖励：{reward}")
                self._log_step(f"签到奖励：{reward}")
            steps.append(f"✍️ 签到{'成功' if sign_success else '失败'}: {sign_msg}")
            self._log_step(f"签到{'成功' if sign_success else '失败'}: {sign_msg}")

            # 3. 用户信息
            try:
                info = self._refresh_user_info_fs(fs_session, uid=self._uid or None) if self._use_flaresolverr else self._refresh_user_info_requests(session, uid=self._uid or None)
                if info:
                    steps.append(f"👤 已刷新用户信息：积分 {info.get('credits') or '-'} / 威望 {info.get('prestige') or '-'} / 贡献 {info.get('contribution') or '-'}")
                    # 资产对比：不仅在 reward 为空时兜底，也用于补全响应里缺失的奖励字段。
                    merged_reward, reward_changed = self._merge_reward_with_asset_delta(reward, pre_info, info)
                    if reward_changed and merged_reward:
                        reward = merged_reward
                        result['reward'] = reward
                        steps.append(f"🎁 签到奖励（资产对比补全）：{reward}")
                        self._log_step(f"签到奖励（资产对比补全）：{reward}")
            except Exception as info_error:
                steps.append(f"⚠️ 用户信息刷新失败：{str(info_error)}")

            result.update({
                'result_label': '成功' if sign_success else '失败',
                'message': sign_msg,
                'reward': reward,
                'finished': True
            })

            steps.append('✅ 执行完成')
            self._log_step("执行完成")

        except Exception as e:
            steps.append(f"💥 执行失败：{str(e)}")
            self._log_step(f"执行失败：{str(e)}")
            result.update({
                'result_label': '失败',
                'message': str(e),
                'finished': True
            })
            if result['login_status'] == '未开始':
                result['login_status'] = '失败'
                result['signin_status'] = '未执行'
            else:
                result['signin_status'] = '失败'
            logger.error(f"司机社签到自用执行失败：{str(e)}", exc_info=True)

        if fs_session:
            self._fs_destroy_session(fs_session)
        self._save_result(result)
        if self._notify:
            self.post_message(mtype=NotificationType.Plugin, title=f"【{self.plugin_name}】", text=self._notify_text(result))

        self._handle_retry_after_result(result, "签到失败")
        return result

    def _handle_retry_after_result(self, result: Dict[str, Any], reason: str):
        """根据执行结果安排失败重试；兼容 MoviePilot 主调度器触发时内部调度器为空的情况。"""
        if result.get("result_label") == "成功":
            if self.get_data(self._retry_state_key):
                self.save_data(self._retry_state_key, None)
            return
        if result.get("result_label") != "失败" or self._retry_count <= 0:
            return

        source_value = str(result.get("source") or "").strip().lower()
        if source_value not in {"cron", "scheduler", "schedule", "service", "auto", "automatic", "retry"}:
            return

        if not self._scheduler:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

        existing_jobs = [j.id for j in self._scheduler.get_jobs()]
        if f"{self.plugin_config_prefix}retry" in existing_jobs:
            return

        retry_state = self.get_data(self._retry_state_key) or {}
        today = datetime.now().strftime("%Y-%m-%d")
        if retry_state.get("date") != today:
            retry_state = {"date": today, "attempt": 0}
        retry_state["attempt"] = retry_state.get("attempt", 0) + 1
        if retry_state["attempt"] <= self._retry_count:
            next_retry_time = datetime.now() + timedelta(minutes=self._retry_interval_minutes)
            if next_retry_time.strftime("%Y-%m-%d") != today:
                self._log_step("失败重试时间已跨天，按当日限制不再安排重试")
                retry_state["attempt"] = self._retry_count
                self.save_data(self._retry_state_key, retry_state)
                return
            self._scheduler.add_job(
                func=self._retry_wrapper,
                trigger="date",
                run_date=next_retry_time,
                id=f"{self.plugin_config_prefix}retry",
                name=f"{self.plugin_name}（第 {retry_state['attempt']} 次重试）",
                kwargs={"attempt": retry_state["attempt"]},
                replace=True,
            )
            if not self._scheduler.running:
                self._scheduler.start()
            self._log_step(
                f"{reason}，第 {retry_state['attempt']} 次重试已安排在 "
                f"{next_retry_time.strftime('%H:%M:%S')}（间隔 {self._retry_interval_minutes} 分钟）"
            )
            retry_state["scheduled_at"] = next_retry_time.strftime("%Y-%m-%d %H:%M:%S")
        else:
            self._log_step(f"当日重试次数（{self._retry_count} 次）已耗尽，不再重试")
            retry_state["attempt"] = self._retry_count
        self.save_data(self._retry_state_key, retry_state)

    def _retry_wrapper(self, attempt: int):
        """由 APScheduler 调用的重试入口，避免直接传递 self.run_once（不可序列化）"""
        self._log_step(f"【重试 #{attempt}】触发执行")
        self.run_once(source="retry")

    def stop_service(self):
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._scheduler = None
