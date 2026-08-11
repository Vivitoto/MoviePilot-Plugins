# 98签到自用：自动回帖独立功能设计

## 目标

在现有 `SehuatangSignin` 插件中新增一个与手动签到互不耦合的“自动回帖”功能：

1. 每天在用户配置的时间窗口内，为每个配置账号随机选择一个执行时间。
2. 到点后自动抓取用户配置版块中的候选帖子。
3. 先做本地硬规则过滤，再调用 MoviePilot 系统 LLM 做钓鱼贴/版务贴/不适合回复判断和回复生成。
4. 只有 AI 明确判断低风险且返回合格回复时才提交回帖。
5. AI 不可用、判断失败、输出不合格、中高风险、无候选帖时，该账号当天跳过回帖。
6. 保持现有 `/sht_signin` 手动验证码签到流程不变；自动回帖不触发签到，签到不依赖自动回帖。

## 非目标

- 不实现“自动回帖后签到”。
- 不改现有验证码 relay、验证码校验、签到提交流程。
- 不实现绕过站点风控/反脚本检测的拟人化行为策略。
- 不在 AI 不可用时使用模板强行回帖。
- 不把版块 `141,166` 写死；它只是默认/示例配置。

## 参考依据

Media Saber 镜像中可见的 `sign98` 线索只作为实现思路参考，不能完整复制：

- 动态表单字段：`forumIds`、`useAI`、`customModel`、`replays`、`customPrompt`、标题/内容/作者黑名单。
- 二进制字符串/方法名：`GetRandomArticle`、`getRandomArticleFromForum`、`isModerationPost`、`isValidThreadContent`、`isThreadReplied`、`polishReplyContent`、`filterAIContent`、`filterThinkTags`。
- 本地只读探测确认：FlareSolverr + 代理 + Cookie 可获取 `141`/`166` 版块页和帖子详情；版块页样本会混入规则/公告/跨版块链接，因此必须详情页校验和 AI 兜底。

## 配置设计

新增配置项：

- `auto_reply_enabled`: 是否启用自动回帖。
- `auto_reply_window_start`: 每日自动回帖时间窗口开始，默认 `09:00`。
- `auto_reply_window_end`: 每日自动回帖时间窗口结束，默认 `12:00`。
- `auto_reply_forum_ids`: 可选版块 ID，默认示例 `141,166`；留空则不执行自动回帖。
- `auto_reply_templates`: 回复模板，每行一条；AI 必须参考模板生成回复。
- `auto_reply_custom_prompt`: AI 补充提示。
- `auto_reply_title_blacklist`: 标题黑名单，每行一条。
- `auto_reply_content_blacklist`: 内容黑名单，每行一条。
- `auto_reply_author_blacklist`: 作者黑名单，每行一条。
- `auto_reply_max_candidates`: 每账号每次最多进入详情/AI 的候选数，默认 5。
- `auto_reply_max_thread_age_days`: 主题最大天数，默认 7；填 `0` 关闭时间过滤。
- `auto_reply_min_interval_minutes`: 多账号当天随机时间点之间的最小间隔，默认 10。

保留/复用现有配置：

- `base_url`
- `flaresolverr_url`
- `use_flaresolverr`
- `proxy_url`
- 多账号 Cookie 配置
- `notify`

## 调度设计

插件初始化时：

1. 解析账号。
2. 启动验证码 relay（保留现状）。
3. 若开启自动回帖，生成并注册当天每个账号的 `date` job。
4. 同时注册一个每日计划刷新 job，在每天凌晨为当天账号重新生成时间。

多账号计划：

- 每个账号每天最多一次自动回帖。
- 每个账号从窗口内随机取一个时间。
- 尽量满足账号之间最小间隔；若窗口过短，不强求，记录日志。
- 计划保存到插件数据 `auto_reply_plan`，用于详情页展示和重启恢复。

重启恢复：

- 若当天已有未过期计划，则恢复未执行任务。
- 已过期未执行任务默认跳过，不补跑，避免重启后集中请求。

## 访问和限流

每个账号回帖任务：

- 创建一个 FlareSolverr session。
- 所有请求使用账号 Cookie 和配置代理。
- 请求间隔使用保守固定策略：版块页/详情页间 sleep 8–12 秒。
- 每个账号最多处理 `auto_reply_max_candidates` 个候选详情。
- 候选按解析到的主题发布时间/最后活动时间从新到旧排序；没有时间的候选排在已知时间之后。
- 所有请求都是 GET，直到最终通过 AI 判定后才 POST 提交回复。
- 最终提交使用同一 FlareSolverr session/cookies/formhash。

## 候选抓取和硬过滤

候选来源：

- 用户配置的 `auto_reply_forum_ids`。
- 从每个版块页提取 `tid`、标题、基础链接。
- 尽量从 Discuz 版块页和详情页提取主题发布时间/最后活动时间，支持常见绝对日期、`MM-DD`、今天/昨天/前天、分钟前/小时前/天前等格式。
- 进入详情页后再次确认实际标题/正文/作者/版块/是否可回复。

硬过滤：

- 已回复过的 `tid`。
- 超过 `auto_reply_max_thread_age_days` 的主题；开启时间过滤时，详情页和候选列表均没有可解析时间的主题按保守策略跳过。
- 黑名单标题/内容/作者。
- 明显规则、版规、公告、置顶、投诉建议、访问方法、教程、管理类关键词。
- 无回复表单、无 `formhash`、权限异常、封禁/安全入口/Cloudflare challenge 页面。
- 实际版块不在用户配置允许集合时跳过。

硬过滤命中 `blocked` 的帖子不进入 AI。

## AI 设计

优先调用 MoviePilot 系统 LLM：

```python
from app.agent.llm import LLMHelper
llm = await LLMHelper.get_llm(streaming=False)
response = await llm.ainvoke(prompt)
```

兼容性：

- 若当前 MoviePilot 版本没有 `app.agent.llm` 或 LLM 未配置，自动回帖直接跳过并通知。
- 不新增独立 API Key 配置，避免重复管理密钥。

AI 必须返回 JSON：

```json
{
  "should_reply": true,
  "risk_level": "low",
  "risk_reasons": [],
  "reply": "..."
}
```

接受条件：

- `should_reply == true`
- `risk_level == "low"`
- `reply` 长度在合理范围内。
- `reply` 不含 `<think>` / `</think>` / 明显模型解释 / JSON 残留。

失败策略：

- AI 不可用、超时、JSON 解析失败、输出不合格、中高风险或不确定：跳过当天该账号回帖。

## 提交回复

提交前：

- 从详情页提取 `formhash`。
- 尽量使用 Discuz 常见 endpoint：`forum.php?mod=post&action=reply&fid=<fid>&tid=<tid>&extra=&replysubmit=yes&infloat=yes&handlekey=fastpost&inajax=1`。
- POST body 包含 `formhash`、`message`、`usesig=1`、`subject=` 等常见字段。

提交后：

- 判断响应中是否包含成功/失败/权限/验证码/安全入口信息。
- 记录结果，不重试提交，避免重复回复。

## 数据记录

新增数据 key：

- `auto_reply_plan`: 当天计划。
- `auto_reply_history`: 最近自动回帖记录。
- `auto_replied_threads`: 已回复 tid 列表，按账号隔离。

记录字段：

- 时间、账号、fid、tid、标题、结果、跳过原因、AI 风险原因、回复摘要。

## UI / 详情页

配置页新增“自动回帖”卡片。

详情页新增自动回帖总览：

- 今日计划。
- 最近回帖记录。
- 每账号状态。

## 测试策略

不做真实发帖单元测试。新增源码级/纯函数测试：

- 版本和 package 元数据一致。
- 新配置默认值存在。
- AI 失败策略源码保护：无 AI fallback 模板提交。
- Media Saber 参考相关字段/黑名单/论坛 ID 配置存在。
- 现有 `/sht_signin`、验证码流程关键字符串仍存在，确保未被耦合。

## 发布注意

- 本次只做本地修改和测试，不推送、不发布。
- 仓库已有聚影签到未提交改动，提交/推送前必须单独处理，避免混入。
