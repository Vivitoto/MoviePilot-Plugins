import ast
import importlib.util
import json
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_INIT = ROOT / "plugins.v2" / "juyingsignin" / "__init__.py"
PACKAGE_JSON = ROOT / "package.v2.json"
BASE_URL = "https://juying.example"


def _plugin_source() -> str:
    return PLUGIN_INIT.read_text(encoding="utf-8")


def _load_plugin_module_with_stubs() -> types.ModuleType:
    module_name = "_juyingsignin_test_plugin"

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
            self.data_store = {}
            self.messages = []

        def get_data(self, key, *args, **kwargs):
            return self.data_store.get(key)

        def save_data(self, key, value, *args, **kwargs):
            self.data_store[key] = value

        def update_config(self, *args, **kwargs):
            pass

        def post_message(self, **kwargs):
            self.messages.append(kwargs)

    class DummyScheduler:
        def __init__(self, *args, **kwargs):
            self.running = False

        def add_job(self, *args, **kwargs):
            pass

        def get_jobs(self):
            return []

        def remove_job(self, *args, **kwargs):
            pass

        def start(self):
            self.running = True

        def shutdown(self, *args, **kwargs):
            self.running = False

    class DummyCronTrigger:
        @classmethod
        def from_crontab(cls, *args, **kwargs):
            return cls()

    class RequestException(Exception):
        pass

    class HTTPError(RequestException):
        pass

    class SSLError(RequestException):
        pass

    requests_module = types.ModuleType("requests")
    requests_module.Session = lambda: None
    requests_module.Response = type("Response", (), {})
    requests_exceptions = types.ModuleType("requests.exceptions")
    requests_exceptions.HTTPError = HTTPError
    requests_exceptions.RequestException = RequestException
    requests_exceptions.SSLError = SSLError
    requests_module.exceptions = requests_exceptions

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
        "requests.exceptions": requests_exceptions,
    }

    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_INIT)
    if not spec or not spec.loader:
        raise AssertionError("failed to build plugin import spec")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, payload=None, headers=None, cookies=None, status_code=200):
        self.payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.status_code = status_code
        self.url = ""

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}
        self.proxies = {}
        self.cookies = {}

    def _call(self, method, url, **kwargs):
        if not self.responses:
            raise AssertionError(f"unexpected {method} {url}")
        response = self.responses.pop(0)
        response.url = url
        self.cookies.update(response.cookies)
        self.calls.append({"method": method, "url": url, **kwargs})
        return response

    def get(self, url, **kwargs):
        return self._call("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._call("POST", url, **kwargs)

    def request(self, method, url, **kwargs):
        return self._call(method.upper(), url, **kwargs)


def _path(call) -> str:
    return call["url"].replace(BASE_URL, "")


def _plugin_with_session(session: FakeSession):
    module = _load_plugin_module_with_stubs()
    module.requests.Session = lambda: session
    plugin = module.JuyingSignIn()
    plugin._base_url = BASE_URL
    plugin._username = "alice"
    plugin._password = "secret"
    plugin._notify = False
    plugin._retry_count = 0
    return plugin


class JuyingSigninNonRetryableTest(unittest.TestCase):
    def test_juying_version_metadata_is_1_0_4_with_recent_history_first(self):
        init_source = _plugin_source()
        package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
        juying = package["JuyingSignIn"]

        self.assertIn('plugin_version = "1.0.4"', init_source)
        self.assertEqual(juying["version"], "1.0.4")
        self.assertEqual(
            list(juying["history"]),
            [
                "v1.0.4",
                "v1.0.3",
                "v1.0.2",
                "v1.0.1",
                "v1.0.0",
            ],
        )
        self.assertLessEqual(len(juying["history"]), 6)

    def test_signin_flow_uses_csrf_stats_profile_token_refresh_and_profile_after_do(self):
        session = FakeSession([
            FakeResponse(cookies={"csrftoken": "csrf-token"}),
            FakeResponse({"status": "success", "token": "login-token", "user": {"username": "login-user", "level_name": "L0"}}),
            FakeResponse(
                {"status": "success", "checked_today": False, "reward_points": 9, "my_total_days": 12},
                headers={"X-Refreshed-Token": "stats-token"},
            ),
            FakeResponse(
                {"status": "success", "user": {"username": "profile-user", "level_name": "L1", "points": 100, "checkin_days": 12}},
                headers={"X-Refreshed-Token": "profile-token"},
            ),
            FakeResponse(
                {"status": "success", "message": "签到成功", "points_awarded": 9, "my_total_days": 13},
                headers={"X-Refreshed-Token": "do-token"},
            ),
            FakeResponse({"status": "success", "user": {"username": "profile-user", "level_name": "L2", "points": 109, "checkin_days": 13}}),
        ])
        plugin = _plugin_with_session(session)

        result = plugin.run_once(source="manual")

        self.assertEqual(
            [(call["method"], _path(call)) for call in session.calls],
            [
                ("GET", "/login"),
                ("POST", "/api/app/login/"),
                ("GET", "/api/app/checkin/stats/"),
                ("GET", "/api/app/profile/"),
                ("POST", "/api/app/checkin/do/"),
                ("GET", "/api/app/profile/"),
            ],
        )
        login_headers = session.calls[1]["headers"]
        self.assertEqual(login_headers["Origin"], BASE_URL)
        self.assertEqual(login_headers["Referer"], f"{BASE_URL}/login")
        self.assertEqual(login_headers["X-Requested-With"], "XMLHttpRequest")
        self.assertEqual(login_headers["X-CSRFToken"], "csrf-token")

        authed_headers = [call["headers"] for call in session.calls[2:]]
        self.assertEqual(
            [headers["X-App-User-Token"] for headers in authed_headers],
            ["login-token", "stats-token", "profile-token", "do-token"],
        )
        self.assertEqual(authed_headers[2]["Origin"], BASE_URL)
        self.assertEqual(authed_headers[2]["X-CSRFToken"], "csrf-token")

        self.assertEqual(result["result_label"], "成功")
        self.assertEqual(result["signin_status"], "成功")
        self.assertTrue(result["signed_today"])
        self.assertEqual(result["points_awarded"], 9)
        self.assertEqual(result["total_days"], 13)
        self.assertEqual(result["username"], "profile-user")
        self.assertEqual(result["level_name"], "L2")
        self.assertEqual(result["points"], 109)
        self.assertEqual(result["checkin_days"], 13)
        self.assertEqual(plugin.data_store["user_info"]["points"], 109)
        self.assertEqual(plugin.data_store["last_result"]["result_label"], "成功")

    def test_checked_today_skips_do_and_uses_profile_data(self):
        session = FakeSession([
            FakeResponse(cookies={"csrftoken": "csrf-token"}),
            FakeResponse({"status": "success", "token": "login-token", "user": {"username": "login-user", "level_name": "L0"}}),
            FakeResponse(
                {"status": "success", "checked_today": True, "message": "今日已签到", "reward_points": 5, "my_total_days": 8},
                headers={"X-Refreshed-Token": "stats-token"},
            ),
            FakeResponse({"status": "success", "user": {"username": "alice", "level_name": "L1", "points": 88, "checkin_days": 8}}),
        ])
        plugin = _plugin_with_session(session)

        result = plugin.run_once(source="manual")

        self.assertEqual(
            [(call["method"], _path(call)) for call in session.calls],
            [
                ("GET", "/login"),
                ("POST", "/api/app/login/"),
                ("GET", "/api/app/checkin/stats/"),
                ("GET", "/api/app/profile/"),
            ],
        )
        self.assertNotIn("/api/app/checkin/do/", [_path(call) for call in session.calls])
        self.assertEqual(session.calls[3]["headers"]["X-App-User-Token"], "stats-token")
        self.assertEqual(result["result_label"], "已签到")
        self.assertEqual(result["signin_status"], "今日已签到")
        self.assertTrue(result["signed_today"])
        self.assertEqual(result["points_awarded"], 5)
        self.assertEqual(result["total_days"], 8)
        self.assertEqual(result["points"], 88)
        self.assertEqual(plugin.data_store["user_info"]["checkin_days"], 8)

    def test_non_retryable_site_failures_are_classified_before_retry_gate(self):
        source = _plugin_source()
        tree = ast.parse(source)
        classes = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
        request_imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "requests.exceptions"
            for alias in node.names
        }
        except_handlers = {
            handler.type.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Try)
            for handler in node.handlers
            if isinstance(handler.type, ast.Name)
        }

        self.assertIn("NonRetryableSiteError", classes)
        self.assertTrue({"HTTPError", "RequestException", "SSLError"}.issubset(request_imports))
        self.assertIn("NonRetryableSiteError", except_handlers)
        self.assertIn("result.get(\"retryable\") is False", source)
        self.assertIn("不安排失败重试", source)

    def test_cert_hostname_and_endpoint_404_are_non_retryable_without_disabling_tls(self):
        source = _plugin_source()

        for keyword in (
            "certificate verify failed",
            "hostname mismatch",
            "not valid for",
        ):
            self.assertIn(keyword, source)
        self.assertRegex(source, r"status_code\s*==\s*404")
        self.assertNotRegex(source, re.compile(r"verify\s*=\s*False", re.IGNORECASE))


if __name__ == "__main__":
    unittest.main()
