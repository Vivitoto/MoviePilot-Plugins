import html
import json
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType


class NodeSeekSignIn(_PluginBase):
    plugin_name = "Nodeseek签到自用"
    plugin_desc = "通过 Cookie 自动完成 NodeSeek 每日签到。"
    plugin_icon = "https://raw.githubusercontent.com/Vivitoto/MoviePilot-Plugins/main/icons/nodeseeksignin.png"
    plugin_version = "1.0.4"
    plugin_author = "Vivitoto"
    author_url = "https://github.com/Vivitoto"
    plugin_config_prefix = "nodeseeksignin_"
    plugin_order = 23
    auth_level = 1

    _enabled = False
    _notify = True
    _onlyonce = False
    _cron = "20 8 * * *"
    _cookie = ""
    _member_id = ""
    _base_url = "https://www.nodeseek.com"
    _random_signin = True
    _use_proxy = False
    _proxy_url = ""
    _timeout = 30
    _use_flaresolverr = False
    _flaresolverr_url = "http://127.0.0.1:8191/v1"
    _retry_count = 3
    _retry_interval_minutes = 5
    _runtime_cookie = ""
    _runtime_user_agent = ""
    _runtime_fs_session = ""
    _runtime_fs_warmed = False

    _scheduler: Optional[BackgroundScheduler] = None
    _history_key = "history"
    _daily_results_key = "daily_results"
    _last_result_key = "last_result"
    _user_info_key = "user_info"
    _retry_state_key = "retry_state"

    def init_plugin(self, config: dict = None):
        self.stop_service()
        try:
            if config:
                self._enabled = config.get("enabled", False)
                self._notify = config.get("notify", True)
                self._onlyonce = config.get("onlyonce", False)
                self._cron = config.get("cron") or "20 8 * * *"
                self._cookie = str(config.get("cookie") or "").strip()
                self._member_id = self._normalize_member_id(config.get("member_id"))
                self._base_url = str(config.get("base_url") or "https://www.nodeseek.com").strip().rstrip("/")
                self._random_signin = config.get("random_signin", True)
                self._proxy_url = str(config.get("proxy_url") or "").strip()
                self._use_proxy = config.get("use_proxy", bool(self._proxy_url))
                self._timeout = max(1, int(config.get("timeout") or 30))
                self._use_flaresolverr = config.get("use_flaresolverr", False)
                self._flaresolverr_url = str(config.get("flaresolverr_url") or "http://127.0.0.1:8191/v1").strip().rstrip("/")
                retry_count = config.get("retry_count", 3)
                retry_interval = config.get("retry_interval_minutes", 5)
                self._retry_count = max(0, int(3 if retry_count in (None, "") else retry_count))
                self._retry_interval_minutes = max(1, int(5 if retry_interval in (None, "") else retry_interval))

            if self._onlyonce:
                logger.info("Nodeseek签到自用：保存配置后执行一次")
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    func=self.run_by_onlyonce,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="Nodeseek签到自用",
                    kwargs={},
                )
                self._onlyonce = False
                self.__update_config()
                if self._scheduler.get_jobs():
                    self._scheduler.start()
        except Exception as e:
            logger.error(f"Nodeseek签到自用初始化错误：{str(e)}", exc_info=True)

    def get_state(self) -> bool:
        return self._enabled and bool(self._cookie)

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "cookie": self._cookie,
            "member_id": self._member_id,
            "base_url": self._base_url,
            "random_signin": self._random_signin,
            "use_proxy": self._use_proxy,
            "proxy_url": self._proxy_url,
            "timeout": self._timeout,
            "use_flaresolverr": self._use_flaresolverr,
            "flaresolverr_url": self._flaresolverr_url,
            "retry_count": self._retry_count,
            "retry_interval_minutes": self._retry_interval_minutes,
        })

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/nodeseek_signin",
            "event": EventType.PluginAction,
            "desc": "执行 NodeSeek 签到",
            "data": {"action": "nodeseek_signin"},
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/run",
            "endpoint": self.api_run,
            "methods": ["GET"],
            "summary": "执行签到",
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._cron:
            return []
        return [{
            "id": "NodeSeekSignIn",
            "name": "Nodeseek签到自用",
            "trigger": CronTrigger.from_crontab(self._cron),
            "func": self.run_by_cron,
            "kwargs": {},
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        version = getattr(settings, "VERSION_FLAG", "v1")
        cron_field_component = "VCronField" if version == "v2" else "VTextField"
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VCard",
                        "props": {"variant": "flat", "class": "mb-4"},
                        "content": [{
                            "component": "VCardItem",
                            "content": [
                                {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mb-3"}, "text": "🟢 基本配置"},
                                {"component": "VRow", "content": [
                                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "notify", "label": "发送通知"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "onlyonce", "label": "保存后执行一次"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "random_signin", "label": "随机鸡腿签到"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "use_flaresolverr", "label": "使用 FlareSolverr"}}]},
                                ]},
                            ],
                        }],
                    },
                    {
                        "component": "VCard",
                        "props": {"variant": "flat", "class": "mb-4"},
                        "content": [{
                            "component": "VCardItem",
                            "content": [
                                {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mb-3"}, "text": "🍪 Cookie 与账号"},
                                {"component": "VRow", "content": [
                                    {"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VTextarea", "props": {"model": "cookie", "label": "NodeSeek Cookie", "rows": 3, "placeholder": "登录 NodeSeek 后从浏览器 Network 请求头复制 Cookie", "auto-grow": True}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "member_id", "label": "成员ID（可选，用于获取用户信息）", "placeholder": "可填纯数字 26589，或完整空间链接 https://www.nodeseek.com/space/26589"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "base_url", "label": "站点地址", "placeholder": "https://www.nodeseek.com"}}]},
                                ]},
                            ],
                        }],
                    },
                    {
                        "component": "VCard",
                        "props": {"variant": "flat", "class": "mb-4"},
                        "content": [{
                            "component": "VCardItem",
                            "content": [
                                {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mb-3"}, "text": "🌐 网络与反代"},
                                {"component": "VRow", "content": [
                                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VSwitch", "props": {"model": "use_proxy", "label": "使用代理"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "proxy_url", "label": "代理地址", "placeholder": "http://127.0.0.1:7890"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "timeout", "label": "请求超时（秒）", "type": "number", "placeholder": "30"}}]},
                                    {"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VTextField", "props": {"model": "flaresolverr_url", "label": "FlareSolverr API 地址", "placeholder": "http://127.0.0.1:8191/v1", "hint": "遇到 Cloudflare 验证时开启；需填写完整 /v1 路径", "persistent-hint": True}}]},
                                ]},
                            ],
                        }],
                    },
                    {
                        "component": "VCard",
                        "props": {"variant": "tonal", "class": "mb-2"},
                        "content": [{
                            "component": "VCardItem",
                            "content": [
                                {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mb-3"}, "text": "⏰ 定时与重试"},
                                {"component": "VRow", "content": [
                                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": cron_field_component, "props": {"model": "cron", "label": "定时任务", "placeholder": "20 8 * * *"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "retry_count", "label": "失败重试次数", "type": "number", "placeholder": "3"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "retry_interval_minutes", "label": "重试间隔（分钟）", "type": "number", "placeholder": "5"}}]},
                                    {"component": "VCol", "props": {"cols": 12}, "content": [
                                        {"component": "div", "props": {"class": "text-body-2 text-medium-emphasis mt-1"}, "text": "💡 Cookie 是签到必需项；成员ID只用于拉取用户信息。遇到 Cloudflare 时建议开启 FlareSolverr；开启后签到和用户信息都会复用同一个 FlareSolverr 浏览器会话。"}
                                    ]},
                                ]},
                            ],
                        }],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cron": "20 8 * * *",
            "cookie": "",
            "member_id": "",
            "base_url": "https://www.nodeseek.com",
            "random_signin": True,
            "use_proxy": False,
            "proxy_url": "",
            "timeout": 30,
            "use_flaresolverr": False,
            "flaresolverr_url": "http://127.0.0.1:8191/v1",
            "retry_count": 3,
            "retry_interval_minutes": 5,
        }

    def get_page(self) -> List[dict]:
        last_result = self.get_data(self._last_result_key) or {}
        history = self.get_data(self._history_key) or []
        daily_results = self.get_data(self._daily_results_key) or []
        user_info = self.get_data(self._user_info_key) or {}
        if not last_result and not history and not daily_results and not user_info:
            return [{"component": "div", "text": "暂无数据", "props": {"class": "text-center"}}]

        history = sorted(history, key=lambda x: x.get("executed_at", ""), reverse=True) if isinstance(history, list) else []
        daily_results = sorted(daily_results, key=lambda x: x.get("date", ""), reverse=True) if isinstance(daily_results, list) else []

        def _chip(text: Any, color: str) -> Dict[str, Any]:
            return {"component": "VChip", "props": {"size": "x-small", "variant": "tonal", "color": color}, "text": str(text or "-")}

        def _status_chip(text: Any) -> Dict[str, Any]:
            value = str(text or "-")
            if "失败" in value or "失效" in value or "阻断" in value:
                return _chip(value, "error")
            if "未" in value:
                return _chip(value, "warning")
            if "已签到" in value:
                return _chip(value, "info")
            if "成功" in value:
                return _chip(value, "success")
            return _chip(value, "primary")

        def _metric_card(label: str, value: Any, color: str) -> Dict[str, Any]:
            color_map = {
                "primary": ("rgba(25,118,210,.08)", "rgba(25,118,210,.22)", "#1565C0"),
                "success": ("rgba(46,125,50,.08)", "rgba(46,125,50,.22)", "#2E7D32"),
                "warning": ("rgba(245,124,0,.10)", "rgba(245,124,0,.24)", "#E65100"),
                "secondary": ("rgba(123,31,162,.08)", "rgba(123,31,162,.22)", "#6A1B9A"),
            }
            bg, border, text_color = color_map.get(color, color_map["primary"])
            return {
                "component": "VCol",
                "props": {"cols": 6, "sm": 4, "md": 2},
                "content": [{
                    "component": "div",
                    "props": {"style": f"background:{bg};border:1px solid {border};border-radius:10px;padding:7px 10px;min-height:54px;"},
                    "content": [
                        {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": label},
                        {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold text-truncate", "style": f"color:{text_color};"}, "text": str(value if value not in (None, "") else "-")},
                    ],
                }],
            }

        user_name = user_info.get("member_name") or last_result.get("member_name") or "NodeSeek"
        page = [{
            "component": "VCard",
            "props": {"variant": "flat", "class": "mb-3"},
            "content": [{
                "component": "VCardText",
                "props": {"class": "py-3"},
                "content": [
                    {"component": "div", "props": {"class": "d-flex align-center justify-space-between mb-2"}, "content": [
                        {"component": "div", "content": [
                            {"component": "div", "props": {"class": "text-subtitle-1 font-weight-bold"}, "text": user_name},
                            {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": f"最近执行：{last_result.get('executed_at', '-')}"},
                        ]},
                        {"component": "VChip", "props": {"size": "small", "variant": "tonal", "color": "primary"}, "text": f"等级 {user_info.get('rank', last_result.get('rank', '-'))}"},
                    ]},
                    {"component": "VRow", "props": {"dense": True}, "content": [
                        _metric_card("结果", last_result.get("result_label", "-"), "primary"),
                        _metric_card("鸡腿收益", last_result.get("reward_coin", "-"), "success"),
                        _metric_card("鸡腿总数", user_info.get("coin", last_result.get("coin", "-")), "warning"),
                        _metric_card("主题", user_info.get("nPost", "-"), "secondary"),
                        _metric_card("评论", user_info.get("nComment", "-"), "secondary"),
                        _metric_card("触发方式", last_result.get("source_text", "-"), "primary"),
                    ]},
                    {"component": "div", "props": {"class": "text-body-2 text-medium-emphasis mt-2"}, "text": f"签到提示：{last_result.get('message', '-')}"},
                ],
            }],
        }]

        if daily_results:
            page.append({
                "component": "VCard",
                "props": {"variant": "flat", "class": "mb-3"},
                "content": [
                    {"component": "VCardTitle", "props": {"class": "text-subtitle-1 py-2"}, "text": f"📅 每日签到记录（共 {len(daily_results)} 天）"},
                    {"component": "VTable", "props": {"density": "compact", "hover": True}, "content": [
                        {"component": "thead", "content": [{"component": "tr", "content": [
                            {"component": "th", "props": {"style": "text-align:center; width: 18%;"}, "text": "日期"},
                            {"component": "th", "props": {"style": "text-align:center; width: 16%;"}, "text": "结果"},
                            {"component": "th", "props": {"style": "text-align:center; width: 16%;"}, "text": "鸡腿收益"},
                            {"component": "th", "props": {"style": "text-align:center; width: 18%;"}, "text": "触发"},
                            {"component": "th", "props": {"style": "text-align:center; width: 32%;"}, "text": "提示"},
                        ]}]},
                        {"component": "tbody", "content": [{"component": "tr", "content": [
                            {"component": "td", "props": {"style": "text-align:center;"}, "text": item.get("date", "-")},
                            {"component": "td", "props": {"style": "text-align:center;"}, "content": [_status_chip(item.get("signin_status") or item.get("result_label", "-"))]},
                            {"component": "td", "props": {"style": "text-align:center;"}, "text": item.get("reward_coin", "-")},
                            {"component": "td", "props": {"style": "text-align:center;"}, "text": item.get("source_text", "-")},
                            {"component": "td", "props": {"style": "text-align:center;"}, "text": str(item.get("message", "-") or "-")[:48]},
                        ]} for item in daily_results[:90]]},
                    ]},
                ],
            })

        if history:
            page.append({
                "component": "VCard",
                "props": {"variant": "flat", "class": "mb-3"},
                "content": [
                    {"component": "VCardTitle", "props": {"class": "text-subtitle-1 py-2"}, "text": f"🗂️ 执行流水（共 {len(history)} 条）"},
                    {"component": "VTable", "props": {"density": "compact", "hover": True}, "content": [
                        {"component": "thead", "content": [{"component": "tr", "content": [
                            {"component": "th", "props": {"style": "text-align:center; width: 24%;"}, "text": "时间"},
                            {"component": "th", "props": {"style": "text-align:center; width: 14%;"}, "text": "触发"},
                            {"component": "th", "props": {"style": "text-align:center; width: 14%;"}, "text": "签到"},
                            {"component": "th", "props": {"style": "text-align:center; width: 14%;"}, "text": "鸡腿"},
                            {"component": "th", "props": {"style": "text-align:center; width: 34%;"}, "text": "消息"},
                        ]}]},
                        {"component": "tbody", "content": [{"component": "tr", "content": [
                            {"component": "td", "props": {"style": "text-align:center;"}, "text": item.get("executed_at", "-")},
                            {"component": "td", "props": {"style": "text-align:center;"}, "content": [_chip(item.get("source_text") or self._source_text(item.get("source")), "info")]},
                            {"component": "td", "props": {"style": "text-align:center;"}, "content": [_status_chip(item.get("signin_status", "-"))]},
                            {"component": "td", "props": {"style": "text-align:center;"}, "text": item.get("reward_coin", "-")},
                            {"component": "td", "props": {"style": "text-align:center;"}, "text": str(item.get("message", "-") or "-")[:50]},
                        ]} for item in history[:30]]},
                    ]},
                ],
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
                return None
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
        if event.event_data.get("action") != "nodeseek_signin":
            return
        self.run_once(source="command")

    def _log_step(self, message: str):
        logger.info(f"{self.plugin_name}：{message}")

    def _source_text(self, source: Any) -> str:
        source_value = str(source or "").strip().lower()
        if source_value in {"cron", "scheduler", "schedule", "service", "auto", "automatic", "retry"}:
            return "自动触发"
        if source_value in {"command", "api", "manual", "onlyonce"}:
            return "手动触发"
        return "自动触发" if "cron" in source_value or "sched" in source_value else "手动触发"

    def _now_text(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _normalize_member_id(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        space_match = re.search(r"/space/(\d+)", text)
        if space_match:
            return space_match.group(1)
        digit_match = re.search(r"\d+", text)
        return digit_match.group(0) if digit_match else ""

    @staticmethod
    def _chrome_major_from_ua(user_agent: str) -> str:
        match = re.search(r"(?:Chrome|Chromium|CriOS)/(\d+)", user_agent or "")
        return match.group(1) if match else ""

    @classmethod
    def _sec_ch_ua_for_user_agent(cls, user_agent: str) -> str:
        major = cls._chrome_major_from_ua(user_agent) or "134"
        return f'"Chromium";v="{major}", "Not:A-Brand";v="24", "Google Chrome";v="{major}"'

    @classmethod
    def _apply_user_agent_headers(cls, headers: Dict[str, str], user_agent: str):
        if not user_agent:
            return
        headers["User-Agent"] = user_agent
        headers["Sec-CH-UA"] = cls._sec_ch_ua_for_user_agent(user_agent)

    def _headers(
        self,
        referer: str = "",
        cookie: Optional[str] = None,
        user_agent: str = "",
        include_cookie: bool = True,
    ) -> Dict[str, str]:
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": self._base_url,
            "Referer": referer or f"{self._base_url}/board",
            "Sec-CH-UA": '"Chromium";v="134", "Not:A-Brand";v="24", "Google Chrome";v="134"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
        }
        if include_cookie:
            headers["Cookie"] = self._cookie if cookie is None else cookie
        self._apply_user_agent_headers(headers, user_agent)
        return headers

    def _proxies(self) -> Dict[str, str]:
        if not self._use_proxy or not self._proxy_url:
            return {}
        return {"http": self._proxy_url, "https": self._proxy_url}

    def _fs_proxy(self) -> Optional[Dict[str, str]]:
        if not self._use_proxy or not self._proxy_url:
            return None
        return {"url": self._proxy_url}

    @staticmethod
    def _looks_like_cf(text: str, status_code: int = 200) -> bool:
        lowered = (text or "").lower()
        challenge_markers = (
            "cf-chl",
            "/cdn-cgi/challenge-platform/",
            "challenge-platform",
            "just a moment",
            "checking your browser",
            "verify you are human",
            "attention required! | cloudflare",
        )
        if any(marker in lowered for marker in challenge_markers):
            return True
        return status_code in {403, 503} and "cloudflare" in lowered

    def _merge_cookie_string(self, base_cookie: str, new_cookies: List[dict]) -> str:
        pairs: Dict[str, str] = {}
        for part in (base_cookie or "").split(";"):
            if "=" in part:
                key, value = part.strip().split("=", 1)
                if key:
                    pairs[key] = value
        for item in new_cookies or []:
            name = item.get("name")
            value = item.get("value")
            if name and value is not None:
                pairs[str(name)] = str(value)
        return "; ".join(f"{k}={v}" for k, v in pairs.items())

    def _cookie_string_to_fs_cookies(self, cookie: str) -> List[Dict[str, str]]:
        domain = urlparse(self._base_url).hostname or "www.nodeseek.com"
        cookies: List[Dict[str, str]] = []
        for part in (cookie or "").split(";"):
            if "=" not in part:
                continue
            name, value = part.strip().split("=", 1)
            if not name:
                continue
            cookies.append({"name": name, "value": value, "domain": domain, "path": "/"})
        return cookies

    @staticmethod
    def _safe_cookie_names(base_cookie: str, new_cookies: List[dict] = None) -> List[str]:
        names = set()
        for part in (base_cookie or "").split(";"):
            if "=" in part:
                name = part.strip().split("=", 1)[0]
                if name:
                    names.add(name)
        for item in new_cookies or []:
            name = item.get("name")
            if name:
                names.add(str(name))
        return sorted(names)

    @staticmethod
    def _cookie_names_text(cookie_names: List[str]) -> str:
        if not cookie_names:
            return "-"
        text = ",".join(cookie_names[:8])
        if len(cookie_names) > 8:
            text += f",+{len(cookie_names) - 8}"
        return text

    def _fs_call(self, payload: Dict[str, Any], timeout: Optional[int] = None) -> Dict[str, Any]:
        resp = requests.post(self._flaresolverr_url, json=payload, timeout=timeout or max(self._timeout * 4, 90))
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "ok":
            raise RuntimeError(data.get("message") or "FlareSolverr 调用失败")
        return data

    def _fs_create_runtime_session(self):
        if self._runtime_fs_session:
            return
        self._runtime_fs_session = f"nodeseek-{int(time.time())}-{random.randint(1000, 9999)}"
        payload = {"cmd": "sessions.create", "session": self._runtime_fs_session}
        proxy = self._fs_proxy()
        if proxy:
            payload["proxy"] = proxy
        self._fs_call(payload, timeout=30)
        self._log_step(f"FlareSolverr 会话已创建：{self._runtime_fs_session}")

    def _fs_destroy_runtime_session(self):
        sid = self._runtime_fs_session
        self._runtime_fs_session = ""
        self._runtime_fs_warmed = False
        self._runtime_cookie = ""
        self._runtime_user_agent = ""
        if not sid:
            return
        try:
            self._fs_call({"cmd": "sessions.destroy", "session": sid}, timeout=30)
            self._log_step(f"FlareSolverr 会话已销毁：{sid}")
        except Exception as e:
            self._log_step(f"FlareSolverr 会话销毁失败（已忽略）：{e}")

    def _update_runtime_from_fs_solution(self, solution: Dict[str, Any], label: str):
        solution_cookies = solution.get("cookies") or []
        if solution_cookies:
            self._runtime_cookie = self._merge_cookie_string(self._runtime_cookie or self._cookie, solution_cookies)
        elif not self._runtime_cookie:
            self._runtime_cookie = self._cookie
        user_agent = solution.get("userAgent") or solution.get("user_agent") or ""
        if user_agent:
            self._runtime_user_agent = user_agent
        cookie_names = self._safe_cookie_names(self._runtime_cookie or self._cookie, solution_cookies)
        self._log_step(
            f"FlareSolverr {label}完成："
            f"HTTP {solution.get('status') or '-'}，"
            f"Cookie 名称 {self._cookie_names_text(cookie_names)}，"
            f"UA Chrome/{self._chrome_major_from_ua(self._runtime_user_agent) or '未知'}"
        )

    def _fs_prepare_session(self):
        if self._runtime_fs_warmed:
            return
        self._fs_create_runtime_session()
        payload = {
            "cmd": "request.get",
            "session": self._runtime_fs_session,
            "url": f"{self._base_url}/board",
            "maxTimeout": max(self._timeout * 1000, 90000),
            "cookies": self._cookie_string_to_fs_cookies(self._cookie),
            "headers": self._headers(referer=f"{self._base_url}/board", include_cookie=False),
        }
        proxy = self._fs_proxy()
        if proxy:
            payload["proxy"] = proxy
        data = self._fs_call(payload)
        solution = data.get("solution") or {}
        self._update_runtime_from_fs_solution(solution, "预热")
        text = solution.get("response") or ""
        if self._looks_like_cf(text, int(solution.get("status") or 200)):
            raise RuntimeError("FlareSolverr 预热仍返回 Cloudflare 验证页，请检查 FlareSolverr/代理是否可正常过盾")
        self._runtime_fs_warmed = True

    def _fs_request_json(self, method: str, url: str, referer: str) -> Dict[str, Any]:
        self._fs_prepare_session()
        method = method.upper()
        payload: Dict[str, Any] = {
            "cmd": "request.post" if method == "POST" else "request.get",
            "session": self._runtime_fs_session,
            "url": url,
            "maxTimeout": max(self._timeout * 1000, 90000),
            "cookies": self._cookie_string_to_fs_cookies(self._runtime_cookie or self._cookie),
            "headers": self._headers(
                referer=referer,
                user_agent=self._runtime_user_agent,
                include_cookie=False,
            ),
        }
        if method == "POST":
            payload["postData"] = ""
        proxy = self._fs_proxy()
        if proxy:
            payload["proxy"] = proxy
        data = self._fs_call(payload)
        solution = data.get("solution") or {}
        self._update_runtime_from_fs_solution(solution, method)
        status = int(solution.get("status") or 200)
        text = solution.get("response") or ""
        if self._looks_like_cf(text, status):
            raise RuntimeError("请求被 Cloudflare 阻断：FlareSolverr 浏览器会话也未通过，请检查 FlareSolverr 版本或代理质量")
        return self._parse_json_response(text, status)

    @staticmethod
    def _parse_json_response(text: str, status: int = 200) -> Dict[str, Any]:
        try:
            return json.loads(text)
        except Exception:
            pre_match = re.search(r"<pre[^>]*>(.*?)</pre>", text or "", re.I | re.S)
            if pre_match:
                try:
                    return json.loads(html.unescape(pre_match.group(1)).strip())
                except Exception:
                    pass
            start = (text or "").find("{")
            end = (text or "").rfind("}")
            if 0 <= start < end:
                try:
                    return json.loads(text[start:end + 1])
                except Exception:
                    pass
            snippet = re.sub(r"\s+", " ", text[:160]).strip()
            raise RuntimeError(f"FlareSolverr 返回非 JSON 响应({status})：{snippet or '空响应'}")

    def _request_json(self, method: str, url: str, headers: Dict[str, str]) -> Tuple[Dict[str, Any], str, int]:
        resp = requests.request(method, url, headers=headers, proxies=self._proxies(), timeout=self._timeout)
        text = resp.text or ""
        if self._looks_like_cf(text, resp.status_code):
            raise RuntimeError("请求被 Cloudflare 阻断，请开启 FlareSolverr 或配置代理")
        resp.raise_for_status()
        return resp.json(), text, resp.status_code

    def _sign_in(self) -> Dict[str, Any]:
        url = f"{self._base_url}/api/attendance?random={'true' if self._random_signin else 'false'}"
        referer = f"{self._base_url}/board"
        if self._use_flaresolverr:
            self._log_step("FlareSolverr 模式：使用同一浏览器会话预热并执行签到")
            return self._fs_request_json("POST", url, referer)
        headers = self._headers(referer=referer)
        data, _, _ = self._request_json("POST", url, headers)
        return data

    def _get_user_info(self) -> Dict[str, Any]:
        if not self._member_id:
            return {}
        url = f"{self._base_url}/api/account/getInfo/{self._member_id}?readme=1"
        referer = f"{self._base_url}/space/{self._member_id}"
        if self._use_flaresolverr:
            data = self._fs_request_json("GET", url, referer)
        else:
            headers = self._headers(referer=referer)
            data, _, _ = self._request_json("GET", url, headers)
        if not data.get("success"):
            raise RuntimeError(data.get("message") or "用户信息获取失败")
        detail = data.get("detail") or {}
        return {
            "member_id": detail.get("member_id") or self._member_id,
            "member_name": detail.get("member_name"),
            "rank": detail.get("rank"),
            "coin": detail.get("coin"),
            "nPost": detail.get("nPost"),
            "nComment": detail.get("nComment"),
            "created_at_str": detail.get("created_at_str"),
        }

    @staticmethod
    def _reward_from_message(message: str) -> Any:
        match = re.search(r"(\d+)\s*个?鸡腿", message or "")
        if match:
            try:
                return int(match.group(1))
            except Exception:
                return match.group(1)
        return "-"

    @staticmethod
    def _already_signed(message: str) -> bool:
        return any(keyword in (message or "") for keyword in ["已完成签到", "已签到", "重复", "请勿重复", "今天已"])

    def _save_result(self, result: Dict[str, Any]):
        history = self.get_data(self._history_key) or []
        if not isinstance(history, list):
            history = []
        result_day = str(result.get("executed_at") or "")[:10]
        result_source = str(result.get("source") or "").strip().lower()
        result_label = str(result.get("result_label") or "")
        auto_sources = {"cron", "scheduler", "schedule", "service", "auto", "automatic", "retry"}
        if result_day and result_source == "retry" and result_label in {"成功", "已签到"}:
            history = [
                item for item in history
                if not (
                    str(item.get("executed_at") or "").startswith(result_day)
                    and str(item.get("source") or "").strip().lower() in auto_sources
                    and str(item.get("result_label") or "") == "失败"
                )
            ]
        history.append(result)
        history = sorted(history, key=lambda x: x.get("executed_at", ""), reverse=True)[:30]
        self.save_data(self._history_key, history)
        self.save_data(self._last_result_key, result)

        daily_results = self.get_data(self._daily_results_key) or []
        if not isinstance(daily_results, list):
            daily_results = []
        if result_day:
            daily_item = {
                "date": result_day,
                "executed_at": result.get("executed_at"),
                "result_label": result.get("result_label"),
                "signin_status": result.get("signin_status"),
                "message": result.get("message"),
                "reward_coin": result.get("reward_coin", "-"),
                "source": result.get("source"),
                "source_text": result.get("source_text"),
            }
            priority = {"成功": 3, "已签到": 2, "失败": 1}
            merged = []
            replaced = False
            for item in daily_results:
                if item.get("date") != result_day:
                    merged.append(item)
                    continue
                old_priority = priority.get(str(item.get("result_label") or ""), 0)
                new_priority = priority.get(str(daily_item.get("result_label") or ""), 0)
                merged.append(daily_item if new_priority >= old_priority else item)
                replaced = True
            if not replaced:
                merged.append(daily_item)
            daily_results = sorted(merged, key=lambda x: x.get("date", ""), reverse=True)[:90]
            self.save_data(self._daily_results_key, daily_results)

    def _notify_text(self, result: Dict[str, Any]) -> str:
        lines = [
            "🥚 NodeSeek 签到通知",
            "━━━━━━━━━━",
            f"⏰ 执行时间：{result.get('executed_at', '-')}",
            f"🚦 触发方式：{result.get('source_text') or self._source_text(result.get('source'))}",
            f"📝 签到状态：{result.get('signin_status', '-')}",
            f"💬 提示信息：{result.get('message', '-')}",
            f"🍗 鸡腿收益：{result.get('reward_coin', '-')}",
        ]
        if result.get("member_name"):
            lines.extend([
                f"👤 用户：{result.get('member_name')}",
                f"🏅 等级：{result.get('rank', '-')}",
                f"🍗 鸡腿总数：{result.get('coin', '-')}",
                f"💬 主题/评论：{result.get('nPost', '-')}/{result.get('nComment', '-')}",
            ])
        elif self._member_id:
            lines.append("👤 用户信息：获取失败，请检查成员ID或网络")
        return "\n".join(lines)

    def run_once(self, source: str = "manual"):
        steps: List[str] = []
        trigger_text = self._source_text(source)
        if str(source).strip().lower() == "cron":
            delay_seconds = random.randint(60, 1800)
            self._log_step(f"随机延时 {delay_seconds // 60} 分 {delay_seconds % 60} 秒后开始执行（{source}）")
            steps.append(f"⏳ 定时任务随机延时 {delay_seconds // 60} 分 {delay_seconds % 60} 秒")
            time.sleep(delay_seconds)

        result = {
            "executed_at": self._now_text(),
            "source": source,
            "source_text": trigger_text,
            "signin_status": "未开始",
            "result_label": "执行中",
            "message": "",
            "reward_coin": "-",
            "steps": steps,
            "finished": False,
            "use_flaresolverr": self._use_flaresolverr,
            "proxy_used": self._proxy_url if self._use_proxy and self._proxy_url else "未启用",
        }

        if not self._cookie:
            steps.append("❌ 未配置 Cookie，终止执行")
            result.update({"signin_status": "未执行", "result_label": "失败", "message": "未配置 Cookie", "finished": True})
            self._save_result(result)
            if self._notify:
                self.post_message(mtype=NotificationType.Plugin, title=f"【{self.plugin_name}】", text=self._notify_text(result))
            return result

        try:
            steps.append("🚀 开始执行 NodeSeek 签到")
            self._log_step(f"开始执行 NodeSeek 签到（{trigger_text}）")
            sign_data = self._sign_in()
            message = sign_data.get("message") or "签到状态未知"
            reward = self._reward_from_message(message)
            success = bool(sign_data.get("success"))
            already = self._already_signed(message)
            result.update({
                "message": message,
                "reward_coin": reward,
                "signin_status": "成功" if success else ("今日已签到" if already else "失败"),
                "result_label": "成功" if success else ("已签到" if already else "失败"),
                "finished": True,
            })
            steps.append(f"📝 签到返回：{message}")
            self._log_step(f"签到返回：{message}")

            if self._member_id:
                try:
                    info = self._get_user_info()
                    if info:
                        result.update(info)
                        self.save_data(self._user_info_key, info)
                        steps.append(f"👤 已刷新用户信息：{info.get('member_name') or '-'} / 鸡腿 {info.get('coin', '-')}")
                except Exception as info_error:
                    steps.append(f"⚠️ 用户信息获取失败：{info_error}")
                    self._log_step(f"用户信息获取失败：{info_error}")

            self._save_result(result)
            if self._notify:
                self.post_message(mtype=NotificationType.Plugin, title=f"【{self.plugin_name}】", text=self._notify_text(result))
            self._handle_retry_after_result(result, "签到失败")
            return result
        except Exception as e:
            steps.append(f"💥 执行失败：{str(e)}")
            self._log_step(f"执行失败：{str(e)}")
            result.update({"signin_status": "失败", "result_label": "失败", "message": str(e), "finished": True})
            self._save_result(result)
            logger.error(f"Nodeseek签到自用执行失败：{str(e)}", exc_info=True)
            if self._notify:
                self.post_message(mtype=NotificationType.Plugin, title=f"【{self.plugin_name}】", text=self._notify_text(result))
            self._handle_retry_after_result(result, "签到失败（异常）")
            return result
        finally:
            self._fs_destroy_runtime_session()

    def _handle_retry_after_result(self, result: Dict[str, Any], reason: str):
        if result.get("result_label") in {"成功", "已签到"}:
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
                replace_existing=True,
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
        self._log_step(f"【重试 #{attempt}】触发执行")
        self.run_once(source="retry")

    def stop_service(self):
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._scheduler = None
