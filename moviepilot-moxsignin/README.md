# MoviePilot Mox SignIn Plugin

一个面向 MoviePilot V2 的第三方插件仓库，提供 `MoxSignIn` 插件：

- 自动访问 `https://mox.moxing.chat/forum/sign`
- 自动登录
- 自动获取验证码并提交签到抽奖
- 返回签到结果和中奖信息
- 支持代理访问
- 支持手动触发和定时执行

## 目录结构

- `plugins.v2/moxsignin/__init__.py`
- `package.v2.json`

## 配置说明

插件配置中需要填写：

- 用户名
- 密码
- 代理地址（默认 `http://192.168.31.216:7890`）
- 执行 CRON
- 是否发送通知
- 时区自动设置值（默认 `Asia/Shanghai`）

## 安全说明

- 账号密码仅通过插件配置保存，不应写入代码仓库。
- 发布前请确认仓库中不包含真实凭据。

## 已知站点前端弹窗

前端页面中至少存在两类弹窗：

1. **时区选择弹窗**：当账号没有设置 timezone 时会弹出，插件可自动提交配置。
2. **签到卡介绍弹窗**：前端通过 localStorage 控制，仅影响网页交互，不影响后端签到接口自动化。
