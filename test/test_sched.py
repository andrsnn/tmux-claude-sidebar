#!/usr/bin/env python3
"""Unit tests for the schedule parsing in sidebar/sched.py.

    python3 test/test_sched.py
"""
import importlib.util
import os
import time
import unittest
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("sched", os.path.join(HERE, "..", "sidebar", "sched.py"))
sched = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sched)


def ts(*a):
    return int(datetime(*a).timestamp())


class Schedules(unittest.TestCase):
    def nexts(self, when, start, n=3):
        s, out, t = sched.Schedule(when), [], start
        for _ in range(n):
            t = s.next(t)
            out.append(datetime.fromtimestamp(t).strftime("%a %H:%M"))
        return out

    # 2026-11-25 is a Wednesday.
    WED_8AM = ts(2026, 11, 25, 8, 0)

    def test_weekdays(self):
        for w in ("weekdays 5:30pm", "every weekday at 5:30pm", "weekdays 17:30", "mon-fri 17:30"):
            self.assertEqual(self.nexts(w, ts(2026, 11, 26, 18, 0)), ["Fri 17:30", "Mon 17:30", "Tue 17:30"], w)
        self.assertEqual(sched.Schedule("weekdays 5:30pm").desc, "weekdays 17:30")

    def test_intervals(self):
        self.assertEqual(self.nexts("every 2h 9am-6pm", self.WED_8AM, 6),
                         ["Wed 09:00", "Wed 11:00", "Wed 13:00", "Wed 15:00", "Wed 17:00", "Thu 09:00"])
        self.assertEqual(self.nexts("every 30m", self.WED_8AM, 2), ["Wed 08:30", "Wed 09:00"])
        self.assertEqual(self.nexts("hourly", self.WED_8AM, 2), ["Wed 09:00", "Wed 10:00"])
        self.assertEqual(self.nexts("every 15 minutes from 9 to 5pm", self.WED_8AM, 2), ["Wed 09:00", "Wed 09:15"])
        self.assertEqual(sched.Schedule("every 2h 9am-6pm weekdays").desc, "every 2h 9:00-18:00 weekdays")

    def test_days_and_times(self):
        self.assertEqual(self.nexts("mon,thu 9:00", self.WED_8AM), ["Thu 09:00", "Mon 09:00", "Thu 09:00"])
        self.assertEqual(self.nexts("monday and thursday 9am", self.WED_8AM, 1), ["Thu 09:00"])
        self.assertEqual(self.nexts("fri 9am,17:00", self.WED_8AM, 2), ["Fri 09:00", "Fri 17:00"])
        self.assertEqual(self.nexts("sat-mon 10", self.WED_8AM), ["Sat 10:00", "Sun 10:00", "Mon 10:00"])
        self.assertEqual(self.nexts("noon", self.WED_8AM, 1), ["Wed 12:00"])
        self.assertEqual(self.nexts("daily 9:30", self.WED_8AM, 1), ["Wed 09:30"])
        self.assertEqual(self.nexts("weekends 10am", self.WED_8AM, 1), ["Sat 10:00"])

    def test_cron(self):
        self.assertEqual(self.nexts("30 17 * * 1-5", ts(2026, 11, 26, 18, 0)), ["Fri 17:30", "Mon 17:30", "Tue 17:30"])
        self.assertEqual(self.nexts("*/20 9 * * *", self.WED_8AM, 3), ["Wed 09:00", "Wed 09:20", "Wed 09:40"])
        # Sparse: once a year, found quickly.
        t0 = time.time()
        nxt = sched.Schedule("0 9 1 1 *").next(self.WED_8AM)
        self.assertLess(time.time() - t0, 1.0)
        self.assertEqual(datetime.fromtimestamp(nxt).strftime("%Y-%m-%d %H:%M"), "2027-01-01 09:00")
        # Both day fields restricted: either matches (cron rule). Dec 1 2026 is a Tuesday.
        self.assertEqual(self.nexts("0 9 1 * 5", self.WED_8AM, 2), ["Fri 09:00", "Tue 09:00"])

    def test_prev(self):
        s = sched.Schedule("weekdays 17:30")
        self.assertEqual(s.prev(ts(2026, 11, 28, 12, 0)), ts(2026, 11, 27, 17, 30))  # Sat -> Fri
        self.assertEqual(s.prev(ts(2026, 11, 27, 17, 30)), ts(2026, 11, 27, 17, 30))
        self.assertIsNone(sched.Schedule("0 9 1 1 *").prev(self.WED_8AM))           # > 8 days back

    def test_rejects(self):
        for bad in ("", "blah", "every 1m", "* * * * *", "25:00", "every 2h 6pm-9am", "13pm", "60 9 * * *"):
            with self.assertRaises(ValueError, msg=bad):
                sched.Schedule(bad)

    def test_labels(self):
        now = ts(2026, 11, 25, 16, 50)
        self.assertEqual(sched.when_label(now + 600, now), "in 10m")
        self.assertEqual(sched.when_label(ts(2026, 11, 25, 18, 30), now), "18:30")
        self.assertEqual(sched.when_label(ts(2026, 11, 30, 9, 0), now), "Mon 9:00")
        self.assertEqual(sched.when_label(ts(2026, 12, 24, 9, 0), now), "Dec 24")


if __name__ == "__main__":
    unittest.main(verbosity=2)
