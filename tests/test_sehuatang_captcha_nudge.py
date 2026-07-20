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

    def test_sehuatang_version_metadata_is_1_0_18_with_six_history_entries(self):
        init_source = PLUGIN_INIT.read_text(encoding="utf-8")
        package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
        sehuatang = package["SehuatangSignin"]

        self.assertIn('plugin_version = "1.0.18"', init_source)
        self.assertEqual(sehuatang["version"], "1.0.18")
        self.assertEqual(
            list(sehuatang["history"]),
            [
                "v1.0.18",
                "v1.0.17",
                "v1.0.16",
                "v1.0.15",
                "v1.0.14",
                "v1.0.13",
            ],
        )


if __name__ == "__main__":
    unittest.main()
