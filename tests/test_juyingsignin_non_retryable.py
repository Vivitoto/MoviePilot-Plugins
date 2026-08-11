import ast
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_INIT = ROOT / "plugins.v2" / "juyingsignin" / "__init__.py"
PACKAGE_JSON = ROOT / "package.v2.json"


def _plugin_source() -> str:
    return PLUGIN_INIT.read_text(encoding="utf-8")


class JuyingSigninNonRetryableTest(unittest.TestCase):
    def test_juying_version_metadata_is_1_0_3_with_recent_history_first(self):
        init_source = _plugin_source()
        package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
        juying = package["JuyingSignIn"]

        self.assertIn('plugin_version = "1.0.3"', init_source)
        self.assertEqual(juying["version"], "1.0.3")
        self.assertEqual(
            list(juying["history"]),
            [
                "v1.0.3",
                "v1.0.2",
                "v1.0.1",
                "v1.0.0",
            ],
        )

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
