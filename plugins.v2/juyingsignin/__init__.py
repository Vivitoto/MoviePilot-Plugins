import random
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


class JuyingSignIn(_PluginBase):
    plugin_name = "聚影签到自用"
    plugin_desc = "自动登录聚影并完成每日签到。"
    plugin_icon = "https://raw.githubusercontent.com/Vivitoto/MoviePilot-Plugins/main/icons/juyingsignin.png"
    plugin_version = "1.0.1"
    plugin_author = "Vivitoto"
    author_url = "https://github.com/Vivitoto"
    plugin_config_prefix = "juyingsignin_"
    plugin_order = 22
    auth_level = 1

    _enabled = False
    _notify = True
    _onlyonce = False
    _cron = "10 9 * * *"
    _username = ""
    _password = ""
    _base_url = "https://share.huamucang.top"
    _proxy_url = ""
    _timeout = 20
    _timezone = "Asia/Shanghai"
    _retry_count = 3
    _retry_interval_minutes = 5

    _scheduler: Optional[BackgroundScheduler] = None
    _history_key = "history"
    _last_result_key = "last_result"
    _daily_results_key = "daily_results"
    _user_info_key = "user_info"
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
                self._base_url = str(config.get("base_url") or "https://share.huamucang.top").strip().rstrip("/")
                self._proxy_url = str(config.get("proxy_url") or "").strip()
                self._timeout = max(1, int(config.get("timeout") or 20))
                retry_count = config.get("retry_count", 3)
                retry_interval = config.get("retry_interval_minutes", 5)
                self._retry_count = max(0, int(3 if retry_count in (None, "") else retry_count))
                self._retry_interval_minutes = max(1, int(5 if retry_interval in (None, "") else retry_interval))

            if self._onlyonce:
                logger.info("聚影签到自用：保存配置后执行一次")
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    func=self.run_by_onlyonce,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="聚影签到自用",
                    kwargs={},
                )
                self._onlyonce = False
                self.__update_config()
                if self._scheduler.get_jobs():
                    self._scheduler.start()
        except Exception as e:
            logger.error(f"聚影签到自用初始化错误：{str(e)}", exc_info=True)

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
            "base_url": self._base_url,
            "proxy_url": self._proxy_url,
            "timeout": self._timeout,
            "timezone": self._timezone,
            "retry_count": self._retry_count,
            "retry_interval_minutes": self._retry_interval_minutes,
        })

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/juying_signin",
            "event": EventType.PluginAction,
            "desc": "执行聚影签到",
            "data": {"action": "juying_signin"},
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
            "id": "JuyingSignIn",
            "name": "聚影签到自用",
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
                                {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mb-3"}, "text": "👤 账号与站点"},
                                {"component": "VRow", "content": [
                                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "username", "label": "用户名"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "password", "label": "密码", "type": "password"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "base_url", "label": "站点地址", "placeholder": "https://share.huamucang.top"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "proxy_url", "label": "代理地址", "placeholder": "http://127.0.0.1:7890"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "timeout", "label": "请求超时（秒）", "type": "number", "placeholder": "20"}}]},
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
                                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": cron_field_component, "props": {"model": "cron", "label": "定时任务", "placeholder": "10 9 * * *"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "retry_count", "label": "失败重试次数", "type": "number", "placeholder": "3"}}]},
                                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "retry_interval_minutes", "label": "重试间隔（分钟）", "type": "number", "placeholder": "5"}}]},
                                    {"component": "VCol", "props": {"cols": 12}, "content": [
                                        {"component": "div", "props": {"class": "text-body-2 text-medium-emphasis mt-1"}, "text": "💡 自动定时首轮会随机延时 1-30 分钟；失败重试限当天内。"}
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
            "cron": "10 9 * * *",
            "username": "",
            "password": "",
            "base_url": "https://share.huamucang.top",
            "proxy_url": "",
            "timeout": 20,
            "timezone": "Asia/Shanghai",
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

        history = sorted(history, key=lambda x: x.get("executed_at", ""), reverse=True) if history else []
        daily_results = sorted(daily_results, key=lambda x: x.get("date", ""), reverse=True) if isinstance(daily_results, list) else []

        def _chip(text: Any, color: str) -> Dict[str, Any]:
            return {"component": "VChip", "props": {"size": "x-small", "variant": "tonal", "color": color}, "text": str(text or "-")}

        def _status_chip(text: Any) -> Dict[str, Any]:
            value = str(text or "-")
            if "失败" in value:
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
                "props": {"cols": 6, "sm": 3, "md": 3},
                "content": [{
                    "component": "div",
                    "props": {"style": f"background:{bg};border:1px solid {border};border-radius:10px;padding:7px 10px;min-height:54px;"},
                    "content": [
                        {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": label},
                        {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold text-truncate", "style": f"color:{text_color};"}, "text": str(value if value not in (None, "") else "-")},
                    ],
                }],
            }

        username = user_info.get("username") or last_result.get("username") or self._username or "未知用户"
        level_name = user_info.get("level_name") or last_result.get("level_name") or "-"
        page = [{
            "component": "VCard",
            "props": {"variant": "flat", "class": "mb-3"},
            "content": [{
                "component": "VCardText",
                "props": {"class": "py-3"},
                "content": [
                    {"component": "div", "props": {"class": "d-flex align-center justify-space-between mb-2"}, "content": [
                        {"component": "div", "content": [
                            {"component": "div", "props": {"class": "text-subtitle-1 font-weight-bold"}, "text": username},
                            {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": f"最近执行：{last_result.get('executed_at', '-')}"},
                        ]},
                        {"component": "VChip", "props": {"size": "small", "variant": "tonal", "color": "primary"}, "text": level_name},
                    ]},
                    {"component": "VRow", "props": {"dense": True}, "content": [
                        _metric_card("结果", last_result.get("result_label", "-"), "primary"),
                        _metric_card("本次积分", last_result.get("points_awarded", "-"), "success"),
                        _metric_card("累计天数", last_result.get("total_days", "-"), "warning"),
                        _metric_card("触发方式", last_result.get("source_text", "-"), "secondary"),
                    ]},
                    {"component": "div", "props": {"class": "text-body-2 text-medium-emphasis mt-2"}, "text": f"提示：{last_result.get('message', '-')}"},
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
                            {"component": "th", "props": {"style": "text-align:center; width: 16%;"}, "text": "积分"},
                            {"component": "th", "props": {"style": "text-align:center; width: 16%;"}, "text": "累计天数"},
                            {"component": "th", "props": {"style": "text-align:center; width: 34%;"}, "text": "提示"},
                        ]}]},
                        {"component": "tbody", "content": [{"component": "tr", "content": [
                            {"component": "td", "props": {"style": "text-align:center;"}, "text": item.get("date", "-")},
                            {"component": "td", "props": {"style": "text-align:center;"}, "content": [_status_chip(item.get("signin_status") or item.get("result_label", "-"))]},
                            {"component": "td", "props": {"style": "text-align:center;"}, "text": item.get("points_awarded", "-")},
                            {"component": "td", "props": {"style": "text-align:center;"}, "text": item.get("total_days", "-")},
                            {"component": "td", "props": {"style": "text-align:center;"}, "text": str(item.get("message", "-") or "-")[:48]},
                        ]} for item in daily_results[:60]]},
                    ]},
                ],
            })

        if history:
            page.append({
                "component": "VCard",
                "props": {"variant": "flat", "class": "mb-3"},
                "content": [
                    {"component": "VCardTitle", "props": {"class": "text-subtitle-1 py-2"}, "text": f"🗂️ 执行记录（共 {len(history)} 条）"},
                    {"component": "VTable", "props": {"density": "compact", "hover": True}, "content": [
                        {"component": "thead", "content": [{"component": "tr", "content": [
                            {"component": "th", "props": {"style": "text-align:center; width: 24%;"}, "text": "时间"},
                            {"component": "th", "props": {"style": "text-align:center; width: 14%;"}, "text": "触发"},
                            {"component": "th", "props": {"style": "text-align:center; width: 12%;"}, "text": "登录"},
                            {"component": "th", "props": {"style": "text-align:center; width: 14%;"}, "text": "签到"},
                            {"component": "th", "props": {"style": "text-align:center; width: 14%;"}, "text": "积分/天数"},
                            {"component": "th", "props": {"style": "text-align:center; width: 22%;"}, "text": "消息"},
                        ]}]},
                        {"component": "tbody", "content": [{"component": "tr", "content": [
                            {"component": "td", "props": {"style": "text-align:center;"}, "text": item.get("executed_at", "-")},
                            {"component": "td", "props": {"style": "text-align:center;"}, "content": [_chip(item.get("source_text") or self._source_text(item.get("source")), "info")]},
                            {"component": "td", "props": {"style": "text-align:center;"}, "content": [_status_chip(item.get("login_status", "-"))]},
                            {"component": "td", "props": {"style": "text-align:center;"}, "content": [_status_chip(item.get("signin_status", "-"))]},
                            {"component": "td", "props": {"style": "text-align:center;"}, "text": f"{item.get('points_awarded', '-')}/{item.get('total_days', '-')}"},
                            {"component": "td", "props": {"style": "text-align:center;"}, "text": str(item.get("message", "-") or "-")[:40]},
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
        if event.event_data.get("action") != "juying_signin":
            return
        self.run_once(source="command")

    def _session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
        })
        if self._proxy_url:
            session.proxies.update({"http": self._proxy_url, "https": self._proxy_url})
        return session

    def _api_url(self, path: str) -> str:
        return f"{self._base_url.rstrip('/')}/{path.lstrip('/')}"

    def _login(self, session: requests.Session) -> Tuple[str, Dict[str, Any]]:
        try:
            resp = session.post(
                self._api_url("/api/app/login/"),
                json={"username": self._username, "password": self._password},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except RequestException as e:
            raise RuntimeError(f"登录请求失败：{e}") from e
        except ValueError as e:
            raise RuntimeError("登录响应不是有效 JSON") from e

        if data.get("status") != "success":
            raise RuntimeError(data.get("message") or f"登录失败：{data}")
        token = data.get("token")
        if not token:
            raise RuntimeError("登录成功但接口未返回 token")
        return str(token), data.get("user") or {}

    def _checkin(self, session: requests.Session, token: str) -> Dict[str, Any]:
        headers = {"x-app-user-token": token}
        try:
            resp = session.post(self._api_url("/api/app/checkin/do/"), headers=headers, timeout=self._timeout)
            resp.raise_for_status()
            return resp.json()
        except RequestException as e:
            raise RuntimeError(f"签到请求失败：{e}") from e
        except ValueError as e:
            raise RuntimeError("签到响应不是有效 JSON") from e

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
                "login_status": result.get("login_status"),
                "signin_status": result.get("signin_status"),
                "message": result.get("message"),
                "points_awarded": result.get("points_awarded", 0),
                "total_days": result.get("total_days", 0),
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
            "🎉 聚影签到通知",
            "━━━━━━━━━━",
            f"⏰ 签到时间：{result.get('executed_at', '-')}",
            f"🚦 触发方式：{result.get('source_text') or self._source_text(result.get('source'))}",
            f"👤 签到用户：{result.get('username') or self._username or '-'}",
            f"🔐 登录状态：{result.get('login_status', '-')}",
            f"📝 签到状态：{result.get('signin_status', '-')}",
            f"💬 提示信息：{result.get('message', '-')}",
            f"💰 本次获得积分：{result.get('points_awarded', 0)} 分",
            f"📅 累计签到天数：{result.get('total_days', 0)} 天",
        ]
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
            "login_status": "未开始",
            "signin_status": "未开始",
            "result_label": "执行中",
            "message": "",
            "points_awarded": 0,
            "total_days": 0,
            "signed_today": False,
            "username": self._username,
            "level_name": "-",
            "proxy_used": self._proxy_url or "未配置",
            "steps": steps,
            "finished": False,
        }

        if not self._username or not self._password:
            steps.append("❌ 未配置账号密码，终止执行")
            result.update({"login_status": "失败", "signin_status": "未执行", "result_label": "失败", "message": "未配置账号密码", "finished": True})
            self._save_result(result)
            if self._notify:
                self.post_message(mtype=NotificationType.Plugin, title=f"【{self.plugin_name}】", text=self._notify_text(result))
            return result

        try:
            steps.append("🚀 开始执行聚影签到流程")
            self._log_step(f"开始执行聚影签到流程（{trigger_text}）")
            session = self._session()
            steps.append(f"🌐 已创建会话，代理：{self._proxy_url or '未配置'}")

            token, user_info = self._login(session)
            result["login_status"] = "成功"
            username = user_info.get("username") or self._username
            result["username"] = username
            result["level_name"] = user_info.get("level_name") or "-"
            self.save_data(self._user_info_key, {
                "username": username,
                "level_name": result["level_name"],
            })
            steps.append(f"🔐 登录成功：{username}")
            self._log_step(f"登录成功：{username}")

            checkin_json = self._checkin(session, token)
            status = str(checkin_json.get("status") or "").lower()
            message = checkin_json.get("message") or ("签到成功" if status == "success" else "签到失败或今日已签到")
            points = checkin_json.get("points_awarded", 0)
            total_days = checkin_json.get("my_total_days", 0)

            result.update({
                "message": message,
                "points_awarded": points,
                "total_days": total_days,
                "finished": True,
            })
            if status == "success":
                result.update({"signin_status": "成功", "result_label": "成功", "signed_today": True})
                steps.append(f"✅ 签到成功：+{points} 分，累计 {total_days} 天")
                self._log_step(f"签到成功：{message}")
            else:
                raw_text = str(checkin_json)
                already_signed = any(keyword in raw_text for keyword in ["已签到", "重复", "今日", "已经签到"])
                result.update({
                    "signin_status": "今日已签到" if already_signed else "失败",
                    "result_label": "已签到" if already_signed else "失败",
                    "signed_today": already_signed,
                })
                steps.append(f"⚠️ 签到未成功：{message}")
                self._log_step(f"签到未成功：{message}，返回：{checkin_json}")

            self._save_result(result)
            if self._notify:
                self.post_message(mtype=NotificationType.Plugin, title=f"【{self.plugin_name}】", text=self._notify_text(result))
            self._handle_retry_after_result(result, "签到失败")
            return result
        except Exception as e:
            steps.append(f"💥 执行失败：{str(e)}")
            self._log_step(f"执行失败：{str(e)}")
            if result["login_status"] == "未开始":
                result["login_status"] = "失败"
                result["signin_status"] = "未执行"
            else:
                result["signin_status"] = "失败"
            result.update({"result_label": "失败", "message": str(e), "finished": True})
            self._save_result(result)
            logger.error(f"聚影签到自用执行失败：{str(e)}", exc_info=True)
            if self._notify:
                self.post_message(mtype=NotificationType.Plugin, title=f"【{self.plugin_name}】", text=self._notify_text(result))
            self._handle_retry_after_result(result, "签到失败（异常）")
            return result

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
