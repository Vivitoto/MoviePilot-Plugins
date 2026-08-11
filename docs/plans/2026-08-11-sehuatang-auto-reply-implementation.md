# 98签到自用：自动回帖实现计划

## 实施状态

- 2026-08-11：已完成 `SehuatangSignin` 独立自动回帖实现。
- 2026-08-11：已完成自动回帖新鲜度优化：候选优先新主题，详情页按最大主题天数硬过滤。
- 验证命令：`PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_sehuatang_captcha_nudge.py tests/test_sehuatang_auto_reply.py -q -p no:cacheprovider`
- 本地执行结果：`12 passed in 0.75s`

## Task 1 — 配置与元数据

- 已完成：bump `SehuatangSignin.plugin_version` 到 `1.1.0`。
- 已完成：更新 `package.v2.json` 中 `SehuatangSignin` 版本和 history，保留最近 6 条。
- 已完成：增加自动回帖配置默认值、成员变量、`_update_config()` 持久化。
- 已完成：在 `get_form()` 新增自动回帖配置卡片。
- 已完成：增加 `auto_reply_max_thread_age_days`，默认 7 天，填 0 关闭时间过滤。

验证：源码测试检查版本、history、默认配置字段。

## Task 2 — 调度计划

- 已完成：新增自动回帖数据 key。
- 已完成：初始化时注册每日计划刷新和当天账号 `date` jobs。
- 已完成：实现时间窗口解析、账号计划生成、重启恢复。
- 已完成：签到 scheduler 与自动回帖 scheduler 共用同一个 `BackgroundScheduler`，避免互相覆盖。

验证：纯函数测试窗口解析/计划生成；源码测试确认自动回帖不调用 `_do_signin()`。

## Task 3 — 只读抓取与候选过滤

- 已完成：复用 `fs_create_session`、`fs_get`、`fs_destroy_session`。
- 已完成：实现版块页提取候选 tid/title。
- 已完成：实现详情页提取标题、正文、作者、formhash、实际版块提示。
- 已完成：实现本地黑名单和规则/版务/公告类硬过滤。
- 已完成：从版块页/详情页解析 Discuz 常见时间格式并写入候选/详情元数据。
- 已完成：候选按新鲜度从新到旧排序，未知时间排在已知时间之后。
- 已完成：详情页超过最大主题天数时硬过滤；启用时间过滤但列表和详情都无法解析时间时保守跳过。

验证：用静态 HTML fixture 或纯字符串测试提取/过滤。

## Task 4 — 系统 LLM 判断与回复生成

- 已完成：新增 async LLM 调用包装，可在同步 scheduler 中用临时 event loop 执行。
- 已完成：使用 MoviePilot 系统 LLM；不可用直接跳过。
- 已完成：严格解析 JSON，过滤 think 标签和模型解释。
- 已完成：只接受 `risk_level=low` 且 `should_reply=true`。

验证：mock/纯函数测试 JSON 提取和失败策略。

## Task 5 — 回复提交与记录

- 已完成：实现 Discuz reply POST。
- 已完成：成功/失败解析，失败不重试提交。
- 已完成：按账号记录 history 和 replied thread。
- 已完成：通知摘要。

验证：不真实发帖；源码/单元测试确认 POST 函数可被 mock，失败不重试。

## Task 6 — 回归测试

- 已完成：跑现有 `test_sehuatang_captcha_nudge.py`。
- 已完成：跑新增自动回帖测试。
- 已完成：检查 `git diff`；仓库仍存在任务开始前已有的聚影签到和 `package.json` 改动，未纳入本次实现。
