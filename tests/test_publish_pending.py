import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "publish_pending.py"
SPEC = importlib.util.spec_from_file_location("publish_pending", MODULE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


class ManifestValidationTests(unittest.TestCase):
    def valid(self):
        return {
            "schema_version": 1,
            "title": "Titolo",
            "slug": "titolo-articolo",
            "content": "<p>Testo</p>",
            "category": "agricoltura-rigenerativa",
            "status": "pending",
            "media": [],
        }

    def test_pending_is_allowed(self):
        bridge.validate_manifest(self.valid())

    def test_publish_is_rejected(self):
        manifest = self.valid()
        manifest["status"] = "publish"
        with self.assertRaisesRegex(bridge.BridgeError, "Only status=pending"):
            bridge.validate_manifest(manifest)

    def test_unknown_media_placeholder_is_rejected(self):
        manifest = self.valid()
        manifest["content"] = '<img src="{{media:missing}}">'
        with self.assertRaisesRegex(bridge.BridgeError, "unknown media placeholders"):
            bridge.validate_manifest(manifest)

    def test_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "secret.webp"
            outside.write_bytes(b"test")
            with self.assertRaisesRegex(bridge.BridgeError, "inbox/assets"):
                bridge.safe_repo_media_path(root, "secret.webp")

    def test_rest_route_fallback_preserves_query(self):
        fallback = bridge.rest_route_fallback("/wp-json/wp/v2/posts?slug=prova&context=edit")
        self.assertEqual(
            fallback,
            "/?rest_route=%2Fwp%2Fv2%2Fposts&slug=prova&context=edit",
        )


if __name__ == "__main__":
    unittest.main()

