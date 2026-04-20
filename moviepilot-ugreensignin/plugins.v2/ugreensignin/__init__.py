# 绿联论坛签到插件（基于参考项目修改）
# 主要修改：
# 1. 插件名称改为"绿联论坛签到自用"
# 2. 添加随机延时 1-30 分钟
# 3. 修复 UID 发现逻辑
# 4. 使用用户提供的图标

import random
import re
import time
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
    plugin_name = "绿联论坛签到自用"
    plugin_desc = "自动登录绿联论坛，刷新用户信息"
    plugin_icon = "https://raw.githubusercontent.com/Vivitoto/MoviePilot-Plugins/main/icons/ugreensignin.png"
    plugin_version = "0.0.5"
    plugin_author = "Vivitoto"
    author_url = "https://github.com/Vivitoto"
    plugin_config_prefix = "ugreensignin_"
    plugin_order = 21
    auth_level = 2

    _enabled = False
    _notify = True
    _onlyonce = False
    _cron = "0 8 * * *"
    _cookie = ""
    _username = ""
    _password = ""
    _history_days = 30
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        self.stop_service()
        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", True)
            self._onlyonce = config.get("onlyonce", False)
            self._cron = config.get("cron", "0 8 * * *")
            self._cookie = (config.get("cookie") or "").strip()
            self._username = (config.get("username") or "").strip()
            self._password = (config.get("password") or "").strip()
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
                name="绿联论坛签到自用"
            )
            self._onlyonce = False
            self.update_config({
                "enabled": self._enabled,
                "notify": self._notify,
                "cookie": self._cookie,
                "cron": self._cron,
                "onlyonce": False,
                "username": self._username,
                "password": self._password,
                "history_days": self._history_days,
            })
            if self._scheduler.get_jobs():
                self._scheduler.start()
        if self._enabled and self._cron:
            logger.info(f"绿联论坛签到自用：注册定时服务: {self._cron}")

    def sign(self):
        """签到主方法，添加随机延时"""
        # 随机延时 1-30 分钟
        delay_seconds = random.randint(60, 1800)
        logger.info(f"绿联论坛签到自用：随机延时 {delay_seconds // 60} 分 {delay_seconds % 60} 秒后开始执行")
        time.sleep(delay_seconds)
        
        logger.info("绿联论坛签到自用：开始执行")
        if not self._cookie:
            logger.info("绿联论坛签到自用：Cookie为空，尝试自动登录")
            ok = self._auto_login()
            if not ok:
                d = {"date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "status": "签到失败", "message": "自动登录失败或未配置用户名密码"}
                self._save_history(d)
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="🔴 绿联论坛签到失败",
                        text=f"⏰ {d['date']}\n❌ {d['message']}"
                    )
                return d
        info = self._fetch_user_profile()
        if not info:
            d = {"date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "status": "签到失败", "message": "无法获取用户资料"}
            self._save_history(d)
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="🔴 绿联论坛签到失败",
                    text=f"⏰ {d['date']}\n❌ {d['message']}"
                )
            return d
        last_raw = self.get_data('last_points')
        first_run = last_raw is None
        last_points = 0
        try:
            if isinstance(last_raw, (int, float)):
                last_points = int(last_raw)
        except Exception:
            pass
        current_points = int(info.get('points') or 0)
        delta = current_points - last_points if not first_run else 0
        delta_str = f"+{delta}" if delta > 0 else f"{delta}"
        status = "首次运行（建立基线）" if first_run else ("签到成功" if delta > 0 else "已签到")
        d = {
            "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "status": status,
            "message": ("已记录当前积分作为基线" if first_run else f"积分变化: {delta_str}"),
            "points": current_points,
            "delta": delta
        }
        self.save_data('last_points', current_points)
        self.save_data('last_user_info', info)
        self._save_history(d)
        logger.info(f"绿联论坛签到自用：签到完成: {status}, 当前积分: {current_points}, 变化: {delta_str}")
        if self._notify:
            name = info.get('username','-')
            uid = info.get('uid', '')
            delta_emoji = '📈' if delta > 0 else ('➖' if delta == 0 else '📉')
            pts_line = f"💰 积分：{current_points}" + (f" ({delta_emoji} {delta_str})" if not first_run else " (首次基线)")
            time_str = d.get('date', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            
            if first_run:
                title = "🎉 绿联论坛首次签到"
                text_parts = [
                    f"👤 用户：{name}",
                    f"🆔 UID：{uid}" if uid else "",
                    pts_line,
                    f"⏰ 时间：{time_str}",
                    "━━━━━━━━━━",
                    "📌 已建立积分基线",
                    "💡 后续签到将显示积分变化"
                ]
            elif delta > 0:
                title = "✅ 绿联论坛签到成功"
                text_parts = [
                    f"👤 用户：{name}",
                    f"🆔 UID：{uid}" if uid else "",
                    pts_line,
                    f"⏰ 时间：{time_str}",
                    "━━━━━━━━━━",
                    f"🎊 本次获得 {delta} 积分！"
                ]
            else:
                title = "✅ 绿联论坛签到"
                text_parts = [
                    f"👤 用户：{name}",
                    f"🆔 UID：{uid}" if uid else "",
                    pts_line,
                    f"⏰ 时间：{time_str}",
                    "━━━━━━━━━━",
                    "📅 今日已完成签到"
                ]
            
            text = "\n".join([p for p in text_parts if p])
            self.post_message(mtype=NotificationType.SiteMessage, title=title, text=text)
        return d

    def _auto_login(self) -> bool:
        """自动登录"""
        try:
            if not (self._username and self._password):
                return False
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                ctx = browser.new_context()
                page = ctx.new_page()
                page.goto("https://club.ugnas.com/", wait_until="domcontentloaded")
                try:
                    btn = page.locator("button:has-text('同意')")
                    if btn.count() > 0:
                        btn.first.click()
                        page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                try:
                    ctx.add_cookies([{ "name": "6LQh_2132_BBRules_ok", "value": "1", "domain": "club.ugnas.com", "path": "/", "secure": True, "httpOnly": False, "expires": int(time.time()) + 31536000 }])
                except Exception:
                    pass
                page.goto("https://club.ugnas.com/member.php?mod=logging&action=login", wait_until="domcontentloaded")
                current_url = page.url
                callback_url = {"url": None}
                def _on_resp(resp):
                    try:
                        u = resp.url
                        if "api-zh.ugnas.com/api/oauth/authorize" in u:
                            h = resp.headers
                            loc = h.get('location') or h.get('Location')
                            if loc and "club.ugnas.com/api/ugreen/callback.php" in loc:
                                callback_url["url"] = loc
                    except Exception:
                        pass
                try:
                    page.on("response", _on_resp)
                except Exception:
                    pass
                if "web.ugnas.com" in current_url:
                    try:
                        u_sels = ["input[name='username']", "input[id='username']", "input[type='text']"]
                        p_sels = ["input[name='password']", "input[id='password']", "input[type='password']"]
                        for s in u_sels:
                            if page.query_selector(s):
                                page.fill(s, self._username)
                                break
                        for s in p_sels:
                            if page.query_selector(s):
                                page.fill(s, self._password)
                                break
                        if not any(page.query_selector(s) for s in u_sels):
                            ti = page.query_selector_all("input[type='text'], input[type='email'], input[autocomplete='username'], input[placeholder*='账号'], input[placeholder*='邮箱'], input[placeholder*='手机号']")
                            if ti:
                                ti[0].fill(self._username)
                        if not any(page.query_selector(s) for s in p_sels):
                            pi = page.query_selector_all("input[type='password'], input[autocomplete='current-password']")
                            if pi:
                                pi[0].fill(self._password)
                        btn_oauth = page.query_selector("button[type='submit']") or page.query_selector("input[type='submit']") or page.query_selector("button:has-text('登录')")
                        if not btn_oauth:
                            for sel in ["button:has-text('Login')", "button:has-text('Sign in')", "button:has-text('登入')"]:
                                b = page.query_selector(sel)
                                if b:
                                    btn_oauth = b
                                    break
                        if btn_oauth:
                            btn_oauth.click()
                            page.wait_for_timeout(8000)
                except Exception:
                    pass
                try:
                    v_dialog = page.locator("text=Account password verification")
                    if v_dialog.count() > 0:
                        pwd_input = page.locator("input[type='password']")
                        if pwd_input.count() > 0:
                            pwd_input.fill(self._password)
                            cont_btn = page.locator("button:has-text('Continue')")
                            if cont_btn.count() > 0:
                                cont_btn.click()
                                page.wait_for_timeout(5000)
                except Exception:
                    pass
                if callback_url["url"]:
                    try:
                        page.goto(callback_url["url"], wait_until="networkidle")
                    except Exception:
                        pass
                try:
                    page.goto("https://club.ugnas.com/", wait_until="networkidle")
                except Exception:
                    pass
                try:
                    cookies = ctx.cookies()
                    filtered = [c for c in cookies if c.get('domain','').endswith('ugnas.com')]
                    if not filtered:
                        return False
                    self._cookie = "; ".join([f"{c['name']}={c['value']}" for c in filtered])
                    self.save_data('cookie', self._cookie)
                    self.update_config({
                        "enabled": self._enabled,
                        "notify": self._notify,
                        "cookie": self._cookie,
                        "cron": self._cron,
                        "onlyonce": False,
                        "username": self._username,
                        "password": self._password,
                        "history_days": self._history_days,
                    })
                except Exception:
                    pass
                browser.close()
            return True
        except Exception:
            return False

    def _discover_uid(self, headers):
        """修复版：发现当前登录用户的 UID"""
        try:
            # 方法1：访问个人中心，看是否重定向到带uid的页面
            r = requests.get('https://club.ugnas.com/home.php?mod=space',
                            headers=headers, timeout=15, allow_redirects=False)
            if r.status_code == 302:
                loc = r.headers.get('location', '')
                uid_match = re.search(r'uid[=:](\d+)', loc)
                if uid_match:
                    logger.info(f"绿联论坛签到自用：从重定向URL发现UID: {uid_match.group(1)}")
                    return uid_match.group(1)
            
            # 方法2：从首页HTML找空间链接
            r2 = requests.get('https://club.ugnas.com/', headers=headers, timeout=15)
            html = r2.text
            
            # 匹配 home.php?mod=space&uid=xxx
            uid_match = re.search(r'home\.php\?mod=space[&;]uid=(\d+)', html)
            if uid_match:
                logger.info(f"绿联论坛签到自用：从首页链接发现UID: {uid_match.group(1)}")
                return uid_match.group(1)
            
            # 匹配 space-uid-xxx.html
            uid_match = re.search(r'space-uid-(\d+)\.html', html)
            if uid_match:
                logger.info(f"绿联论坛签到自用：从space链接发现UID: {uid_match.group(1)}")
                return uid_match.group(1)
                
            # 方法3：从js变量中找
            uid_match = re.search(r'uid[:\s=]*["\']?(\d+)["\']?', html)
            if uid_match:
                logger.info(f"绿联论坛签到自用：从JS变量发现UID: {uid_match.group(1)}")
                return uid_match.group(1)
                
        except Exception as e:
            logger.warning(f"绿联论坛签到自用：发现UID失败: {e}")
        return None

    def _fetch_user_profile(self):
        """抓取用户资料"""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
                "Cookie": self._cookie,
                "Referer": "https://club.ugnas.com/"
            }
            
            # 发现UID
            uid = self._discover_uid(headers)
            html = ""
            
            if uid:
                # 访问用户主页
                url = f'https://club.ugnas.com/home.php?mod=space&uid={uid}'
                logger.info(f"绿联论坛签到自用：访问用户主页: {url}")
                resp = requests.get(url, headers=headers, timeout=15)
                html = resp.text or ""
            else:
                logger.warning("绿联论坛签到自用：未发现UID，尝试直接访问个人中心")
                resp = requests.get('https://club.ugnas.com/home.php?mod=space',
                                 headers=headers, timeout=15, allow_redirects=True)
                html = resp.text or ""
                # 从最终URL中提取UID
                uid_match = re.search(r'uid[=:](\d+)', resp.url)
                if uid_match:
                    uid = uid_match.group(1)
            
            # 解析用户信息
            username = "-"
            points = 0
            usergroup = "-"
            
            # 提取用户名
            patterns = [
                r'<li><em>用户名</em>\s*([^<\s][^<]*)</li>',
                r'class="kmname"[^>]*>([^<]+)',
                r'username[:\s=]*["\']?([^"\'<\s]+)',
            ]
            for p in patterns:
                m = re.search(p, html)
                if m:
                    username = m.group(1).strip()
                    break
            
            # 提取积分
            patterns = [
                r'class="kmjifen[^"]*"[^>]*>.*?<span>(\d+)</span>',
                r'积分[：:]\s*(\d+)',
                r'class="xg1"[^>]*>积分:\s*(\d+)',
            ]
            for p in patterns:
                m = re.search(p, html)
                if m:
                    points = int(m.group(1))
                    break
            
            # 提取用户组
            patterns = [
                r'<li><em>用户组</em>.*?<a[^>]*>([^<]+)</a>',
                r'用户组[：:]\s*([^<\s]+)',
            ]
            for p in patterns:
                m = re.search(p, html)
                if m:
                    usergroup = m.group(1).strip()
                    break
            
            result = {
                "username": username,
                "points": points,
                "usergroup": usergroup,
                "uid": uid,
            }
            
            logger.info(f"绿联论坛签到自用：解析结果: 用户名={username}, UID={uid}, 积分={points}, 用户组={usergroup}")
            return result
            
        except Exception as e:
            logger.error(f"绿联论坛签到自用：获取用户资料失败: {e}", exc_info=True)
            return {}

    def _save_history(self, d: dict):
        history = self.get_data("history") or []
        if not isinstance(history, list):
            history = []
        history.append(d)
        cutoff = datetime.now() - timedelta(days=self._history_days)
        history = [h for h in history if datetime.strptime(h.get("date", "1970-01-01"), '%Y-%m-%d %H:%M:%S') > cutoff]
        self.save_data("history", history[-100:])

    def get_state(self) -> bool:
        return self._enabled and bool(self._cookie or (self._username and self._password))

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._cron:
            return []
        return [{
            "id": "UgreenSignIn",
            "name": "绿联论坛签到自用",
            "trigger": CronTrigger.from_crontab(self._cron),
            "func": self.sign,
            "kwargs": {}
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        version = getattr(settings, "VERSION_FLAG", "v1")
        cron_field = "VCronField" if version == "v2" else "VTextField"
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '保存后执行一次'}}]},
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'username', 'label': '用户名/手机号'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'password', 'label': '密码', 'type': 'password'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': cron_field, 'props': {'model': 'cron', 'label': '定时任务', 'placeholder': '0 8 * * *'}}]},
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VTextarea', 'props': {'model': 'cookie', 'label': 'Cookie（可选，优先级高于自动登录）', 'placeholder': '从浏览器复制的完整Cookie字符串', 'rows': 3}}]},
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'div', 'props': {'class': 'text-caption grey--text'}, 'text': '说明：自动登录绿联论坛，登录即刷新Cookie（视为签到）'}]},
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'div', 'props': {'class': 'text-caption grey--text'}, 'text': '备注：定时任务触发后会随机延时 1-30 分钟再执行，避免整点并发'}}]},
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cron": "0 8 * * *",
            "cookie": "",
            "username": "",
            "password": "",
            "history_days": 30,
        }

    def get_page(self) -> List[dict]:
        history = self.get_data("history") or []
        if not history:
            return [{'component': 'div', 'text': '暂无数据', 'props': {'class': 'text-center'}}]
        history = sorted(history, key=lambda x: x.get('date', ''), reverse=True)
        rows = [{'component': 'tr', 'content': [{'component': 'td', 'text': h.get('date', '-')}, {'component': 'td', 'text': h.get('status', '-')}, {'component': 'td', 'text': h.get('message', '-')}, {'component': 'td', 'text': str(h.get('points', '-'))}]} for h in history[:30]]
        return [
            {'component': 'VCard', 'content': [
                {'component': 'VCardTitle', 'text': '签到历史'},
                {'component': 'VTable', 'content': [
                    {'component': 'thead', 'content': [{'component': 'tr', 'content': [{'component': 'th', 'text': '时间'}, {'component': 'th', 'text': '状态'}, {'component': 'th', 'text': '消息'}, {'component': 'th', 'text': '积分'}]}]},
                    {'component': 'tbody', 'content': rows}
                ]}
            ]}
        ]

    def stop_service(self):
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._scheduler = None
