import os
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import main
from google_cloud_services import GoogleCloudServices


class CloudReadyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = main.DB_PATH
        main.DB_PATH = os.path.join(self.temp_dir.name, "complaints.db")
        main.init_db()
        self.client = TestClient(main.app)

    def tearDown(self):
        main.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_public_config_never_exposes_server_maps_key(self):
        payload = self.client.get("/config/public").json()
        self.assertIn("firebase", payload)
        self.assertIn("google_services", payload)
        self.assertNotIn("google_maps_server_api_key", payload)

    def test_policymaker_priority_api_requires_authentication(self):
        response = self.client.get("/regions/priority")
        self.assertEqual(response.status_code, 401)

    def test_text_intake_tracks_and_aggregates_with_local_fallback(self):
        with mock.patch.object(main.firebase, "allocate_id", return_value=None), \
             mock.patch.object(main.firebase, "save_complaint"), \
             mock.patch.object(main.bigquery_analytics, "insert_complaint", return_value=False), \
             mock.patch.object(main.bigquery_analytics, "get_region_contexts", return_value={}):
            response = self.client.post(
                "/complaints/text",
                data={
                    "text": "There is no drinking water near the school for three days",
                    "citizen_id": "test-citizen",
                    "latitude": "13.08",
                    "longitude": "80.27",
                    "region": "Chennai",
                },
            )
            self.assertEqual(response.status_code, 200)
            complaint = response.json()
            self.assertEqual(complaint["category"], "water")
            self.assertTrue(complaint["ai_summary"])

            tracking = self.client.get(
                "/complaints/track/{}".format(complaint["tracking_code"])
            )
            self.assertEqual(tracking.status_code, 200)
            self.assertEqual(tracking.json()["status"], "submitted")

            priorities = main.get_region_priorities("test-policymaker")
            self.assertEqual(len(priorities), 1)
            self.assertEqual(priorities[0].category, "water")

    def test_gemini_adapter_has_a_safe_deterministic_fallback(self):
        with mock.patch.dict(
            os.environ,
            {"GOOGLE_CLOUD_PROJECT": "", "GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""},
            clear=False,
        ):
            services = GoogleCloudServices()
            result = services.analyze_complaint("The electricity has been out for days")
        self.assertEqual(result.category, "power")
        self.assertEqual(result.provider, "local-fallback")
        self.assertGreater(result.severity_score, 0.5)

    def test_ai_studio_key_takes_precedence_for_local_development(self):
        with mock.patch.dict(
            os.environ,
            {"GOOGLE_CLOUD_PROJECT": "demo-project", "GEMINI_API_KEY": "test-key"},
            clear=False,
        ), mock.patch("google.genai.Client") as client:
            services = GoogleCloudServices()
            services._get_gemini_client()

        client.assert_called_once_with(api_key="test-key")


if __name__ == "__main__":
    unittest.main()
