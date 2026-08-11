import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_INIT = ROOT / "plugins.v2" / "sehuatangsignin" / "__init__.py"


def _source() -> str:
    return PLUGIN_INIT.read_text(encoding="utf-8")


def _class_node(source: str) -> ast.ClassDef:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "SehuatangSignin":
            return node
    raise AssertionError("SehuatangSignin class not found")


def _method_source(source: str, name: str) -> str:
    lines = source.splitlines()
    klass = _class_node(source)
    for node in klass.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{name} method not found")


def _method_node(source: str, name: str) -> ast.FunctionDef:
    klass = _class_node(source)
    for node in klass.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} method not found")


def _if_guard_for_name(method_node: ast.FunctionDef, name: str) -> ast.If:
    for node in ast.walk(method_node):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.UnaryOp)
            and isinstance(test.op, ast.Not)
            and isinstance(test.operand, ast.Name)
            and test.operand.id == name
        ):
            return node
    raise AssertionError(f"if not {name} guard not found")


class SehuatangAutoReplyTest(unittest.TestCase):
    def test_auto_reply_plugin_version_is_current(self):
        source = _source()
        self.assertIn('plugin_version = "1.1.0"', source)

    def test_auto_reply_defaults_and_data_keys_exist(self):
        source = _source()

        for token in [
            '_auto_reply_enabled = False',
            '_auto_reply_onlyonce = False',
            '_auto_reply_window_start = "09:00"',
            '_auto_reply_window_end = "12:00"',
            '_auto_reply_forum_ids = "141,166"',
            '_auto_reply_max_candidates = 0',
            '_auto_reply_max_attempts_per_day = 1',
            '_auto_reply_max_thread_age_days = 7',
            '_auto_reply_min_interval_minutes = 10',
            '_auto_reply_plan_key = "auto_reply_plan"',
            '_auto_reply_history_key = "auto_reply_history"',
            '_auto_replied_threads_key = "auto_replied_threads"',
            '_auto_reply_success_key = "auto_reply_success_by_day"',
            '"auto_reply_forum_ids": "141,166"',
            '"auto_reply_onlyonce": False',
            '"auto_reply_max_attempts_per_day": 1',
            '"auto_reply_max_thread_age_days": 7',
        ]:
            self.assertIn(token, source)

    def test_expected_pure_helpers_are_present(self):
        source = _source()
        klass = _class_node(source)
        methods = {
            node.name for node in klass.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        for method in [
            "_parse_config_bool",
            "_parse_auto_reply_window",
            "_parse_forum_ids",
            "_parse_line_list",
            "_extract_thread_candidates",
            "_parse_auto_reply_thread_time",
            "_extract_auto_reply_time_metadata",
            "_sort_auto_reply_candidates_by_newness",
            "_auto_reply_detail_time_context",
            "_extract_formhash",
            "_strip_html",
            "_first_match_raw",
            "_hard_filter_auto_reply_candidate",
            "_is_auto_reply_sticky_context",
            "_has_auto_reply_risky_link",
            "_normalize_auto_reply_risk_text",
            "_has_auto_reply_contact_or_diversion_text",
            "_extract_auto_reply_post_authors",
            "_extract_auto_reply_logged_in_identity",
            "_extract_auto_reply_post_author_refs",
            "_has_auto_reply_fast_reply_form",
            "_preflight_auto_reply_submit",
            "_run_auto_reply_once",
            "_auto_reply_job_id",
            "_claim_auto_reply_run",
            "_release_auto_reply_run",
            "_auto_reply_account_aliases",
            "_auto_reply_current_account_has_replied_in_detail",
            "_auto_reply_thread_age_filter_reason",
            "_assess_auto_reply_with_ai",
            "_polish_auto_reply_with_ai",
            "_call_auto_reply_llm_with_timeout",
            "_call_auto_reply_llm",
            "_extract_llm_response_text",
            "_build_auto_reply_assessment_prompt",
            "_build_auto_reply_polish_prompt",
            "_extract_auto_reply_ai_json",
            "_extract_auto_reply_reply",
            "_validate_auto_reply_assessment",
            "_validate_auto_reply_text",
            "_extract_auto_reply_thread_detail",
            "_mark_auto_reply_success_for_day",
        ]:
            self.assertIn(method, methods)

    def test_auto_reply_prefers_newest_candidates_without_old_shuffle_or_hard_candidate_cap(self):
        source = _source()
        auto_body = _method_source(source, "_auto_reply_single")
        sort_body = _method_source(source, "_sort_auto_reply_candidates_by_newness")
        newness_body = _method_source(source, "_auto_reply_newness_timestamp")
        extract_body = _method_source(source, "_extract_thread_candidates")

        self.assertNotIn("random.shuffle(all_candidates)", auto_body)
        self.assertIn("_sort_auto_reply_candidates_by_newness(all_candidates)", auto_body)
        self.assertNotIn("all_candidates[:max_candidates]", auto_body)
        self.assertNotIn("max_candidates =", auto_body)
        self.assertIn("for candidate in all_candidates", auto_body)
        self.assertIn("continue", auto_body)
        self.assertLess(
            auto_body.index("_sort_auto_reply_candidates_by_newness(all_candidates)"),
            auto_body.index("for candidate in all_candidates"),
        )
        self.assertIn("fresh_at_ts", newness_body)
        self.assertIn("reverse=True", sort_body)
        self.assertIn("time_context = context.replace(match.group(0), \" \")", extract_body)
        self.assertIn("_extract_auto_reply_time_metadata(time_context)", extract_body)

    def test_auto_reply_max_attempts_are_per_account_retries_until_success(self):
        source = _source()
        init_body = _method_source(source, "init_plugin")
        form_source = _method_source(source, "get_form")
        plan_body = _method_source(source, "_generate_auto_reply_plan")
        schedule_body = _method_source(source, "_schedule_auto_reply_jobs_for_today")
        run_body = _method_source(source, "_run_auto_reply_for_account")
        skip_body = _method_source(source, "_skip_remaining_auto_reply_plan_jobs")

        self.assertIn("auto_reply_max_attempts_per_day", init_body)
        self.assertIn("min(10", init_body)
        self.assertIn("每日最大回帖尝试次数", form_source)
        self.assertIn("失败/跳过才在窗口内重试", form_source)
        self.assertIn("成功一次后当天后续尝试自动跳过", form_source)
        self.assertIn("account_times: List[datetime] = []", plan_body)
        self.assertIn("account_times.sort()", plan_body)
        self.assertIn("enumerate(account_times, start=1)", plan_body)
        self.assertIn("for item in account_times", plan_body)
        self.assertIn('"max_attempts_per_day": max_attempts', plan_body)
        self.assertIn('job.get("status") not in ("scheduled", "pending", "")', schedule_body)
        self.assertIn("_auto_reply_job_id", schedule_body)
        self.assertIn("_has_auto_reply_success_for_day", _method_source(source, "_claim_auto_reply_run"))
        self.assertIn("_skip_remaining_auto_reply_plan_jobs(account_id, plan_date, attempt_index)", run_body)
        self.assertIn('job.get("status") in ("scheduled", "pending", "")', skip_body)
        self.assertIn('job["status"] = "skipped"', skip_body)
        self.assertIn("self._scheduler.remove_job(job_id)", skip_body)
        self.assertIn("今日已成功回帖，后续尝试跳过", skip_body)

    def test_auto_reply_hard_filters_old_or_unknown_detail_time(self):
        source = _source()
        hard_filter_body = _method_source(source, "_hard_filter_auto_reply_candidate")
        age_body = _method_source(source, "_auto_reply_thread_age_filter_reason")
        detail_body = _method_source(source, "_extract_auto_reply_thread_detail")

        self.assertIn("_auto_reply_thread_age_filter_reason(item, require_detail=require_detail)", hard_filter_body)
        self.assertIn("_auto_reply_max_thread_age_days", age_body)
        self.assertIn("max_age_days <= 0", age_body)
        self.assertIn("详情未解析到主题时间", age_body)
        self.assertIn("详情未解析到可信主题发布时间", age_body)
        self.assertIn("published_time_source", age_body)
        self.assertIn("主题时间超过", age_body)
        self.assertIn("time_context = cls._auto_reply_detail_time_context(html_text)", detail_body)
        self.assertIn("for key in list(detail.keys())", detail_body)
        self.assertIn('key.startswith(("published_", "last_activity_", "fresh_"))', detail_body)
        self.assertIn("_extract_auto_reply_time_metadata(time_context)", detail_body)

    def test_auto_reply_hard_filters_admin_access_and_contact_posts_locally(self):
        source = _source()
        hard_filter_body = _method_source(source, "_hard_filter_auto_reply_candidate")
        blocked_page_body = _method_source(source, "_is_auto_reply_blocked_page")
        contact_body = _method_source(source, "_has_auto_reply_contact_or_diversion_text")
        author_body = _method_source(source, "_extract_auto_reply_post_authors")
        logged_in_body = _method_source(source, "_extract_auto_reply_logged_in_identity")
        author_refs_body = _method_source(source, "_extract_auto_reply_post_author_refs")
        normalize_body = _method_source(source, "_normalize_auto_reply_risk_text")

        for token in [
            "_auto_reply_title_blacklist",
            "_auto_reply_content_blacklist",
            "_auto_reply_author_blacklist",
            "_has_auto_reply_contact_or_diversion_text",
        ]:
            self.assertIn(token, hard_filter_body)

        for keyword in [
            "永久访问", "访问本站", "访问方法", "发布器", "白名单", "报毒", "安装",
            "备用网址", "最新地址", "防丢", "二次验证", "申诉", "官方", "通知",
            "教程", "版务", "站务", "安全入口", "验证入口", "加群", "私信",
            "联系方式", "Telegram", "TG", "QQ", "微信", "群号", "邀请码", "招聘", "破解",
        ]:
            self.assertIn(f'"{keyword}"', hard_filter_body)

        for marker in ["cf-challenge", "cf-turnstile", "challenge-platform", "Just a moment", "请完成安全验证", "访问过于频繁"]:
            self.assertIn(f'"{marker}"', blocked_page_body)
        for marker in ["加群", "私信", "联系方式", "站外", "外站", "最新地址", "备用网址", "访问方法"]:
            self.assertIn(marker, contact_body)
        for marker in ["telegram", "tg群", "vx", "防失联", "永久地址"]:
            self.assertIn(marker, contact_body)
        self.assertIn("unicodedata.normalize", normalize_body)
        self.assertIn("isalnum()", normalize_body)
        self.assertIn("账号已在详情页回复过该主题", hard_filter_body)
        manual_reply_body = _method_source(source, "_auto_reply_current_account_has_replied_in_detail")
        self.assertIn("post_authors", manual_reply_body)
        self.assertIn("_auto_reply_current_account_has_replied_in_detail(item, account_id)", hard_filter_body)
        self.assertIn("discuz_uid", logged_in_body)
        self.assertIn("id=[", logged_in_body)
        self.assertIn("um", logged_in_body)
        self.assertIn("post_author_refs", detail_body := _method_source(source, "_extract_auto_reply_thread_detail"))
        self.assertIn("account_identity", detail_body)
        self.assertIn("post_blocks", author_refs_body)
        self.assertIn("home\\.php\\?mod=space", author_refs_body)
        self.assertIn("authorid", author_body)

        self.assertIn("management_author_keywords", hard_filter_body)
        for author_keyword in ["admin", "管理员", "版主", "管理组"]:
            self.assertIn(f'"{author_keyword}"', hard_filter_body)
        self.assertIn("管理账号作者", hard_filter_body)
        self.assertIn("trap_keywords", hard_filter_body)
        for trap_keyword in ["自动回复检测", "回帖后识别", "真人验证", "机器人验证"]:
            self.assertIn(f'"{trap_keyword}"', hard_filter_body)
        self.assertIn("钓鱼/诱捕/自动回复检测风险", hard_filter_body)

    def test_config_bool_parsing_avoids_string_false_one_shots(self):
        source = _source()
        parse_body = _method_source(source, "_parse_config_bool")
        init_body = _method_source(source, "init_plugin")

        for token in [
            'text in {"1", "true", "yes", "y", "on", "启用", "开启", "是"}',
            'text in {"0", "false", "no", "n", "off", "disabled", "disable", "关闭", "否", ""}',
        ]:
            self.assertIn(token, parse_body)
        self.assertIn('self._onlyonce = self._parse_config_bool(config.get("onlyonce", False), default=False)', init_body)
        self.assertIn('self._auto_reply_onlyonce = self._parse_config_bool(config.get("auto_reply_onlyonce", False), default=False)', init_body)

    def test_auto_reply_hard_filters_sticky_links_and_requires_real_detail_page(self):
        source = _source()
        extract_body = _method_source(source, "_extract_thread_candidates")
        detail_body = _method_source(source, "_extract_auto_reply_thread_detail")
        raw_match_body = _method_source(source, "_first_match_raw")
        hard_filter_body = _method_source(source, "_hard_filter_auto_reply_candidate")
        risky_link_body = _method_source(source, "_has_auto_reply_risky_link")
        sticky_body = _method_source(source, "_is_auto_reply_sticky_context")
        fastpost_body = _method_source(source, "_has_auto_reply_fast_reply_form")

        self.assertIn('"context": cls._strip_html(context)[:500]', extract_body)
        self.assertIn('"sticky_like": cls._is_auto_reply_sticky_context(context)', extract_body)
        self.assertIn('"risky_link": cls._has_auto_reply_risky_link(context, base_url)', extract_body)
        first_post_body = _method_source(source, "_extract_auto_reply_first_post_html")
        self.assertIn("content_html = cls._extract_auto_reply_first_post_html(html_text)", detail_body)
        self.assertIn("without stopping at nested table cells", first_post_body)
        self.assertIn("postmessage_", first_post_body)
        self.assertIn("post_rate_div_", first_post_body)
        self.assertIn("postlistreply", first_post_body)
        self.assertNotIn("[:80]", raw_match_body)
        self.assertIn('"content": content', detail_body)
        self.assertNotIn('"content": content[:3000]', detail_body)
        self.assertIn("can_reply = bool(formhash) and cls._has_auto_reply_fast_reply_form(html_text)", detail_body)
        self.assertIn('"thread_subject_found"', detail_body)
        self.assertIn('"content_found"', detail_body)
        self.assertIn('"post_authors"', detail_body)
        self.assertIn('"sticky_like"', detail_body)
        self.assertIn('"risky_link"', detail_body)
        self.assertIn('item.get("sticky_like")', hard_filter_body)
        self.assertIn('item.get("risky_link")', hard_filter_body)
        self.assertIn('require_detail and not item.get("thread_subject_found")', hard_filter_body)
        self.assertIn('require_detail and not item.get("content_found")', hard_filter_body)
        for scheme in ["magnet", "ed2k", "thunder", "ftp"]:
            self.assertIn(f'"{scheme}"', risky_link_body)
        for domain in ["t.me", "discord.gg", "mega.nz", "pan.baidu.com", "bit.ly"]:
            self.assertIn(f'"{domain}"', risky_link_body)
        for marker in ["displayorder", "stickthread", "置顶", "全局置顶", "公告", "版规"]:
            self.assertIn(f'"{marker}"', sticky_body)
        self.assertIn("fastpostform", fastpost_body)
        self.assertIn("fastpostmessage", fastpost_body)
        self.assertIn("replysubmit=yes", fastpost_body)
        self.assertIn('"url": f"{base_url.rstrip(\'/\')}/forum.php?mod=viewthread&tid={tid}"', extract_body)
        self.assertIn('"source_url": urljoin', extract_body)

    def test_auto_reply_run_path_does_not_call_signin_or_captcha_flow(self):
        source = _source()
        auto_body = "\n".join([
            _method_source(source, "_run_auto_reply_for_account"),
            _method_source(source, "_auto_reply_single"),
            _method_source(source, "_register_auto_reply_schedule"),
        ])

        for forbidden in [
            "_do_signin(",
            "_signin_single(",
            "fetch_captcha_for_account(",
            "submit_check(",
            "complete_signin(",
            "_send_captcha_notification(",
            "/sht_signin",
        ]:
            self.assertNotIn(forbidden, auto_body)

        signin_body = "\n".join([
            _method_source(source, "_do_signin"),
            _method_source(source, "_signin_single"),
            _method_source(source, "_plugin_action_handler"),
        ])
        self.assertNotIn("_auto_reply_single(", signin_body)
        self.assertNotIn("_run_auto_reply_for_account(", signin_body)
        self.assertNotIn("_register_auto_reply_schedule(", signin_body)

    def test_staged_ai_pipeline_runs_hard_filter_assess_polish_then_post(self):
        source = _source()
        auto_body = _method_source(source, "_auto_reply_single")
        submit_body = _method_source(source, "_submit_auto_reply")

        candidate_filter = auto_body.index("_hard_filter_auto_reply_candidate(candidate")
        candidate_append = auto_body.index("all_candidates.append(candidate)")
        detail_filter = auto_body.index("_hard_filter_auto_reply_candidate(\n                    detail")
        assess_call = auto_body.index("_assess_auto_reply_with_ai(detail)")
        polish_call = auto_body.index("_polish_auto_reply_with_ai(detail, assessment)")
        preflight_call = auto_body.index("_preflight_auto_reply_submit(detail, reply, forum_ids, account_id)")
        final_choice_log = auto_body.index("自动回帖最终选择")
        post_call = auto_body.index("_submit_auto_reply(")
        self.assertLess(candidate_filter, candidate_append)
        self.assertLess(detail_filter, assess_call)
        self.assertLess(assess_call, polish_call)
        self.assertLess(polish_call, preflight_call)
        self.assertLess(preflight_call, final_choice_log)
        self.assertLess(final_choice_log, post_call)
        self.assertLess(polish_call, post_call)
        self.assertIn("标题={detail.get('title')", auto_body)
        self.assertIn("回复={reply}", auto_body)
        self.assertNotIn("_auto_reply_templates", submit_body)
        self.assertNotIn("_generate_auto_reply_with_ai(", auto_body)
        self.assertEqual(source.count("_submit_auto_reply("), 2)
        self.assertEqual(source.count("_assess_auto_reply_with_ai("), 2)
        self.assertEqual(source.count("_polish_auto_reply_with_ai("), 2)
        self.assertEqual(source.count("_preflight_auto_reply_submit("), 2)

    def test_ai_rejection_continues_to_next_candidate(self):
        source = _source()
        auto_node = _method_node(source, "_auto_reply_single")

        assessment_guard = _if_guard_for_name(auto_node, "assessment")
        reply_guard = _if_guard_for_name(auto_node, "reply")

        self.assertTrue(any(isinstance(node, ast.Continue) for node in ast.walk(assessment_guard)))
        self.assertFalse(any(isinstance(node, ast.Return) for node in ast.walk(assessment_guard)))
        self.assertTrue(any(isinstance(node, ast.Continue) for node in ast.walk(reply_guard)))
        self.assertFalse(any(isinstance(node, ast.Return) for node in ast.walk(reply_guard)))

    def test_llm_call_uses_moviepilot_configured_llm_and_official_text_extractor(self):
        source = _source()
        call_body = _method_source(source, "_call_auto_reply_llm")
        extract_body = _method_source(source, "_extract_llm_response_text")

        self.assertIn("from app.agent.llm import LLMHelper", call_body)
        self.assertIn("LLMHelper.get_llm(streaming=False)", call_body)
        self.assertNotIn("provider=", call_body)
        self.assertNotIn("model=", call_body)
        self.assertNotIn("api_key=", call_body)
        self.assertNotIn("base_url=", call_body)
        self.assertIn("inspect.isawaitable(llm)", call_body)
        self.assertIn("llm.ainvoke(prompt)", call_body)
        self.assertIn("llm.invoke", call_body)
        self.assertIn("_extract_llm_response_text(response, LLMHelper)", call_body)

        self.assertIn("extract_text_content", extract_body)
        self.assertIn("content = response.content", extract_body)
        self.assertIn("fallback_to_string=True", extract_body)
        self.assertIn("except TypeError", extract_body)
        self.assertIn("_llm_response_to_text(response)", extract_body)

    def test_assessment_prompt_is_json_only_without_reply_generation(self):
        source = _source()
        assess_body = _method_source(source, "_build_auto_reply_assessment_prompt")
        validate_body = _method_source(source, "_validate_auto_reply_assessment")

        self.assertIn("字段只允许 should_reply、risk_level、risk_reasons", assess_body)
        self.assertIn('content = str(detail.get("content") or "")', assess_body)
        self.assertNotIn('[:1500]', assess_body)
        self.assertNotIn("正文摘录", assess_body)
        self.assertIn("首楼正文完整内容", assess_body)
        self.assertIn("不要输出 reply 字段", assess_body)
        self.assertIn('{\\"should_reply\\": true, \\"risk_level\\": \\"low\\", \\"risk_reasons\\": []}', assess_body)
        self.assertNotIn('reply\\": \\"', assess_body)
        self.assertIn('allowed_keys = {"should_reply", "risk_level", "risk_reasons"}', validate_body)
        self.assertIn('assessment.get("should_reply") is not True', validate_body)
        self.assertIn('risk_level != "low"', validate_body)
        self.assertNotIn('"reply"', validate_body)

        for policy in [
            "成人/敏感/资源内容本身不是风险理由",
            "不得仅因这类内容提高 risk_level 或拒绝",
            "Adult/sensitive/resource content alone is not a rejection reason",
            "普通资源分享/预览帖如未命中强制拒绝项",
            "Ordinary resource share/preview posts may be should_reply=true with risk_level=low",
            "回复可见、预览播放器、番号列表、资源说明本身不等同于诱导下载",
            "只有外链跳转、联系方式、加群、私信、站点访问方法或明确下载诱导才必须拒绝",
            "钓鱼/诱捕/反自动回复检测必须极度保守",
            "回帖后会识别、验证、筛选、登记、检测账号/真人/机器人/自动回复",
            "replying reveals risk, identifies the account, tests automation, verifies humans/bots",
        ]:
            self.assertIn(policy, assess_body)

        for category in [
            "rules", "announcements", "moderation", "site-admin", "safe-gate", "access-method",
            "latest-address", "whitelist", "publisher", "help", "tutorials", "complaints",
            "appeals", "recruitment", "cracking-tutorial", "external", "contact", "group",
            "private-message", "traffic-diversion", "link-risk", "download-inducement",
            "phishing", "scam", "ads", "soft-ad", "disputes", "inadequate-info",
        ]:
            self.assertIn(category, assess_body)

    def test_polish_prompt_and_validator_limit_reply_to_30_chars(self):
        source = _source()
        polish_body = _method_source(source, "_build_auto_reply_polish_prompt")
        parser_body = _method_source(source, "_extract_auto_reply_reply")
        validator_body = _method_source(source, "_validate_auto_reply_text")

        self.assertIn('content = str(detail.get("content") or "")', polish_body)
        self.assertNotIn('[:800]', polish_body)
        self.assertNotIn("正文摘录", polish_body)
        self.assertIn("首楼正文完整内容", polish_body)
        self.assertIn("必须只输出最终要提交的纯文本回复", polish_body)
        self.assertIn("不要输出 JSON、字段名、引号", polish_body)
        self.assertNotIn('{\\"reply\\": \\"...\\"}', polish_body)
        self.assertIn("6-18 个中文字符最佳", polish_body)
        self.assertIn("最长 30 个字符", polish_body)
        self.assertIn("禁止 emoji、Markdown、URL", polish_body)
        self.assertIn("联系方式", polish_body)
        self.assertIn("AI/机器人/模型自称", polish_body)
        self.assertIn("标点根据回复内容自然选择", polish_body)
        self.assertIn("可以不加句末标点", polish_body)
        self.assertIn("一个普通空格作停顿", polish_body)
        self.assertIn("。！？~等少量常见标点", polish_body)
        self.assertIn("不要固定套用某一种", polish_body)
        self.assertIn("短、低调、不刷屏", polish_body)
        self.assertIn("不添加奇怪符号或颜文字", polish_body)
        self.assertIn("不要重复标题", polish_body)
        self.assertIn('data.get("reply")', parser_body)
        self.assertIn("lines[0] if lines else text", parser_body)
        self.assertIn('re.sub(r"\\s+", " ", reply)', validator_body)
        self.assertIn('reply.count(" ") > 1', validator_body)
        self.assertIn("_has_auto_reply_contact_or_diversion_text(reply)", validator_body)
        for fragment in ["下载", "链接", "地址", "网盘", "磁力", "私发", "求资源"]:
            self.assertIn(f'"{fragment}"', validator_body)
        self.assertIn("len(reply) < 2 or len(reply) > 30", validator_body)
        self.assertNotIn("len(reply) > 80", validator_body)
        self.assertIn("self._parse_line_list(self._auto_reply_templates)", validator_body)
        self.assertIn("title in reply", validator_body)
        self.assertIn("https?://", validator_body)

    def test_auto_reply_locks_duplicate_runs_and_treats_any_replied_tid_as_seen(self):
        source = _source()
        class_body = _method_source(source, "_claim_auto_reply_run")
        release_body = _method_source(source, "_release_auto_reply_run")
        once_body = _method_source(source, "_run_auto_reply_once")
        run_body = _method_source(source, "_run_auto_reply_for_account")
        replied_body = _method_source(source, "_is_auto_thread_replied")
        success_body = _method_source(source, "_has_auto_reply_success_for_day")
        record_body = _method_source(source, "_record_auto_reply_result")
        mark_success_body = _method_source(source, "_mark_auto_reply_success_for_day")
        hard_filter_body = _method_source(source, "_hard_filter_auto_reply_candidate")
        aliases_body = _method_source(source, "_auto_reply_account_aliases")
        manual_reply_body = _method_source(source, "_auto_reply_current_account_has_replied_in_detail")

        self.assertIn("_auto_reply_lock = threading.Lock()", source)
        self.assertIn("_auto_reply_running_keys", source)
        self.assertIn("_auto_reply_success_key", source)
        self.assertIn("with self._auto_reply_lock", class_body)
        self.assertIn("self._auto_reply_running_keys.add(key)", class_body)
        self.assertIn("self._auto_reply_running_keys.discard(key)", release_body)
        self.assertIn("_claim_auto_reply_run", once_body)
        self.assertIn("_release_auto_reply_run", once_body)
        self.assertIn("_claim_auto_reply_run", run_body)
        self.assertIn("_release_auto_reply_run", run_body)
        self.assertIn("for owner, records in data.items()", replied_body)
        self.assertIn("self.get_data(self._auto_reply_history_key)", replied_body)
        self.assertIn("item.get(\"success\")", replied_body)
        self.assertNotIn("data.get(account_id)", replied_body)
        self.assertIn("self.get_data(self._auto_reply_success_key)", success_body)
        self.assertIn('job.get("status") == "done"', success_body)
        self.assertIn("_mark_auto_reply_success_for_day(account_id, result)", record_body)
        self.assertIn("keep_days", mark_success_body)
        self.assertIn("username", aliases_body)
        self.assertIn("_auto_reply_current_account_has_replied_in_detail(item, account_id)", hard_filter_body)
        self.assertIn("manual", manual_reply_body.lower())
        self.assertIn("account_identity", manual_reply_body)
        self.assertIn("post_author_refs", manual_reply_body)
        self.assertIn("account_uids", manual_reply_body)
        self.assertIn("account_names", manual_reply_body)
        self.assertIn('item.get("post_authors")', manual_reply_body)

    def test_post_result_parsing_prefers_failures_and_uses_strict_success_markers(self):
        source = _source()
        parse_body = _method_source(source, "_parse_auto_reply_post_result")

        self.assertIn('success_markers = ["post_reply_succeed", "succeedhandle_fastpost", "回复发布成功", "发表回复成功"]', parse_body)
        self.assertIn('failure_markers = [', parse_body)
        self.assertIn("请不要重复发帖", parse_body)
        self.assertIn("两次发表间隔", parse_body)
        self.assertIn("failure_markers", parse_body)
        self.assertLess(parse_body.index("for marker in failure_markers"), parse_body.index("for marker in success_markers"))

    def test_flaresolverr_is_required_without_misleading_toggle(self):
        source = _source()
        init_body = _method_source(source, "init_plugin")

        self.assertIn("self._use_flaresolverr = True", init_body)
        self.assertIn("FlareSolverr API 地址（必需）", source)
        self.assertIn("签到与自动回帖都通过 FlareSolverr", source)
        self.assertNotIn("'model': 'use_flaresolverr'", source)

    def test_preflight_revalidates_detail_and_reply_before_post(self):
        source = _source()
        preflight_body = _method_source(source, "_preflight_auto_reply_submit")

        self.assertIn("_hard_filter_auto_reply_candidate", preflight_body)
        self.assertIn("require_detail=True", preflight_body)
        self.assertIn("_validate_auto_reply_text(reply, detail)", preflight_body)
        self.assertIn("回复文本提交前校验失败", preflight_body)

    def test_reply_post_is_isolated_flaresolverr_request_post_without_retry(self):
        source = _source()
        fs_post_body = _method_source(source, "_fs_post")
        submit_body = _method_source(source, "_submit_auto_reply")

        self.assertIn('"cmd": "request.post"', fs_post_body)
        self.assertIn("requests.post(", fs_post_body)
        self.assertIn("replysubmit=yes", submit_body)
        self.assertIn('"formhash": formhash', submit_body)
        self.assertIn("urlencode({", submit_body)
        self.assertNotIn("while ", submit_body)
        self.assertNotIn("retry", submit_body.lower())
        self.assertNotIn("fs_get(", submit_body)

    def test_scheduler_keeps_signin_and_auto_reply_onlyonce_separate(self):
        source = _source()
        init_body = _method_source(source, "init_plugin")
        schedule_body = _method_source(source, "_register_auto_reply_schedule")
        auto_once_body = _method_source(source, "_run_auto_reply_once")

        self.assertIn("if self._auto_reply_onlyonce and not self._enabled:", init_body)
        self.assertIn("if self._onlyonce or (self._enabled and (self._auto_reply_enabled or self._auto_reply_onlyonce)):", init_body)
        self.assertIn("func=self._run_once", init_body)
        self.assertIn("signin_onlyonce", init_body)
        self.assertIn("func=self._run_auto_reply_once", init_body)
        self.assertIn("auto_reply_onlyonce", init_body)
        self.assertIn("self._auto_reply_onlyonce = False", init_body)
        self.assertIn("if self._enabled and self._auto_reply_enabled:", init_body)
        self.assertIn("保存配置后执行一次签到", source)
        self.assertIn("保存后执行一次回帖", source)
        self.assertIn("self._register_auto_reply_schedule()", init_body)
        self.assertIn("self._scheduler.start()", init_body)
        self.assertIn("auto_reply_refresh", schedule_body)
        self.assertIn("_schedule_auto_reply_jobs_for_today", schedule_body)
        self.assertIn("_auto_reply_single", auto_once_body)
        self.assertNotIn("_do_signin", auto_once_body)
        self.assertNotIn("_run_once", auto_once_body)


if __name__ == "__main__":
    unittest.main()
