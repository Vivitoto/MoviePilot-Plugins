import html
import json
import re
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


class MoxSignIn(_PluginBase):
    # 插件名称
    plugin_name = "Mox签到自用"
    # 插件描述
    plugin_desc = "自动登录魔性论坛签到。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/Vivitoto/MoviePilot-Plugins/main/icons/moxsignin.png"
    # 插件版本
    plugin_version = "0.0.5"
    # 插件作者
    plugin_author = "Vivitoto"
    # 作者主页
    author_url = "https://github.com/Vivitoto"
    # 插件配置项ID前缀
    plugin_config_prefix = "moxsignin_"
    # 加载顺序
    plugin_order = 20
    # 可使用的用户级别
    auth_level = 1

    _enabled = False
    _notify = True
    _onlyonce = False
    _cron = "10 9 * * *"
    _username = ""
    _password = ""
    _proxy_url = "http://192.168.31.216:7890"
    _base_url = "https://mox.moxing.chat"
    _timeout = 20
    _timezone = "Asia/Shanghai"
    _remember = True

    _scheduler: Optional[BackgroundScheduler] = None
    _history_key = "history"
    _last_result_key = "last_result"
    _current_trigger_type = "手动触发"
    _icon_url = "https://raw.githubusercontent.com/Vivitoto/MoviePilot-Plugins/main/icons/moxsignin.png"

    def init_plugin(self, config: dict = None):
        self.stop_service()

        try:
            if config:
                self._enabled = config.get("enabled", False)
                self._notify = config.get("notify", True)
                self._onlyonce = config.get("onlyonce", False)
                self._cron = config.get("cron") or "10 9 * * *"
                self._username = config.get("username") or ""
                self._password = config.get("password") or ""
                self._proxy_url = config.get("proxy_url") or "http://192.168.31.216:7890"
                self._base_url = "https://mox.moxing.chat"
                self._timeout = 20
                self._timezone = "Asia/Shanghai"
                self._remember = config.get("remember", True)

            if self._onlyonce:
                logger.info("Mox签到自用：保存配置后执行一次")
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    func=self.run_once,
                    trigger='date',
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="Mox签到自用"
                )
                self._onlyonce = False
                self.__update_config()
                if self._scheduler.get_jobs():
                    self._scheduler.start()
        except Exception as e:
            logger.error(f"Mox签到自用初始化错误：{str(e)}", exc_info=True)

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
            "remember": self._remember,
        })

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/mox_signin",
            "event": EventType.PluginAction,
            "desc": "执行魔性签到",
            "category": "站点",
            "data": {"action": "mox_signin"}
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
            "id": "MoxSignIn",
            "name": "Mox签到自用",
            "trigger": CronTrigger.from_crontab(self._cron),
            "func": self.run_once,
            "kwargs": {"source": "cron"}
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
                        'content': [
                            {
                                'component': 'VCardItem',
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 3},
                                                'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 3},
                                                'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 3},
                                                'content': [{'component': 'VSwitch', 'props': {'model': 'remember', 'label': '保持登录'}}]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 3},
                                                'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '保存后执行一次'}}]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VCard',
                        'props': {'variant': 'flat', 'class': 'mb-4'},
                        'content': [
                            {
                                'component': 'VCardItem',
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12},
                                                'content': [
                                                    {'component': 'div', 'props': {'class': 'text-subtitle-2 mb-3'}, 'text': '账号配置'}
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 6},
                                                'content': [{'component': 'VTextField', 'props': {'model': 'username', 'label': '用户名'}}]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 6},
                                                'content': [{'component': 'VTextField', 'props': {'model': 'password', 'label': '密码', 'type': 'password'}}]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12},
                                                'content': [
                                                    {'component': 'div', 'props': {'class': 'text-subtitle-2 mt-2 mb-3'}, 'text': '执行配置'}
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 6},
                                                'content': [{
                                                    'component': cron_field_component,
                                                    'props': {'model': 'cron', 'label': '定时任务', 'placeholder': '10 9 * * *'}
                                                }]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 6},
                                                'content': [{'component': 'VTextField', 'props': {'model': 'proxy_url', 'label': '代理地址', 'placeholder': 'http://192.168.31.216:7890'}}]
                                            },
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '支持 cron 定时、保存后执行一次、远程命令 /mox_signin、API /run；若当天已签到，会记录状态但不会重复签到。\n固定参数：站点地址 https://mox.moxing.chat，超时 20 秒，时区 Asia/Shanghai。代理地址可自定义，示例：http://192.168.31.216:7890。'
                                        }
                                    }
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
        last_result = self.get_data(self._last_result_key) or {}
        history = self.get_data(self._history_key) or []

        if not last_result and not history:
            return [{
                'component': 'div',
                'text': '暂无数据',
                'props': {'class': 'text-center'}
            }]

        history = sorted(history, key=lambda x: x.get('executed_at', ''), reverse=True) if history else []
        latest = history[0] if history else last_result
        steps = latest.get('steps') or []

        summary_rows = [
            ('🕒 最近一次执行时间', latest.get('executed_at', '暂无')),
            ('✅ 最近一次执行结果', latest.get('result_label', '暂无')),
            ('🎁 最近一次中奖信息', latest.get('reward_text', '暂无')),
            ('📅 今日是否已签到', '是' if latest.get('signed_today') else '否'),
            ('🚦 触发方式', '自动触发' if latest.get('source') == 'cron' else '手动触发'),
            ('🔐 登录状态', latest.get('login_status', '-')),
            ('✍️ 签到状态', latest.get('signin_status', '-')),
            ('🌐 代理地址', latest.get('proxy_used') or self._proxy_url or '未配置'),
            ('📍 站点地址', self._base_url),
            ('🏁 是否执行完毕', '是' if latest.get('finished') else '否'),
            ('📝 最近说明', latest.get('message', '暂无')),
        ]

        page = [{
            'component': 'VCard',
            'props': {'variant': 'flat', 'class': 'mb-4'},
            'content': [
                {'component': 'VCardTitle', 'text': '📊 执行概览'},
                {
                    'component': 'VTable',
                    'props': {'density': 'compact', 'hover': True},
                    'content': [
                        {
                            'component': 'tbody',
                            'content': [
                                {
                                    'component': 'tr',
                                    'content': [
                                        {'component': 'td', 'props': {'style': 'width: 220px; font-weight: 600;'}, 'text': label},
                                        {'component': 'td', 'text': value},
                                    ]
                                }
                                for label, value in summary_rows
                            ]
                        }
                    ]
                }
            ]
        }]

        if steps:
            page.append({
                'component': 'VCard',
                'props': {'variant': 'flat', 'class': 'mb-4'},
                'content': [
                    {'component': 'VCardTitle', 'text': '🧭 本次关键步骤'},
                    {
                        'component': 'VTable',
                        'props': {'density': 'compact'},
                        'content': [
                            {
                                'component': 'tbody',
                                'content': [
                                    {
                                        'component': 'tr',
                                        'content': [
                                            {'component': 'td', 'props': {'style': 'width: 60px; font-weight: 600;'}, 'text': str(idx + 1)},
                                            {'component': 'td', 'text': item},
                                        ]
                                    }
                                    for idx, item in enumerate(steps)
                                ]
                            }
                        ]
                    }
                ]
            })

        if history:
            page.append({
                'component': 'VCard',
                'props': {'variant': 'flat'},
                'content': [
                    {'component': 'VCardTitle', 'text': '🗂️ 最近执行记录（近10次）'},
                    {
                        'component': 'VTable',
                        'props': {'density': 'compact', 'hover': True},
                        'content': [
                            {
                                'component': 'thead',
                                'content': [{
                                    'component': 'tr',
                                    'content': [
                                        {'component': 'th', 'text': '时间'},
                                        {'component': 'th', 'text': '触发方式'},
                                        {'component': 'th', 'text': '登录'},
                                        {'component': 'th', 'text': '签到'},
                                        {'component': 'th', 'text': '奖励'},
                                        {'component': 'th', 'text': '完成'},
                                    ]
                                }]
                            },
                            {
                                'component': 'tbody',
                                'content': [
                                    {
                                        'component': 'tr',
                                        'content': [
                                            {'component': 'td', 'text': item.get('executed_at', '-')},
                                            {'component': 'td', 'text': '自动触发' if item.get('source') == 'cron' else '手动触发'},
                                            {'component': 'td', 'text': item.get('login_status', '-')},
                                            {'component': 'td', 'text': item.get('signin_status', '-')},
                                            {'component': 'td', 'text': item.get('reward_text', '-')},
                                            {'component': 'td', 'text': '是' if item.get('finished') else '否'},
                                        ]
                                    }
                                    for item in history[:10]
                                ]
                            }
                        ]
                    }
                ]
            })
        return page

    def api_run(self):
        return self.run_once(source="api")

    @eventmanager.register(EventType.PluginAction)
    def remote_run(self, event: Event):
        if not event or not event.event_data:
            return
        if event.event_data.get("action") != "mox_signin":
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
            raise RuntimeError("站点页面缺少 csrf token")
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

    def _login(self, session: requests.Session):
        try:
            resp = session.get(f"{self._base_url}/login", timeout=self._timeout)
            resp.raise_for_status()
        except RequestException as e:
            raise RuntimeError(f"打开登录页失败：{e}") from e

        csrf = self._extract_csrf(resp.text)
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
            login_resp = session.post(f"{self._base_url}/api/forum/login", json=payload, timeout=self._timeout)
            login_resp.raise_for_status()
            data = login_resp.json()
        except RequestException as e:
            raise RuntimeError(f"登录请求失败：{e}") from e

        if not data.get("success"):
            raise RuntimeError(f"登录失败：{data.get('message') or '账号密码错误或登录被拒绝'}")
        return data

    def _load_sign_page(self, session: requests.Session) -> Dict[str, Any]:
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
        return page.get("props", {}) if isinstance(page, dict) else {}

    def _ensure_timezone(self, session: requests.Session, props: Dict[str, Any]) -> Optional[str]:
        auth = props.get("auth", {}) if isinstance(props, dict) else {}
        user = auth.get("user") or {}
        if user.get("timezone"):
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

    def _notify_text(self, result: Dict[str, Any]) -> str:
        lines = [
            f"执行时间：{result.get('executed_at', '-')}",
            f"触发方式：{result.get('source', '-')}",
            f"登录：{result.get('login_status', '-')}",
            f"签到：{result.get('signin_status', '-')}",
            f"验证码：{result.get('captcha_result', '-')}",
            f"抽中奖励：{result.get('reward_text', '无')}",
            f"执行完毕：{'是' if result.get('finished') else '否'}",
            f"结果：{result.get('message', '-')}",
        ]
        return "\n".join(lines)

    def _save_result(self, result: Dict[str, Any]):
        history = self.get_data(self._history_key) or []
        if not isinstance(history, list):
            history = []
        history.append(result)
        history = sorted(history, key=lambda x: x.get('executed_at', ''), reverse=True)[:30]
        self.save_data(self._history_key, history)
        self.save_data(self._last_result_key, result)

    def _now_text(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def run_once(self, source: str = "manual"):
        self._current_trigger_type = "定时触发" if source == "cron" else "手动触发"
        steps: List[str] = []
        result = {
            "executed_at": self._now_text(),
            "source": source,
            "login_status": "未开始",
            "signin_status": "未开始",
            "reward_text": "无",
            "signed_today": False,
            "result_label": "执行中",
            "message": "",
            "finished": False,
            "captcha_result": "未获取",
            "proxy_used": self._proxy_url or "未配置",
            "steps": steps,
        }

        if not self._username or not self._password:
            steps.append("❌ 未配置账号密码，终止执行")
            result.update({
                "login_status": "失败",
                "signin_status": "未执行",
                "result_label": "失败",
                "message": "未配置账号密码",
                "finished": True,
            })
            self._save_result(result)
            if self._notify:
                self.post_message(mtype=NotificationType.Plugin, title=f"【{self.plugin_name}】", text=self._notify_text(result))
            return result

        try:
            steps.append("🚀 开始执行签到流程")
            session = self._session()
            steps.append(f"🌐 已创建会话，代理：{self._proxy_url or '未配置'}")
            self._login(session)
            result["login_status"] = "成功"
            steps.append("🔐 登录成功")

            props = self._load_sign_page(session)
            steps.append("📄 已打开签到页")
            timezone_note = self._ensure_timezone(session, props)
            if timezone_note:
                props = self._load_sign_page(session)
                result["message"] = timezone_note
                steps.append(f"🌍 {timezone_note}")

            if props.get("is_checked_in"):
                steps.append("ℹ️ 检测到今天已经签到过")
                result.update({
                    "signin_status": "今日已签到",
                    "signed_today": True,
                    "result_label": "已签到",
                    "message": "今天已经签到过了，本次不会重复请求签到接口",
                    "finished": True,
                })
                self._save_result(result)
                if self._notify:
                    self.post_message(mtype=NotificationType.Plugin, title=f"【{self.plugin_name}】", text=self._notify_text(result))
                return result

            payload = self._captcha(session)
            result["captcha_result"] = f"识别成功：{payload.get('captcha', '')}"
            steps.append(f"🔢 验证码识别成功：{payload.get('captcha', '')}")
            try:
                resp = session.post(f"{self._base_url}/api/forum/check-in/sign", json=payload, timeout=self._timeout)
                resp.raise_for_status()
                data = resp.json()
                steps.append("📝 已提交签到请求")
            except RequestException as e:
                steps.append(f"❌ 签到请求失败：{e}")
                raise RuntimeError(f"签到请求失败：{e}") from e

            reward_index = (((data or {}).get("data") or {}).get("data"))
            reward = self._reward_from_props(props, reward_index)
            reward_text = None
            if reward:
                reward_text = reward.get("text") or reward.get("name")
                steps.append(f"🎁 获得奖励：{reward_text}")
            else:
                steps.append("🎁 未解析到明确奖励信息")
            message = (((data or {}).get("data") or {}).get("message")) or data.get("message") or "签到完成"
            if reward_text and reward_text not in message:
                message = f"{message}；抽奖结果：{reward_text}"

            result.update({
                "signin_status": "成功",
                "signed_today": True,
                "reward_text": reward_text or "未解析到奖励详情",
                "result_label": "成功",
                "message": message,
                "finished": True,
            })
            steps.append("✅ 执行完成")
            self._save_result(result)
            if self._notify:
                self.post_message(mtype=NotificationType.Plugin, title=f"【{self.plugin_name}】", text=self._notify_text(result))
            return result
        except Exception as e:
            steps.append(f"💥 执行失败：{str(e)}")
            result.update({
                "result_label": "失败",
                "message": str(e),
                "finished": True,
            })
            if result["login_status"] == "未开始":
                result["login_status"] = "失败"
                result["signin_status"] = "未执行"
                steps.append("🔐 登录未完成")
            else:
                result["signin_status"] = "失败"
            self._save_result(result)
            logger.error(f"Mox签到自用执行失败：{str(e)}", exc_info=True)
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
