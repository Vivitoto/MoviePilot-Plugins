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
    plugin_name = "Mox签到自用"
    plugin_desc = "自动登录魔性论坛签到。"
    plugin_icon = "https://raw.githubusercontent.com/Vivitoto/MoviePilot-Plugins/main/icons/moxsignin.png"
    plugin_version = "0.0.8"
    plugin_author = "Vivitoto"
    author_url = "https://github.com/Vivitoto"
    plugin_config_prefix = "moxsignin_"
    plugin_order = 20
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
    _user_id = ""

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
                self._username = config.get("username") or ""
                self._password = config.get("password") or ""
                self._proxy_url = config.get("proxy_url") or "http://192.168.31.216:7890"
                self._remember = config.get("remember", True)
                self._user_id = str(config.get("user_id") or "").strip()

            if self._onlyonce:
                logger.info("Mox签到自用：保存配置后执行一次")
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    func=self.run_once,
                    trigger="date",
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
            "user_id": self._user_id,
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
                        'content': [{
                            'component': 'VCardItem',
                            'content': [{
                                'component': 'VRow',
                                'content': [
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'remember', 'label': '保持登录'}}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '保存后执行一次'}}]},
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
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'username', 'label': '用户名'}}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'password', 'label': '密码', 'type': 'password'}}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'user_id', 'label': '用户ID（可选）', 'placeholder': '如 458775'}}]},
                                    {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'div', 'props': {'class': 'text-subtitle-2 mt-2 mb-3'}, 'text': '执行配置'}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': cron_field_component, 'props': {'model': 'cron', 'label': '定时任务', 'placeholder': '10 9 * * *'}}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'proxy_url', 'label': '代理地址', 'placeholder': 'http://192.168.31.216:7890'}}]},
                                ]
                            }]
                        }]
                    },
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12},
                            'content': [{
                                'component': 'VAlert',
                                'props': {
                                    'type': 'info',
                                    'variant': 'tonal',
                                    'text': '支持 cron 定时、保存后执行一次、远程命令 /mox_signin、API /run。固定参数：站点地址 https://mox.moxing.chat，超时 20 秒，时区 Asia/Shanghai。代理地址可自定义，示例：http://192.168.31.216:7890。'
                                }
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
            "proxy_url": "http://192.168.31.216:7890",
            "base_url": "https://mox.moxing.chat",
            "timeout": 20,
            "timezone": "Asia/Shanghai",
            "remember": True,
            "user_id": "",
        }

    def get_page(self) -> List[dict]:
        last_result = self.get_data(self._last_result_key) or {}
        history = self.get_data(self._history_key) or []
        user_info = self.get_data(self._user_info_key) or {}
        asset_history = self.get_data(self._asset_history_key) or []
        if not last_result and not history and not user_info:
            return [{'component': 'div', 'text': '暂无数据', 'props': {'class': 'text-center'}}]

        history = sorted(history, key=lambda x: x.get('executed_at', ''), reverse=True) if history else []
        latest = history[0] if history else last_result

        record_rows = []
        if latest:
            record_rows.extend([
                ('最近一次执行时间', latest.get('executed_at', '暂无')),
                ('最近一次执行结果', latest.get('result_label', '暂无')),
                ('最近一次中奖信息', latest.get('reward_text', '暂无')),
                ('今日是否已签到', '是' if latest.get('signed_today') else '否'),
                ('触发方式', '自动触发' if latest.get('source') == 'cron' else '手动触发'),
                ('登录状态', latest.get('login_status', '-')),
                ('签到状态', latest.get('signin_status', '-')),
                ('完成', '是' if (latest.get('finished') or latest.get('signin_status') == '今日已签到') else '否'),
                ('最近说明', latest.get('message', '暂无')),
            ])

        page = [{
            'component': 'VCard',
            'props': {'variant': 'flat', 'class': 'mb-4'},
            'content': [
                {'component': 'VCardTitle', 'text': '🗂️ 执行记录'},
                {'component': 'VTable', 'props': {'density': 'compact', 'hover': True}, 'content': [{
                    'component': 'tbody',
                    'content': [{
                        'component': 'tr',
                        'content': [
                            {'component': 'td', 'props': {'style': 'width: 220px; font-weight: 600;'}, 'text': label},
                            {'component': 'td', 'text': value},
                        ]
                    } for label, value in record_rows]
                }]}
            ]
        }]

        if history:
            page[0]['content'].append({
                'component': 'VTable',
                'props': {'density': 'compact', 'hover': True, 'class': 'mt-4'},
                'content': [
                    {'component': 'thead', 'content': [{
                        'component': 'tr', 'content': [
                            {'component': 'th', 'text': '时间'},
                            {'component': 'th', 'text': '触发方式'},
                            {'component': 'th', 'text': '登录'},
                            {'component': 'th', 'text': '签到'},
                            {'component': 'th', 'text': '奖励'},
                            {'component': 'th', 'text': '完成'},
                        ]
                    }]},
                    {'component': 'tbody', 'content': [{
                        'component': 'tr', 'content': [
                            {'component': 'td', 'text': item.get('executed_at', '-')},
                            {'component': 'td', 'text': '自动触发' if item.get('source') == 'cron' else '手动触发'},
                            {'component': 'td', 'text': item.get('login_status', '-')},
                            {'component': 'td', 'text': item.get('signin_status', '-')},
                            {'component': 'td', 'text': item.get('reward_text', '-')},
                            {'component': 'td', 'text': '是' if (item.get('finished') or item.get('signin_status') == '今日已签到') else '否'},
                        ]
                    } for item in history[:30]]}
                ]
            })

        if user_info:
            member_rows = [
                {'component': 'tr', 'content': [
                    {'component': 'td', 'props': {'style': 'width: 180px; font-weight: 600;'}, 'text': k},
                    {'component': 'td', 'text': v},
                ]}
                for k, v in (user_info.get('member_status') or {}).items()
            ] or [{'component': 'tr', 'content': [{'component': 'td', 'text': '会员状态'}, {'component': 'td', 'text': user_info.get('member_status_raw', '暂无')}]}]
            asset_rows = [
                {'component': 'tr', 'content': [
                    {'component': 'td', 'props': {'style': 'width: 180px; font-weight: 600;'}, 'text': k},
                    {'component': 'td', 'text': str(v)},
                ]}
                for k, v in (user_info.get('assets') or {}).items()
            ] or [{'component': 'tr', 'content': [{'component': 'td', 'text': '虚拟资产'}, {'component': 'td', 'text': user_info.get('assets_raw', '暂无')}]}]
            page.append({
                'component': 'VRow',
                'content': [
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{
                        'component': 'VCard',
                        'props': {'variant': 'flat', 'class': 'mb-4'},
                        'content': [
                            {'component': 'VCardTitle', 'text': f"👤 用户信息：{user_info.get('username', self._username or '未知用户')}"},
                            {'component': 'VCardText', 'text': f"资料页：{user_info.get('profile_url', '未获取')}"},
                            {'component': 'VTable', 'props': {'density': 'compact'}, 'content': [{'component': 'tbody', 'content': member_rows}]}
                        ]
                    }]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{
                        'component': 'VCard',
                        'props': {'variant': 'flat', 'class': 'mb-4'},
                        'content': [
                            {'component': 'VCardTitle', 'text': '💰 虚拟资产'},
                            {'component': 'VTable', 'props': {'density': 'compact'}, 'content': [{'component': 'tbody', 'content': asset_rows}]}
                        ]
                    }]},
                ]
            })
            chart_card = self._asset_chart_card(asset_history)
            if chart_card:
                page.append(chart_card)

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
        session.headers.update({"x-csrf-token": csrf, "referer": f"{self._base_url}/login"})
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

    def _load_sign_page(self, session: requests.Session) -> Tuple[str, Dict[str, Any]]:
        try:
            resp = session.get(f"{self._base_url}/forum/sign", timeout=self._timeout)
            resp.raise_for_status()
        except RequestException as e:
            raise RuntimeError(f"打开签到页失败：{e}") from e
        csrf = self._extract_csrf(resp.text)
        session.headers.update({"x-csrf-token": csrf, "referer": f"{self._base_url}/forum/sign"})
        page = self._extract_page_data(resp.text)
        return resp.text, page.get("props", {}) if isinstance(page, dict) else {}

    def _ensure_timezone(self, session: requests.Session, props: Dict[str, Any]) -> Optional[str]:
        auth = props.get("auth", {}) if isinstance(props, dict) else {}
        user = auth.get("user") or {}
        if user.get("timezone"):
            return None
        try:
            resp = session.post(f"{self._base_url}/api/forum/check-in/timezone/update", json={"timezone": self._timezone}, timeout=self._timeout)
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
        return rewards[idx] if 0 <= idx < len(rewards) else None

    def _log_step(self, message: str):
        logger.info(f"{self.plugin_name}：{message}")

    def _notify_text(self, result: Dict[str, Any]) -> str:
        trigger = '自动触发' if result.get('source') == 'cron' else '手动触发'
        lines = [
            '✨ Mox 签到结果',
            '━━━━━━━━━━',
            f"🕒 执行时间：{result.get('executed_at', '-')}",
            f"🚦 触发方式：{trigger}",
            f"🔐 登录状态：{result.get('login_status', '-')}",
            f"✍️ 签到状态：{result.get('signin_status', '-')}",
            f"🔢 验证码：{result.get('captcha_result', '-')}",
            f"🎁 奖励结果：{result.get('reward_text', '无')}",
            f"🏁 执行完毕：{'是' if result.get('finished') else '否'}",
            f"📝 结果说明：{result.get('message', '-')}",
        ]
        steps = result.get('steps') or []
        if steps:
            lines.append('━━━━━━━━━━')
            lines.append('🧭 关键步骤：')
            lines.extend([f"- {item}" for item in steps[-4:]])
        return "\n".join(lines)

    def _save_result(self, result: Dict[str, Any]):
        history = self.get_data(self._history_key) or []
        if not isinstance(history, list):
            history = []
        history.append(result)
        history = sorted(history, key=lambda x: x.get('executed_at', ''), reverse=True)[:30]
        self.save_data(self._history_key, history)
        self.save_data(self._last_result_key, result)

    def _to_number(self, value: Any) -> float:
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        m = re.search(r'-?\d+(?:\.\d+)?', str(value).replace(',', ''))
        return float(m.group(0)) if m else 0.0

    def _save_asset_point(self, user_info: Dict[str, Any]):
        assets = user_info.get('assets') or {}
        if not assets:
            return
        items = list(assets.items())[:3]
        point = {'day': datetime.now().strftime('%Y-%m-%d')}
        for idx, (k, v) in enumerate(items, start=1):
            point[f'label{idx}'] = k
            point[f'value{idx}'] = self._to_number(v)
        history = self.get_data(self._asset_history_key) or []
        history = [x for x in history if x.get('day') != point['day']]
        history.append(point)
        history = sorted(history, key=lambda x: x.get('day', ''), reverse=True)[:30]
        self.save_data(self._asset_history_key, list(reversed(history)))

    def _search_user_id(self, session: requests.Session, username: str) -> Optional[str]:
        candidates = [
            {'keyword': username},
            {'q': username},
            {'keyword': username, 'type': 'user'},
            {'query': username},
        ]
        for params in candidates:
            try:
                resp = session.get(f"{self._base_url}/forum/search", params=params, timeout=self._timeout)
                if resp.status_code != 200:
                    continue
                match = re.search(r'/forum/profile/(\d+)', resp.text)
                if match:
                    return match.group(1)
                decoded = html.unescape(resp.text)
                match = re.search(r'"url":"\\?/forum/profile/(\d+)"', decoded)
                if match:
                    return match.group(1)
            except Exception:
                continue
        return None

    def _parse_info_pairs(self, text: str) -> Dict[str, str]:
        pairs = {}
        lines = [x.strip(' ：:') for x in text.splitlines() if x.strip()]
        i = 0
        while i < len(lines) - 1:
            key = lines[i]
            val = lines[i + 1]
            if len(key) <= 20 and key not in pairs:
                pairs[key] = val
                i += 2
            else:
                i += 1
        return pairs

    def _fetch_profile_sections(self, session: requests.Session, props: Dict[str, Any]) -> Dict[str, Any]:
        user_info = {
            'username': self._username,
            'profile_url': '',
            'member_status': {},
            'member_status_raw': '',
            'assets': {},
            'assets_raw': '',
        }
        auth_user = (props.get('auth') or {}).get('user') or {}
        user_id = auth_user.get('id')
        if self._user_id:
            user_id = self._user_id
        if auth_user.get('name'):
            user_info['username'] = auth_user.get('name')

        profile_url = ''
        if user_id:
            profile_url = f"{self._base_url}/forum/profile/{user_id}"

        if not profile_url:
            candidate_urls = []
            for key in ['profile_url', 'profileUrl', 'url', 'link']:
                value = auth_user.get(key)
                if isinstance(value, str) and '/forum/profile/' in value:
                    candidate_urls.append(value)
            for value in props.values() if isinstance(props, dict) else []:
                if isinstance(value, str) and '/forum/profile/' in value:
                    candidate_urls.append(value)
            if candidate_urls:
                raw_url = candidate_urls[0]
                profile_url = raw_url if raw_url.startswith('http') else f"{self._base_url}{raw_url}"
                match = re.search(r'/forum/profile/(\d+)', profile_url)
                if match:
                    user_id = match.group(1)

        if not profile_url and self._username:
            user_id = self._search_user_id(session, self._username)
            if user_id:
                profile_url = f"{self._base_url}/forum/profile/{user_id}"

        if not profile_url and self._user_id:
            profile_url = f"{self._base_url}/forum/profile/{self._user_id}"

        if not profile_url:
            home_resp = session.get(f"{self._base_url}/forum/sign", timeout=self._timeout)
            home_resp.raise_for_status()
            match = re.search(r'/forum/profile/(\d+)', home_resp.text)
            if match:
                user_id = match.group(1)
                profile_url = f"{self._base_url}/forum/profile/{user_id}"

        if not profile_url:
            return user_info

        user_info['profile_url'] = profile_url
        resp = session.get(profile_url, timeout=self._timeout)
        resp.raise_for_status()
        self._log_step(f"已获取用户主页：{profile_url}")
        text = re.sub(r'<[^>]+>', '\n', resp.text)
        text = html.unescape(text)
        text = re.sub(r'\n+', '\n', text)
        member_match = re.search(r'会员状态(.*?)(虚拟资产|$)', text, re.S)
        assets_match = re.search(r'虚拟资产(.*?)(勋章|成就|最近访客|$)', text, re.S)
        member_raw = member_match.group(1).strip() if member_match else ''
        assets_raw = assets_match.group(1).strip() if assets_match else ''
        user_info['member_status_raw'] = member_raw or '暂无'
        user_info['assets_raw'] = assets_raw or '暂无'
        user_info['member_status'] = self._parse_info_pairs(member_raw)
        user_info['assets'] = self._parse_info_pairs(assets_raw)
        if not user_info.get('username') or user_info.get('username') == self._username:
            profile_name = re.search(r'个人主页\s*\n\s*([^\n]+)', text)
            if profile_name:
                user_info['username'] = profile_name.group(1).strip()
        return user_info

    def _asset_chart_card(self, asset_history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not asset_history:
            return None
        first = asset_history[-1]
        return {
            'component': 'VCard',
            'props': {'variant': 'flat', 'class': 'mb-4'},
            'content': [
                {'component': 'VCardTitle', 'text': '📈 虚拟资产近30天趋势'},
                {
                    'component': 'VApexChart',
                    'props': {
                        'height': 320,
                        'options': {
                            'chart': {'type': 'line', 'toolbar': {'show': True}},
                            'stroke': {'curve': 'smooth', 'width': 3},
                            'xaxis': {'categories': [x.get('day', '') for x in asset_history]},
                            'colors': ['#3B82F6', '#F59E0B', '#10B981'],
                            'legend': {'show': True},
                            'noData': {'text': '暂无资产趋势数据'}
                        },
                        'series': [
                            {'name': first.get('label1') or '资产1', 'data': [self._to_number(x.get('value1')) for x in asset_history]},
                            {'name': first.get('label2') or '资产2', 'data': [self._to_number(x.get('value2')) for x in asset_history]},
                            {'name': first.get('label3') or '资产3', 'data': [self._to_number(x.get('value3')) for x in asset_history]},
                        ]
                    }
                }
            ]
        }

    def _now_text(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def run_once(self, source: str = "manual"):
        steps: List[str] = []
        trigger_text = "自动触发" if source == "cron" else "手动触发"
        result = {
            'executed_at': self._now_text(),
            'source': source,
            'login_status': '未开始',
            'signin_status': '未开始',
            'reward_text': '无',
            'signed_today': False,
            'result_label': '执行中',
            'message': '',
            'finished': False,
            'captcha_result': '未获取',
            'proxy_used': self._proxy_url or '未配置',
            'steps': steps,
        }
        if not self._username or not self._password:
            steps.append('❌ 未配置账号密码，终止执行')
            result.update({'login_status': '失败', 'signin_status': '未执行', 'result_label': '失败', 'message': '未配置账号密码', 'finished': True})
            self._save_result(result)
            if self._notify:
                self.post_message(mtype=NotificationType.Plugin, title=f"【{self.plugin_name}】", text=self._notify_text(result))
            return result
        try:
            steps.append('🚀 开始执行签到流程')
            self._log_step(f"开始执行签到流程（{trigger_text}）")
            session = self._session()
            steps.append(f"🌐 已创建会话，代理：{self._proxy_url or '未配置'}")
            self._log_step(f"已创建会话，代理：{self._proxy_url or '未配置'}")
            self._login(session)
            result['login_status'] = '成功'
            steps.append('🔐 登录成功')
            self._log_step('登录成功')
            _, props = self._load_sign_page(session)
            steps.append('📄 已打开签到页')
            self._log_step('已打开签到页')
            timezone_note = self._ensure_timezone(session, props)
            if timezone_note:
                _, props = self._load_sign_page(session)
                result['message'] = timezone_note
                steps.append(f"🌍 {timezone_note}")
                self._log_step(timezone_note)
            if props.get('is_checked_in'):
                steps.append('ℹ️ 检测到今天已经签到过')
                self._log_step('检测到今天已经签到过')
                result.update({'signin_status': '今日已签到', 'signed_today': True, 'result_label': '已签到', 'message': '今天已经签到过了，本次不会重复请求签到接口', 'finished': True})
                try:
                    user_info = self._fetch_profile_sections(session, props)
                    if user_info:
                        self.save_data(self._user_info_key, user_info)
                        self._save_asset_point(user_info)
                        steps.append('👤 已刷新用户信息与资产数据')
                        self._log_step('已刷新用户信息与资产数据')
                except Exception as info_error:
                    steps.append(f"⚠️ 用户信息刷新失败：{info_error}")
                self._save_result(result)
                if self._notify:
                    self.post_message(mtype=NotificationType.Plugin, title=f"【{self.plugin_name}】", text=self._notify_text(result))
                return result
            payload = self._captcha(session)
            result['captcha_result'] = f"识别成功：{payload.get('captcha', '')}"
            steps.append(f"🔢 验证码识别成功：{payload.get('captcha', '')}")
            self._log_step(f"验证码识别成功：{payload.get('captcha', '')}")
            try:
                resp = session.post(f"{self._base_url}/api/forum/check-in/sign", json=payload, timeout=self._timeout)
                resp.raise_for_status()
                data = resp.json()
                steps.append('📝 已提交签到请求')
                self._log_step('已提交签到请求')
            except RequestException as e:
                steps.append(f"❌ 签到请求失败：{e}")
                raise RuntimeError(f"签到请求失败：{e}") from e
            reward_index = (((data or {}).get('data') or {}).get('data'))
            reward = self._reward_from_props(props, reward_index)
            reward_text = reward.get('text') or reward.get('name') if reward else None
            if reward_text:
                steps.append(f"🎁 获得奖励：{reward_text}")
                self._log_step(f"获得奖励：{reward_text}")
            else:
                steps.append('🎁 未解析到明确奖励信息')
                self._log_step('未解析到明确奖励信息')
            message = (((data or {}).get('data') or {}).get('message')) or data.get('message') or '签到完成'
            if reward_text and reward_text not in message:
                message = f"{message}；抽奖结果：{reward_text}"
            result.update({'signin_status': '成功', 'signed_today': True, 'reward_text': reward_text or '未解析到奖励详情', 'result_label': '成功', 'message': message, 'finished': True})
            try:
                user_info = self._fetch_profile_sections(session, props)
                if user_info:
                    self.save_data(self._user_info_key, user_info)
                    self._save_asset_point(user_info)
                    steps.append('👤 已刷新用户信息与资产数据')
            except Exception as info_error:
                steps.append(f"⚠️ 用户信息刷新失败：{info_error}")
            steps.append('✅ 执行完成')
            self._log_step('执行完成')
            self._save_result(result)
            if self._notify:
                self.post_message(mtype=NotificationType.Plugin, title=f"【{self.plugin_name}】", text=self._notify_text(result))
            return result
        except Exception as e:
            steps.append(f"💥 执行失败：{str(e)}")
            self._log_step(f"执行失败：{str(e)}")
            result.update({'result_label': '失败', 'message': str(e), 'finished': True})
            if result['login_status'] == '未开始':
                result['login_status'] = '失败'
                result['signin_status'] = '未执行'
                steps.append('🔐 登录未完成')
            else:
                result['signin_status'] = '失败'
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
