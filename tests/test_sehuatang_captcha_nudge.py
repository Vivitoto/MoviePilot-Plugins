import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CAPTCHA_SERVER = ROOT / "plugins.v2" / "sehuatangsignin" / "captcha_server.py"
PLUGIN_INIT = ROOT / "plugins.v2" / "sehuatangsignin" / "__init__.py"
PACKAGE_JSON = ROOT / "package.v2.json"


def _captcha_html_source() -> str:
    return CAPTCHA_SERVER.read_text(encoding="utf-8")


def _template_branch(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


class SehuatangCaptchaNudgeTest(unittest.TestCase):
    def test_slide_and_rotate_nudge_controls_stay_in_their_template_branches(self):
        source = _captcha_html_source()
        slide_branch = _template_branch(
            source,
            "{% if captcha_type == 'slide' %}",
            "{% elif captcha_type == 'rotate' %}",
        )
        rotate_branch = _template_branch(
            source,
            "{% elif captcha_type == 'rotate' %}",
            "{% elif captcha_type == 'click' %}",
        )
        click_branch = _template_branch(
            source,
            "{% elif captcha_type == 'click' %}",
            "{% else %}",
        )

        self.assertIn('type="button"', slide_branch)
        self.assertIn("nudgeSlide(-1)", slide_branch)
        self.assertIn("nudgeSlide(1)", slide_branch)
        self.assertIn('type="button"', rotate_branch)
        self.assertIn("nudgeRotate(-1)", rotate_branch)
        self.assertIn("nudgeRotate(1)", rotate_branch)
        self.assertNotIn("nudgeSlide(", click_branch)
        self.assertNotIn("nudgeRotate(", click_branch)

    def test_nudge_handlers_reuse_existing_answer_render_paths(self):
        source = _captcha_html_source()

        slide_match = re.search(r"function nudgeSlide\(delta\) \{(?P<body>.*?)\n  \}", source, re.S)
        self.assertIsNotNone(slide_match)
        slide_body = slide_match.group("body")
        self.assertRegex(slide_body, r"left\s*=\s*clamp\(.+delta,\s*0,\s*masterW - tw\);")
        self.assertIn("render();", slide_body)
        self.assertNotIn("setAnswer(", slide_body)
        self.assertIn("x + ',' + y", source)

        rotate_match = re.search(r"function nudgeRotate\(delta\) \{(?P<body>.*?)\n  \}", source, re.S)
        self.assertIsNotNone(rotate_match)
        rotate_body = rotate_match.group("body")
        self.assertIn("renderAngle(true);", rotate_body)
        self.assertNotIn("setAnswer(", rotate_body)
        self.assertIn("String(angle)", source)

    def test_sehuatang_version_metadata_is_current_with_six_history_entries(self):
        init_source = PLUGIN_INIT.read_text(encoding="utf-8")
        package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
        sehuatang = package["SehuatangSignin"]

        self.assertIn('plugin_version = "1.2.5"', init_source)
        self.assertEqual(sehuatang["version"], "1.2.5")
        self.assertEqual(
            list(sehuatang["history"]),
            [
                "v1.2.5",
                "v1.2.4",
                "v1.2.3",
                "v1.2.2",
                "v1.2.1",
                "v1.2.0",
            ],
        )

    def test_cloakbrowser_launch_requires_proxy_when_proxy_configured(self):
        import importlib.util
        import sys
        import types
        from unittest.mock import patch

        module_name = "plugins.v2.sehuatangsignin.captcha_server_proxy_test"
        spec = importlib.util.spec_from_file_location(module_name, CAPTCHA_SERVER)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
            "app.log": types.SimpleNamespace(logger=types.SimpleNamespace(
                debug=lambda *a, **k: None,
                info=lambda *a, **k: None,
                warning=lambda *a, **k: None,
                error=lambda *a, **k: None,
            )),
        }):
            spec.loader.exec_module(module)

        calls = []

        def fake_cloak_launch(**kwargs):
            calls.append(kwargs)
            raise TypeError("unsupported")

        module.cloak_launch = fake_cloak_launch
        browser, runner, err = module._launch_cloak_browser("http://proxy.local:7890")
        self.assertIsNone(browser)
        self.assertIsNone(runner)
        self.assertIn("launch proxy option", err)
        self.assertIn("chromium proxy arg", err)
        self.assertEqual(
            calls,
            [
                {"headless": True, "proxy": {"server": "http://proxy.local:7890"}},
                {"headless": True, "args": ["--proxy-server=http://proxy.local:7890"]},
            ],
        )

    def test_cloakbrowser_plain_launch_only_when_no_proxy_configured(self):
        import importlib.util
        import sys
        import types
        from unittest.mock import patch

        module_name = "plugins.v2.sehuatangsignin.captcha_server_plain_test"
        spec = importlib.util.spec_from_file_location(module_name, CAPTCHA_SERVER)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
            "app.log": types.SimpleNamespace(logger=types.SimpleNamespace(
                debug=lambda *a, **k: None,
                info=lambda *a, **k: None,
                warning=lambda *a, **k: None,
                error=lambda *a, **k: None,
            )),
        }):
            spec.loader.exec_module(module)

        calls = []
        sentinel = object()

        def fake_cloak_launch(**kwargs):
            calls.append(kwargs)
            return sentinel

        module.cloak_launch = fake_cloak_launch
        browser, runner, err = module._launch_cloak_browser("")
        self.assertIs(browser, sentinel)
        self.assertIsNone(runner)
        self.assertEqual(err, "")
        self.assertEqual(calls, [{"headless": True}])

    def test_playwright_launch_receives_proxy_when_proxy_configured(self):
        import importlib.util
        import sys
        import types
        from unittest.mock import patch

        module_name = "plugins.v2.sehuatangsignin.captcha_server_playwright_proxy_test"
        spec = importlib.util.spec_from_file_location(module_name, CAPTCHA_SERVER)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
            "app.log": types.SimpleNamespace(logger=types.SimpleNamespace(
                debug=lambda *a, **k: None,
                info=lambda *a, **k: None,
                warning=lambda *a, **k: None,
                error=lambda *a, **k: None,
            )),
        }):
            spec.loader.exec_module(module)

        launch_calls = []
        sentinel = object()

        class FakeChromium:
            def launch(self, **kwargs):
                launch_calls.append(kwargs)
                return sentinel

        class FakeRunner:
            chromium = FakeChromium()

        module.sync_playwright = lambda: types.SimpleNamespace(start=lambda: FakeRunner())
        browser, runner, err = module._launch_playwright_browser("http://proxy.local:7890")
        self.assertIs(browser, sentinel)
        self.assertIsNotNone(runner)
        self.assertEqual(err, "")
        self.assertEqual(launch_calls, [{"headless": True, "proxy": {"server": "http://proxy.local:7890"}}])


if __name__ == "__main__":
    unittest.main()
