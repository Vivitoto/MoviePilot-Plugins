import html
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pytz
import requests
from apscheduler.triggers.cron import CronTrigger
from requests import RequestException

from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType


class MoxSignIn(_PluginBase):
    plugin_name = "mox签到自用"
    plugin_desc = "自动登录魔性论坛签到。"
    plugin_icon = "moxsignin.png"
    plugin_version = "1.0.0"
    plugin_author = "Vivitoto"
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

    _history_key = "history"
    _last_result_key = "last_result"

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
                self.run_once(source="manual-config")
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
                "kwargs": {"source": "cron"}
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'remember', 'label': '保持登录'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '保存后执行一次'}}]},
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'username', 'label': '用户名'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'password', 'label': '密码', 'type': 'password'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'cron', 'label': 'Cron', 'placeholder': '10 9 * * *'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'proxy_url', 'label': '代理地址'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'base_url', 'label': '站点地址'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'timeout', 'label': '超时秒数'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'timezone', 'label': '时区'}}]},
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {'component': 'div', 'text': '说明：支持定时执行、保存后执行一次、远程命令 /mox_signin、API /run。'},
                                    {'component': 'div', 'text': '说明：若当天已签到，会记录结果但不重复请求签到接口。'},
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            'enabled': False,
            'notify': True,
            'onlyonce': False,
            'cron': '10 9 * * *',
            'username': '',
            'password': '',
            'proxy_url': 'http://192.168.31.216:7890',
            'base_url': 'https://mox.moxing.chat',
            'timeout': 20,
            'timezone': 'Asia/Shanghai',
            'remember': True,
        }

    def get_page(self) -> List[dict]:
        last = self.get_data(self._last_result_key) or {}
        history = self._history()
        recent = sorted(history.values(), key=lambda x: x.get('executed_at', ''), reverse=True)[:10]

        contents = []
        contents.append({
            'component': 'VCard',
            'content': [
                {'component': 'VCardTitle', 'text': '最近状态'},
                {'component': 'VCardText', 'text': f"最近一次执行时间：{last.get('executed_at') or '暂无'}"},
                {'component': 'VCardText', 'text': f"最近一次执行结果：{last.get('result_label') or '暂无'}"},
                {'component': 'VCardText', 'text': f"最近一次中奖信息：{last.get('reward_text') or '暂无'}"},
                {'component': 'VCardText', 'text': f"今日是否已签到：{'是' if (last.get('day') == self._today_key() and last.get('signed_today')) else '否'}"},
                {'component': 'VCardText', 'text': f"站点：{self._base_url}"},
                {'component': 'VCardText', 'text': f"代理：{last.get('proxy') or '未配置'}"},
                {'component': 'VCardText', 'text': f"最近说明：{last.get('message') or '暂无'}"},
            ]
        })

        if recent:
            history_text = []
            for item in recent:
                history_text.append(
                    f"{item.get('day') or '-'} | {item.get('executed_at') or '-'} | {item.get('source') or '-'} | 登录:{item.get('login_status') or '-'} | 签到:{item.get('signin_status') or '-'} | 奖励:{item.get('reward_text') or '-'} | 结果:{item.get('result_label') or '-'}"
                )
            contents.append({
                'component': 'VCard',
                'content': [
                    {'component': 'VCardTitle', 'text': '最近执行记录'},
                    {'component': 'VCardText', 'text': '\n'.join(history_text)}
                ]
            })
        else:
            contents.append({
                'component': 'div',
                'text': '暂无执行记录',
                'props': {'class': 'text-center'}
            })

        return contents

    def api_run(self):
        return self.run_once(source="api")

    @eventmanager.register(EventType.PluginAction)
    def remote_run(self, event: Event):
        event_data = event.event_data or {}
        if event_data.get("action") != "mox_signin":
            return
        self.run_once(source="command")

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
            raise ValueError("站点页面缺少 csrf token")
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
        try:
            resp = session.get(f"{self._base_url}/api/forum/captcha/generate", timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
        except RequestException as e:
            raise RuntimeError(f"获取验证码失败：{e}") from e
        if not data.get("key") or not data.get("image"):
            raise RuntimeError("获取验证码失败：接口未返回有效验证码")
        return {"captcha": data["image"], "captcha_key": data["key"]}

    def _login(self, session: requests.Session) -> Dict[str, Any]:
        try:
            login_page = session.get(f"{self._base_url}/login", timeout=self._timeout)
            login_page.raise_for_status()
        except RequestException as e:
            raise RuntimeError(f"打开登录页失败：{e}") from e
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
        try:
            resp = session.post(f"{self._base_url}/api/forum/login", json=payload, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
        except RequestException as e:
            raise RuntimeError(f"登录请求失败：{e}") from e
        if not data.get("success"):
            msg = data.get("message") or "账号密码错误或登录被拒绝"
            raise RuntimeError(f"登录失败：{msg}")
        return data

    def _load_sign_page(self, session: requests.Session) -> Tuple[str, Dict[str, Any]]:
        try:
            resp = session.get(f"{self._base_url}/forum/sign", timeout=self._timeout)
            resp.raise_for_status()
        except RequestException as e:
            raise RuntimeError(f"打开签到页失败：{e}") from e
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
            return "账号未设置时区，需要手动先选择时区"
        try:
            resp = session.post(
                f"{self._base_url}/api/forum/check-in/timezone/update",
                json={"timezone": self._timezone},
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except RequestException as e:
            raise RuntimeError(f"时区设置失败：{e}") from e
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

    def _today_key(self) -> str:
        return datetime.now(pytz.timezone("UTC")).strftime("%Y-%m-%d")

    def _now_text(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _history(self) -> Dict[str, Dict[str, Any]]:
        data = self.get_data(self._history_key) or {}
        return data if isinstance(data, dict) else {}

    def _save_history_item(self, result: Dict[str, Any]):
        history = self._history()
        day = result.get("day") or self._today_key()
        history[day] = result
        # keep last 30 days
        keys = sorted(history.keys(), reverse=True)[:30]
        history = {k: history[k] for k in keys}
        self.save_data(self._history_key, history)
        self.save_data(self._last_result_key, result)

    def _masked_proxy(self) -> str:
        return self._proxy_url or "未配置"

    def stop_service(self):
        pass

    def _notify_text(self, result: Dict[str, Any]) -> str:
        popup_notes = result.get("popup_notes") or []
        lines = [
            f"执行时间：{result.get('executed_at', '-')}",
            f"登录：{result.get('login_status', '-')}",
            f"签到：{result.get('signin_status', '-')}",
            f"抽中奖励：{result.get('reward_text', '无')}",
            f"结果：{result.get('message', '-')}",
        ]
        if popup_notes:
            lines.append("附加说明：")
            lines.extend([f"- {x}" for x in popup_notes])
        return "\n".join(lines)

    def run_once(self, source: str = "manual"):
        executed_at = self._now_text()
        day = self._today_key()
        result: Dict[str, Any] = {
            "day": day,
            "executed_at": executed_at,
            "source": source,
            "proxy": self._masked_proxy(),
            "login_status": "未开始",
            "signin_status": "未开始",
            "reward_text": "无",
            "signed_today": False,
            "popup_notes": [],
            "result_label": "执行中",
            "message": "",
        }

        if not self._username or not self._password:
            result.update({
                "success": False,
                "login_status": "失败",
                "signin_status": "未执行",
                "result_label": "失败",
                "message": "未配置账号密码",
            })
            self._save_history_item(result)
            logger.error("魔性签到抽奖：未配置账号密码")
            if self._notify:
                self.post_message(title=f"【{self.plugin_name}】", mtype=NotificationType.Plugin, text=self._notify_text(result))
            return result

        session = self._session()
        try:
            self._login(session)
            result["login_status"] = "成功"

            _, props = self._load_sign_page(session)
            timezone_note = self._ensure_timezone(session, props)
            if timezone_note:
                result["popup_notes"].append(timezone_note)
                _, props = self._load_sign_page(session)

            result["popup_notes"].append("站点前端存在签到介绍弹窗，但不影响接口自动签到")

            if props.get("is_checked_in"):
                result.update({
                    "success": True,
                    "signin_status": "今日已签到",
                    "signed_today": True,
                    "result_label": "已签到",
                    "message": "今天已经签到过了，本次不会重复请求签到接口",
                })
                self._save_history_item(result)
                logger.info("魔性签到抽奖：今天已经签到过了")
                if self._notify:
                    self.post_message(title=f"【{self.plugin_name}】", mtype=NotificationType.Plugin, text=self._notify_text(result))
                return result

            payload = self._captcha(session)
            try:
                resp = session.post(f"{self._base_url}/api/forum/check-in/sign", json=payload, timeout=self._timeout)
                resp.raise_for_status()
                data = resp.json()
            except RequestException as e:
                raise RuntimeError(f"签到请求失败：{e}") from e

            reward_index = (((data or {}).get("data") or {}).get("data"))
            reward = self._reward_from_props(props, reward_index)
            reward_text = None
            if reward:
                reward_text = reward.get("text") or reward.get("name")
            message = (((data or {}).get("data") or {}).get("message")) or data.get("message") or "签到完成"
            if reward_text and reward_text not in message:
                message = f"{message}；抽奖结果：{reward_text}"

            result.update({
                "success": True,
                "signin_status": "成功",
                "signed_today": True,
                "reward_index": reward_index,
                "reward": reward,
                "reward_text": reward_text or "未解析到奖励详情",
                "result_label": "成功",
                "message": message,
            })
            self._save_history_item(result)
            logger.info(f"魔性签到抽奖成功：{message}")
            if self._notify:
                self.post_message(title=f"【{self.plugin_name}】", mtype=NotificationType.Plugin, text=self._notify_text(result))
            return result
        except Exception as e:
            result.update({
                "success": False,
                "signin_status": result.get("signin_status") if result.get("signin_status") != "未开始" else "失败",
                "result_label": "失败",
                "message": str(e),
            })
            if result["login_status"] == "未开始":
                result["login_status"] = "失败"
                result["signin_status"] = "未执行"
            self._save_history_item(result)
            logger.error(f"魔性签到抽奖失败：{e}")
            if self._notify:
                self.post_message(title=f"【{self.plugin_name}】", mtype=NotificationType.Plugin, text=self._notify_text(result))
            return result
