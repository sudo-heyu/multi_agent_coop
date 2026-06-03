from datetime import datetime, timezone, timedelta
import unittest

from state_server.academic_plot import history_origin, latest_elapsed_seconds, sliding_series


class AcademicPlotWindowTests(unittest.TestCase):
    def test_sliding_window_keeps_latest_25_seconds(self):
        base = datetime(2026, 6, 3, tzinfo=timezone.utc)
        rows = [
            {"t": (base + timedelta(seconds=s)).isoformat(), "cwmin": s}
            for s in (0, 10, 20, 30, 40)
        ]
        history = {"ap1": rows, "ap2": [], "ap3": []}
        origin = history_origin(history)
        latest = latest_elapsed_seconds(history, origin)

        xs, ys = sliding_series(rows, "cwmin", origin, latest, 25.0)

        self.assertEqual(xs, [5.0, 15.0, 25.0])
        self.assertEqual(ys, [20.0, 30.0, 40.0])

    def test_initial_window_fills_from_left_before_25_seconds(self):
        base = datetime(2026, 6, 3, tzinfo=timezone.utc)
        rows = [
            {"t": (base + timedelta(seconds=s)).isoformat(), "cwmin": s}
            for s in (0, 8, 16)
        ]
        history = {"ap1": rows, "ap2": [], "ap3": []}
        origin = history_origin(history)
        latest = latest_elapsed_seconds(history, origin)

        xs, ys = sliding_series(rows, "cwmin", origin, latest, 25.0)

        self.assertEqual(xs, [0.0, 8.0, 16.0])
        self.assertEqual(ys, [0.0, 8.0, 16.0])


if __name__ == "__main__":
    unittest.main()
