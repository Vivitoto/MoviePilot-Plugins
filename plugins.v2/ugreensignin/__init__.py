import re
import time
import random
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.plugins import _PluginBase
from app.log import logger
from app.schemas import NotificationType
import requests


class UgreenSignIn(_PluginBase):
    plugin_name = "绿联论坛签到"
    plugin_desc = "通过Cookie自动访问绿联论坛，保持登录状态即完成签到"
    plugin_icon = "https://raw.githubusercontent.com/Vivitoto/MoviePilot-Plugins/main/icons/ugreensignin.png"
    plugin_version = "0.1.0"
    plugin_author = "Vivitoto"
    author_url = "https://github.com/Vivitoto"
    plugin_config_prefix = "ugreensignin_"
    plugin_order = 1
    auth_level = 2

    _enabled = False
    _notify = True
    _onlyonce = False
    _cron = "0 8 * * *"
    _cookie = ""
    _username = ""
    _password = ""
    _user_agent = ""
    _history_days = 30
    _scheduler: Optional[BackgroundScheduler] = None

    # 论坛地址
    FORUM_URL = "https://club.ugnas.com/"
    SPACE_URL = "https://club.ugnas.com/home.php?mod=space"
    SIGN_PLUGIN_URL = "https://club.ugnas.com/plugin.php?id=dsu_paulsign:sign"

    def init_plugin(self, config: dict = None):
        self.stop_service()
        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", True)
            self._onlyonce = config.get("onlyonce", False)
            self._cron = config.get("cron", "0 8 * * *")
            self._cookie = self._normalize_cookie(config.get("cookie") or "")
            self._username = (config.get("username") or "").strip()
            self._password = (config.get("password") or "").strip()
            self._user_agent = (config.get("user_agent") or "").strip()
            try:
                self._history_days = int(config.get("history_days", 30))
            except Exception:
                self._history_days = 30

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.sign,
                trigger='date',
                run_date=datetime.now() + timedelta(seconds=3),
                name="Ugreen论坛签到"
            )
            self._onlyonce = False
            self.update_config({
                "enabled": self._enabled,
                "notify": self._notify,
                "cookie": self._cookie,
                "username": self._username,
                "password": self._password,
                "user_agent": self._user_agent,
                "cron": self._cron,
                "onlyonce": False,
                "history_days": self._history_days,
            })
            if self._scheduler.get_jobs():
                self._scheduler.start()

    @staticmethod
    def _normalize_cookie(raw: str) -> str:
        """将各种格式的 cookie 统一为 k=v; k=v 格式"""
        if not raw:
            return ""
        raw = raw.strip()
        # 如果是 JSON 数组格式
        if raw.startswith("["):
            try:
                import json
                items = json.loads(raw)
                return "; ".join(f"{item.get('name', '')}={item.get('value', '')}" for item in items if item.get('name'))
            except Exception:
                pass
        return raw

    def sign(self, source=None):
        """执行签到：访问论坛保持登录状态 + 获取用户信息"""
        # 随机延时（仅定时任务）
        if source == "cron":
            delay = random.randint(60, 1800)
            logger.info(f"绿联论坛签到：随机延时 {delay // 60}分{delay % 60}秒")
            time.sleep(delay)

        logger.info("开始绿联论坛签到")

        # 1. 检查 cookie
        if not self._cookie:
            msg = "Cookie为空，请在插件设置中填入论坛Cookie"
            return self._fail(msg)

        # 2. 检查登录状态
        if not self._is_logged_in():
            msg = "Cookie已过期，请重新登录论坛并更新Cookie"
            return self._fail(msg)

        # 3. 获取用户信息
        info = self._fetch_user_profile()
        if not info or not info.get("username"):
            msg = "无法获取用户信息"
            return self._fail(msg)

        # 4. 计算积分变化
        last_raw = self.get_data('last_points')
        first_run = last_raw is None
        last_points = int(last_raw) if last_raw else 0
        current_points = int(info.get('points') or 0)
        delta = current_points - last_points if not first_run else 0

        if first_run:
            status = "首次运行（建立基线）"
            message = "已记录当前积分作为基线"
        elif delta > 0:
            status = "签到成功"
            message = f"积分变化: +{delta}"
        else:
            status = "已签到"
            message = f"积分无变化 (当前 {current_points})"

        d = {
            "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "status": status,
            "message": message,
            "points": current_points,
            "delta": delta,
        }
        self.save_data('last_points', current_points)
        self.save_data('last_user_info', info)
        self._save_history(d)

        logger.info(f"签到完成: {status}, 积分: {current_points}, 变化: {'+' + str(delta) if delta > 0 else delta}")

        # 5. 通知
        if self._notify:
            self._send_notify(info, d, first_run, delta)

        return d

    def _fail(self, message: str) -> Dict:
        """签到失败处理"""
        d = {
            "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "status": "签到失败",
            "message": message,
        }
        self._save_history(d)
        logger.warning(f"绿联论坛签到失败: {message}")
        if self._notify:
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="🔴 绿联论坛签到失败",
                text=f"⏰ {d['date']}\n❌ {message}"
            )
        return d

    def _is_logged_in(self) -> bool:
        """检查 Cookie 是否有效"""
        try:
            headers = self._build_headers()
            resp = requests.get(self.SPACE_URL, headers=headers, timeout=15, allow_redirects=False)
            # 200 = 已登录, 302 = 未登录
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"检查登录状态失败: {e}")
            return False

    def _build_headers(self) -> Dict[str, str]:
        ua = self._user_agent or 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36'
        return {
            'User-Agent': ua,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Cookie': self._cookie,
        }

    def _fetch_user_profile(self) -> Dict[str, Any]:
        """获取用户资料（用户名、积分、用户组等）"""
        try:
            headers = self._build_headers()
            resp = requests.get(self.SPACE_URL, headers=headers, timeout=15)
            html = resp.text or ""
            logger.info(f"获取用户主页, HTML长度: {len(html)}")

            if 'discuz_uid = \'0\'' in html or 'discuz_uid = "0"' in html:
                logger.warning("Cookie无效（uid=0）")
                return {}

            # UID
            uid_match = re.search(r"discuz_uid\s*=\s*['\"]?(\d+)", html)
            uid = uid_match.group(1) if uid_match else None

            # 用户名 - 从 kmname class 提取
            username = "-"
            m = re.search(r'class="kmname"[^>]*>([^<]+)</(?:a|span)', html)
            if m:
                username = m.group(1).strip()
            if username == "-":
                m2 = re.search(r'<h2[^>]*>基本资料</h2>[\s\S]*?<li><em>用户名</em>([^<]+)</li>', html)
                if m2:
                    username = m2.group(1).strip()

            # 积分
            points = 0
            p = re.search(r'class="kmjifen[^"]*"><span>(\d+)</span>积分', html)
            if p:
                points = int(p.group(1))
            else:
                p2 = re.search(r'积分[：:]\s*(\d+)', html)
                if p2:
                    points = int(p2.group(1))

            # 用户组
            usergroup = None
            ug = re.search(r'<li><em>用户组</em>.*?<a[^>]*>([^<]+)</a>', html)
            if ug:
                usergroup = ug.group(1).strip()

            # 头像
            avatar = "https://bbs-cn-oss.ugnas.com/bbs/avatar/noavatar.png"
            am = re.search(r'<img[^>]*class="user_avatar"[^>]*src="([^"]+)"', html)
            if am:
                avatar = am.group(1)

            # 主题数、回帖数、好友数
            threads = 0
            posts = 0
            friends = 0
            th = re.search(r'<span>(\d+)</span>主题数', html)
            if th:
                threads = int(th.group(1))
            po = re.search(r'<span>(\d+)</span>回帖数', html)
            if po:
                posts = int(po.group(1))
            fr = re.search(r'<span>(\d+)</span>好友数', html)
            if fr:
                friends = int(fr.group(1))

            info = {
                "uid": uid,
                "username": username,
                "points": points,
                "avatar": avatar,
                "usergroup": usergroup,
                "threads": threads,
                "posts": posts,
                "friends": friends,
            }
            logger.info(f"用户信息: {username}, UID={uid}, 积分={points}, 用户组={usergroup}")
            return info

        except Exception as e:
            logger.error(f"获取用户资料失败: {e}")
            return {}

    def _send_notify(self, info: Dict, d: Dict, first_run: bool, delta: int):
        """发送签到通知"""
        name = info.get('username', '-')
        uid = info.get('uid', '')
        points = info.get('points', 0)
        time_str = d.get('date', '')

        if first_run:
            title = "🎉 绿联论坛首次签到"
            text = (
                f"👤 用户：{name}\n"
                f"🆔 UID：{uid}\n"
                f"💰 积分：{points}\n"
                f"⏰ 时间：{time_str}\n"
                "━━━━━━━━━━\n"
                "📌 已建立积分基线"
            )
        elif delta > 0:
            title = "✅ 绿联论坛签到成功"
            text = (
                f"👤 用户：{name}\n"
                f"💰 积分：{points} (📈 +{delta})\n"
                f"⏰ 时间：{time_str}\n"
                f"🎊 本次获得 {delta} 积分！"
            )
        else:
            title = "✅ 绿联论坛签到"
            text = (
                f"👤 用户：{name}\n"
                f"💰 积分：{points}\n"
                f"⏰ 时间：{time_str}\n"
                "📅 今日已完成签到"
            )

        self.post_message(mtype=NotificationType.SiteMessage, title=title, text=text)

    def _save_history(self, record: Dict[str, Any]):
        """保存签到历史"""
        try:
            history = self.get_data('sign_history') or []
            history.append(record)
            tz = pytz.timezone(settings.TZ)
            now = datetime.now(tz)
            keep = []
            for r in history:
                try:
                    dt_str = r.get('date', '')
                    dt = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')
                    if dt.tzinfo is None:
                        dt = tz.localize(dt)
                except Exception:
                    dt = now
                if (now - dt).days < self._history_days:
                    keep.append(r)
            self.save_data('sign_history', keep)
        except Exception as e:
            logger.error(f"保存历史失败: {e}")

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{
                "id": "ugreensignin",
                "name": "绿联论坛签到",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sign,
                "kwargs": {"source": "cron"}
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {'component': 'VRow', 'content': [
                        {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [
                            {'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}
                        ]},
                        {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [
                            {'component': 'VSwitch', 'props': {'model': 'notify', 'label': '开启通知'}}
                        ]},
                        {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [
                            {'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}
                        ]},
                    ]},
                    {'component': 'VRow', 'content': [
                        {'component': 'VCol', 'props': {'cols': 12}, 'content': [
                            {'component': 'VAlert', 'props': {
                                'type': 'info', 'variant': 'tonal',
                                'text': '💡 Cookie获取方法：在浏览器中登录 club.ugnas.com，按F12打开开发者工具，在Console中输入 document.cookie 并复制结果填入下方。Cookie中必须包含 6LQh_2132_auth 字段。'
                            }}
                        ]}
                    ]},
                    {'component': 'VRow', 'content': [
                        {'component': 'VCol', 'props': {'cols': 12}, 'content': [
                            {'component': 'VTextarea', 'props': {
                                'model': 'cookie',
                                'label': '论坛Cookie',
                                'placeholder': '6LQh_2132_auth=xxx; PHPSESSID=xxx; ...',
                                'rows': 3,
                                'hint': '支持 document.cookie 的原始格式或 JSON 数组格式'
                            }}
                        ]}
                    ]},
                    {'component': 'VRow', 'content': [
                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                            {'component': 'VTextField', 'props': {
                                'model': 'username',
                                'label': '论坛账号',
                                'placeholder': '手机号/邮箱'
                            }}
                        ]},
                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                            {'component': 'VTextField', 'props': {
                                'model': 'password',
                                'label': '论坛密码',
                                'type': 'password',
                                'placeholder': '论坛登录密码'
                            }}
                        ]},
                    ]},
                    {'component': 'VRow', 'content': [
                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                            {'component': 'VTextField', 'props': {
                                'model': 'user_agent',
                                'label': 'User-Agent',
                                'placeholder': '留空使用默认值'
                            }}
                        ]},
                        {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [
                            {'component': 'VCronField', 'props': {'model': 'cron', 'label': '签到周期'}}
                        ]},
                    ]},
                    {'component': 'VRow', 'content': [
                        {'component': 'VCol', 'props': {'cols': 12}, 'content': [
                            {'component': 'VAlert', 'props': {
                                'type': 'info', 'variant': 'tonal',
                                'text': '⏳ 定时任务会随机延时1-30分钟执行；手动点击"立即运行一次"则无延时。绿联论坛的签到机制为"保持每日登录"，插件通过定时访问论坛来维持登录状态。'
                            }}
                        ]}
                    ]},
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cookie": "",
            "username": "",
            "password": "",
            "user_agent": "",
            "cron": "0 8 * * *",
            "history_days": 30,
        }

    def get_page(self) -> List[dict]:
        """插件详情页"""
        info = self.get_data('last_user_info') or {}
        historys = self.get_data('sign_history') or []

        if not historys:
            return [{
                'component': 'VAlert',
                'props': {'type': 'info', 'variant': 'tonal', 'text': '暂无签到记录，请先配置Cookie并启用插件', 'class': 'mb-2'}
            }]

        historys = sorted(historys, key=lambda x: x.get("date", ""), reverse=True)

        card = []
        if info:
            name = info.get('username', '-')
            avatar = info.get('avatar')
            points = info.get('points', 0)
            usergroup = info.get('usergroup', '')
            threads = info.get('threads', 0)
            posts = info.get('posts', 0)
            friends = info.get('friends', 0)
            uid_val = info.get('uid', '')
            latest = historys[0]
            latest_status = latest.get('status', '-')
            latest_delta = latest.get('delta', 0)
            latest_date = latest.get('date', '-')
            latest_color = 'success' if any(k in str(latest_status) for k in ['成功', '已签到']) else ('warning' if '基线' in str(latest_status) else 'error')
            latest_delta_emoji = '📈' if latest_delta > 0 else ('➖' if latest_delta == 0 else '📉')

            card = [{
                'component': 'VCard',
                'props': {'variant': 'elevated', 'elevation': 2, 'rounded': 'lg', 'class': 'mb-4'},
                'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'text-h5 font-weight-bold'}, 'text': '👤 绿联论坛'},
                    {'component': 'VCardText', 'content': [
                        {'component': 'VRow', 'props': {'align': 'center'}, 'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [
                                {'component': 'VAvatar', 'props': {'size': 96, 'class': 'mx-auto'}, 'content': [
                                    {'component': 'VImg', 'props': {'src': avatar}}
                                ]} if avatar else {'component': 'VAvatar', 'props': {'size': 96, 'color': 'grey-lighten-2', 'class': 'mx-auto'}, 'text': name[:1]}
                            ]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 10}, 'content': [
                                {'component': 'div', 'props': {'class': 'text-h5 font-weight-bold'}, 'text': name},
                                {'component': 'div', 'props': {'class': 'text-subtitle-2 text-medium-emphasis mt-1'}, 'text': f"🆔 {uid_val}" + (f" | 👥 {usergroup}" if usergroup else "")},
                                {'component': 'VRow', 'props': {'class': 'mt-3'}, 'content': [
                                    {'component': 'VCol', 'props': {'cols': 6, 'sm': 3}, 'content': [{'component': 'VChip', 'props': {'size': 'large', 'variant': 'tonal', 'color': 'amber-darken-2'}, 'text': f'💰 {points}'}]},
                                    {'component': 'VCol', 'props': {'cols': 6, 'sm': 3}, 'content': [{'component': 'VChip', 'props': {'size': 'large', 'variant': 'tonal', 'color': 'blue'}, 'text': f'📝 {threads}'}]},
                                    {'component': 'VCol', 'props': {'cols': 6, 'sm': 3}, 'content': [{'component': 'VChip', 'props': {'size': 'large', 'variant': 'tonal', 'color': 'green'}, 'text': f'💬 {posts}'}]},
                                    {'component': 'VCol', 'props': {'cols': 6, 'sm': 3}, 'content': [{'component': 'VChip', 'props': {'size': 'large', 'variant': 'tonal', 'color': 'purple'}, 'text': f'👥 {friends}'}]},
                                ]},
                                {'component': 'VDivider'},
                                {'component': 'VRow', 'props': {'class': 'mt-3'}, 'content': [
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VChip', 'props': {'color': latest_color}, 'text': latest_status}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VChip', 'props': {}, 'text': f'{latest_delta_emoji} {("+" + str(latest_delta)) if latest_delta > 0 else str(latest_delta)}'}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VChip', 'props': {'variant': 'tonal'}, 'text': latest_date}]},
                                ]},
                            ]}
                        ]}
                    ]}
                ]
            }]

        rows = []
        for h in historys:
            st = h.get('status', '未知')
            sc = 'success' if any(k in st for k in ['成功', '已签到', '基线']) else 'error'
            d = h.get('delta', 0)
            dc = 'success' if d > 0 else 'grey'
            de = '📈' if d > 0 else ('➖' if d == 0 else '📉')
            dt = f"+{d}" if d > 0 else str(d)
            rows.append({
                'component': 'tr', 'content': [
                    {'component': 'td', 'props': {'class': 'text-caption'}, 'text': h.get('date', '')},
                    {'component': 'td', 'content': [{'component': 'VChip', 'props': {'size': 'small', 'variant': 'outlined', 'color': sc}, 'text': st}]},
                    {'component': 'td', 'content': [{'component': 'VChip', 'props': {'size': 'small', 'variant': 'outlined', 'color': dc}, 'text': f'{de} {dt}'}]},
                    {'component': 'td', 'props': {'class': 'text-caption'}, 'text': h.get('message', '-')},
                ]
            })

        table = [{
            'component': 'VCard',
            'props': {'variant': 'elevated', 'elevation': 2, 'rounded': 'lg', 'class': 'mb-4'},
            'content': [
                {'component': 'VCardTitle', 'props': {'class': 'text-h6 font-weight-bold'}, 'text': f'📊 签到历史 (近{len(rows)}条)'},
                {'component': 'VCardText', 'content': [
                    {'component': 'VTable', 'props': {'hover': True, 'density': 'comfortable'}, 'content': [
                        {'component': 'thead', 'content': [{'component': 'tr', 'content': [
                            {'component': 'th', 'text': '时间'}, {'component': 'th', 'text': '状态'},
                            {'component': 'th', 'text': '积分变化'}, {'component': 'th', 'text': '消息'},
                        ]}]},
                        {'component': 'tbody', 'content': rows}
                    ]}
                ]}
            ]
        }]

        return card + table

    def stop_service(self):
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        return True

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []
