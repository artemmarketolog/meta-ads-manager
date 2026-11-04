"""Тесты публичной части: конфиг, приём токена, относительные пути спек, статистика."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _isolate import ROOT, SCRIPTS  # noqa: F401  (до импорта скриптов)

import meta_api  # noqa: E402
import meta_launch  # noqa: E402
import meta_launch_builders as B  # noqa: E402

sys.path.insert(0, str(ROOT))
import setup  # noqa: E402


class DefaultsTests(unittest.TestCase):
    def test_url_tags_use_names_and_configurable_medium(self) -> None:
        self.assertIn("utm_medium=meta_ads", B.DEFAULT_URL_TAGS)
        for macro in ("{{campaign.name}}", "{{adset.name}}", "{{ad.name}}"):
            self.assertIn(macro, B.DEFAULT_URL_TAGS)
        self.assertNotIn(".id}}", B.DEFAULT_URL_TAGS)

    def test_default_placements_instagram_threads(self) -> None:
        targeting: dict = {}
        self.assertTrue(B.apply_default_placements(targeting))
        self.assertEqual(targeting["publisher_platforms"], ["instagram", "threads"])
        self.assertEqual(B.placement_violations(targeting), [])
        profile: dict = {}
        B.apply_default_placements(profile, threads=False)
        self.assertEqual(profile["publisher_platforms"], ["instagram"])

    def test_facebook_default_gets_explicit_positions(self) -> None:
        with mock.patch.object(B, "DEFAULT_PLATFORMS", ["instagram", "facebook"]):
            targeting: dict = {}
            B.apply_default_placements(targeting)
        self.assertIn("facebook_positions", targeting)
        self.assertNotIn("marketplace", targeting["facebook_positions"])
        self.assertEqual(B.placement_violations(targeting), [])

    def test_no_private_paths_in_scripts(self) -> None:
        for path in list(SCRIPTS.glob("*.py")) + [ROOT / "setup.py"]:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("/home/", text, path.name)
            self.assertNotIn("/srv/", text, path.name)


class TokenIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="cfg-"))
        self.env = self.dir / "meta.env"
        patches = [mock.patch.object(setup, "CONFIG_DIR", self.dir), mock.patch.object(setup, "ENV_FILE", self.env)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_save_token_keeps_other_settings_and_is_private(self) -> None:
        self.env.write_text("META_TOKEN=\nMETA_UTM_MEDIUM=my_agency\n")
        setup.save_token("EAAB" + "x" * 120)
        lines = self.env.read_text().splitlines()
        self.assertIn("META_UTM_MEDIUM=my_agency", lines)
        self.assertIn("META_TOKEN=EAAB" + "x" * 120, lines)
        self.assertEqual(stat.S_IMODE(os.stat(self.env).st_mode), 0o600)

    def test_rejects_non_token_without_saving(self) -> None:
        with mock.patch("getpass.getpass", return_value="hello"), self.assertRaises(SystemExit):
            setup.token(from_clipboard=False)
        self.assertFalse(self.env.exists())

    def test_invalid_token_is_not_saved(self) -> None:
        with mock.patch("getpass.getpass", return_value="EAAB" + "y" * 120), \
                mock.patch.object(setup, "debug_token", return_value={"is_valid": False, "error": {"message": "bad"}}), \
                self.assertRaises(SystemExit):
            setup.token(from_clipboard=False)
        self.assertFalse(self.env.exists())

    def test_valid_token_output_is_masked(self) -> None:
        token = "EAAB" + "z" * 116 + "WXYZ"
        with mock.patch("getpass.getpass", return_value=token), \
                mock.patch.object(setup, "debug_token", return_value={"is_valid": True, "type": "SYSTEM_USER",
                                                                    "application": "Test", "expires_at": 0, "scopes": ["ads_management"]}), \
                mock.patch("builtins.print") as printed:
            setup.token(from_clipboard=False)
        output = " ".join(str(c.args) for c in printed.call_args_list)
        self.assertNotIn(token, output)
        self.assertIn("WXYZ", output)


class SpecPathTests(unittest.TestCase):
    def test_relative_media_resolved_from_spec_folder(self) -> None:
        spec = meta_launch.load_spec(str(ROOT / "examples/specs/lead_site_video.json"))
        media = spec["media"]["vid"]
        self.assertTrue(os.path.isabs(media["file"]))
        self.assertTrue(os.path.exists(media["file"]))
        self.assertTrue(os.path.exists(media["thumbnail"]))

    def test_all_examples_pass_local_validation(self) -> None:
        for path in sorted((ROOT / "examples/specs").glob("*.json")):
            with self.subTest(spec=path.name):
                spec = meta_launch.load_spec(str(path))
                launcher = meta_launch.Launcher(spec, meta_api.MetaAdsAPI(dry_run=True), Path(tempfile.mkdtemp()) / "s.json")
                self.assertEqual(launcher.validate_local(), [])
                self.assertEqual(launcher.naming_warnings(), [])


class InsightsTests(unittest.TestCase):
    def test_main_result_follows_objective(self) -> None:
        api = meta_api.MetaAdsAPI(token="t", dry_run=False)
        row = {"campaign_name": "[almaty] | whatsapp | кофе | 05.11", "objective": "OUTCOME_ENGAGEMENT", "spend": "20",
               "impressions": "1000", "reach": "800", "account_currency": "USD",
               "actions": [{"action_type": "lead", "value": "2"},
                           {"action_type": "onsite_conversion.messaging_conversation_started_7d", "value": "10"}]}
        with mock.patch.object(api, "_request", return_value={"data": [row]}), \
                mock.patch.object(meta_api, "cli_print") as printed:
            api.insights("act_1", level="campaign")
        line = printed.call_args_list[-1].args[0]
        self.assertIn("2.00 USD за 1 (переписки)", line)


if __name__ == "__main__":
    unittest.main()
