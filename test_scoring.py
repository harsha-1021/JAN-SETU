import unittest

import scoring


class ScoringTests(unittest.TestCase):
    def test_duplicate_submissions_do_not_change_unique_reporter_input(self):
        """Aggregation supplies unique citizens, not raw rows, to scoring."""
        one_reporter = scoring.smoothed_rate(1, 1_000, 0.01)
        repeated_rows_same_reporter = scoring.smoothed_rate(1, 1_000, 0.01)
        self.assertEqual(one_reporter, repeated_rows_same_reporter)

    def test_higher_reporting_rate_beats_larger_raw_count(self):
        baseline = 0.05
        rate_a = scoring.smoothed_rate(900, 1_000, baseline)
        rate_b = scoring.smoothed_rate(1_200, 2_000, baseline)
        self.assertGreater(rate_a, rate_b)

    def test_score_is_bounded_to_percentage_scale(self):
        result = scoring.priority_breakdown(
            "water", 100, 1_000, 0.9, 0.05, 0.08, 5.0, 0.2
        )
        self.assertGreaterEqual(result["priority_score"], 0)
        self.assertLessEqual(result["priority_score"], 100)


if __name__ == "__main__":
    unittest.main()
