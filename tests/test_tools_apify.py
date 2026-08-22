"""Unit tests for bounded Apify business research (all provider calls mocked)."""

from __future__ import annotations

import json
import unittest
import urllib.parse
from typing import Any
from unittest.mock import patch

from host.tools import apify
from host.tools.apify import ApifyTool
from host.tools.results import ActionExecuted, ActionFailed
from host.tools.shared.web import WebRequestError

from test_tools import FakeHostAPI, assert_matches_output_schema


PLACE_ID = "ChIJN1t_tDeuEmsRUsoyG83frY4"


def configured_api() -> FakeHostAPI:
    api = FakeHostAPI()
    api.config["APIFY_API_TOKEN"] = "apify-token"
    return api


def business() -> dict[str, object]:
    return {
        "placeId": PLACE_ID,
        "title": "Acme Bakery",
        "description": "Family bakery",
        "categoryName": "Bakery",
        "categories": ["Bakery", "Cafe"],
        "address": "1 High Street",
        "city": "London",
        "postalCode": "SW1A 1AA",
        "countryCode": "GB",
        "location": {"lat": 51.5, "lng": -0.12},
        "phoneUnformatted": "+442000000000",
        "website": "https://acme.example/",
        "totalScore": 4.7,
        "reviewsCount": 120,
        "imagesCount": 18,
        "openingHours": [{"day": "Monday", "hours": "08:00-17:00"}],
        "reviewsDistribution": {"fiveStar": 100, "fourStar": 12},
        "emails": ["hello@acme.example"],
        "phones": ["+442000000000"],
        "facebooks": ["https://facebook.com/acme"],
        "images": [
            {
                "imageUrl": "https://lh5.googleusercontent.com/acme.jpg",
                "authorName": "Acme Bakery",
                "authorUrl": "https://www.google.com/maps/contrib/123",
            }
        ],
        "reviews": [
            {
                "text": "Excellent bread",
                "stars": 5,
                "publishedAtDate": "2026-01-01",
                "name": "Public Reviewer",
                "reviewerUrl": "https://www.google.com/maps/contrib/456",
                "reviewerPhotoUrl": "https://lh3.googleusercontent.com/reviewer.jpg",
                "reviewUrl": "https://www.google.com/maps/reviews/data=abc",
            }
        ],
        "additionalInfo": {"Service options": [{"Takeout": True}, {"Delivery": False}]},
        "questionsAndAnswers": [{"question": "Gluten free?", "answer": "Yes"}],
    }


class ApifyToolTests(unittest.TestCase):
    def test_manifest_exposes_only_bounded_business_actions_and_full_guide(self) -> None:
        manifest = ApifyTool().manifest
        self.assertEqual(manifest.tool_id, "apify")
        self.assertEqual(manifest.connection, "enable_only")
        self.assertEqual([action.id for action in manifest.actions], ["search_businesses", "get_business_details"])
        self.assertTrue(all(action.approval == "direct" for action in manifest.actions))
        self.assertEqual([item.key for item in manifest.config], ["APIFY_API_TOKEN"])
        self.assertEqual(len(manifest.data_summary.cards), 4)
        self.assertEqual(manifest.data_summary.cards[0].title, "What leaves this host")
        self.assertTrue(manifest.setup_steps[-1].show_config)
        combined = " ".join((*manifest.protections, *manifest.technical_details, manifest.agent_notes))
        self.assertIn("$0.50", manifest.actions[0].description)
        self.assertIn("$0.25", manifest.actions[1].description)
        self.assertIn("cannot choose or run another Actor", combined)
        self.assertIn("does not grant", combined)
        self.assertIn("permission", combined)

    def test_every_direct_result_matches_its_declared_output_schema(self) -> None:
        for action, tool_input in (
            ("search_businesses", {"query": "bakery", "location": "London, United Kingdom"}),
            ("get_business_details", {"place_id": PLACE_ID}),
        ):
            with self.subTest(action=action), patch.object(
                apify, "request_bytes", lambda *a, **k: json.dumps([business()]).encode()
            ):
                result = ApifyTool().execute(action, tool_input, configured_api())
            assert_matches_output_schema(self, apify.MANIFEST, action, result)

    def test_search_uses_fixed_actor_header_caps_and_guarded_input(self) -> None:
        seen: dict[str, Any] = {}

        def fake_request(method: str, url: str, **kwargs: Any) -> bytes:
            seen.update(method=method, url=url, **kwargs)
            return json.dumps([business()]).encode()

        with patch.object(apify, "request_bytes", fake_request):
            result = ApifyTool().execute(
                "search_businesses",
                {
                    "query": "bakery",
                    "location": "London, United Kingdom",
                    "limit": "7",
                    "minimum_rating": "4",
                    "website_filter": "without_website",
                    "skip_closed": False,
                },
                configured_api(),
            )

        assert isinstance(result, ActionExecuted)
        parsed = urllib.parse.urlsplit(seen["url"])
        params = urllib.parse.parse_qs(parsed.query)
        payload = json.loads(seen["data"])
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(parsed.netloc, "api.apify.com")
        self.assertEqual(parsed.path, "/v2/acts/compass~crawler-google-places/run-sync-get-dataset-items")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer apify-token")
        self.assertNotIn("apify-token", seen["url"])
        self.assertEqual(params["maxItems"], ["7"])
        self.assertEqual(params["maxTotalChargeUsd"], [apify.SEARCH_MAX_CHARGE_USD])
        self.assertEqual(params["timeout"], [str(apify.API_TIMEOUT_SECONDS)])
        self.assertEqual(seen["timeout"], apify.API_TIMEOUT_SECONDS + 5)
        self.assertEqual(payload["searchStringsArray"], ["bakery"])
        self.assertEqual(payload["locationQuery"], "London, United Kingdom")
        self.assertEqual(payload["maxCrawledPlacesPerSearch"], 7)
        self.assertEqual(payload["placeMinimumStars"], "four")
        self.assertEqual(payload["website"], "withoutWebsite")
        self.assertFalse(payload["skipClosedPlaces"])
        self.assertFalse(payload["scrapePlaceDetailPage"])
        self.assertFalse(payload["scrapeContacts"])
        self.assertFalse(payload["scrapeReviewsPersonalData"])
        self.assertEqual(payload["maxReviews"], 0)
        self.assertEqual(payload["maxImages"], 0)
        self.assertEqual(payload["maximumLeadsEnrichmentRecords"], 0)
        self.assertFalse(payload["enableCompetitorAnalysis"])
        self.assertNotIn("maxQuestions", payload)
        self.assertNotIn("actorId", payload)
        self.assertEqual(result.result["businesses"][0]["name"], "Acme Bakery")
        self.assertIn(f"query_place_id={PLACE_ID}", result.result["businesses"][0]["google_maps_url"])

    def test_detail_is_one_exact_place_with_bounded_maps_data(self) -> None:
        seen: dict[str, Any] = {}

        def fake_request(method: str, url: str, **kwargs: Any) -> bytes:
            seen["url"] = url
            seen["payload"] = json.loads(kwargs["data"])
            return json.dumps([business()]).encode()

        with patch.object(apify, "request_bytes", fake_request):
            result = ApifyTool().execute(
                "get_business_details",
                {
                    "place_id": PLACE_ID,
                    "max_reviews": "5",
                    "max_images": "8",
                },
                configured_api(),
            )

        assert isinstance(result, ActionExecuted)
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(seen["url"]).query)
        payload = seen["payload"]
        self.assertEqual(params["maxItems"], ["1"])
        self.assertEqual(params["maxTotalChargeUsd"], [apify.DETAIL_MAX_CHARGE_USD])
        self.assertEqual(payload["placeIds"], [PLACE_ID])
        self.assertNotIn("searchStringsArray", payload)
        self.assertEqual(payload["maxCrawledPlacesPerSearch"], 1)
        self.assertEqual(payload["maxReviews"], 5)
        self.assertEqual(payload["maxImages"], 8)
        self.assertFalse(payload["scrapeContacts"])
        self.assertFalse(payload["scrapeReviewsPersonalData"])
        self.assertTrue(payload["scrapeImageAuthors"])
        self.assertEqual(payload["maxQuestions"], 0)
        self.assertEqual(payload["maximumLeadsEnrichmentRecords"], 0)
        self.assertFalse(any(payload["scrapeSocialMediaProfiles"].values()))
        detail = result.result["business"]
        self.assertEqual(detail["images"][0]["author_name"], "Acme Bakery")
        self.assertEqual(detail["reviews"][0]["text"], "Excellent bread")
        self.assertEqual(detail["additional_info"][0]["label"], "Service options")
        self.assertEqual(detail["additional_info"][0]["value"], "Takeout: yes, Delivery: no")
        self.assertNotIn("contacts", detail)
        self.assertNotIn("questions_and_answers", detail)
        self.assertNotIn("review_distribution", detail)

    def test_reviewer_identifiers_are_never_returned(self) -> None:
        with patch.object(apify, "request_bytes", return_value=json.dumps([business()]).encode()):
            result = ApifyTool().execute("get_business_details", {"place_id": PLACE_ID}, configured_api())
        assert isinstance(result, ActionExecuted)
        review = result.result["business"]["reviews"][0]
        self.assertNotIn("author_name", review)
        self.assertNotIn("author_url", review)
        self.assertNotIn("author_photo_url", review)
        self.assertNotIn("review_url", review)

    def test_zero_detail_caps_return_no_reviews_or_images(self) -> None:
        with patch.object(apify, "request_bytes", return_value=json.dumps([business()]).encode()):
            result = ApifyTool().execute(
                "get_business_details",
                {"place_id": PLACE_ID, "max_reviews": "0", "max_images": "0"},
                configured_api(),
            )
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["business"]["reviews"], [])
        self.assertEqual(result.result["business"]["images"], [])

    def test_provider_urls_are_normalized_and_attacker_urls_are_dropped(self) -> None:
        raw = business()
        raw["website"] = "https://user@attacker.example:444/private"
        raw["images"] = [
            {"imageUrl": "https://evil.example/photo.jpg"},
            {"imageUrl": "http://lh5.googleusercontent.com/insecure.jpg"},
            {"imageUrl": "https://lh5.googleusercontent.com/safe.jpg", "authorUrl": "https://evil.example/a"},
        ]
        with patch.object(apify, "request_bytes", return_value=json.dumps([raw]).encode()):
            result = ApifyTool().execute("get_business_details", {"place_id": PLACE_ID}, configured_api())
        assert isinstance(result, ActionExecuted)
        detail = result.result["business"]
        self.assertEqual(detail["website"], "")
        self.assertEqual([item["url"] for item in detail["images"]], ["https://lh5.googleusercontent.com/safe.jpg"])
        self.assertEqual(detail["images"][0]["author_url"], "")
        self.assertNotIn("attacker.example", str(detail))

    def test_result_counts_and_text_are_bounded_even_if_provider_overreturns(self) -> None:
        raw = business()
        raw["description"] = "x" * 5_000
        raw["reviews"] = [{"text": f"review {index}", "stars": 5} for index in range(30)]
        raw["images"] = [{"imageUrl": f"https://lh5.googleusercontent.com/{index}.jpg"} for index in range(30)]
        with patch.object(apify, "request_bytes", return_value=json.dumps([raw]).encode()):
            result = ApifyTool().execute(
                "get_business_details",
                {"place_id": PLACE_ID, "max_reviews": "5", "max_images": "8"},
                configured_api(),
            )
        assert isinstance(result, ActionExecuted)
        detail = result.result["business"]
        self.assertLessEqual(len(detail["description"].encode()), 1_000)
        self.assertTrue(detail["description"].endswith("…"))
        self.assertEqual(len(detail["reviews"]), 5)
        self.assertEqual(len(detail["images"]), 8)
        self.assertLessEqual(
            len(json.dumps(result.result, ensure_ascii=False).encode()),
            apify.MAX_NORMALIZED_RESULT_BYTES + 1_000,
        )

    def test_adversarial_maximal_search_response_stays_below_host_limit(self) -> None:
        rows = []
        for index in range(apify.MAX_SEARCH_RESULTS):
            raw = business()
            raw["placeId"] = f"ChIJN1t_tDeuEmsRUsoyG83frY{index:02d}"
            raw["title"] = "\U0001f9c1" * 300
            raw["description"] = "\U0001f9c1" * 1_000
            raw["categories"] = ["\U0001f9c1" * 160 for _ in range(12)]
            raw["website"] = "https://example.com/" + "x" * 1_900
            rows.append(raw)
        with patch.object(apify, "request_bytes", return_value=json.dumps(rows).encode()):
            result = ApifyTool().execute(
                "search_businesses",
                {"query": "bakery", "location": "London", "limit": "20"},
                configured_api(),
            )
        assert isinstance(result, ActionExecuted)
        self.assertLessEqual(
            len(json.dumps(result.result, ensure_ascii=False).encode()),
            apify.MAX_NORMALIZED_RESULT_BYTES + 1_000,
        )

    def test_invalid_inputs_do_not_call_provider(self) -> None:
        tool = ApifyTool()
        cases = (
            ("search_businesses", {}),
            ("search_businesses", {"query": "bakery", "location": "London", "limit": "21"}),
            ("search_businesses", {"query": "bakery", "location": "London", "minimum_rating": "5"}),
            ("search_businesses", {"query": "bakery", "location": "London", "skip_closed": "yes"}),
            ("get_business_details", {"place_id": "../../actors"}),
            ("get_business_details", {"place_id": "customer_record_abc123"}),
            ("get_business_details", {"place_id": "ChIJN1t_tDeuEmsRUsoyG83frY"}),
            ("get_business_details", {"place_id": "XiIJN1t_tDeuEmsRUsoyG83frY4"}),
            ("get_business_details", {"place_id": PLACE_ID, "max_reviews": "all"}),
            ("get_business_details", {"place_id": PLACE_ID, "max_images": "9"}),
            ("get_business_details", {"place_id": PLACE_ID, "include_contacts": True}),
            ("get_business_details", {"place_id": PLACE_ID, "include_reviewer_attribution": True}),
        )
        with patch.object(apify, "request_bytes", side_effect=AssertionError("provider must not be called")):
            for action, tool_input in cases:
                with self.subTest(action=action, tool_input=tool_input):
                    self.assertIsInstance(tool.execute(action, tool_input, configured_api()), ActionFailed)

    def test_secret_shaped_search_is_blocked_by_host_parameter_guard(self) -> None:
        with patch.object(apify, "request_bytes", side_effect=AssertionError("provider must not be called")):
            result = ApifyTool().execute(
                "search_businesses",
                {"query": "api_key=abcdefghijklmnopqrstuvwxyz123456", "location": "London"},
                configured_api(),
            )
        self.assertIsInstance(result, ActionFailed)

    def test_actor_place_id_grammar_accepts_both_supported_prefixes(self) -> None:
        provider_data = json.dumps([business()]).encode()
        with patch.object(apify, "request_bytes", return_value=provider_data) as request:
            for place_id in (PLACE_ID, "GhIJN1t_tDeuEmsRUsoyG83frY4"):
                with self.subTest(place_id=place_id):
                    result = ApifyTool().execute(
                        "get_business_details",
                        {"place_id": place_id},
                        configured_api(),
                    )
                    self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(request.call_count, 2)

    def test_provider_errors_and_invalid_json_are_sanitized(self) -> None:
        with patch.object(apify, "request_bytes", side_effect=WebRequestError("raw", status=403, body=b"secret")):
            result = ApifyTool().execute("search_businesses", {"query": "bakery", "location": "London"}, configured_api())
        assert isinstance(result, ActionFailed)
        self.assertIn("rejected the configured API key", result.error)
        self.assertNotIn("secret", result.error)

        for response in (b"not-json", b'{"error":{"message":"private provider detail"}}'):
            with self.subTest(response=response), patch.object(apify, "request_bytes", return_value=response):
                result = ApifyTool().execute("search_businesses", {"query": "bakery", "location": "London"}, configured_api())
            assert isinstance(result, ActionFailed)
            self.assertNotIn("private provider detail", result.error)


if __name__ == "__main__":
    unittest.main()
