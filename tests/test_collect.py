import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector"))
import collect


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)

    def test_slug_extracts_only_first_path_segment(self):
        self.assertEqual(collect.slug_from_url("https://jobs.ashbyhq.com/acme/abc"), "acme")
        self.assertIsNone(collect.slug_from_url("https://jobs.ashbyhq.com/api/v1"))

    def test_board_validation_uses_provider_get_payload(self):
        with patch.object(collect, "request_json", return_value={"jobs": []}) as request:
            self.assertTrue(collect.validate_board("ashby", "sampleco"))
            request.assert_called_once_with(collect.board_api_url("ashby", "sampleco"), timeout=18, retries=2)

    def test_board_validation_rejects_unexpected_payload(self):
        with patch.object(collect, "request_json", return_value={"unexpected": []}):
            self.assertFalse(collect.validate_board("greenhouse", "sampleco"))

    def test_greenhouse_normalizes_title_location_and_date(self):
        row = collect.normalize_job("greenhouse", "sampleco", {
            "id": 123, "title": "Data Analyst", "location": {"name": "Remote - United States"},
            "first_published": (self.now - timedelta(days=4)).isoformat(),
            "absolute_url": "https://boards.greenhouse.io/sampleco/jobs/123"
        }, self.now)
        self.assertEqual(row["title"], "Data Analyst")
        self.assertEqual(row["remote_status"], "remote")
        self.assertEqual(row["company"], "Sampleco")

    def test_lever_epoch_milliseconds_are_normalized(self):
        timestamp = int((self.now - timedelta(days=2)).timestamp() * 1000)
        row = collect.normalize_job("lever", "sampleco", {
            "id": "abc", "text": "Support Specialist", "createdAt": timestamp,
            "hostedUrl": "https://jobs.lever.co/sampleco/abc",
            "categories": {"location": "Lagos, Nigeria", "commitment": "Full-time"},
            "workplaceType": "hybrid"
        }, self.now)
        self.assertEqual(row["remote_status"], "hybrid")
        self.assertTrue(row["published_at"].endswith("Z"))

    def test_old_jobs_outside_30_days_are_excluded(self):
        row = collect.normalize_job("ashby", "sampleco", {
            "id": "abc", "title": "Engineer", "publishedAt": (self.now - timedelta(days=31)).isoformat(),
            "jobUrl": "https://jobs.ashbyhq.com/sampleco/abc", "isListed": True
        }, self.now)
        self.assertIsNone(row)

    def test_jobs_without_a_published_date_are_not_mislabelled_recent(self):
        row = collect.normalize_job("ashby", "sampleco", {
            "id": "abc", "title": "Engineer", "jobUrl": "https://jobs.ashbyhq.com/sampleco/abc", "isListed": True
        }, self.now)
        self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
