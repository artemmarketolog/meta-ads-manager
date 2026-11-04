from __future__ import annotations

import inspect
import os
import subprocess
import sys
import unittest


from _isolate import SCRIPTS  # noqa: F401  (до импорта скриптов)

import meta_api  # noqa: E402


class MetaPermissionFlowTests(unittest.TestCase):
    def test_legacy_video_thumbnail_is_normalized_before_create(self) -> None:
        params = {
            "object_story_spec": {
                "page_id": "1",
                "video_data": {
                    "video_id": "2",
                    "image_hash": "hash",
                    "image_url": "https://example.com/thumb.jpg",
                },
            }
        }

        blocked = meta_api.MetaAdsAPI._guard_creative_schema(params)

        self.assertIsNone(blocked)
        video_data = params["object_story_spec"]["video_data"]
        self.assertEqual(video_data["image_hash"], "hash")
        self.assertNotIn("image_url", video_data)

    def test_public_write_methods_have_no_approval_parameters(self) -> None:
        self.assertNotIn("approved", inspect.signature(meta_api.MetaAdsAPI.activate).parameters)
        self.assertNotIn("approved", inspect.signature(meta_api.MetaAdsAPI.batch_activate).parameters)
        self.assertNotIn("emergency_approved", inspect.signature(meta_api.MetaAdsAPI.delete).parameters)

    def test_activate_delete_and_batch_run_directly_in_dry_run(self) -> None:
        api = meta_api.MetaAdsAPI(token="test-token", dry_run=True)

        activated = api.activate("campaign", "123", check_parents=False)
        deleted = api.delete("ad", "456")
        batch = api.batch_activate("campaign", ["123", "456"])

        self.assertNotIn("error", activated)
        self.assertIn("readback", activated)
        self.assertNotIn("error", deleted)
        self.assertIn("readback", deleted)
        self.assertEqual(len(batch), 2)
        self.assertTrue(all("error" not in row["result"] for row in batch))

    def test_copy_activate_targets_the_reported_launch_gate(self) -> None:
        api = meta_api.MetaAdsAPI(token="test-token", dry_run=True)
        calls: list[tuple[str, str]] = []

        def activate(entity_type: str, entity_id: str):
            calls.append((entity_type, entity_id))
            return {"success": True, "readback": {"status": "ACTIVE"}}

        api.activate = activate  # type: ignore[method-assign]
        result = api._activate_after_duplicate(
            {"launch_gate": "adset", "campaign_id": "10", "adset_id": "20"}
        )

        self.assertEqual(calls, [("adset", "20")])
        self.assertTrue(result["activated"])

    def test_cli_accepts_copy_and_duplicate_activate_without_network_or_spend(self) -> None:
        env = os.environ.copy()
        cases = (
            ("duplicate-adset", "123", "safe duplicate adset", "500"),
            ("copy-adset-native", "123", "safe native adset", "500"),
            ("copy-campaign-native", "123", "safe native campaign"),
            ("duplicate-campaign-chunked", "123", "safe chunked campaign"),
        )
        for case in cases:
            with self.subTest(command=case[0]):
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPTS / "meta_api.py"),
                        *case,
                        "--activate",
                        "--dry-run",
                    ],
                    cwd=str(SCRIPTS),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )

                self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
                self.assertNotIn("запрещён", proc.stdout)
                self.assertIn("Readback after activate", proc.stdout)


if __name__ == "__main__":
    unittest.main()
