import ast
import importlib.util
import json
import sys
import threading
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_INIT = ROOT / "plugins.v2" / "sehuatangsignin" / "__init__.py"
PACKAGE_JSON = ROOT / "package.v2.json"


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


def _load_plugin_module_with_stubs() -> types.ModuleType:
    module_name = "_sehuatangsignin_test_plugin"

    class DummyLogger:
        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    class DummyEventManager:
        def register(self, *args, **kwargs):
            def decorator(func):
                return func

            return decorator

    class DummyPluginBase:
        def __init__(self):
            self.messages = []
            self.data_store = {}

        def get_data(self, key, *args, **kwargs):
            return self.data_store.get(key)

        def save_data(self, key, value, *args, **kwargs):
            self.data_store[key] = value

        def post_message(self, **kwargs):
            self.messages.append(kwargs)

        def get_data_path(self):
            return ROOT

    class DummyScheduler:
        def __init__(self, *args, **kwargs):
            pass

        def add_job(self, *args, **kwargs):
            pass

        def get_jobs(self):
            return []

        def remove_job(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def shutdown(self):
            pass

    class DummyCronTrigger:
        @classmethod
        def from_crontab(cls, *args, **kwargs):
            return cls()

    def noop(*args, **kwargs):
        return None

    app_core_config = types.ModuleType("app.core.config")
    app_core_config.settings = types.SimpleNamespace(TZ="UTC", VERSION_FLAG="v2")
    app_core_event = types.ModuleType("app.core.event")
    app_core_event.eventmanager = DummyEventManager()
    app_core_event.Event = type("Event", (), {})
    app_log = types.ModuleType("app.log")
    app_log.logger = DummyLogger()
    app_plugins = types.ModuleType("app.plugins")
    app_plugins._PluginBase = DummyPluginBase
    app_schemas = types.ModuleType("app.schemas")
    app_schemas.NotificationType = types.SimpleNamespace(Plugin="Plugin")
    app_schemas_types = types.ModuleType("app.schemas.types")
    app_schemas_types.EventType = types.SimpleNamespace(PluginAction="PluginAction")
    apscheduler_background = types.ModuleType("apscheduler.schedulers.background")
    apscheduler_background.BackgroundScheduler = DummyScheduler
    apscheduler_cron = types.ModuleType("apscheduler.triggers.cron")
    apscheduler_cron.CronTrigger = DummyCronTrigger
    requests_module = types.ModuleType("requests")
    requests_module.RequestException = Exception
    requests_module.post = noop
    captcha_server = types.ModuleType(f"{module_name}.captcha_server")
    for name in [
        "check_sign_status", "complete_signin", "destroy_session", "fetch_captcha_for_account",
        "get_answer", "get_solved_at", "init_session", "is_expired", "is_requested", "is_solved",
        "set_captcha_data", "set_base_url", "set_fs_url", "set_proxy_url", "set_session_store_path",
        "start_server", "stop_server", "submit_check",
    ]:
        setattr(captcha_server, name, noop)
    captcha_server.fs_create_session = lambda: "fs-test"
    captcha_server.fs_destroy_session = noop
    captcha_server.fs_browser_session_start = lambda *args, **kwargs: False
    captcha_server.fs_browser_session_destroy = noop
    captcha_server.fs_browser_session_get_text = lambda *args, **kwargs: ""
    captcha_server.fs_browser_session_post = lambda *args, **kwargs: {"html": ""}
    captcha_server.fs_browser_get_text = lambda *args, **kwargs: ""
    captcha_server.fs_browser_post = lambda *args, **kwargs: {"html": ""}
    captcha_server.fs_get = lambda *args, **kwargs: ""
    captcha_server.site_captcha_lock = threading.Lock()

    stubs = {
        "app": types.ModuleType("app"),
        "app.core": types.ModuleType("app.core"),
        "app.core.config": app_core_config,
        "app.core.event": app_core_event,
        "app.log": app_log,
        "app.plugins": app_plugins,
        "app.schemas": app_schemas,
        "app.schemas.types": app_schemas_types,
        "apscheduler": types.ModuleType("apscheduler"),
        "apscheduler.schedulers": types.ModuleType("apscheduler.schedulers"),
        "apscheduler.schedulers.background": apscheduler_background,
        "apscheduler.triggers": types.ModuleType("apscheduler.triggers"),
        "apscheduler.triggers.cron": apscheduler_cron,
        "requests": requests_module,
        f"{module_name}.captcha_server": captcha_server,
    }

    spec = importlib.util.spec_from_file_location(
        module_name,
        PLUGIN_INIT,
        submodule_search_locations=[str(PLUGIN_INIT.parent)],
    )
    if not spec or not spec.loader:
        raise AssertionError("failed to build plugin import spec")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return module


def _walk_schema(value):
    if isinstance(value, dict):
        yield value
        content = value.get("content")
        if content is not None:
            yield from _walk_schema(content)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_schema(item)


def _schema_text(value) -> str:
    parts = []
    for node in _walk_schema(value):
        text = node.get("text")
        if text not in (None, ""):
            parts.append(str(text))
        props = node.get("props") or {}
        prop_text = props.get("text") if isinstance(props, dict) else None
        if prop_text not in (None, ""):
            parts.append(str(prop_text))
    return "\n".join(parts)


def _nodes_by_component(value, component: str) -> list:
    return [node for node in _walk_schema(value) if node.get("component") == component]


def _top_level_card_index(page: list, marker: str) -> int:
    for idx, node in enumerate(page):
        if isinstance(node, dict) and node.get("component") == "VCard" and marker in _schema_text(node):
            return idx
    raise AssertionError(f"top-level card containing {marker!r} not found")


def _top_level_card(page: list, marker: str) -> dict:
    return page[_top_level_card_index(page, marker)]


def _plugin_with_auto_reply_ui_data():
    plugin_module = _load_plugin_module_with_stubs()
    plugin = plugin_module.SehuatangSignin()
    plugin._accounts = [{"name": "alpha", "cookie_str": "a=b"}]
    plugin._auto_reply_enabled = True
    today = plugin._auto_reply_now().strftime("%Y-%m-%d")
    plugin.data_store[plugin._auto_reply_plan_key] = {
        "date": today,
        "created_at": f"{today} 08:50:00",
        "enabled": True,
        "forum_ids": ["141", "166"],
        "window_start": "09:00",
        "window_end": "12:00",
        "jobs": [
            {
                "account": "alpha",
                "account_index": 0,
                "attempt_index": 1,
                "run_at": f"{today} 10:15:00",
                "status": "scheduled",
                "message": "",
            },
        ],
        "message": "",
    }
    plugin.data_store[plugin._auto_reply_history_key] = [
        {
            "time": f"{today} 09:30:00",
            "date": today,
            "account": "alpha",
            "success": True,
            "status": "success",
            "result": "成功",
            "fid": "141",
            "tid": "888",
            "title": "一个很长的测试主题标题用于验证表格不会因为文本过长而撑开页面布局",
            "reason": "回帖成功",
            "reply_summary": "感谢分享",
        },
        {
            "time": f"{today} 09:25:00",
            "date": today,
            "account": "beta",
            "success": True,
            "status": "success",
            "result": "成功",
            "fid": "141",
            "tid": "889",
            "title": "回帖成功 / 回复：谢谢楼主",
            "reason": "",
            "reply_summary": "",
        },
    ]
    plugin.data_store[plugin._history_key] = [
        {
            "time": f"{today} 09:10:00",
            "date": today,
            "account": "alpha",
            "success": True,
            "message": "签到成功",
        }
    ]
    return plugin


class SehuatangAutoReplyTest(unittest.TestCase):
    def test_auto_reply_plugin_version_is_current(self):
        source = _source()
        package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
        sehuatang = package["SehuatangSignin"]

        self.assertIn('plugin_version = "1.2.1"', source)
        self.assertEqual(sehuatang["version"], "1.2.1")
        self.assertEqual(list(sehuatang["history"])[:1], ["v1.2.1"])
        self.assertLessEqual(len(sehuatang["history"]), 6)

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
            '_auto_reply_skipped_threads_key = "auto_reply_skipped_threads"',
            '_auto_reply_success_key = "auto_reply_success_by_day"',
            '"auto_reply_forum_ids": "141,166"',
            '"auto_reply_onlyonce": False',
            '"auto_reply_max_attempts_per_day": 1',
            '"auto_reply_max_thread_age_days": 7',
        ]:
            self.assertIn(token, source)

    def test_detail_page_places_account_status_before_auto_reply_card(self):
        plugin = _plugin_with_auto_reply_ui_data()

        page = plugin.get_page()

        overview_index = _top_level_card_index(page, "执行总览")
        account_index = _top_level_card_index(page, "账号状态")
        auto_reply_index = _top_level_card_index(page, "自动回帖")
        self.assertEqual(account_index, overview_index + 1)
        self.assertLess(account_index, auto_reply_index)

    def test_auto_reply_card_shows_stats_and_expandable_detail(self):
        plugin = _plugin_with_auto_reply_ui_data()

        auto_reply_card = _top_level_card(plugin.get_page(), "自动回帖")
        card_text = _schema_text(auto_reply_card)

        for marker in ["今日计划数", "成功", "失败", "跳过", "待执行/计划中", "查看回帖详情"]:
            self.assertIn(marker, card_text)
        component_names = [node.get("component") for node in _walk_schema(auto_reply_card)]
        self.assertIn("VExpansionPanels", component_names)
        self.assertIn("VExpansionPanelTitle", component_names)
        self.assertIn("VExpansionPanelText", component_names)
        self.assertNotIn("mdi-chevron-down", card_text)

    def test_auto_reply_detail_uses_table_with_required_columns_and_truncation(self):
        plugin = _plugin_with_auto_reply_ui_data()

        auto_reply_card = _top_level_card(plugin.get_page(), "自动回帖")
        tables = _nodes_by_component(auto_reply_card, "VTable")
        self.assertTrue(tables)
        table = tables[0]
        table_text = _schema_text(table)

        for header in ["账号", "结果", "时间", "版块/主题", "标题", "原因/回复摘要"]:
            self.assertIn(header, table_text)
        self.assertIn("auto-reply-detail-table", str(table.get("props") or {}))
        self.assertIn("table-layout:fixed", str(table.get("props") or {}))
        td_props = [str(node.get("props") or {}) for node in _nodes_by_component(table, "td")]
        self.assertTrue(any("max-width" in props and "text-overflow:ellipsis" in props for props in td_props))

    def test_auto_reply_detail_repairs_polluted_title_for_display(self):
        plugin = _plugin_with_auto_reply_ui_data()

        auto_reply_card = _top_level_card(plugin.get_page(), "自动回帖")
        table = _nodes_by_component(auto_reply_card, "VTable")[0]
        beta_row = next(
            row for row in _nodes_by_component(table, "tr")
            if "beta" in _schema_text(row)
        )
        cells = _nodes_by_component(beta_row, "td")

        self.assertEqual(cells[4].get("text"), "-")
        self.assertEqual(cells[5].get("text"), "回帖成功 / 回复：谢谢楼主")

    def test_signin_history_is_collapsible_by_default(self):
        plugin = _plugin_with_auto_reply_ui_data()

        signin_card = _top_level_card(plugin.get_page(), "签到记录")
        card_text = _schema_text(signin_card)
        component_names = [node.get("component") for node in _walk_schema(signin_card)]

        self.assertIn("查看签到记录", card_text)
        self.assertIn("VExpansionPanels", component_names)
        self.assertIn("VExpansionPanelTitle", component_names)
        self.assertIn("VExpansionPanelText", component_names)
        self.assertIn("signin-history-table", str(signin_card))

    def test_config_page_mentions_moviepilot_llm_dependency_for_auto_reply(self):
        plugin_module = _load_plugin_module_with_stubs()
        plugin = plugin_module.SehuatangSignin()

        form, _ = plugin.get_form()
        form_text = _schema_text(form)
        llm_alerts = [
            node for node in _nodes_by_component(form, "VAlert")
            if "大语言模型" in _schema_text(node)
        ]

        self.assertTrue(llm_alerts)
        self.assertIn("自动回帖依赖大语言模型", form_text)
        self.assertIn("设定-系统-智能助手配置", form_text)

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
            "_auto_reply_block_markers",
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
            "_load_auto_reply_skipped_threads",
            "_prune_auto_reply_skipped_threads",
            "_save_auto_reply_skipped_threads",
            "_get_auto_reply_skipped_thread",
            "_is_auto_reply_skip_reason_cacheable",
            "_maybe_mark_auto_reply_skipped_thread",
            "_mark_auto_reply_skipped_thread",
            "_auto_reply_ai_skip_cache_reason",
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
            "_validate_auto_reply_text_with_reason",
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


    def test_auto_reply_does_not_depend_on_curl_cffi_fallback(self):
        source = _source()
        requirements = (ROOT / "plugins.v2" / "sehuatangsignin" / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("curl_cffi", source)
        self.assertNotIn("curl_cffi", requirements)
        self.assertNotIn("HAS_CURL_CFFI", source)
        self.assertNotIn("CURL_CFFI_IMPERSONATE", source)

    def test_auto_reply_diagnostic_helpers_are_sanitized(self):
        plugin_module = _load_plugin_module_with_stubs()
        plugin = plugin_module.SehuatangSignin()
        cookies = [
            {"name": "cPNj_2132_auth", "value": "secret-auth"},
            {"name": "_safe", "value": "secret-safe"},
        ]
        names = plugin._auto_reply_cookie_names(cookies)
        self.assertEqual(names, ["_safe", "cPNj_2132_auth"])
        self.assertNotIn("secret", ",".join(names))
        self.assertEqual(plugin._auto_reply_page_classes('<html>safeid=abc static/safe/js/web.js</html>'), ["safe_gate"])
        self.assertIn("forum", plugin._auto_reply_page_classes('<a href="forum.php?mod=viewthread&tid=1">x</a><div id="threadlist"></div>'))
        self.assertIn("thread", plugin._auto_reply_page_classes('<div id="thread_subject">t</div><textarea id="fastpostmessage"></textarea>'))
        normal_with_cloudflare_asset = '<html><footer>Cloudflare</footer><a href="forum.php?mod=viewthread&tid=1">x</a><div id="threadlist"></div><textarea id="fastpostmessage"></textarea></html>'
        normal_classes = plugin._auto_reply_page_classes(normal_with_cloudflare_asset)
        normal_diag = plugin._auto_reply_page_diag(normal_with_cloudflare_asset)
        self.assertIn("forum", normal_classes)
        self.assertIn("thread", normal_classes)
        self.assertNotIn("cloudflare", normal_classes)
        self.assertFalse(plugin._is_auto_reply_blocked_page(normal_with_cloudflare_asset))
        self.assertEqual(plugin._auto_reply_block_markers(normal_with_cloudflare_asset), [])
        self.assertNotIn("blocked=", normal_diag)
        weak_challenge_platform_html = '<html><script src="/cdn-cgi/challenge-platform/foo.js"></script><a href="forum.php?mod=viewthread&tid=1">x</a><div id="threadlist"></div><textarea id="fastpostmessage"></textarea></html>'
        weak_classes = plugin._auto_reply_page_classes(weak_challenge_platform_html)
        self.assertIn("forum", weak_classes)
        self.assertIn("thread", weak_classes)
        self.assertNotIn("cloudflare", weak_classes)
        self.assertFalse(plugin._is_auto_reply_blocked_page(weak_challenge_platform_html))
        self.assertEqual(plugin._auto_reply_block_markers(weak_challenge_platform_html), [])
        self.assertNotIn("blocked=", plugin._auto_reply_page_diag(weak_challenge_platform_html))
        challenge_html = '<html>_cf_chl_opt cf-challenge Just a moment</html>'
        challenge_markers = plugin._auto_reply_block_markers(challenge_html)
        challenge_diag = plugin._auto_reply_page_diag(challenge_html)
        self.assertTrue(plugin._is_auto_reply_blocked_page(challenge_html))
        self.assertIn("cloudflare_cf_challenge", challenge_markers)
        self.assertIn("cloudflare_cf_chl_opt", challenge_markers)
        self.assertIn("cloudflare_just_a_moment", challenge_markers)
        self.assertIn("blocked=cloudflare_cf_challenge,cloudflare_cf_chl_opt,cloudflare_just_a_moment", challenge_diag)
        diag = plugin._auto_reply_page_diag('<div id="thread_subject">t</div>')
        self.assertIn("len=", diag)
        self.assertIn("thread", diag)
        self.assertNotIn("secret", diag)



    def test_forum_and_detail_pages_use_browser_primary_after_fs_warmup_on_success(self):
        plugin_module = _load_plugin_module_with_stubs()
        plugin = plugin_module.SehuatangSignin()
        plugin._auto_reply_max_thread_age_days = 0
        forum_html = '<a href="forum.php?mod=viewthread&tid=888">普通分享帖</a>'
        detail_html = "<html>detail</html>"
        browser_calls = []
        fs_get_calls = []
        submit_saw_browser_cookie = []

        def fake_browser_get(fs_sid, url, cookies):
            browser_calls.append(url)
            if "forumdisplay" in url:
                cookies.append({"name": "gate_passed", "value": "1", "domain": ".sehuatang.net", "path": "/"})
                return forum_html
            return detail_html

        def fake_fs_get(fs_sid, url, cookies, *args, **kwargs):
            fs_get_calls.append(url)
            return ""

        def fake_detail(html_text, candidate, allowed_forum_ids):
            self.assertEqual(html_text, detail_html)
            detail = dict(candidate)
            detail.update({
                "content": "普通内容",
                "author": "user",
                "thread_subject_found": True,
                "content_found": True,
                "formhash": "abcdef12",
                "can_reply": True,
                "blocked_page": False,
                "post_authors": [],
                "post_author_refs": [],
                "account_identity": {},
            })
            return detail

        def fake_submit(fs_sid, cookies, detail, reply, **kwargs):
            submit_saw_browser_cookie.append(any(c.get("name") == "gate_passed" for c in cookies))
            return plugin._auto_reply_result("success", "回帖成功")

        with patch.object(plugin_module, "fs_create_session", return_value="fs-test"), \
                patch.object(plugin_module, "fs_destroy_session"), \
                patch.object(plugin_module, "fs_browser_get_text", side_effect=fake_browser_get), \
                patch.object(plugin_module, "fs_get", side_effect=fake_fs_get), \
                patch.object(plugin_module.random, "uniform", return_value=8), \
                patch.object(plugin_module.time, "sleep"), \
                patch.object(plugin, "_extract_auto_reply_thread_detail", side_effect=fake_detail), \
                patch.object(plugin, "_assess_auto_reply_with_ai", return_value={"risk_reasons": []}), \
                patch.object(plugin, "_polish_auto_reply_with_ai", return_value="感谢分享"), \
                patch.object(plugin, "_preflight_auto_reply_submit", return_value=(True, "")), \
                patch.object(plugin, "_submit_auto_reply", side_effect=fake_submit):
            result = plugin._auto_reply_single({"cookie_str": "a=b"}, "account-a", ["141"])

        self.assertEqual(result["status"], "success")
        self.assertEqual(
            browser_calls,
            [
                f"{plugin._base_url}/forum.php?mod=forumdisplay&fid=141",
                f"{plugin._base_url}/forum.php?mod=viewthread&tid=888",
            ],
        )
        self.assertEqual(fs_get_calls, [f"{plugin._base_url}/plugin.php?id=dd_sign"])
        self.assertEqual(submit_saw_browser_cookie, [True])

    def test_blocked_forum_pages_fail_after_browser_primary_when_no_usable_candidates_remain(self):
        plugin_module = _load_plugin_module_with_stubs()
        plugin = plugin_module.SehuatangSignin()
        browser_calls = []
        fs_get_calls = []

        def fake_browser_get(fs_sid, url, cookies):
            browser_calls.append(url)
            return "<html>请完成安全验证 safeid=still-blocked</html>"

        def fake_fs_get(fs_sid, url, cookies, *args, **kwargs):
            fs_get_calls.append(url)
            return ""

        with patch.object(plugin_module, "fs_create_session", return_value="fs-test"), \
                patch.object(plugin_module, "fs_destroy_session"), \
                patch.object(plugin_module, "fs_browser_get_text", side_effect=fake_browser_get), \
                patch.object(plugin_module, "fs_get", side_effect=fake_fs_get), \
                patch.object(plugin_module.random, "uniform", return_value=8), \
                patch.object(plugin_module.time, "sleep"):
            result = plugin._auto_reply_single({"cookie_str": "a=b"}, "account-a", ["141", "166"])

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["failed"])
        self.assertFalse(result["skipped"])
        self.assertIn("安全页/权限页", result["reason"])
        self.assertIn("141,166", result["reason"])
        self.assertEqual(
            browser_calls,
            [
                f"{plugin._base_url}/forum.php?mod=forumdisplay&fid=141",
                f"{plugin._base_url}/forum.php?mod=forumdisplay&fid=166",
            ],
        )
        self.assertEqual(
            fs_get_calls,
            [
                f"{plugin._base_url}/plugin.php?id=dd_sign",
                f"{plugin._base_url}/forum.php?mod=forumdisplay&fid=141",
                f"{plugin._base_url}/forum.php?mod=forumdisplay&fid=166",
            ],
        )
        self.assertEqual(plugin._auto_reply_status_label(result), "失败")
        plugin._notify_auto_reply_result("account-a", result)
        self.assertEqual(plugin.messages[-1]["title"], "98自动回帖失败")

    def test_empty_browser_forum_page_uses_fs_get_backup_and_preserves_blocked_fids_when_backup_blocked(self):
        plugin_module = _load_plugin_module_with_stubs()
        plugin = plugin_module.SehuatangSignin()
        fetch_events = []

        def fake_browser_get(fs_sid, url, cookies):
            fetch_events.append(("browser", url))
            return ""

        def fake_fs_get(fs_sid, url, cookies, *args, **kwargs):
            fetch_events.append(("fs_get", url))
            return "<html>请完成安全验证 safeid=backup-blocked</html>"

        with patch.object(plugin_module, "fs_create_session", return_value="fs-test"), \
                patch.object(plugin_module, "fs_destroy_session"), \
                patch.object(plugin_module, "fs_browser_get_text", side_effect=fake_browser_get), \
                patch.object(plugin_module, "fs_get", side_effect=fake_fs_get), \
                patch.object(plugin_module.random, "uniform", return_value=8) as uniform_mock, \
                patch.object(plugin_module.time, "sleep") as sleep_mock:
            result = plugin._auto_reply_single({"cookie_str": "a=b"}, "account-a", ["141"])

        forum_url = f"{plugin._base_url}/forum.php?mod=forumdisplay&fid=141"
        self.assertEqual(result["status"], "failed")
        self.assertIn("blocked_fids=141", result["reason"])
        self.assertEqual(fetch_events, [
            ("fs_get", f"{plugin._base_url}/plugin.php?id=dd_sign"),
            ("browser", forum_url),
            ("fs_get", forum_url),
        ])
        uniform_mock.assert_not_called()
        sleep_mock.assert_not_called()

    def test_blocked_forum_pages_do_not_fail_when_other_forums_have_candidates(self):
        plugin_module = _load_plugin_module_with_stubs()
        plugin = plugin_module.SehuatangSignin()
        plugin._auto_reply_max_thread_age_days = 0
        responses = {
            "fid=141": "<html>请完成安全验证 safeid=abc</html>",
            "fid=166": '<a href="forum.php?mod=viewthread&tid=888">普通分享帖</a>',
        }
        fs_get_calls = []

        def fake_browser_get(fs_sid, url, cookies):
            return next((value for key, value in responses.items() if key in url), "<html>detail</html>")

        def fake_fs_get(fs_sid, url, cookies, *args, **kwargs):
            fs_get_calls.append(url)
            return ""

        def fake_detail(html_text, candidate, allowed_forum_ids):
            detail = dict(candidate)
            detail.update({
                "content": "普通内容",
                "author": "user",
                "thread_subject_found": True,
                "content_found": True,
                "formhash": "abcdef12",
                "can_reply": True,
                "blocked_page": False,
                "post_authors": [],
                "post_author_refs": [],
                "account_identity": {},
            })
            return detail

        with patch.object(plugin_module, "fs_create_session", return_value="fs-test"), \
                patch.object(plugin_module, "fs_destroy_session"), \
                patch.object(plugin_module, "fs_browser_get_text", side_effect=fake_browser_get), \
                patch.object(plugin_module, "fs_get", side_effect=fake_fs_get), \
                patch.object(plugin_module.random, "uniform", return_value=8), \
                patch.object(plugin_module.time, "sleep"), \
                patch.object(plugin, "_extract_auto_reply_thread_detail", side_effect=fake_detail), \
                patch.object(plugin, "_assess_auto_reply_with_ai", return_value=None):
            result = plugin._auto_reply_single({"cookie_str": "a=b"}, "account-a", ["141", "166"])

        self.assertEqual(result["status"], "skipped")
        self.assertIn("AI 评估未通过", result["reason"])
        self.assertNotIn("安全页/权限页", result["reason"])
        self.assertEqual(fs_get_calls, [
            f"{plugin._base_url}/plugin.php?id=dd_sign",
            f"{plugin._base_url}/forum.php?mod=forumdisplay&fid=141",
        ])

    def test_detail_page_uses_browser_primary_before_hard_filtering(self):
        plugin_module = _load_plugin_module_with_stubs()
        plugin = plugin_module.SehuatangSignin()
        plugin._auto_reply_max_thread_age_days = 0
        forum_html = '<a href="forum.php?mod=viewthread&tid=321">普通分享帖</a>'
        normal_detail_html = "<html><div id='thread_subject'>普通分享帖</div><td id='postmessage_1'>普通内容</td></html>"
        events = []

        def fake_browser_get(fs_sid, url, cookies):
            events.append(("browser", url))
            if "forumdisplay" in url:
                return forum_html
            return normal_detail_html

        def fake_fs_get(fs_sid, url, cookies, *args, **kwargs):
            events.append(("fs_get", url))
            return ""

        def fake_detail(html_text, candidate, allowed_forum_ids):
            events.append(("extract", html_text))
            detail = dict(candidate)
            detail.update({
                "content": "普通内容",
                "author": "user",
                "thread_subject_found": True,
                "content_found": True,
                "formhash": "abcdef12",
                "can_reply": True,
                "blocked_page": False,
                "post_authors": [],
                "post_author_refs": [],
                "account_identity": {},
            })
            return detail

        def fake_hard_filter(item, allowed_forum_ids, account_id="", require_detail=False):
            events.append(("hard_detail" if require_detail else "hard_candidate", str(item.get("tid") or "")))
            return True, ""

        with patch.object(plugin_module, "fs_create_session", return_value="fs-test"), \
                patch.object(plugin_module, "fs_destroy_session"), \
                patch.object(plugin_module, "fs_browser_get_text", side_effect=fake_browser_get), \
                patch.object(plugin_module, "fs_get", side_effect=fake_fs_get), \
                patch.object(plugin_module.random, "uniform", return_value=8), \
                patch.object(plugin_module.time, "sleep"), \
                patch.object(plugin, "_extract_auto_reply_thread_detail", side_effect=fake_detail), \
                patch.object(plugin, "_hard_filter_auto_reply_candidate", side_effect=fake_hard_filter), \
                patch.object(plugin, "_assess_auto_reply_with_ai", return_value=None):
            result = plugin._auto_reply_single({"cookie_str": "a=b"}, "account-a", ["141"])

        self.assertEqual(result["status"], "skipped")
        self.assertIn(("browser", f"{plugin._base_url}/forum.php?mod=viewthread&tid=321"), events)
        self.assertNotIn(("fs_get", f"{plugin._base_url}/forum.php?mod=viewthread&tid=321"), events)
        self.assertIn(("extract", normal_detail_html), events)
        self.assertLess(events.index(("hard_candidate", "321")), events.index(("browser", f"{plugin._base_url}/forum.php?mod=viewthread&tid=321")))
        self.assertLess(events.index(("browser", f"{plugin._base_url}/forum.php?mod=viewthread&tid=321")), events.index(("extract", normal_detail_html)))
        self.assertLess(events.index(("extract", normal_detail_html)), events.index(("hard_detail", "321")))

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
        blocked_markers_body = _method_source(source, "_auto_reply_block_markers")
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

        for marker in ["cf-challenge", "cf-turnstile", "just a moment", "请完成安全验证", "访问过于频繁"]:
            self.assertIn(f'"{marker}"', blocked_markers_body)
        self.assertNotIn('"challenge-platform"', blocked_markers_body)
        self.assertIn("_auto_reply_block_markers(html_text)", _method_source(source, "_is_auto_reply_blocked_page"))
        self.assertNotIn('"cloudflare"', blocked_markers_body)
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
            self.assertNotIn(f'"{scheme}"', risky_link_body)
        for domain in ["t.me", "discord.gg", "bit.ly", "tinyurl.com", "t.co"]:
            self.assertIn(f'"{domain}"', risky_link_body)
        for domain in ["mega.nz", "pan.baidu.com", "aliyundrive.com", "pan.quark.cn", "115.com", "pikpak.com"]:
            self.assertNotIn(f'"{domain}"', risky_link_body)
        for marker in ["短链接", "跳转链接"]:
            self.assertIn(f'"{marker}"', risky_link_body)
        for marker in ["百度网盘", "夸克网盘", "网盘链接", "网盘地址"]:
            self.assertNotIn(f'"{marker}"', risky_link_body)
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
            "回复可见、预览播放器、番号列表、资源说明、普通外链和 magnet/ed2k/thunder/ftp/http 下载协议本身不等同于风险或诱导下载",
            "只有联系方式、加群、私信、站点访问方法、短链/跳转引流或明显诈骗式下载诱导才必须拒绝",
            "钓鱼/诱捕/反自动回复检测必须极度保守",
            "回帖后会识别、验证、筛选、登记、检测账号/真人/机器人/自动回复",
            "replying reveals risk, identifies the account, tests automation, verifies humans/bots",
        ]:
            self.assertIn(policy, assess_body)

        for category in [
            "rules", "announcements", "moderation", "site-admin", "safe-gate", "access-method",
            "latest-address", "whitelist", "publisher", "help", "tutorials", "complaints",
            "appeals", "recruitment", "cracking-tutorial", "contact", "group",
            "private-message", "traffic-diversion", "shortener",
            "phishing", "scam", "ads", "soft-ad", "disputes", "inadequate-info",
        ]:
            self.assertIn(category, assess_body)

    def test_polish_prompt_and_validator_limit_reply_to_30_chars(self):
        source = _source()
        polish_body = _method_source(source, "_build_auto_reply_polish_prompt")
        parser_body = _method_source(source, "_extract_auto_reply_reply")
        validator_body = "\n".join([
            _method_source(source, "_validate_auto_reply_text"),
            _method_source(source, "_validate_auto_reply_text_with_reason"),
        ])

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
        for policy in [
            "电影/视频/影视/资源分享类帖子",
            "轻口语短评",
            "语气松弛",
            "不要像客服或模板",
            "简介是否清楚、题材/主题、画质/版本、演员/人物/主体、画风/风格",
            "只根据已出现的信息",
            "不编造剧情、演员、清晰度、评价",
            "所有回复必须只使用标题和首楼正文中的可见线索",
            "不要引入帖子里没出现的信息",
            "不得编造剧情、演员/人物、导演、字幕、画质、版本、时长、评分、资源质量或观看体验",
            "必须从标题/正文中挑一个可见线索再泛化成短评",
            "没有对应线索就不要写该方向",
            "如果标题和正文内容太薄",
            "选择中性、内容有依据的短句",
            "不要假装看过细节",
            "避免空洞泛泛的库存短语",
            "即使未命中禁用词",
            "不错不错、看起来不错、可以可以、很棒、收藏了",
            "明确不要写论坛套话",
            "感谢分享、支持一下、路过看看、顶一下、楼主辛苦、辛苦了、前排支持",
            "不要在回复里出现 下载、链接、地址、资源",
            "不要复述片名、标题、番号或专名",
            "用泛化说法，不照搬原文名词",
            "合格方向示例",
            "禁止逐字套用",
            "简介看着挺清楚",
            "这个题材挺有意思",
            "预览感觉还可以",
            "这些示例不是模板",
            "无效示例",
            "求个资源",
            "有下载吗",
            "看看链接",
            "磁力有吗",
            "禁止出现的套话和资源词",
            "上一轮被本地校验拒绝",
            "被拒回复",
            "拒绝原因",
        ]:
            self.assertIn(policy, polish_body)
        self.assertIn('data.get("reply")', parser_body)
        self.assertIn("lines[0] if lines else text", parser_body)
        self.assertIn('re.sub(r"\\s+", " ", reply)', validator_body)
        self.assertIn('reply.count(" ") > 1', validator_body)
        self.assertIn("_has_auto_reply_contact_or_diversion_text(reply)", validator_body)
        for fragment in ["下载", "链接", "地址", "资源", "网盘", "磁力", "私发", "求资源"]:
            self.assertIn(f'"{fragment}"', validator_body)
        for fragment in ["感谢分享", "支持一下", "路过看看", "顶一下", "楼主辛苦", "辛苦了"]:
            self.assertIn(f'"{fragment}"', validator_body)
        for fragment in ["内容不错", "不错不错", "看起来不错", "可以可以", "很棒", "收藏了"]:
            self.assertIn(f'"{fragment}"', validator_body)
        self.assertIn("compact_reply", validator_body)
        self.assertIn("title_for_repeat_check", validator_body)
        self.assertIn("影视资源分享", validator_body)
        self.assertIn("len(reply) < 2 or len(reply) > 30", validator_body)
        self.assertNotIn("len(reply) > 80", validator_body)
        self.assertIn("self._parse_line_list(self._auto_reply_templates)", validator_body)
        self.assertIn("title in reply", validator_body)
        self.assertIn("https?://", validator_body)

    def test_auto_reply_polish_retries_after_rejected_first_reply_and_returns_valid_second_reply(self):
        plugin_module = _load_plugin_module_with_stubs()
        plugin = plugin_module.SehuatangSignin()
        detail = {
            "fid": "141",
            "title": "某某影视资源分享",
            "author": "alice",
            "content": "简介写得比较清楚，附带预览图。",
        }
        prompts = []

        def fake_llm(prompt, stage_name):
            prompts.append(prompt)
            return ["感谢分享", "简介看着挺清楚"][len(prompts) - 1]

        with patch.object(plugin, "_call_auto_reply_llm_with_timeout", side_effect=fake_llm):
            reply = plugin._polish_auto_reply_with_ai(detail, {"risk_reasons": []})

        self.assertEqual(reply, "简介看着挺清楚")
        self.assertEqual(len(prompts), 2)
        self.assertNotIn("上一轮被本地校验拒绝", prompts[0])
        self.assertIn("上一轮被本地校验拒绝", prompts[1])
        self.assertIn("被拒回复：\"感谢分享\"", prompts[1])
        self.assertIn("拒绝原因：命中论坛套话：感谢分享", prompts[1])

    def test_auto_reply_polish_keeps_retrying_same_candidate_until_valid_reply(self):
        plugin_module = _load_plugin_module_with_stubs()
        plugin = plugin_module.SehuatangSignin()
        detail = {
            "fid": "141",
            "title": "某某影视资源分享",
            "content": "简介写得比较清楚，附带预览图。",
        }
        prompts = []
        replies = ["感谢分享", "求个资源", "支持一下", "内容不错", "看起来不错", "预览感觉还可以"]

        def fake_llm(prompt, stage_name):
            prompts.append(prompt)
            return replies[len(prompts) - 1]

        with patch.object(plugin, "_call_auto_reply_llm_with_timeout", side_effect=fake_llm):
            reply = plugin._polish_auto_reply_with_ai(detail, {"risk_reasons": []})

        self.assertEqual(reply, "预览感觉还可以")
        self.assertEqual(len(prompts), 6)
        self.assertIn("拒绝原因：命中论坛套话：感谢分享", prompts[1])
        self.assertIn("被拒回复：\"求个资源\"", prompts[2])
        self.assertIn("拒绝原因：包含禁用词：资源", prompts[2])
        self.assertIn("被拒回复：\"看起来不错\"", prompts[5])
        self.assertIn("继续修正当前候选", _method_source(_source(), "_polish_auto_reply_with_ai"))
        self.assertNotIn("max_attempts = 3", _method_source(_source(), "_polish_auto_reply_with_ai"))

    def test_auto_reply_polish_propagates_ai_errors_instead_of_looping_forever(self):
        plugin_module = _load_plugin_module_with_stubs()
        plugin = plugin_module.SehuatangSignin()
        detail = {"title": "某某影视资源分享", "content": "简介写得比较清楚。"}

        with patch.object(plugin, "_call_auto_reply_llm_with_timeout", side_effect=RuntimeError("AI调用失败：润色调用超时")):
            with self.assertRaisesRegex(RuntimeError, "AI调用失败"):
                plugin._polish_auto_reply_with_ai(detail, {"risk_reasons": []})

    def test_auto_reply_polish_deadline_stops_invalid_reply_repair_loop(self):
        plugin_module = _load_plugin_module_with_stubs()
        plugin = plugin_module.SehuatangSignin()
        plugin._auto_reply_polish_deadline_seconds = 180
        detail = {"title": "某某影视资源分享", "content": "简介写得比较清楚。"}
        calls = []

        def fake_llm(prompt, stage_name):
            calls.append((prompt, stage_name))
            return "感谢分享"

        with patch.object(plugin_module.time, "monotonic", side_effect=[0.0, 0.0, 181.0]), \
                patch.object(plugin, "_call_auto_reply_llm_with_timeout", side_effect=fake_llm):
            reply = plugin._polish_auto_reply_with_ai(detail, {"risk_reasons": []})

        self.assertIsNone(reply)
        self.assertEqual(len(calls), 1)

    def test_validator_rejects_stock_resource_terms_and_share_title_repetition(self):
        plugin_module = _load_plugin_module_with_stubs()
        plugin = plugin_module.SehuatangSignin()
        detail = {
            "title": "某某影视资源分享",
            "content": "简介写得比较清楚，附带预览图。",
        }

        for reply in [
            "感谢分享",
            "感谢 分享。",
            "支持一下",
            "路过看看",
            "顶一下",
            "楼主辛苦了",
            "求个资源",
            "有下载吗",
            "看看链接",
            "这个地址稳吗",
            "某某看起来不错",
            "内容不错",
            "不错不错",
            "可以可以",
            "很棒",
            "收藏了",
        ]:
            self.assertIsNone(plugin._validate_auto_reply_text(reply, detail), reply)

        for reply in ["简介看着挺清楚", "这个题材挺有意思", "预览感觉还可以"]:
            self.assertEqual(plugin._validate_auto_reply_text(reply, detail), reply)

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

    def test_auto_reply_result_notifications_distinguish_success_failure_and_skip(self):
        source = _source()
        notify_body = _method_source(source, "_notify_auto_reply_result")
        render_body = _method_source(source, "_render_auto_reply_notify")
        record_body = _method_source(source, "_record_auto_reply_result")
        run_body = _method_source(source, "_run_auto_reply_for_account")
        auto_body = _method_source(source, "_auto_reply_single")
        submit_body = _method_source(source, "_submit_auto_reply")
        parse_body = _method_source(source, "_parse_auto_reply_post_result")
        llm_timeout_body = _method_source(source, "_call_auto_reply_llm_with_timeout")
        llm_body = _method_source(source, "_call_auto_reply_llm")

        for method in [
            "_normalize_auto_reply_status_value",
            "_auto_reply_result_status",
            "_auto_reply_status_label",
            "_normalize_auto_reply_result",
            "_auto_reply_result",
        ]:
            self.assertIn(f"def {method}", source)
        self.assertIn('_auto_reply_status_labels = {"success": "成功", "failed": "失败", "skipped": "跳过"}', source)

        self.assertIn('title=f"98自动回帖{label}"', notify_body)
        self.assertIn("self._render_auto_reply_notify(account_id, result, label)", notify_body)
        for token in [
            '"account": str(account_id or "")',
            '"result": str(label or "")',
            '"reason": str(result.get("reason") or result.get("message") or "-")',
            '"fid": str(result.get("fid") or "")',
            '"tid": str(result.get("tid") or "")',
            '"title": str(result.get("title") or "")',
            '"reply": str(result.get("reply_summary") or result.get("reply") or "")',
            '"time": str(result.get("time") or self._auto_reply_now().strftime("%Y-%m-%d %H:%M:%S"))',
        ]:
            self.assertIn(token, render_body)
        self.assertIn("template.format(**variables)", render_body)
        self.assertIn("回帖通知模板无效，回退默认文本", render_body)
        self.assertIn('result.get("reason") or result.get("message")', render_body)

        for token in [
            '"failed": status == "failed"',
            '"status": status',
            '"result_status": status',
            '"result_category": status',
            '"result": self._auto_reply_status_label(status)',
            '"reason": reason',
        ]:
            self.assertIn(token, record_body)
        self.assertIn('"done" if result_status == "success" else result_status', run_body)

        self.assertIn('self._auto_reply_result("failed", "无法创建 FlareSolverr 会话")', auto_body)
        self.assertIn("blocked_forum_fids = []", auto_body)
        self.assertIn("blocked_forum_fids.append(str(fid))", auto_body)
        self.assertIn('self._auto_reply_result("failed", message)', auto_body)
        self.assertIn("安全页/权限页", auto_body)
        self.assertIn("blocked_fids", auto_body)
        self.assertIn('self._auto_reply_result("skipped", message)', auto_body)
        self.assertIn('self._auto_reply_result("skipped", last_skip_message)', auto_body)
        self.assertIn('self._auto_reply_result("failed", f"异常：{str(e)}")', auto_body)
        self.assertIn('str(e).startswith("AI调用失败")', auto_body)
        self.assertIn('self._auto_reply_result(\n                            "failed",\n                            str(e),', auto_body)

        self.assertIn('self._auto_reply_result("failed", "缺少回帖参数")', submit_body)
        self.assertIn('self._auto_reply_result("failed", f"回帖 POST 异常：{e}")', submit_body)
        self.assertIn('cls._auto_reply_result("failed", compact[:120] or "回帖失败")', parse_body)
        self.assertIn('cls._auto_reply_result("success", "回帖成功")', parse_body)
        self.assertIn('cls._auto_reply_result("failed", unknown_message or "回帖结果未知")', parse_body)

        self.assertIn('raise RuntimeError(f"AI调用失败：{stage_name}调用超时")', llm_timeout_body)
        self.assertIn('raise RuntimeError(f"AI调用失败：{stage_name}不可用或调用失败：{message}")', llm_timeout_body)
        self.assertIn('raise RuntimeError(f"AI调用失败：系统 LLM 不可用：{e}")', llm_body)
        self.assertIn('raise RuntimeError("AI调用失败：系统 LLM 不可用")', llm_body)
        self.assertIn('raise RuntimeError("AI调用失败：系统 LLM 不支持调用")', llm_body)

    def test_post_result_parsing_prefers_failures_and_uses_strict_success_markers(self):
        source = _source()
        parse_body = _method_source(source, "_parse_auto_reply_post_result")

        self.assertIn('success_markers = ["post_reply_succeed", "succeedhandle_fastpost", "回复发布成功", "发表回复成功"]', parse_body)
        self.assertIn('failure_markers = [', parse_body)
        self.assertIn("请不要重复发帖", parse_body)
        self.assertIn("两次发表间隔", parse_body)
        self.assertIn("failure_markers", parse_body)
        self.assertLess(parse_body.index("for marker in failure_markers"), parse_body.index("for marker in success_markers"))

    def test_auto_reply_submit_uses_browser_post_primary_without_fs_post_on_success(self):
        plugin_module = _load_plugin_module_with_stubs()
        plugin = plugin_module.SehuatangSignin()
        detail_url = f"{plugin._base_url}/forum.php?mod=viewthread&tid=321"
        detail = {"fid": "141", "tid": "321", "formhash": "abcdef12", "url": detail_url}
        browser_calls = []

        def fake_browser_post(fs_sid, url, body, cookies, headers=None, referer_url=None, **kwargs):
            browser_calls.append((fs_sid, url, body, cookies, headers, referer_url))
            return {"html": "<script>post_reply_succeed</script>", "status": 200, "via": "playwright"}

        with patch.object(plugin_module, "fs_browser_post", side_effect=fake_browser_post), \
                patch.object(plugin, "_fs_post") as fs_post_mock:
            result = plugin._submit_auto_reply("fs-test", [{"name": "a", "value": "b"}], detail, "感谢分享")

        self.assertEqual(result["status"], "success")
        fs_post_mock.assert_not_called()
        self.assertEqual(len(browser_calls), 1)
        _, post_url, post_body, _, headers, referer_url = browser_calls[0]
        self.assertIn("replysubmit=yes", post_url)
        self.assertEqual(referer_url, detail_url)
        self.assertEqual(headers["Referer"], detail_url)
        parsed_body = parse_qs(post_body)
        self.assertEqual(parsed_body["formhash"], ["abcdef12"])
        self.assertEqual(parsed_body["message"], ["感谢分享"])

    def test_empty_browser_submit_falls_back_to_fs_post(self):
        plugin_module = _load_plugin_module_with_stubs()
        plugin = plugin_module.SehuatangSignin()
        detail = {
            "fid": "141",
            "tid": "321",
            "formhash": "abcdef12",
            "url": f"{plugin._base_url}/forum.php?mod=viewthread&tid=321",
        }
        browser_calls = []
        fs_calls = []

        def fake_browser_post(fs_sid, url, body, cookies, headers=None, referer_url=None, **kwargs):
            browser_calls.append(url)
            return {"html": "", "status": 599, "via": "browser"}

        def fake_fs_post(fs_sid, url, body, cookies, headers=None):
            fs_calls.append(url)
            return {"html": "<script>succeedhandle_fastpost()</script>", "status": 200}

        with patch.object(plugin_module, "fs_browser_post", side_effect=fake_browser_post), \
                patch.object(plugin, "_fs_post", side_effect=fake_fs_post):
            result = plugin._submit_auto_reply("fs-test", [{"name": "a", "value": "b"}], detail, "感谢分享")

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(browser_calls), 1)
        self.assertEqual(fs_calls, browser_calls)

    def test_flaresolverr_is_required_without_misleading_toggle(self):
        source = _source()
        init_body = _method_source(source, "init_plugin")

        self.assertIn("self._use_flaresolverr = True", init_body)
        self.assertIn("FlareSolverr API 地址（必需）", source)
        self.assertNotIn("'model': 'use_flaresolverr'", source)

    def test_preflight_revalidates_detail_and_reply_before_post(self):
        source = _source()
        preflight_body = _method_source(source, "_preflight_auto_reply_submit")

        self.assertIn("_hard_filter_auto_reply_candidate", preflight_body)
        self.assertIn("require_detail=True", preflight_body)
        self.assertIn("_validate_auto_reply_text(reply, detail)", preflight_body)
        self.assertIn("回复文本提交前校验失败", preflight_body)

    def test_reply_post_is_browser_primary_with_flaresolverr_request_post_backup_without_retry(self):
        source = _source()
        fs_post_body = _method_source(source, "_fs_post")
        submit_body = _method_source(source, "_submit_auto_reply")

        self.assertIn("fs_browser_post(", submit_body)
        self.assertIn('"cmd": "request.post"', fs_post_body)
        self.assertIn("requests.post(", fs_post_body)
        self.assertIn("replysubmit=yes", submit_body)
        self.assertIn('"formhash": formhash', submit_body)
        self.assertIn("urlencode({", submit_body)
        self.assertNotIn("while ", submit_body)
        self.assertNotIn("retry", submit_body.lower())
        self.assertNotIn("fs_get(", submit_body)
        self.assertLess(submit_body.index("fs_browser_post("), submit_body.index("self._fs_post("))

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
