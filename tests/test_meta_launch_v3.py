"""Тесты правок 10.09.2026: crops, guards, validate_only, ig_profile, meta_launch dry-run."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from _isolate import ROOT, RUNTIME, SCRIPTS  # noqa: F401  (до импорта скриптов)

import meta_api  # noqa: E402
import meta_launch_builders as B  # noqa: E402
import meta_launch  # noqa: E402


class CropTests(unittest.TestCase):
    def test_vertical_focus_presets(self) -> None:
        self.assertEqual(B.crop_box(1080, 1920, "100x100", "top"), [[0, 0], [1080, 1080]])
        self.assertEqual(B.crop_box(1080, 1920, "100x100", "center"), [[0, 420], [1080, 1500]])
        self.assertEqual(B.crop_box(1080, 1920, "100x100", "bottom"), [[0, 840], [1080, 1920]])

    def test_vertical_focus_fraction_centres_accent(self) -> None:
        box = B.crop_box(1080, 1920, "100x100", 0.35)
        self.assertEqual(box, [[0, 132], [1080, 1212]])
        self.assertEqual(B.crop_box(1080, 1920, "100x100", 0.0), [[0, 0], [1080, 1080]])
        self.assertEqual(B.crop_box(1080, 1920, "100x100", 1.0), [[0, 840], [1080, 1920]])

    def test_four_five_and_wide_source(self) -> None:
        self.assertEqual(B.crop_box(1080, 1920, "400x500", "top"), [[0, 0], [1080, 1350]])
        # широкая картинка режется по ширине
        self.assertEqual(B.crop_box(1920, 1080, "100x100", "center"), [[420, 0], [1500, 1080]])
        self.assertEqual(B.crop_box(1920, 1080, "100x100", "top"), [[0, 0], [1080, 1080]])

    def test_image_crops_for_default_keys(self) -> None:
        crops = B.image_crops_for(1080, 1920, "top")
        self.assertEqual(set(crops), {"100x100", "400x500"})
        self.assertNotIn("191x100", crops)

    def test_unknown_focus_rejected(self) -> None:
        with self.assertRaises(ValueError):
            B.crop_box(1080, 1920, "100x100", "middle")

    def test_placement_assets_and_lead_form_rules(self) -> None:
        crops = B.image_crops_for(1080, 1920, "top")
        images, rules = B.placement_image_assets("h1", crops)
        self.assertEqual(len(images), 2)
        self.assertEqual(images[1]["image_crops"], crops)
        self.assertEqual(len(rules), 2)
        creative = B.lead_form_static_creative(
            name="c", page_id="1", message="m", lead_gen_form_id="9", image_hash="h1", image_crops=crops
        )
        feed = creative["asset_feed_spec"]
        self.assertEqual(feed["optimization_type"], "PLACEMENT")
        self.assertEqual(len(feed["asset_customization_rules"]), 2)
        self.assertEqual(feed["asset_customization_rules"][0]["body_label"], {"name": "body_main"})
        simple = B.lead_form_static_creative(
            name="c", page_id="1", message="m", lead_gen_form_id="9", image_hash="h1",
            image_crops=crops, creative_mode="simple_story",
        )
        self.assertEqual(simple["object_story_spec"]["link_data"]["image_crops"], crops)


class GuardTests(unittest.TestCase):
    def test_standard_enhancements_stripped(self) -> None:
        params = {
            "object_story_spec": {"page_id": "1", "link_data": {"link": "x", "image_hash": "h"}},
            "degrees_of_freedom_spec": {"creative_features_spec": {
                "standard_enhancements": {"enroll_status": "OPT_IN"},
                "image_touchups": {"enroll_status": "OPT_OUT"},
            }},
        }
        self.assertIsNone(meta_api.MetaAdsAPI._guard_creative_schema(params))
        features = params["degrees_of_freedom_spec"]["creative_features_spec"]
        self.assertNotIn("standard_enhancements", features)
        self.assertIn("image_touchups", features)

    def test_advantage_audience_forced_explicit(self) -> None:
        params = {"targeting": {"geo_locations": {"countries": ["KZ"]}, "age_min": 18}}
        meta_api.MetaAdsAPI._guard_targeting_expansion(params)
        auto = params["targeting"]["targeting_automation"]
        self.assertEqual(auto["advantage_audience"], 0)
        self.assertEqual(auto["individual_setting"], {"age": 0, "geo": 0})
        bare = {"targeting": {"custom_audiences": [{"id": "1"}]}}
        meta_api.MetaAdsAPI._guard_targeting_expansion(bare)
        self.assertEqual(bare["targeting"]["targeting_automation"]["advantage_audience"], 0)
        self.assertEqual(B.normalize_targeting_for_create({"custom_audiences": []})["targeting_automation"]["advantage_audience"], 0)

    def test_placements_guard_strips_explore_on_any_version(self) -> None:
        params = {"targeting": {"publisher_platforms": ["instagram"],
                                "instagram_positions": ["stream", "explore", "explore_home", "reels"]}}
        self.assertIsNone(meta_api.MetaAdsAPI._guard_placements(params))
        self.assertEqual(params["targeting"]["instagram_positions"], ["stream", "reels"])
        self.assertEqual(B.normalize_targeting_for_create({"instagram_positions": ["explore_home", "story"]})["instagram_positions"], ["story"])

    def test_placements_guard_blocks_forbidden_placements(self) -> None:
        guard = meta_api.MetaAdsAPI._guard_placements
        auto = {"targeting": {"age_min": 18}}  # автоплейсменты → Instagram везде + Threads
        self.assertIsNone(guard(auto))
        self.assertEqual(auto["targeting"]["publisher_platforms"], ["instagram", "threads"])
        self.assertEqual(auto["targeting"]["instagram_positions"], B.DEFAULT_INSTAGRAM_POSITIONS)
        profile = {"destination_type": "INSTAGRAM_PROFILE", "targeting": {"age_min": 18}}
        self.assertIsNone(guard(profile))
        self.assertEqual(profile["targeting"]["publisher_platforms"], ["instagram"])
        self.assertTrue(guard({"targeting": {"publisher_platforms": ["instagram", "audience_network"]}}).get("blocked"))
        self.assertTrue(guard({"targeting": {"publisher_platforms": ["facebook"]}}).get("blocked"))  # без facebook_positions
        self.assertTrue(guard({"targeting": {"publisher_platforms": ["facebook"], "facebook_positions": ["feed", "marketplace"]}}).get("blocked"))
        self.assertIsNone(guard({"targeting": {"publisher_platforms": ["facebook", "instagram"], "facebook_positions": ["feed", "story"]}}))
        self.assertTrue(any("marketplace" in i for i in B.validate_targeting({"publisher_platforms": ["facebook"], "facebook_positions": ["marketplace"]})))

    def test_creative_guard_forces_opt_out_and_blocks_opt_in(self) -> None:
        params = {"object_story_spec": {"page_id": "1", "link_data": {"image_hash": "h", "link": "x"}}}
        self.assertIsNone(meta_api.MetaAdsAPI._guard_creative_schema(params))
        features = params["degrees_of_freedom_spec"]["creative_features_spec"]
        self.assertEqual(set(features), set(B.ADVANTAGE_CREATIVE_FEATURES))
        self.assertTrue(all(v["enroll_status"] == "OPT_OUT" for v in features.values()))
        self.assertEqual(params["contextual_multi_ads"], {"enroll_status": "OPT_OUT"})
        multi_on = {"object_story_spec": {"page_id": "1", "link_data": {"image_hash": "h", "link": "x"}},
                    "contextual_multi_ads": {"enroll_status": "OPT_IN"}}
        self.assertTrue(meta_api.MetaAdsAPI._guard_creative_schema(multi_on).get("blocked"))
        bad = {"object_story_spec": {"page_id": "1", "link_data": {"image_hash": "h", "link": "x"}},
               "degrees_of_freedom_spec": {"creative_features_spec": {"image_touchups": {"enroll_status": "OPT_IN"}}}}
        self.assertTrue(meta_api.MetaAdsAPI._guard_creative_schema(bad).get("blocked"))
        self.assertTrue(meta_api.MetaAdsAPI(dry_run=True).update_entity("1", {"is_dynamic_creative": True}).get("blocked"))

    def test_rate_limit_detection_uses_official_codes_only(self) -> None:
        self.assertIn(2446079, meta_api.RATE_LIMIT_SUBCODES)
        self.assertIn(5044001, meta_api.RATE_LIMIT_SUBCODES)
        self.assertIn(80004, meta_api.RATE_LIMIT_CODES)
        self.assertNotIn(100, meta_api.RATE_LIMIT_CODES)

    def test_preview_formats_follow_placements(self) -> None:
        self.assertEqual(B.preview_formats_for({"publisher_platforms": ["instagram", "threads"], "instagram_positions": ["stream", "reels"]}),
                         ["INSTAGRAM_STANDARD", "INSTAGRAM_REELS", "THREADS_STREAM"])
        self.assertIn("FACEBOOK_REELS_MOBILE", B.preview_formats_for({"publisher_platforms": ["facebook"], "facebook_positions": ["feed", "facebook_reels"]}))

    def test_validate_only_dry_run_sends_execution_option(self) -> None:
        api = meta_api.MetaAdsAPI(dry_run=True)
        result = api.validate_creative("act_1", {"object_story_spec": {"page_id": "1", "link_data": {"image_hash": "h", "link": "x"}}})
        self.assertTrue(result.get("validate_only"))
        result = api.validate_adset("123", {"name": "a", "optimization_goal": "CONVERSATIONS", "destination_type": "WHATSAPP",
                                            "promoted_object": {"page_id": "1"}, "targeting": {"age_min": 18, "publisher_platforms": ["instagram"]}})
        self.assertTrue(result.get("success"))

    def test_delays_are_relaxed_but_bounded(self) -> None:
        self.assertLessEqual(meta_api.WRITE_DELAY_SEC, 1.0)
        self.assertEqual(meta_api.BUDGET_MAX_PER_HOUR, 3)
        self.assertGreaterEqual(meta_api.BATCH_HARD_CAP, 10)
        self.assertGreaterEqual(meta_api.BATCH_GROUP_PAUSE_SEC, 0)


class ScenarioTests(unittest.TestCase):
    def test_ig_profile_scenarios(self) -> None:
        params = B.ig_profile_adset_params(name="a", daily_budget=500, page_id="1",
                                           targeting={"age_min": 18, "publisher_platforms": ["instagram"]})
        self.assertEqual(params["optimization_goal"], "PROFILE_VISIT")
        self.assertEqual(params["destination_type"], "INSTAGRAM_PROFILE")
        self.assertEqual(B.validate_adset_params("ig_profile_video", params, {"page_id": "1"}), [])
        bad = dict(params, targeting={"publisher_platforms": ["facebook", "instagram"]})
        self.assertTrue(any("instagram" in issue for issue in B.validate_adset_params("ig_profile_video", bad, {"page_id": "1"})))
        creative = B.ig_profile_video_creative(name="c", page_id="1", instagram_user_id="2", instagram_username="@coffee.house",
                                               message="m", video_id="3", image_url="u")
        cta = creative["object_story_spec"]["video_data"]["call_to_action"]
        self.assertEqual(cta["type"], "VIEW_INSTAGRAM_PROFILE")
        self.assertEqual(cta["value"]["app_link"], "instagram://user?username=coffee.house")
        self.assertEqual(B.validate_creative_params("ig_profile_video", creative, {"page_id": "1", "instagram_user_id": "2"}), [])

    def test_ctwa_adset_defaults(self) -> None:
        params = B.ctwa_adset_params(name="a", daily_budget=500, page_id="1", targeting={"age_min": 18},
                                     whatsapp_phone_number="+77000000001")
        self.assertEqual(params["attribution_spec"], [{"event_type": "CLICK_THROUGH", "window_days": 1}])
        self.assertEqual(params["promoted_object"]["whatsapp_phone_number"], "77000000001")

    def test_naming_convention(self) -> None:
        ok = B.validate_names("[almaty] | whatsapp | кофе с собой | 10.09", "Капучино баннер | 25+ все | 10.09", "Капучино баннер")
        self.assertEqual(ok, [])
        bad = B.validate_names("[kz]. wp. вебинар (04.09)", "Тюк 500 | 26+ все | 11.09", "Тюк одежда 500")
        self.assertTrue(any("[гео] | цель" in i for i in bad))
        self.assertTrue(any("должно совпадать" in i for i in bad))
        wrong_goal = B.validate_names("[kz] | wp | вебинар | 04.09", "плакат Елена | интересы | 04.09", "плакат Елена")
        self.assertTrue(any("не из словаря" in i for i in wrong_goal))
        date_mismatch = B.validate_names("[almaty] | whatsapp | кофе | 10.09", "Капучино баннер | 25+ все | 11.09", "Капучино баннер")
        self.assertTrue(any("не совпадает с датой" in i for i in date_mismatch))

    def test_advantage_creative_opt_out_has_no_bundle(self) -> None:
        features = B.advantage_creative_opt_out()["creative_features_spec"]
        self.assertNotIn("standard_enhancements", features)
        self.assertTrue(all(v == {"enroll_status": "OPT_OUT"} for v in features.values()))


class LauncherDryRunTests(unittest.TestCase):
    def setUp(self) -> None:
        from PIL import Image

        self.dir = Path(tempfile.mkdtemp(prefix="launch-"))
        Image.new("RGB", (1080, 1920), (10, 20, 30)).save(self.dir / "poster.png")
        self.spec = {
            "client": "test", "label": "dry", "account_id": "act_1", "scenario": "ctwa_static",
            "bindings": {"page_id": "11", "whatsapp_phone_number": "77000000000"},
            "media": {"poster": {"file": str(self.dir / "poster.png")}},
            "defaults": {
                "adset": {"daily_budget": 600, "targeting": {"age_min": 18, "geo_locations": {"countries": ["KZ"]},
                                                              "publisher_platforms": ["instagram"]}},
                "creative": {"message": "hi", "headline": "h"},
            },
            "campaigns": [{"name": "camp", "adsets": [
                {"name": "adset 1", "ads": [{"name": "ad 1", "media": "poster", "creative": {"focus": "top"}}]},
                {"name": "adset 2", "ads": [{"name": "ad 2", "media": "poster", "creative": {"focus": 0.5}}]},
            ]}],
        }

    def test_dry_run_builds_tree_and_resumes(self) -> None:
        api = meta_api.MetaAdsAPI(dry_run=True)
        state_path = self.dir / "state.json"
        launcher = meta_launch.Launcher(self.spec, api, state_path)
        self.assertEqual(launcher.validate_local(), [])
        summary = launcher.run()
        self.assertEqual(summary["ads"], 2)
        self.assertEqual(summary["errors"], 0)
        state = json.loads(state_path.read_text())
        self.assertIn("camp", state["index"]["campaigns"])
        self.assertEqual(len(state["index"]["ads"]), 2)
        # focus для link_data игнорируется: image_crops там режут и Reels/Stories (проверено 10.09.2026)
        camp = self.spec["campaigns"][0]
        params = launcher.creative_params(camp, camp["adsets"][0], camp["adsets"][0]["ads"][0])
        link_data = params["object_story_spec"]["link_data"]
        self.assertNotIn("image_crops", link_data)
        self.assertEqual(link_data["call_to_action"]["type"], "WHATSAPP_MESSAGE")
        self.assertIsNone(meta_api.MetaAdsAPI._guard_creative_schema(dict(params)))
        self.assertNotIn("url_tags", params)
        self.assertIn("degrees_of_freedom_spec", params)
        # повторный запуск ничего не досоздаёт
        again = meta_launch.Launcher(self.spec, api, state_path, json.loads(state_path.read_text()))
        summary2 = again.run()
        self.assertEqual(summary2["ads"], 2)
        self.assertEqual(len(json.loads(state_path.read_text())["created_ids"]["ads"]), 2)

    def test_lead_form_focus_goes_to_asset_feed_only(self) -> None:
        spec = dict(self.spec, scenario="lead_form_static",
                    bindings={"page_id": "11", "lead_gen_form_id": "77"})
        launcher = meta_launch.Launcher(spec, meta_api.MetaAdsAPI(dry_run=True), self.dir / "s3.json")
        launcher.upload_media()
        camp = spec["campaigns"][0]
        params = launcher.creative_params(camp, camp["adsets"][0], camp["adsets"][0]["ads"][0])
        feed = params["asset_feed_spec"]
        self.assertEqual(len(feed["images"]), 2)
        self.assertIn("image_crops", feed["images"][1])
        self.assertEqual(len(feed["asset_customization_rules"]), 2)
        self.assertNotIn("link_data", params["object_story_spec"])

    def test_link_data_crops_are_blocked(self) -> None:
        params = {"object_story_spec": {"page_id": "1", "link_data": {"link": "x", "image_hash": "h",
                                                                        "image_crops": {"100x100": [[0, 0], [10, 10]]}}}}
        blocked = meta_api.MetaAdsAPI._guard_creative_schema(params)
        self.assertTrue(blocked and blocked.get("blocked"))
        issues = B.validate_creative_params("ctwa_static", {"object_story_spec": {"page_id": "1", "link_data": {
            "image_hash": "h", "image_crops": {"100x100": [[0, 0], [10, 10]]},
            "call_to_action": {"type": "WHATSAPP_MESSAGE"}}}}, {"page_id": "1"})
        self.assertTrue(any("Reels" in issue for issue in issues))

    def test_missing_binding_is_reported(self) -> None:
        spec = dict(self.spec, scenario="lead_site_static")
        launcher = meta_launch.Launcher(spec, meta_api.MetaAdsAPI(dry_run=True), self.dir / "s2.json")
        with self.assertRaises(meta_launch.LaunchError):
            launcher.validate_local()


if __name__ == "__main__":
    unittest.main()
