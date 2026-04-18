import os
import re
import json
import time
import random
import asyncio
import base64
import requests
from datetime import datetime
from typing import Dict, Any, Optional, List
from playwright.async_api import async_playwright, Page, Browser

from app.core.config import settings
from app.core.event import eventmanager
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType


class SehuatangSignin(_PluginBase):
    # 插件基础信息
    plugin_name = "98签到自用"
    plugin_desc = "自动登录98账号进行签到"
    plugin_icon = "https://raw.githubusercontent.com/Vivitoto/MoviePilot-Plugins/main/icons/shtsignin.png"
    plugin_version = "0.0.3"
    plugin_author = "Vivitoto"
    author_url = "https://github.com/Vivitoto"
    plugin_config_prefix = "sehuatang_"
    plugin_order = 1
    auth_level = 1

    # 安全问题选项（和色花堂网站一致）
    SECURITY_QUESTIONS = [
        {"value": "", "text": "无安全提问"},
        {"value": "1", "text": "母亲的名字"},
        {"value": "2", "text": "爷爷的名字"},
        {"value": "3", "text": "父亲出生的城市"},
        {"value": "4", "text": "您其中一位老师的名字"},
        {"value": "5", "text": "您个人计算机的型号"},
        {"value": "6", "text": "您最喜欢的餐馆名称"},
        {"value": "7", "text": "驾驶执照的最后四位数字"}
    ]

    # 状态
    _enabled = False
    _accounts = []
    _base_url = "https://sehuatang.net/"
    _sign_url = "https://sehuatang.net/plugin.php?id=dd_sign"
    _proxy = None
    _retry_count = 3
    _delay_min = 5
    _delay_max = 15
    _ai_api_url = "http://192.168.31.192:3000/api/v1/chat"
    _ai_agent_id = "main"
    _notify = True
    _onlyonce = False
    _cron = None
    _lock = asyncio.Lock()

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._accounts = config.get("accounts", [])
            self._base_url = config.get("base_url", "https://sehuatang.net/")
            self._sign_url = f"{self._base_url.rstrip('/')}/plugin.php?id=dd_sign"
            self._proxy = config.get("proxy", "")
            self._retry_count = int(config.get("retry_count", 3))
            self._delay_min = int(config.get("delay_min", 5))
            self._delay_max = int(config.get("delay_max", 15))
            self._ai_api_url = config.get("ai_api_url", "http://192.168.31.192:3000/api/v1/chat")
            self._ai_agent_id = config.get("ai_agent_id", "main")
            self._notify = config.get("notify", True)
            self._cron = config.get("cron", "0 9 * * *")
            self._onlyonce = config.get("onlyonce", False)

        if self._onlyonce:
            self._onlyonce = False
            self._update_config()
            asyncio.create_task(self.signin())

    def _update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "accounts": self._accounts,
            "base_url": self._base_url,
            "proxy": self._proxy,
            "retry_count": self._retry_count,
            "delay_min": self._delay_min,
            "delay_max": self._delay_max,
            "ai_api_url": self._ai_api_url,
            "ai_agent_id": self._ai_agent_id,
            "notify": self._notify,
            "cron": self._cron,
            "onlyonce": self._onlyonce
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> list[dict]:
        return [{
            "cmd": "/sht_signin",
            "event": EventType.PluginAction,
            "desc": "执行色花堂签到",
            "category": "",
            "data": {"action": "sht_signin"}
        }]

    def get_api(self) -> list[dict]:
        return [{
            "path": "/sehuatang/signin",
            "endpoint": self.signin,
            "methods": ["GET"],
            "summary": "执行色花堂签到"
        }]

    def get_service(self) -> list[dict]:
        if self._enabled and self._cron:
            return [{
                "id": "SehuatangSignin",
                "name": "色花堂签到",
                "trigger": "cron",
                "func": self.signin,
                "kwargs": {"cron": self._cron}
            }]
        return []

    def get_form(self) -> tuple[list[dict], dict]:
        question_options = [{"title": q["text"], "value": q["value"]} for q in self.SECURITY_QUESTIONS]
        
        # 构建账号列表
        account_list_component = {
            'component': 'VDataTable',
            'props': {
                'items': 'accounts',
                'headers': [
                    {'text': '用户名', 'value': 'username'},
                    {'text': '安全问题', 'value': 'question_text'},
                    {'text': '操作', 'value': 'actions', 'sortable': False}
                ],
                'item-value': 'username',
                'class': 'elevation-1 mb-4'
            },
            'slots': {
                'item.question_text': 'getQuestionText(item.question_id)',
                'item.actions': 'deleteBtn(slotProps)'
            }
        } if self._accounts else {
            'component': 'VAlert',
            'props': {'type': 'warning'},
            'text': '暂无账号，点击下方按钮添加'
        }

        form_content = [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知'
                                        }
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
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'base_url',
                                            'label': '网站地址',
                                            'placeholder': 'https://sehuatang.net/',
                                            'hint': '色花堂网站地址（末尾可加/或不加）'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'proxy',
                                            'label': '代理地址',
                                            'placeholder': 'http://192.168.31.216:7890',
                                            'hint': '访问网站使用的代理（留空则不使用）'
                                        }
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
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '定时周期',
                                            'placeholder': '0 9 * * *',
                                            'hint': 'Cron表达式，默认每天9点执行'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VDivider',
                        'props': {'class': 'my-4'}
                    },
                    {
                        'component': 'div',
                        'props': {'class': 'text-h6 mb-2'},
                        'text': '账号列表'
                    },
                    account_list_component,
                    # 添加账号区域
                    {
                        'component': 'VCard',
                        'props': {'class': 'mt-4'},
                        'content': [
                            {
                                'component': 'VCardTitle',
                                'text': '添加新账号'
                            },
                            {
                                'component': 'VCardText',
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 6},
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'new_account.username',
                                                            'label': '用户名'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 6},
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'new_account.password',
                                                            'label': '密码',
                                                            'type': 'password'
                                                        }
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
                                                'props': {'cols': 12, 'md': 6},
                                                'content': [
                                                    {
                                                        'component': 'VSelect',
                                                        'props': {
                                                            'model': 'new_account.question_id',
                                                            'label': '安全问题',
                                                            'items': question_options,
                                                            'clearable': True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 6},
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'new_account.answer',
                                                            'label': '安全答案'
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VBtn',
                                        'props': {
                                            'color': 'primary',
                                            'click': 'addAccount'
                                        },
                                        'text': '添加账号'
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VDivider',
                        'props': {'class': 'my-4'}
                    },
                    {
                        'component': 'div',
                        'props': {'class': 'text-h6 mb-2'},
                        'text': '风控设置'
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'retry_count',
                                            'label': '最大重试次数',
                                            'type': 'number'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'delay_min',
                                            'label': '最小延迟(秒)',
                                            'type': 'number'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'delay_max',
                                            'label': '最大延迟(秒)',
                                            'type': 'number'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VAlert',
                        'props': {
                            'type': 'info',
                            'class': 'mt-2 mb-4',
                            'dense': True
                        },
                        'text': f'账号间随机延迟 {self._delay_min}-{self._delay_max} 秒，每个账号独立浏览器实例'
                    },
                    {
                        'component': 'VDivider',
                        'props': {'class': 'my-4'}
                    },
                    {
                        'component': 'div',
                        'props': {'class': 'text-h6 mb-2'},
                        'text': '验证设置'
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 8},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'ai_api_url',
                                            'label': 'AI分析API地址',
                                            'placeholder': 'http://192.168.31.192:3000/api/v1/chat'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'ai_agent_id',
                                            'label': 'AI Agent ID',
                                            'placeholder': 'main'
                                        }
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
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即执行一次'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VDivider',
                        'props': {'class': 'my-4'}
                    },
                    {
                        'component': 'VAlert',
                        'props': {
                            'type': 'warning',
                            'variant': 'tonal',
                            'class': 'mt-2'
                        },
                        'text': '容器内需先安装依赖后再使用本插件。进入 MoviePilot 容器后执行：\n1. pip install playwright requests\n2. python -m playwright install chromium\n若容器里没有 pip/python，请改用 pip3/python3 对应命令。'
                    }
                ]
            }
        ]
        
        form_data = {
            "enabled": self._enabled,
            "accounts": self._accounts,
            "base_url": self._base_url,
            "proxy": self._proxy,
            "retry_count": self._retry_count,
            "delay_min": self._delay_min,
            "delay_max": self._delay_max,
            "ai_api_url": self._ai_api_url,
            "ai_agent_id": self._ai_agent_id,
            "notify": self._notify,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "new_account": {"username": "", "password": "", "question_id": "", "answer": ""}
        }
        
        form_data["getQuestionText"] = self._get_question_text
        
        return form_content, form_data

    def _get_question_text(self, question_id: str) -> str:
        for q in self.SECURITY_QUESTIONS:
            if q["value"] == question_id:
                return q["text"]
        return ""

    def get_page(self) -> dict:
        account_count = len(self._accounts) if self._accounts else 0
        return {
            "title": "色花堂签到",
            "content": f"""
            <div style="padding: 20px;">
                <h2>98签到自用</h2>
                <p>当前状态: {'<span style="color: green;">已启用</span>' if self._enabled else '<span style="color: red;">已禁用</span>'}</p>
                <p>网站地址: {self._base_url}</p>
                <p>配置账号数: {account_count}</p>
                <p>延迟设置: {self._delay_min}-{self._delay_max} 秒</p>
            </div>
            """
        }

    def stop_service(self):
        pass

    async def signin(self, *args, **kwargs):
        """主签到流程 - 每个账号独立浏览器，间隔随机延迟"""
        async with self._lock:
            if not self._enabled:
                return

            env_error = await self._check_runtime_env()
            if env_error:
                self.log_error(env_error)
                await self._send_notify("98签到自用环境缺失", env_error)
                return

            if not self._accounts:
                await self._send_notify("色花堂签到失败", "没有配置任何账号")
                return

            valid_accounts = [a for a in self._accounts if a.get("username") and a.get("password")]
            if not valid_accounts:
                await self._send_notify("色花堂签到失败", "没有有效的账号配置")
                return

            self.log_info(f"开始色花堂签到，共 {len(valid_accounts)} 个账号，延迟范围 {self._delay_min}-{self._delay_max} 秒...")
            start_time = time.time()
            results = []

            for idx, account in enumerate(valid_accounts):
                username = account.get("username", "")
                
                # 第一个账号不延迟，后续账号随机延迟
                if idx > 0:
                    delay = random.randint(self._delay_min, self._delay_max)
                    self.log_info(f"账号 {username} 等待 {delay} 秒后执行...")
                    await asyncio.sleep(delay)
                
                try:
                    self.log_info(f"========== 开始处理账号 {idx+1}/{len(valid_accounts)}: {username} ==========")
                    
                    # 每个账号独立启动浏览器，执行完立即关闭
                    result = await self._do_signin_for_account(account)
                    results.append({"username": username, **result})
                    
                    self.log_info(f"账号 {username} 处理完成: {'成功' if result.get('success') else '失败'}")
                    
                except Exception as e:
                    self.log_error(f"账号 {username} 签到异常: {str(e)}")
                    results.append({"username": username, "success": False, "message": f"异常: {str(e)}"})

            duration = round(time.time() - start_time, 2)
            success_count = sum(1 for r in results if r["success"])
            
            summary = f"成功: {success_count}/{len(results)}, 耗时: {duration}秒\n\n"
            for r in results:
                status = "✅" if r["success"] else "❌"
                summary += f"{status} {r['username']}: {r['message']}\n"

            if self._notify:
                title = "色花堂签到完成" if success_count == len(results) else "色花堂签到部分失败"
                await self._send_notify(title, summary)

    async def _do_signin_for_account(self, account: dict) -> dict:
        """为单个账号执行签到 - 独立浏览器实例"""
        result = {"success": False, "message": ""}
        username = account.get("username", "")
        password = account.get("password", "")
        question_id = account.get("question_id", "")
        answer = account.get("answer", "")
        
        browser = None
        
        try:
            async with async_playwright() as p:
                # 启动浏览器
                browser_args = {"headless": True}
                if self._proxy:
                    browser_args["proxy"] = {"server": self._proxy}
                
                browser = await p.chromium.launch(**browser_args)
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                )
                page = await context.new_page()
                
                # 1. 访问首页
                self.log_info(f"[{username}] 访问色花堂首页 ({self._base_url})...")
                page_start = time.time()
                await page.goto(self._base_url, wait_until="domcontentloaded", timeout=30000)
                page_load_time = round(time.time() - page_start, 2)
                self.log_info(f"[{username}] 首页加载完成，耗时 {page_load_time} 秒")
                await asyncio.sleep(2)
                
                # 2. 处理年龄验证
                age_link = await page.query_selector('a:has-text("满18岁")')
                if age_link:
                    self.log_info(f"[{username}] 发现年龄验证，点击进入...")
                    await age_link.click()
                    await asyncio.sleep(2)
                
                # 3. 登录
                self.log_info(f"[{username}] 开始登录流程...")
                login_start = time.time()
                
                # 点击登录链接
                self.log_info(f"[{username}] 点击登录入口...")
                await page.evaluate("""() => {
                    const links = document.querySelectorAll('a');
                    for (const a of links) {
                        if (a.textContent && a.textContent.trim() === '登录') {
                            a.click();
                            break;
                        }
                    }
                }""")
                await asyncio.sleep(3)
                
                # 填写用户名
                self.log_info(f"[{username}] 填写用户名...")
                for sel in ['input[name="username"]', 'input[name="UserName"]', '#username']:
                    try:
                        await page.fill(sel, username, timeout=2000)
                        self.log_info(f"[{username}] 用户名已填写")
                        break
                    except:
                        continue
                
                # 填写密码
                self.log_info(f"[{username}] 填写密码...")
                for sel in ['input[name="password"]', 'input[type="password"]']:
                    try:
                        await page.fill(sel, password, timeout=2000)
                        self.log_info(f"[{username}] 密码已填写")
                        break
                    except:
                        continue
                
                # 选择安全问题
                if question_id:
                    question_text = self._get_question_text(question_id)
                    self.log_info(f"[{username}] 选择安全问题: {question_text}")
                    question_found = False
                    for sel in ['select[name="questionid"]', 'select[name="question"]', '#questionid']:
                        try:
                            options = await page.query_selector_all(f'{sel} option')
                            for opt in options:
                                text = await opt.text_content()
                                if text and question_text in text:
                                    value = await opt.get_attribute("value")
                                    await page.select_option(sel, value)
                                    self.log_info(f"[{username}] 安全问题已选择 (value={value})")
                                    question_found = True
                                    break
                            if question_found:
                                break
                        except:
                            continue
                    if not question_found:
                        self.log_error(f"[{username}] 未能选择安全问题: {question_text}")
                
                # 填写答案
                if answer:
                    self.log_info(f"[{username}] 填写安全答案...")
                    for sel in ['input[name="answer"]', 'input[name="secanswer"]', '#answer']:
                        try:
                            await page.fill(sel, answer, timeout=2000)
                            self.log_info(f"[{username}] 安全答案已填写")
                            break
                        except:
                            continue
                
                # 点击登录
                self.log_info(f"[{username}] 点击登录按钮...")
                login_clicked = False
                for sel in ['button[type="submit"]', 'input[type="submit"]', '.btn-login']:
                    try:
                        btn = await page.query_selector(sel)
                        if btn:
                            await btn.click()
                            login_clicked = True
                            self.log_info(f"[{username}] 登录按钮已点击 (selector: {sel})")
                            break
                    except:
                        continue
                
                if not login_clicked:
                    self.log_error(f"[{username}] 未能点击登录按钮")
                    result["message"] = "登录按钮点击失败"
                    return result
                
                await asyncio.sleep(5)
                login_time = round(time.time() - login_start, 2)
                
                # 检查登录结果
                page_content = await page.content()
                if username not in page_content and "退出" not in page_content and "欢迎" not in page_content:
                    self.log_error(f"[{username}] 登录失败，页面未检测到登录状态，耗时 {login_time} 秒")
                    result["message"] = "登录失败，请检查账号密码"
                    return result
                
                self.log_info(f"[{username}] 登录成功，耗时 {login_time} 秒")
                
                # 4. 访问签到页面
                self.log_info(f"[{username}] 访问签到页面 ({self._sign_url})...")
                sign_page_start = time.time()
                await page.goto(self._sign_url, wait_until="domcontentloaded", timeout=30000)
                sign_page_time = round(time.time() - sign_page_start, 2)
                self.log_info(f"[{username}] 签到页面加载完成，耗时 {sign_page_time} 秒")
                await asyncio.sleep(2)
                
                # 5. 点击签到
                sign_btn = await page.query_selector("#signin-btn")
                if not sign_btn:
                    self.log_error(f"[{username}] 未找到签到按钮 (#signin-btn)")
                    result["message"] = "未找到签到按钮"
                    return result
                
                btn_text = await sign_btn.text_content()
                self.log_info(f"[{username}] 签到按钮状态: '{btn_text}'")
                
                if "已签到" in btn_text or "今日已" in btn_text:
                    self.log_info(f"[{username}] 今日已签到，无需重复操作")
                    result["success"] = True
                    result["message"] = "今日已签到"
                    return result
                
                # 处理验证码循环
                captcha_start_time = time.time()
                for attempt in range(1, self._retry_count + 1):
                    self.log_info(f"[{username}] ========== 签到尝试 {attempt}/{self._retry_count} ==========")
                    self.log_info(f"[{username}] 点击签到按钮...")
                    await sign_btn.click()
                    await asyncio.sleep(3)
                    
                    # 截图并分析验证码
                    self.log_info(f"[{username}] 截图并分析验证码...")
                    ai_start = time.time()
                    screenshot = await page.screenshot()
                    captcha_info = await self._analyze_captcha(base64.b64encode(screenshot).decode())
                    ai_time = round(time.time() - ai_start, 2)
                    
                    if captcha_info:
                        captcha_type = captcha_info.get("type", "unknown")
                        self.log_info(f"[{username}] AI分析完成，耗时 {ai_time} 秒，验证码类型: {captcha_type}")
                        
                        if captcha_type == "none":
                            self.log_info(f"[{username}] 无需验证码处理")
                        elif captcha_type != "unknown":
                            coords = captcha_info.get("coordinates", [{}])
                            if coords:
                                c = coords[0]
                                self.log_info(f"[{username}] 执行验证码操作: {captcha_type}，坐标 ({c['x']},{c['y']}) -> ({c.get('to_x', c['x'])},{c.get('to_y', c['y'])})")
                                await page.mouse.move(c["x"], c["y"])
                                await page.mouse.down()
                                await page.mouse.move(c.get("to_x", c["x"]), c.get("to_y", c["y"]), steps=20)
                                await page.mouse.up()
                                self.log_info(f"[{username}] 验证码操作执行完成")
                                await asyncio.sleep(2)
                            else:
                                self.log_error(f"[{username}] 验证码坐标信息缺失")
                        else:
                            self.log_error(f"[{username}] 无法识别验证码类型")
                    else:
                        self.log_error(f"[{username}] AI分析失败，耗时 {ai_time} 秒")
                    
                    # 检查是否成功
                    sign_btn = await page.query_selector("#signin-btn")
                    if sign_btn:
                        btn_text = await sign_btn.text_content()
                        self.log_info(f"[{username}] 签到按钮当前状态: '{btn_text}'")
                        
                        if "已签到" in btn_text or "今日已" in btn_text:
                            total_captcha_time = round(time.time() - captcha_start_time, 2)
                            self.log_info(f"[{username}] 签到成功！验证码处理总耗时 {total_captcha_time} 秒，共尝试 {attempt} 次")
                            result["message"] = f"签到成功 (尝试{attempt}次)"
                            result["success"] = True
                            return result
                    else:
                        self.log_info(f"[{username}] 未找到签到按钮，可能页面已跳转或签到成功")
                        result["success"] = True
                        result["message"] = f"签到可能成功 (尝试{attempt}次)"
                        return result
                
                total_captcha_time = round(time.time() - captcha_start_time, 2)
                self.log_error(f"[{username}] 达到最大重试次数({self._retry_count})，验证码处理总耗时 {total_captcha_time} 秒")
                result["message"] = f"达到最大重试次数({self._retry_count})，签到失败"
                
        except Exception as e:
            result["message"] = f"执行异常: {str(e)}"
            
        finally:
            # 确保浏览器关闭
            if browser:
                try:
                    await browser.close()
                    self.log_info(f"[{username}] 浏览器已关闭")
                except:
                    pass
        
        return result

    async def _analyze_captcha(self, screenshot_b64: str) -> Optional[dict]:
        try:
            prompt = """分析验证码类型和坐标。返回JSON：{"type": "slide|rotate|jigsaw|text|none", "coordinates": [{"x": 100, "y": 200, "to_x": 300, "to_y": 200}]}"""
            
            response = requests.post(
                self._ai_api_url,
                json={
                    "agentId": self._ai_agent_id,
                    "message": prompt,
                    "attachments": [{"type": "image", "data": screenshot_b64}]
                },
                timeout=60
            )
            
            if response.status_code == 200:
                resp_text = response.json().get("response", "")
                json_match = re.search(r'\{[\s\S]*\}', resp_text)
                if json_match:
                    return json.loads(json_match.group())
            return None
        except Exception as e:
            self.log_error(f"AI分析请求失败: {e}")
            return None

    async def _check_runtime_env(self) -> Optional[str]:
        """检查运行环境，缺失时返回错误文本"""
        try:
            import importlib.util
            if importlib.util.find_spec("playwright") is None:
                return "缺少 Python 依赖 playwright。请在 MoviePilot 容器内执行：pip install playwright requests"
        except Exception:
            return "Python 环境异常，无法检查 playwright 依赖。"

        try:
            test = await async_playwright().start()
            browser = await test.chromium.launch(headless=True)
            await browser.close()
            await test.stop()
            return None
        except Exception as e:
            msg = str(e)
            if "Executable doesn't exist" in msg or "playwright install" in msg:
                return "缺少 Chromium 浏览器运行时。请在 MoviePilot 容器内执行：python -m playwright install chromium"
            return f"Playwright/Chromium 环境不可用：{msg}"

    async def _send_notify(self, title: str, message: str):
        try:
            from app.chain.message import MessageChain
            MessageChain().send_message(title=title, text=message)
        except:
            pass

    def log_info(self, msg: str):
        self.info(f"[色花堂签到] {msg}")

    def log_error(self, msg: str):
        self.error(f"[色花堂签到] {msg}")
