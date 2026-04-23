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
    plugin_desc = "用于司机社的自用签到插件，支持自动登录并完成每日签到。"
    plugin_icon = "https://raw.githubusercontent.com/Vivitoto/MoviePilot-Plugins/main/icons/sijishe.png"
    plugin_version = "0.0.1"
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

    _scheduler: Optional[BackgroundScheduler] = None
    _history_key = "history"
    _last_result_key = "last_result"
    _user_info_key = "user_info"
    _asset_history_key = "asset_history"

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
        })

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/sijishe_signin",
            "event": EventType.PluginAction,
            "desc": "执行司机社签到",
            "category": "站点",
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
                    {
                        'component': 'VCard',
                        'props': {'variant': 'flat', 'class': 'mb-4'},
                        'content': [{
                            'component': 'VCardItem',
                            'content': [{
                                'component': 'VRow',
                                'content': [
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '保存后执行一次'}}]},
                                ]
                            }]
                        }]
                    },
                    {
                        'component': 'VCard',
                        'props': {'variant': 'flat', 'class': 'mb-4'},
                        'content': [{
                            'component': 'VCardItem',
                            'content': [{
                                'component': 'VRow',
                                'content': [
                                    {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'div', 'props': {'class': 'text-subtitle-2 mb-3'}, 'text': '账号配置'}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'username', 'label': '用户名'}}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'password', 'label': '密码', 'type': 'password', 'placeholder': '明文密码，会自动MD5加密'}}]},
                                    {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'div', 'props': {'class': 'text-subtitle-2 mt-2 mb-3'}, 'text': '执行配置'}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': cron_field_component, 'props': {'model': 'cron', 'label': '定时任务', 'placeholder': '10 9 * * *'}}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'timeout', 'label': '请求超时（秒）', 'type': 'number', 'placeholder': '20'}}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'base_url', 'label': '站点地址', 'placeholder': 'https://xsijishe.net'}}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'proxy_url', 'label': '代理地址（可留空）', 'placeholder': 'http://127.0.0.1:7890'}}]},
                                ]
                            }]
                        }]
                    },
                    {
                        'component': 'VCard',
                        'props': {'variant': 'tonal', 'class': 'mb-2'},
                        'content': [{
                            'component': 'VCardItem',
                            'content': [{
                                'component': 'VRow',
                                'content': [
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                        {'component': 'div', 'props': {'class': 'text-subtitle-2 font-weight-bold'}, 'text': '📋 使用说明'},
                                        {'component': 'div', 'props': {'class': 'mt-2 text-body-2 d-flex align-center'}, 'content': [
                                            {'component': 'VIcon', 'props': {'icon': 'mdi-lock', 'size': 18, 'class': 'mr-2', 'color': 'primary'}},
                                            {'component': 'span', 'text': '密码使用明文填写，登录时会自动MD5加密'}
                                        ]},
                                        {'component': 'div', 'props': {'class': 'mt-1 text-body-2 d-flex align-center'}, 'content': [
                                            {'component': 'VIcon', 'props': {'icon': 'mdi-web-network', 'size': 18, 'class': 'mr-2', 'color': 'warning'}},
                                            {'component': 'span', 'text': '该站点需要代理访问'}
                                        ]},
                                    ]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                                        {'component': 'div', 'props': {'class': 'text-subtitle-2 font-weight-bold'}, 'text': '⚙️ 默认行为'},
                                        {'component': 'div', 'props': {'class': 'mt-2 text-body-2 d-flex align-center'}, 'content': [
                                            {'component': 'VIcon', 'props': {'icon': 'mdi-web', 'size': 18, 'class': 'mr-2', 'color': 'info'}},
                                            {'component': 'span', 'text': '默认站点：https://xsijishe.net'}
                                        ]},
                                        {'component': 'div', 'props': {'class': 'mt-1 text-body-2 d-flex align-center'}, 'content': [
                                            {'component': 'VIcon', 'props': {'icon': 'mdi-timer-outline', 'size': 18, 'class': 'mr-2', 'color': 'info'}},
                                            {'component': 'span', 'text': '默认超时：20 秒'}
                                        ]},
                                        {'component': 'div', 'props': {'class': 'mt-1 text-body-2 d-flex align-center'}, 'content': [
                                            {'component': 'VIcon', 'props': {'icon': 'mdi-shuffle', 'size': 18, 'class': 'mr-2', 'color': 'warning'}},
                                            {'component': 'span', 'text': '仅定时任务会随机延时 1-30 分钟执行'}
                                        ]},
                                    ]},
                                ]
                            }]
                        }]
                    }
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
        }

    def get_page(self) -> List[dict]:
        history = self.get_data(self._history_key) or []
        user_info = self.get_data(self._user_info_key) or {}
        asset_history = self.get_data(self._asset_history_key) or []
        
        if not history and not user_info:
            return [{'component': 'div', 'text': '暂无数据', 'props': {'class': 'text-center'}}]

        history = sorted(history, key=lambda x: x.get('executed_at', ''), reverse=True) if history else []
        
        # 构建页面组件列表
        components = []
        
        # 1. 用户信息卡片
        if user_info:
            username = user_info.get('username', self._username or '未知用户')
            user_group = user_info.get('user_group', '-')
            credits = user_info.get('credits', '-')
            prestige = user_info.get('prestige', '-')
            tickets = user_info.get('tickets', '-')
            contribution = user_info.get('contribution', '-')
            reg_time = user_info.get('reg_time', '-')
            
            components.append({
                'component': 'VCard',
                'props': {'variant': 'flat', 'class': 'mb-3'},
                'content': [
                    {'component': 'VCardTitle', 'text': f'👤 用户信息：{username}'},
                    {'component': 'VCardText', 'props': {'class': 'pt-2'}, 'content': [
                        {'component': 'VRow', 'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4, 'sm': 6}, 'content': [
                                {'component': 'div', 'props': {'class': 'text-body-2'}, 'content': [
                                    {'component': 'span', 'props': {'class': 'text-medium-emphasis'}, 'text': '用户组：'},
                                    {'component': 'span', 'props': {'class': 'font-weight-medium'}, 'text': str(user_group)}
                                ]}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4, 'sm': 6}, 'content': [
                                {'component': 'div', 'props': {'class': 'text-body-2'}, 'content': [
                                    {'component': 'span', 'props': {'class': 'text-medium-emphasis'}, 'text': '积分：'},
                                    {'component': 'span', 'props': {'class': 'font-weight-medium text-primary'}, 'text': str(credits)}
                                ]}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4, 'sm': 6}, 'content': [
                                {'component': 'div', 'props': {'class': 'text-body-2'}, 'content': [
                                    {'component': 'span', 'props': {'class': 'text-medium-emphasis'}, 'text': '威望：'},
                                    {'component': 'span', 'props': {'class': 'font-weight-medium'}, 'text': str(prestige)}
                                ]}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4, 'sm': 6}, 'content': [
                                {'component': 'div', 'props': {'class': 'text-body-2'}, 'content': [
                                    {'component': 'span', 'props': {'class': 'text-medium-emphasis'}, 'text': '车票：'},
                                    {'component': 'span', 'props': {'class': 'font-weight-medium text-success'}, 'text': str(tickets)}
                                ]}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4, 'sm': 6}, 'content': [
                                {'component': 'div', 'props': {'class': 'text-body-2'}, 'content': [
                                    {'component': 'span', 'props': {'class': 'text-medium-emphasis'}, 'text': '贡献：'},
                                    {'component': 'span', 'props': {'class': 'font-weight-medium'}, 'text': str(contribution)}
                                ]}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4, 'sm': 6}, 'content': [
                                {'component': 'div', 'props': {'class': 'text-body-2'}, 'content': [
                                    {'component': 'span', 'props': {'class': 'text-medium-emphasis'}, 'text': '注册时间：'},
                                    {'component': 'span', 'props': {'class': 'font-weight-medium'}, 'text': str(reg_time)}
                                ]}
                            ]},
                        ]}
                    ]}
                ]
            })
        
        # 2. 资产趋势图（如果有历史数据）
        if asset_history and len(asset_history) >= 2:
            asset_history = sorted(asset_history, key=lambda x: x.get('date', ''))[-30:]  # 最近30条
            dates = [item.get('date', '') for item in asset_history]
            credits_data = [item.get('credits', 0) for item in asset_history]
            prestige_data = [item.get('prestige', 0) for item in asset_history]
            tickets_data = [item.get('tickets', 0) for item in asset_history]
            contribution_data = [item.get('contribution', 0) for item in asset_history]
            
            components.append({
                'component': 'VCard',
                'props': {'variant': 'flat', 'class': 'mb-3'},
                'content': [
                    {'component': 'VCardTitle', 'text': '📈 资产趋势（最近30次）'},
                    {'component': 'VCardText', 'props': {'class': 'pt-2'}, 'content': [
                        {'component': 'VApexChart',
                         'props': {
                             'type': 'line',
                             'height': 300,
                             'options': {
                                 'chart': {'toolbar': {'show': False}, 'zoom': {'enabled': False}},
                                 'stroke': {'curve': 'smooth', 'width': 2},
                                 'xaxis': {'categories': dates, 'labels': {'rotate': -45, 'style': {'fontSize': '10px'}}},
                                 'yaxis': {'labels': {'style': {'fontSize': '10px'}}},
                                 'legend': {'position': 'top'},
                                 'grid': {'strokeDashArray': 3},
                             },
                             'series': [
                                 {'name': '积分', 'data': credits_data},
                                 {'name': '威望', 'data': prestige_data},
                                 {'name': '车票', 'data': tickets_data},
                                 {'name': '贡献', 'data': contribution_data},
                             ]
                         }}
                    ]}
                ]
            })
        
        # 3. 执行记录表格
        if history:
            components.append({
                'component': 'VCard',
                'props': {'variant': 'flat', 'class': 'mb-3'},
                'content': [
                    {'component': 'VCardTitle', 'text': f'🗂️ 执行记录（共 {len(history)} 条，显示最近30条）'},
                    {'component': 'VTable', 'props': {'density': 'compact', 'hover': True}, 'content': [
                        {'component': 'thead', 'content': [{
                            'component': 'tr', 'content': [
                                {'component': 'th', 'props': {'style': 'text-align:center; width: 22%;'}, 'text': '时间'},
                                {'component': 'th', 'props': {'style': 'text-align:center; width: 12%;'}, 'text': '触发'},
                                {'component': 'th', 'props': {'style': 'text-align:center; width: 12%;'}, 'text': '登录'},
                                {'component': 'th', 'props': {'style': 'text-align:center; width: 14%;'}, 'text': '签到'},
                                {'component': 'th', 'props': {'style': 'text-align:center; width: 20%;'}, 'text': '奖励'},
                                {'component': 'th', 'props': {'style': 'text-align:center; width: 20%;'}, 'text': '消息'},
                            ]
                        }]},
                        {'component': 'tbody', 'content': [{
                            'component': 'tr', 'content': [
                                {'component': 'td', 'props': {'style': 'text-align:center; font-size:12px;'}, 'text': item.get('executed_at', '-')},
                                {'component': 'td', 'props': {'style': 'text-align:center;'}, 'text': item.get('source_text', '-')},
                                {'component': 'td', 'props': {'style': 'text-align:center;'}, 'text': item.get('login_status', '-')},
                                {'component': 'td', 'props': {'style': 'text-align:center;'}, 'text': item.get('signin_status', '-')},
                                {'component': 'td', 'props': {'style': 'text-align:center; font-size:12px;'}, 'text': item.get('reward_text', '-')},
                                {'component': 'td', 'props': {'style': 'text-align:center; font-size:12px;'}, 'text': item.get('message', '-')[:30]},
                            ]
                        } for item in history[:30]]}
                    ]}
                ]
            })
        
        return components

    def api_run(self):
        return self.run_once(source="api")

    def run_by_cron(self):
        self._log_step("【run_by_cron】被主调度器调用，source=cron")
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
        if source_value in {'cron', 'scheduler', 'schedule', 'service', 'auto', 'automatic'}:
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
            f"📝 结果说明：{result.get('message', '-')}",
        ]
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
        match = re.search(r'loginhash=([a-zA-Z0-9]{5})', text)
        if match:
            return match.group(1)
        return None

    def _login(self, session: requests.Session) -> Tuple[bool, str]:
        """登录并返回(是否成功, 消息)"""
        try:
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
            
            # 检查登录结果
            if "登录成功" in resp.text or "欢迎您回来" in resp.text:
                return True, "登录成功"
            elif "密码错误" in resp.text or "密码" in resp.text and "错误" in resp.text:
                return False, "密码错误"
            elif "用户不存在" in resp.text:
                return False, "用户不存在"
            elif "登录失败" in resp.text:
                return False, "登录失败"
            else:
                # 检查是否有 auth cookie
                for cookie in session.cookies:
                    if "auth" in cookie.name.lower():
                        return True, "登录成功(通过Cookie验证)"
                return False, f"登录结果未知: {resp.text[:200]}"
                
        except RequestException as e:
            return False, f"登录请求失败: {str(e)}"
        except Exception as e:
            return False, f"登录异常: {str(e)}"

    def _signin(self, session: requests.Session) -> Tuple[bool, str]:
        """签到并返回(是否成功, 消息)"""
        try:
            # 1. 访问签到页面获取 formhash
            sign_page_url = f"{self._base_url}/k_misign-sign.html"
            self._log_step(f"获取签到页: {sign_page_url}")
            resp = session.get(sign_page_url, timeout=self._timeout)
            resp.raise_for_status()
            
            # 检查是否已经签到
            if "今日已签到" in resp.text or "已签到" in resp.text:
                return True, "今日已签到"
            
            formhash = self._get_formhash(resp.text)
            if not formhash:
                return False, "无法获取签到 formhash"
            
            self._log_step(f"签到 formhash={formhash}")
            
            # 2. 提交签到
            sign_url = f"{self._base_url}/plugin.php?id=k_misign:sign&operation=qiandao&formhash={formhash}&format=empty&inajax=1&ajaxtarget=JD_sign"
            self._log_step(f"提交签到: {sign_url}")
            resp = session.get(sign_url, timeout=self._timeout)
            resp.raise_for_status()
            
            # 解析签到结果
            if "今日已签到" in resp.text:
                return True, "今日已签到"
            elif "签到成功" in resp.text:
                return True, "签到成功"
            elif resp.text.strip() == "":
                # 空响应通常表示成功
                return True, "签到完成"
            else:
                return True, f"签到响应: {resp.text[:200]}"
                
        except RequestException as e:
            return False, f"签到请求失败: {str(e)}"
        except Exception as e:
            return False, f"签到异常: {str(e)}"

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

        session = self._session()
        steps.append(f"🌐 已创建会话，代理：{self._proxy_url or '未配置'}")
        self._log_step(f"开始执行签到流程（{trigger_text}）")

        try:
            # 1. 登录
            steps.append('🔐 开始登录')
            self._log_step("开始登录")
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
                return result

            # 2. 签到
            steps.append('✍️ 开始签到')
            self._log_step("开始签到")
            sign_success, sign_msg = self._signin(session)
            result['signin_status'] = '成功' if sign_success else '失败'
            steps.append(f"✍️ 签到{'成功' if sign_success else '失败'}: {sign_msg}")
            self._log_step(f"签到{'成功' if sign_success else '失败'}: {sign_msg}")

            result.update({
                'result_label': '成功' if sign_success else '失败',
                'message': sign_msg,
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

        self._save_result(result)
        if self._notify:
            self.post_message(mtype=NotificationType.Plugin, title=f"【{self.plugin_name}】", text=self._notify_text(result))
        return result

    def stop_service(self):
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._scheduler = None
