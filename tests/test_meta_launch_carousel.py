from __future__ import annotations

import unittest


from _isolate import SCRIPTS  # noqa: F401  (до импорта скриптов)

from meta_launch_builders import lead_site_carousel_creative, validate_creative_params  # noqa: E402


class LeadSiteCarouselTests(unittest.TestCase):
    def test_preserves_order_and_needs_no_parent_image(self) -> None:
        creative = lead_site_carousel_creative(
            name="Coffee site carousel",
            page_id="100000000000001",
            instagram_user_id="17840000000000001",
            link="https://example.com/coffee",
            message="Same source text",
            cards=[
                {"image_hash": "hash-one", "name": "Card one"},
                {"image_hash": "hash-two", "name": "Card two"},
            ],
            multi_share_end_card=False,
            multi_share_optimized=True,
        )

        link_data = creative["object_story_spec"]["link_data"]
        self.assertNotIn("image_hash", link_data)
        self.assertEqual(
            [card["image_hash"] for card in link_data["child_attachments"]],
            ["hash-one", "hash-two"],
        )
        self.assertTrue(
            all(card["link"] == "https://example.com/coffee" for card in link_data["child_attachments"])
        )
        self.assertTrue(
            all(card["call_to_action"]["type"] == "LEARN_MORE" for card in link_data["child_attachments"])
        )
        self.assertEqual(
            validate_creative_params(
                "lead_site_static",
                creative,
                {"page_id": "100000000000001", "instagram_user_id": "17840000000000001"},
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
