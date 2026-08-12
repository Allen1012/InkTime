"""展示页生效时间段解析与判定的单元测试。

按「先测后写」执行：星期范围与列表、跨零点后半段归属次日、合并只在同一星期内
进行，这三处是本功能最容易出错的地方。
"""

from __future__ import annotations

import unittest
from datetime import datetime

from src.configuration import is_within_windows, parse_time_windows


def _minutes(text: str) -> int:
    """把 HH:MM 转换为当日分钟数，便于书写期望值。"""
    hour, minute = text.split(":")
    return int(hour) * 60 + int(minute)


def _day(windows, weekday: int) -> tuple[tuple[int, int], ...]:
    """取出指定星期的区间列表，周一为 0。"""
    return windows[weekday]


class ParseTimeWindowsTestCase(unittest.TestCase):
    """校验时间段字符串的解析、归一化与非法输入拒绝。"""

    def test_empty_value_means_always_active(self) -> None:
        """验证空配置解析为空结构，并被判定为全天生效。"""
        for raw in ("", "   ", ";", ";;"):
            with self.subTest(raw=raw):
                windows = parse_time_windows(raw)
                self.assertEqual((), tuple(item for day in windows for item in day))
                self.assertTrue(
                    is_within_windows(datetime(2026, 8, 12, 3, 0), windows)
                )

    def test_single_window_applies_to_every_weekday(self) -> None:
        """验证不带星期前缀的时间段对每一天生效。"""
        windows = parse_time_windows("07:00-09:00")
        for weekday in range(7):
            self.assertEqual(
                ((_minutes("07:00"), _minutes("09:00")),), _day(windows, weekday)
            )

    def test_multiple_windows_are_sorted_and_kept_per_weekday(self) -> None:
        """验证多段配置按开始时间排序后保存。"""
        windows = parse_time_windows("18:00-23:00;07:00-09:00")
        self.assertEqual(
            (
                (_minutes("07:00"), _minutes("09:00")),
                (_minutes("18:00"), _minutes("23:00")),
            ),
            _day(windows, 0),
        )

    def test_weekday_range_and_list_prefixes(self) -> None:
        """验证星期范围、星期列表与两者混用的前缀写法。"""
        windows = parse_time_windows(
            "Mon-Fri@07:00-09:00;Sat,Sun@09:00-23:00"
        )
        for weekday in range(0, 5):
            self.assertEqual(
                ((_minutes("07:00"), _minutes("09:00")),), _day(windows, weekday)
            )
        for weekday in (5, 6):
            self.assertEqual(
                ((_minutes("09:00"), _minutes("23:00")),), _day(windows, weekday)
            )

        mixed = parse_time_windows("Mon-Wed,Fri@20:00-21:00")
        self.assertEqual(
            [0, 1, 2, 4],
            [index for index, day in enumerate(mixed) if day],
        )

    def test_weekday_names_are_case_insensitive_and_accept_iso_numbers(self) -> None:
        """验证星期名大小写不敏感，并接受 1 到 7 的 ISO 数字写法。"""
        expected = parse_time_windows("Mon,Sun@08:00-09:00")
        self.assertEqual(expected, parse_time_windows("mon,SUN@08:00-09:00"))
        self.assertEqual(expected, parse_time_windows("1,7@08:00-09:00"))

    def test_cross_midnight_window_spills_into_next_weekday(self) -> None:
        """验证跨零点区间拆分后，后半段归属次日星期。"""
        windows = parse_time_windows("Fri@22:00-01:30")

        self.assertEqual(
            ((_minutes("22:00"), _minutes("24:00")),), _day(windows, 4)
        )
        self.assertEqual(
            ((_minutes("00:00"), _minutes("01:30")),), _day(windows, 5)
        )
        self.assertEqual((), _day(windows, 3))

    def test_cross_midnight_on_sunday_wraps_to_monday(self) -> None:
        """验证周日跨零点的后半段落到周一，而不是越界。"""
        windows = parse_time_windows("Sun@23:00-00:30")

        self.assertEqual(
            ((_minutes("23:00"), _minutes("24:00")),), _day(windows, 6)
        )
        self.assertEqual(
            ((_minutes("00:00"), _minutes("00:30")),), _day(windows, 0)
        )

    def test_overlapping_and_adjacent_windows_are_merged(self) -> None:
        """验证重叠与首尾相接的区间被合并为一段。"""
        self.assertEqual(
            ((_minutes("08:00"), _minutes("12:00")),),
            _day(parse_time_windows("08:00-10:00;09:00-12:00"), 0),
        )
        self.assertEqual(
            ((_minutes("08:00"), _minutes("12:00")),),
            _day(parse_time_windows("08:00-10:00;10:00-12:00"), 0),
        )

    def test_merge_does_not_cross_weekdays(self) -> None:
        """验证合并只在同一星期内进行，不会把不同星期的区间并起来。"""
        windows = parse_time_windows("Mon@08:00-10:00;Tue@10:00-12:00")

        self.assertEqual(
            ((_minutes("08:00"), _minutes("10:00")),), _day(windows, 0)
        )
        self.assertEqual(
            ((_minutes("10:00"), _minutes("12:00")),), _day(windows, 1)
        )

    def test_full_day_window_is_accepted(self) -> None:
        """验证 00:00-24:00 表示整天生效。"""
        windows = parse_time_windows("00:00-24:00")
        self.assertEqual(((0, 24 * 60),), _day(windows, 0))
        self.assertTrue(is_within_windows(datetime(2026, 8, 12, 3, 0), windows))
        self.assertTrue(is_within_windows(datetime(2026, 8, 12, 23, 59), windows))

    def test_invalid_values_are_rejected(self) -> None:
        """验证各类非法写法整批拒绝并给出可读原因。"""
        cases = {
            "25:00-26:00": "小时",
            "08:70-09:00": "分钟",
            "08:00": "时间段",
            "09:00-09:00": "零长度",
            "Funday@08:00-09:00": "星期",
            "8@08:00-09:00": "星期",
            "Mon@": "时间段",
            "08:00-09:00-10:00": "时间段",
        }
        for raw, keyword in cases.items():
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as captured:
                    parse_time_windows(raw)
                self.assertIn(keyword, str(captured.exception))


class IsWithinWindowsTestCase(unittest.TestCase):
    """校验按星期与分钟数的生效判定，边界为左闭右开。"""

    def setUp(self) -> None:
        """准备工作日早晚两段、周末白天一段的典型配置。"""
        self.windows = parse_time_windows(
            "Mon-Fri@07:00-09:00;Mon-Fri@18:00-23:00;Sat,Sun@09:00-23:00"
        )

    def test_weekday_windows(self) -> None:
        """验证工作日在段内生效、段外休息。"""
        # 2026-08-12 是周三
        self.assertTrue(is_within_windows(datetime(2026, 8, 12, 7, 0), self.windows))
        self.assertTrue(is_within_windows(datetime(2026, 8, 12, 8, 59), self.windows))
        self.assertFalse(is_within_windows(datetime(2026, 8, 12, 9, 0), self.windows))
        self.assertFalse(is_within_windows(datetime(2026, 8, 12, 17, 59), self.windows))
        self.assertTrue(is_within_windows(datetime(2026, 8, 12, 22, 59), self.windows))
        self.assertFalse(is_within_windows(datetime(2026, 8, 12, 23, 0), self.windows))
        self.assertFalse(is_within_windows(datetime(2026, 8, 12, 3, 0), self.windows))

    def test_weekend_windows_differ_from_weekdays(self) -> None:
        """验证周末使用自己的时间段。"""
        # 2026-08-15 是周六
        self.assertTrue(is_within_windows(datetime(2026, 8, 15, 9, 0), self.windows))
        self.assertFalse(is_within_windows(datetime(2026, 8, 15, 8, 59), self.windows))
        self.assertFalse(is_within_windows(datetime(2026, 8, 15, 7, 30), self.windows))

    def test_cross_midnight_spans_two_weekdays(self) -> None:
        """验证跨零点配置在两个星期上都能正确判定。"""
        windows = parse_time_windows("Fri@22:00-01:30")
        # 2026-08-14 是周五
        self.assertTrue(is_within_windows(datetime(2026, 8, 14, 23, 30), windows))
        self.assertTrue(is_within_windows(datetime(2026, 8, 15, 0, 30), windows))
        self.assertFalse(is_within_windows(datetime(2026, 8, 15, 2, 0), windows))
        self.assertFalse(is_within_windows(datetime(2026, 8, 14, 21, 59), windows))

    def test_next_resume_time_is_the_next_window_start(self) -> None:
        """验证休息期能算出下一个生效时间段的开始时刻。"""
        from src.configuration import next_window_start

        # 周三 09:00 之后，下一段是同日 18:00
        self.assertEqual(
            datetime(2026, 8, 12, 18, 0),
            next_window_start(datetime(2026, 8, 12, 9, 0), self.windows),
        )
        # 周三 23:30 之后，下一段是周四 07:00
        self.assertEqual(
            datetime(2026, 8, 13, 7, 0),
            next_window_start(datetime(2026, 8, 12, 23, 30), self.windows),
        )
        # 周五 23:30 之后，下一段是周六 09:00
        self.assertEqual(
            datetime(2026, 8, 15, 9, 0),
            next_window_start(datetime(2026, 8, 14, 23, 30), self.windows),
        )
        # 全天生效或未配置时没有「下一个开始时刻」
        self.assertIsNone(
            next_window_start(datetime(2026, 8, 12, 9, 0), parse_time_windows(""))
        )
