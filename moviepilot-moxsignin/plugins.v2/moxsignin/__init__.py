import html
import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz
import requests
from apscheduler.triggers.cron import CronTrigger

from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType


class MoxSignIn(_PluginBase):
    plugin_name = "魔性签到抽奖"
    plugin_desc = "自动登录 mox.moxing.chat 执行每日签到抽奖并返回中奖信息。"
    plugin_icon = "Lucky_A.png"
    plugin_version = "0.1.0"
    plugin_author = "OpenClaw Assistant"
    author_url = "https://github.com"
    plugin_config_prefix = "moxsignin_"
    plugin_order = 20
    auth_level = 1

    _enabled: bool = False
    _notify: bool = True
    _onlyonce: bool = False
    _cron: str = "10 9 * * *"
    _username: str = ""
    _password: str = ""
    _proxy_url: str = "http://192.168.31.216:7890"
    _base_url: str = "https://mox.moxing.chat"
    _timeout: int = 20
    _timezone: str = "Asia/Shanghai"
    _remember: bool = True

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify", True))
            self._onlyonce = bool(config.get("onlyonce"))
            self._cron = config.get("cron") or "10 9 * * *"
            self._username = config.get("username") or ""
            self._password = config.get("password") or ""
            self._proxy_url = config.get("proxy_url") or "http://192.168.31.216:7890"
            self._base_url = (config.get("base_url") or "https://mox.moxing.chat").rstrip("/")
            self._timeout = int(config.get("timeout") or 20)
            self._timezone = config.get("timezone") or "Asia/Shanghai"
            self._remember = bool(config.get("remember", True))
            self.__update_config()

        if self._onlyonce:
            try:
                self.run_once()
            finally:
                self._onlyonce = False
                self.__update_config()

    def get_state(self) -> bool:
        return bool(self._enabled and self._username and self._password)

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
            "remember": self._remember,
        })

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/mox_signin",
            "event": EventType.PluginAction,
            "desc": "执行魔性签到抽奖",
            "category": "站点",
            "data": {
                "action": "mox_signin"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/run",
            "endpoint": self.api_run,
            "methods": ["GET"],
            "summary": "执行签到",
            "description": "手动执行 mox.moxing.chat 的签到抽奖流程"
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        if self.get_state() and self._cron:
            return [{
                "id": "MoxSignIn",
                "name": "魔性签到抽奖服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.run_once,
                "kwargs": {}
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VSwitch", "props": {"model": "notify", "label": "发送通知"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VSwitch", "props": {"model": "remember", "label": "保持登录偏好"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即运行一次"}}]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "username", "label": "用户名"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "password", "label": "密码", "type": "password"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VCronField", "props": {"model": "cron", "label": "执行周期", "placeholder": "5位cron表达式"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "proxy_url", "label": "代理地址"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "base_url", "label": "站点地址"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "timeout", "label": "超时秒数"}}]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "timezone", "label": "默认时区"}}]}
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "插件通过站点实际接口自动获取验证码并提交签到抽奖。若账号缺少时区设置，会先自动提交时区。前端另有签到卡介绍弹窗，但不影响后端自动化执行。"}}
                                ]
                            }
                        ]
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
            "proxy_url": "http://192.168.31.216:7890",
            "base_url": "https://mox.moxing.chat",
            "timeout": 20,
            "timezone": "Asia/Shanghai",
            "remember": True,
        }

    def get_page(self) -> List[dict]:
        return []

    def api_run(self):
        return self.run_once()

    @eventmanager.register(EventType.PluginAction)
    def remote_run(self, event: Event):
        event_data = event.event_data or {}
        if event_data.get("action") != "mox_signin":
            return
        self.run_once()

    def _session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "user-agent": "Mozilla/5.0",
            "accept": "application/json, text/plain, */*",
            "x-requested-with": "XMLHttpRequest",
        })
        if self._proxy_url:
            session.proxies.update({"http": self._proxy_url, "https": self._proxy_url})
        return session

    def _extract_csrf(self, html_text: str) -> str:
        match = re.search(r'csrf-token" content="([^"]+)"', html_text)
        if not match:
            raise ValueError("未找到 csrf token")
        return match.group(1)

    def _extract_page_data(self, html_text: str) -> Dict[str, Any]:
        match = re.search(r'data-page="(.*?)"', html_text)
        if not match:
            return {}
        raw = html.unescape(match.group(1))
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _captcha(self, session: requests.Session) -> Dict[str, str]:
        resp = session.get(f"{self._base_url}/api/forum/captcha/generate", timeout=self._timeout)
        resp.raise_for_status()
        data = resp.json()
        return {"captcha": data["image"], "captcha_key": data["key"]}

    def _login(self, session: requests.Session):
        login_page = session.get(f"{self._base_url}/login", timeout=self._timeout)
        login_page.raise_for_status()
        csrf = self._extract_csrf(login_page.text)
        session.headers.update({
            "x-csrf-token": csrf,
            "referer": f"{self._base_url}/login",
        })
        payload = self._captcha(session)
        payload.update({
            "username": self._username,
            "password": self._password,
            "sec": None,
            "secanswer": "",
            "remember": bool(self._remember),
        })
        resp = session.post(f"{self._base_url}/api/forum/login", json=payload, timeout=self._timeout)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise ValueError(data.get("message") or "登录失败")
        return data

    def _load_sign_page(self, session: requests.Session) -> Tuple[str, Dict[str, Any]]:
        resp = session.get(f"{self._base_url}/forum/sign", timeout=self._timeout)
        resp.raise_for_status()
        csrf = self._extract_csrf(resp.text)
        session.headers.update({
            "x-csrf-token": csrf,
            "referer": f"{self._base_url}/forum/sign",
        })
        page = self._extract_page_data(resp.text)
        props = page.get("props", {}) if isinstance(page, dict) else {}
        return resp.text, props

    def _ensure_timezone(self, session: requests.Session, props: Dict[str, Any]) -> Optional[str]:
        auth = props.get("auth", {}) if isinstance(props, dict) else {}
        user = auth.get("user") or {}
        timezone = user.get("timezone")
        if timezone:
            return None
        if not self._timezone:
            return "账号未设置时区，需要先手动选择时区"
        resp = session.post(f"{self._base_url}/api/forum/check-in/timezone/update", json={"timezone": self._timezone}, timeout=self._timeout)
        resp.raise_for_status()
        return f"已自动设置时区为 {self._timezone}"

    def _reward_from_props(self, props: Dict[str, Any], index: Any) -> Optional[Dict[str, Any]]:
        rewards = props.get("rewardsReal") or []
        try:
            idx = int(index)
        except Exception:
            return None
        if 0 <= idx < len(rewards):
            return rewards[idx]
        return None

    def run_once(self):
        if not self._username or not self._password:
            logger.error("魔性签到抽奖：未配置账号密码")
            return {"success": False, "message": "未配置账号密码"}
        session = self._session()
        popup_notes: List[str] = []
        try:
            self._login(session)
            _, props = self._load_sign_page(session)

            timezone_note = self._ensure_timezone(session, props)
            if timezone_note:
                popup_notes.append(timezone_note)
                _, props = self._load_sign_page(session)

            popup_notes.append("站点前端存在签到卡介绍弹窗，但仅由浏览器 localStorage 控制，不影响接口自动签到")

            if props.get("is_checked_in"):
                message = "今天已经签到过了"
                result = {
                    "success": True,
                    "message": message,
                    "signed": False,
                    "already_checked_in": True,
                    "popup_notes": popup_notes,
                }
                if self._notify:
                    self.post_message(title=f"【{self.plugin_name}】", mtype=NotificationType.Plugin, text=message)
                return result

            payload = self._captcha(session)
            resp = session.post(f"{self._base_url}/api/forum/check-in/sign", json=payload, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
            reward_index = (((data or {}).get("data") or {}).get("data"))
            reward = self._reward_from_props(props, reward_index)
            reward_text = None
            if reward:
                reward_text = reward.get("text") or reward.get("name")
            message = (((data or {}).get("data") or {}).get("message")) or data.get("message") or "签到完成"
            if reward_text and reward_text not in message:
                message = f"{message}；抽奖结果：{reward_text}"

            result = {
                "success": True,
                "message": message,
                "signed": True,
                "already_checked_in": False,
                "reward_index": reward_index,
                "reward": reward,
                "popup_notes": popup_notes,
            }
            if self._notify:
                extra = "\n" + "\n".join(f"- {x}" for x in popup_notes) if popup_notes else ""
                self.post_message(title=f"【{self.plugin_name}】", mtype=NotificationType.Plugin, text=f"{message}{extra}")
            logger.info(f"魔性签到抽奖成功：{message}")
            return result
        except Exception as e:
            logger.error(f"魔性签到抽奖失败：{e}")
            if self._notify:
                self.post_message(title=f"【{self.plugin_name}】", mtype=NotificationType.Plugin, text=f"执行失败：{e}")
            return {"success": False, "message": str(e), "popup_notes": popup_notes}
