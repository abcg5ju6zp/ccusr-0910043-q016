#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  tests/temporal.py
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are
#  met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following disclaimer
#    in the documentation and/or other materials provided with the
#    distribution.
#  * Neither the name of the project nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
#  "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
#  LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
#  A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
#  OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
#  SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
#  LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
#  DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
#  THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#

"""时态快照（temporal snapshot）回归测试。

覆盖：业务时刻重放、时区/日历版本确认、DST 重叠与缺失、跨日窗口、嵌套评估、
自定义时间函数，以及未使用时间的规则不承担快照成本。
"""

import datetime
import pickle
import threading
import unittest
from zoneinfo import ZoneInfo

import rule_engine.builtins as builtins
import rule_engine.engine as engine
import rule_engine.errors as errors
import rule_engine.temporal as temporal

UTC = datetime.timezone.utc
NYC = 'America/New_York'

HOLIDAYS_2022 = (
        # 一个周一，用来验证"工作日"会跳过节假日
        datetime.date(2022, 3, 14),
)


def _nyc(year, month, day, hour=0, minute=0, second=0):
    return datetime.datetime(year, month, day, hour, minute, second, tzinfo=ZoneInfo(NYC))


class BusinessCalendarTests(unittest.TestCase):
    def test_immutable(self):
        cal = temporal.BusinessCalendar(HOLIDAYS_2022, version='hol-2022.1')
        self.assertIsInstance(cal.holidays, frozenset)
        self.assertEqual(cal.version, 'hol-2022.1')
        with self.assertRaises(AttributeError):
            cal.version = 'other'  # type: ignore[misc]

    def test_content_fingerprint(self):
        cal1 = temporal.BusinessCalendar([datetime.date(2022, 3, 14)])
        cal2 = temporal.BusinessCalendar([datetime.date(2022, 3, 14)])
        cal3 = temporal.BusinessCalendar([datetime.date(2022, 3, 15)])
        self.assertEqual(cal1.version, cal2.version)
        self.assertNotEqual(cal1.version, cal3.version)
        self.assertEqual(temporal.BusinessCalendar().version, 'empty-v1')

    def test_invalid_inputs(self):
        with self.assertRaises(TypeError):
            temporal.BusinessCalendar([datetime.datetime(2022, 3, 14)])  # datetime 不是 date
        with self.assertRaises(ValueError):
            temporal.BusinessCalendar(weekends=(7,))

    def test_business_day_shifts(self):
        cal = temporal.BusinessCalendar(HOLIDAYS_2022)
        friday = datetime.date(2022, 3, 11)
        # 周五 +1 工作日 -> 跳过周末与周一节假日 -> 周二
        self.assertEqual(cal.shift_business_days(friday, 1), datetime.date(2022, 3, 15))
        self.assertEqual(cal.shift_business_days(friday, 2), datetime.date(2022, 3, 16))
        self.assertEqual(cal.shift_business_days(friday, -1), datetime.date(2022, 3, 10))
        # count=0 落在非工作日时回退到最近工作日
        self.assertEqual(cal.shift_business_days(datetime.date(2022, 3, 13), 0), friday)

    def test_custom_weekends(self):
        cal = temporal.BusinessCalendar((), weekends=(6,))  # 仅周日休息
        sunday = datetime.date(2022, 3, 13)
        self.assertFalse(cal.is_business_day(sunday))
        self.assertTrue(cal.is_business_day(datetime.date(2022, 3, 12)))  # 周六是工作日
        self.assertEqual(cal.shift_business_days(datetime.date(2022, 3, 12), 1), datetime.date(2022, 3, 14))


class SnapshotConstructionTests(unittest.TestCase):
    def test_requires_aware_moment(self):
        with self.assertRaises(ValueError):
            temporal.TemporalSnapshot(
                    moment=datetime.datetime(2022, 3, 13, 12, 0),
                    timezone=ZoneInfo(NYC),
            )

    def test_frozen_iso_string(self):
        snap = temporal.TemporalSnapshot.frozen('2022-03-13T12:00:00-04:00', NYC)
        self.assertEqual(snap.moment, _nyc(2022, 3, 13, 12))
        self.assertEqual(snap.today_date, datetime.date(2022, 3, 13))

    def test_naive_moment_localized_to_timezone(self):
        snap = temporal.TemporalSnapshot.create(datetime.datetime(2022, 3, 13, 12, 0), timezone=NYC)
        self.assertEqual(snap.moment.utcoffset(), datetime.timedelta(hours=-4))

    def test_versions_frozen(self):
        cal = temporal.BusinessCalendar(HOLIDAYS_2022, version='hol-2022.1')
        snap = temporal.TemporalSnapshot.frozen('2022-03-13T12:00:00-04:00', NYC, calendar=cal)
        self.assertIsNotNone(snap.versions['timezone'])
        self.assertEqual(snap.versions['calendar'], 'hol-2022.1')
        # 快照不可变（dataclass frozen）
        with self.assertRaises(AttributeError):
            snap.moment = datetime.datetime.now(tz=UTC)  # type: ignore[misc]

    def test_version_mismatch_timezone(self):
        with self.assertRaises(temporal.TemporalVersionMismatchError) as ctx:
            temporal.TemporalSnapshot.frozen(
                    '2022-03-13T12:00:00-04:00', NYC,
                    timezone_version='1900a', verify_versions=True)
        self.assertEqual(ctx.exception.expected, '1900a')

    def test_version_mismatch_calendar(self):
        cal = temporal.BusinessCalendar(HOLIDAYS_2022, version='v9')
        with self.assertRaises(temporal.TemporalVersionMismatchError):
            temporal.TemporalSnapshot.frozen(
                    '2022-03-13T12:00:00-04:00', NYC,
                    calendar=cal, calendar_version='v1', verify_versions=True)

    def test_version_match_passes(self):
        cal = temporal.BusinessCalendar(HOLIDAYS_2022, version='v1')
        snap = temporal.TemporalSnapshot.frozen(
                '2022-03-13T12:00:00-04:00', NYC,
                calendar=cal, calendar_version='v1',
                timezone_version=temporal.detect_timezone_version(), verify_versions=True)
        self.assertEqual(snap.versions['calendar'], 'v1')


class DSTSemanticsTests(unittest.TestCase):
    # 2022 年纽约：春令时开始 3/13 02:00 -> 03:00（gap），结束 11/6 02:00 -> 01:00（overlap）
    def test_gap_raises_by_default(self):
        snap = temporal.TemporalSnapshot.frozen('2022-03-13T12:00:00-04:00', NYC)
        with self.assertRaises(temporal.TemporalAmbiguityError):
            snap.localize(datetime.datetime(2022, 3, 13, 2, 30))

    def test_gap_policies(self):
        snap = temporal.TemporalSnapshot.frozen('2022-03-13T12:00:00-04:00', NYC)
        wall = datetime.datetime(2022, 3, 13, 2, 30)
        forward = snap.replace(gap_policy=temporal.DSTDisambiguation.FORWARD).localize(wall)
        self.assertEqual(forward.utcoffset(), datetime.timedelta(hours=-4))
        backward = snap.replace(gap_policy=temporal.DSTDisambiguation.BACKWARD).localize(wall)
        self.assertEqual(backward.utcoffset(), datetime.timedelta(hours=-5))

    def test_overlap_earlier_vs_later(self):
        snap = temporal.TemporalSnapshot.frozen('2022-11-06T12:00:00-05:00', NYC)
        wall = datetime.datetime(2022, 11, 6, 1, 30)
        earlier = snap.localize(wall)
        later = snap.replace(disambiguation=temporal.DSTDisambiguation.LATER).localize(wall)
        self.assertEqual(earlier.utcoffset(), datetime.timedelta(hours=-4))  # EDT 较早一次
        self.assertEqual(later.utcoffset(), datetime.timedelta(hours=-5))    # EST 较晚一次
        self.assertLess(earlier.astimezone(UTC), later.astimezone(UTC))
        with self.assertRaises(temporal.TemporalAmbiguityError):
            snap.replace(disambiguation=temporal.DSTDisambiguation.RAISE).localize(wall)

    def test_fixed_duration_is_physical_time(self):
        # 3/12 23:00 EST + 固定 24 小时 -> 3/14 00:00 EDT（钟点变了，物理时长 24h）
        base = _nyc(2022, 3, 12, 23)
        snap = temporal.TemporalSnapshot.frozen(base, NYC)
        result = snap.shift(datetime.timedelta(hours=24))
        self.assertEqual(result, _nyc(2022, 3, 14, 0))
        # 注意：CPython 对 tzinfo 相同的两个感知时刻相减按墙钟计算（此处得 25h）；
        # 物理时长必须在 UTC 上比较。
        self.assertEqual(
                result.astimezone(UTC) - base.astimezone(UTC),
                datetime.timedelta(hours=24))

    def test_calendar_days_keep_wall_clock(self):
        base = _nyc(2022, 3, 12, 23)
        snap = temporal.TemporalSnapshot.frozen(base, NYC)
        result = snap.add_calendar_days(1)
        self.assertEqual(result.replace(tzinfo=None), datetime.datetime(2022, 3, 13, 23))
        self.assertEqual(result.utcoffset(), datetime.timedelta(hours=-4))

    def test_calendar_months_clamp_short_month(self):
        snap = temporal.TemporalSnapshot.frozen('2022-01-31T10:00:00-05:00', NYC)
        self.assertEqual(snap.add_calendar_months(1).replace(tzinfo=None), datetime.datetime(2022, 2, 28, 10))

    def test_rule_arithmetic_gap_raises(self):
        cal_ctx = engine.Context(default_timezone=NYC)
        rule = engine.Rule('event + t"PT2H"', context=cal_ctx)
        event = _nyc(2022, 3, 13, 0, 30)  # +2h 落在 02:30 gap
        snap = temporal.TemporalSnapshot.frozen('2022-03-13T12:00:00-04:00', NYC)
        with self.assertRaises(temporal.TemporalAmbiguityError):
            rule.evaluate({'event': event}, snapshot=snap)
        forward = snap.replace(gap_policy=temporal.DSTDisambiguation.FORWARD)
        result = rule.evaluate({'event': event}, snapshot=forward)
        self.assertEqual(result.utcoffset(), datetime.timedelta(hours=-4))

    def test_rule_arithmetic_overlap_policy(self):
        ctx = engine.Context(default_timezone=NYC)
        rule = engine.Rule('event - t"PT1H"', context=ctx)
        event = _nyc(2022, 11, 6, 1, 30).replace(fold=0)  # 较早一次 EDT，-1h -> 00:30，唯一
        snap = temporal.TemporalSnapshot.frozen('2022-11-06T12:00:00-05:00', NYC)
        result = rule.evaluate({'event': event}, snapshot=snap)
        self.assertEqual(result.utcoffset(), datetime.timedelta(hours=-4))

    def test_midnight_gap_truncates_to_first_valid_instant(self):
        # 圣保罗 2012-10-21 午夜发生春令时跳变；该日截断端点应为当日第一个有效瞬间
        snap = temporal.TemporalSnapshot.frozen('2012-10-21T12:00:00-03:00', 'America/Sao_Paulo')
        today = snap.today
        # 墙上显示 00:00，fold=1 表示采用跳变后的偏移
        self.assertEqual(today.replace(tzinfo=None), datetime.datetime(2012, 10, 21, 0, 0))
        self.assertEqual(today.fold, 1)
        # 即使显式时刻策略为 raise，日期截断仍然安全
        snap_raise = snap.replace(gap_policy=temporal.DSTDisambiguation.RAISE)
        self.assertEqual(snap_raise.today, today)
        with self.assertRaises(temporal.TemporalAmbiguityError):
            snap_raise.localize(datetime.datetime(2012, 10, 21, 0, 0))


class RuleReplayTests(unittest.TestCase):
    def setUp(self):
        self.cal = temporal.BusinessCalendar(HOLIDAYS_2022, version='hol-2022.1')
        self.ctx = engine.Context(default_timezone=NYC, calendar=self.cal)

    def test_replay_is_deterministic(self):
        rule = engine.Rule('$now > d"2022-03-13T12:00:00-04:00"', context=self.ctx)
        moment = _nyc(2022, 3, 13, 12, 30)
        first = rule.matches({}, moment=moment)
        second = rule.matches({}, moment=moment)
        self.assertIs(first, True)
        self.assertEqual(first, second)
        self.assertFalse(rule.matches({}, moment=_nyc(2022, 3, 13, 11, 30)))

    def test_today_derived_from_moment_timezone(self):
        rule = engine.Rule('$today == d"2022-03-13T00:00:00-05:00"', context=self.ctx)
        self.assertTrue(rule.matches({}, moment=_nyc(2022, 3, 13, 12, 30)))
        # UTC 05:00（EDT 01:00）对应纽约已进入 3/14
        rule_utc = engine.Rule('$today == d"2022-03-14T00:00:00-04:00"', context=self.ctx)
        self.assertTrue(rule_utc.matches({}, moment=datetime.datetime(2022, 3, 14, 5, 0, tzinfo=UTC)))

    def test_now_constant_within_single_evaluation(self):
        rule = engine.Rule('$now - $now == t"PT"', context=self.ctx)
        self.assertTrue(rule.matches({}))

    def test_versions_available_after_decision(self):
        rule = engine.Rule('$now > d"2020-01-01T00:00:00Z"', context=self.ctx)
        rule.matches({}, moment=_nyc(2022, 3, 13, 12, 30))
        snap = rule.last_snapshot()
        self.assertIsNotNone(snap)
        self.assertEqual(snap.versions['calendar'], 'hol-2022.1')
        self.assertIsNotNone(snap.versions['timezone'])

    def test_snapshot_object_passed_directly(self):
        rule = engine.Rule('$now.year == 2019', context=self.ctx)
        snap = temporal.TemporalSnapshot.frozen('2019-06-01T00:00:00Z', NYC)
        self.assertTrue(rule.matches({}, snapshot=snap))

    def test_context_manager_shares_snapshot_across_rules(self):
        rule1 = engine.Rule('$now == d"2022-03-13T12:30:00-04:00"', context=self.ctx)
        rule2 = engine.Rule('$today == d"2022-03-13T00:00:00-05:00"', context=self.ctx)
        with self.ctx.temporal_snapshot(moment=_nyc(2022, 3, 13, 12, 30)) as snap:
            self.assertTrue(rule1.matches({}))
            self.assertTrue(rule2.matches({}))
            self.assertEqual(snap.versions['calendar'], 'hol-2022.1')

    def test_batch_replay_uses_recorded_business_moment(self):
        # 批量重放：每条输入携带自己的业务时刻，互不串扰、不受当前时钟影响
        rule = engine.Rule('expiry >= $now', context=self.ctx)
        cases = (
                (_nyc(2022, 3, 13, 12, 0), False),
                (_nyc(2022, 3, 13, 13, 0), True),
        )
        decision_moment = _nyc(2022, 3, 13, 12, 30)
        for expiry, expected in cases:
            self.assertIs(rule.matches({'expiry': expiry}, moment=decision_moment), expected)

    def test_parse_datetime_uses_snapshot_timezone(self):
        rule = engine.Rule('$parse_datetime("2022-03-13T12:00:00") == d"2022-03-13T12:00:00-04:00"', context=self.ctx)
        self.assertTrue(rule.matches({}, moment=_nyc(2022, 3, 13, 12, 30)))

    def test_no_time_rule_has_no_snapshot(self):
        rule = engine.Rule('1 < 2 and name == "Alice"', context=self.ctx)
        self.assertTrue(rule.matches({'name': 'Alice'}))
        # 没有引用 $now/$today/时态函数：不建立快照，不读取时钟
        self.assertIsNone(rule.last_snapshot())
        self.assertIsNone(self.ctx.current_temporal_snapshot())

    def test_default_auto_snapshot_still_reads_clock(self):
        # 向后兼容：不传 moment 时规则仍可运行（快照惰性建立一次）
        rule = engine.Rule('$now - t"PT1S" < d"2100-01-01T00:00:00Z"', context=self.ctx)
        self.assertTrue(rule.matches({}))
        self.assertIsNotNone(rule.last_snapshot())


class TemporalBuiltinsTests(unittest.TestCase):
    def setUp(self):
        self.cal = temporal.BusinessCalendar(HOLIDAYS_2022, version='hol-2022.1')
        self.ctx = engine.Context(default_timezone=NYC, calendar=self.cal)
        self.moment = _nyc(2022, 3, 13, 12, 30)

    def test_start_of_day(self):
        rule = engine.Rule(
                '$start_of_day(d"2022-03-13 15:23:00-04:00") == d"2022-03-13T00:00:00-05:00"',
                context=self.ctx)
        self.assertTrue(rule.matches({}, moment=self.moment))

    def test_add_days(self):
        rule = engine.Rule('$add_days(d"2022-03-12 23:00:00-05:00", 1) == d"2022-03-13 23:00:00-04:00"', context=self.ctx)
        self.assertTrue(rule.matches({}, moment=self.moment))

    def test_add_months(self):
        rule = engine.Rule('$add_months(d"2022-01-31 10:00:00-05:00", 1) == d"2022-02-28 10:00:00-05:00"', context=self.ctx)
        self.assertTrue(rule.matches({}, moment=self.moment))

    def test_add_business_days_skips_weekend_and_holiday(self):
        rule = engine.Rule(
                '$add_business_days(d"2022-03-11 10:00:00-05:00", 1) == d"2022-03-15 10:00:00-04:00"',
                context=self.ctx)
        self.assertTrue(rule.matches({}, moment=self.moment))

    def test_is_holiday_with_argument(self):
        rule = engine.Rule('$is_holiday(d"2022-03-14 10:00:00-04:00")', context=self.ctx)
        self.assertTrue(rule.matches({}, moment=self.moment))
        rule = engine.Rule('$is_holiday(d"2022-03-15 10:00:00-04:00")', context=self.ctx)
        self.assertFalse(rule.matches({}, moment=self.moment))

    def test_is_holiday_defaults_to_business_moment(self):
        rule = engine.Rule('$is_holiday()', context=self.ctx)
        # 业务时刻本身落在节假日 3/14
        self.assertTrue(rule.matches({}, moment=_nyc(2022, 3, 14, 9)))
        self.assertFalse(rule.matches({}, moment=_nyc(2022, 3, 15, 9)))

    def test_is_business_day(self):
        rule = engine.Rule('$is_business_day(d"2022-03-12 10:00:00-05:00")', context=self.ctx)  # 周六
        self.assertFalse(rule.matches({}, moment=self.moment))

    def test_temporal_functions_without_calendar_or_snapshot(self):
        ctx = engine.Context(default_timezone=NYC)  # 无日历
        rule = engine.Rule('$is_holiday(d"2022-03-14 10:00:00-04:00")', context=ctx)
        with self.assertRaises(errors.FunctionCallError):
            rule.matches({}, moment=self.moment)

    def test_snapshot_required_for_temporal_functions_in_bare_builtins(self):
        blts = builtins.Builtins.from_defaults()
        with self.assertRaises(errors.FunctionCallError):
            blts['is_holiday'](datetime.datetime(2022, 3, 14))


class WindowAndGraceTests(unittest.TestCase):
    def setUp(self):
        self.cal = temporal.BusinessCalendar(HOLIDAYS_2022, version='hol-2022.1')

    def test_lookback_window_half_open(self):
        snap = temporal.TemporalSnapshot.frozen(_nyc(2022, 3, 13, 12, 30), NYC, calendar=self.cal)
        start, end = snap.window(datetime.timedelta(days=30))
        self.assertEqual(end, snap.moment)
        # 物理时长在 UTC 上比较（端点时区相同但跨 DST，墙上差为 30 天 +1 小时）
        self.assertEqual(end.astimezone(UTC) - start.astimezone(UTC), datetime.timedelta(days=30))
        self.assertLess(start, end)
        # 半开区间：右端点不含
        self.assertTrue(start <= _nyc(2022, 3, 13, 12, 29) < end)
        self.assertFalse(start <= end < end)

    def test_cross_day_window(self):
        # 从当日 00:00 起 2 个本地日历日；春令时凌晨不影响（纽约 gap 在 2-3 点）
        snap = temporal.TemporalSnapshot.frozen(_nyc(2022, 3, 13, 12, 30), NYC, calendar=self.cal)
        start, end = snap.window(datetime.timedelta(), calendar_days=2)
        self.assertEqual(start, _nyc(2022, 3, 13, 0))
        self.assertEqual(end, _nyc(2022, 3, 15, 0))

    def test_cross_day_window_over_midnight_gap_timezone(self):
        # 圣保罗跨日窗口端点落在午夜 gap 时取当日第一个有效瞬间
        snap = temporal.TemporalSnapshot.frozen(
                datetime.datetime(2012, 10, 21, 12, tzinfo=ZoneInfo('America/Sao_Paulo')),
                'America/Sao_Paulo', calendar=self.cal)
        start, end = snap.window(datetime.timedelta(), calendar_days=1)
        self.assertEqual(start.replace(tzinfo=None), datetime.datetime(2012, 10, 21, 0, 0))
        self.assertEqual(end.replace(tzinfo=None), datetime.datetime(2012, 10, 22, 0, 0))

    def test_grace_period_fixed_duration(self):
        snap = temporal.TemporalSnapshot.frozen(_nyc(2022, 3, 15, 10), NYC, calendar=self.cal)
        deadline = _nyc(2022, 3, 11, 10)
        self.assertFalse(snap.within_grace_period(deadline, datetime.timedelta(days=3)))  # 14 日 10 点截止
        self.assertTrue(snap.within_grace_period(deadline, datetime.timedelta(days=4)))   # 15 日 10 点恰好截止
        # 端点含等号
        snap2 = temporal.TemporalSnapshot.frozen(_nyc(2022, 3, 15, 10), NYC, calendar=self.cal)
        self.assertTrue(snap2.within_grace_period(deadline, datetime.timedelta(days=4, hours=0)))

    def test_grace_period_business_days(self):
        # 截止日周五 3/11；+2 个工作日跳过周末与周一节假日 -> 3/16
        snap = temporal.TemporalSnapshot.frozen(_nyc(2022, 3, 15, 10), NYC, calendar=self.cal)
        deadline = _nyc(2022, 3, 11, 10)
        self.assertTrue(snap.within_grace_period(deadline, datetime.timedelta(days=2), business_days=True))
        snap_late = temporal.TemporalSnapshot.frozen(_nyc(2022, 3, 16, 10, 0, 1), NYC, calendar=self.cal)
        self.assertFalse(snap_late.within_grace_period(deadline, datetime.timedelta(days=2), business_days=True))


class NestedEvaluationTests(unittest.TestCase):
    def test_nested_evaluation_inherits_snapshot(self):
        ctx = engine.Context(default_timezone=NYC)
        inner_rule = engine.Rule('$now == d"2022-03-13T12:30:00-04:00"', context=ctx)
        seen = []

        def resolver(thing, name):
            if name == 'check':
                seen.append(inner_rule.matches({}))
                return True
            return thing[name]
        ctx._Context__resolver = resolver
        outer = engine.Rule('check', context=ctx)
        with ctx.temporal_snapshot(moment=_nyc(2022, 3, 13, 12, 30)):
            self.assertTrue(outer.evaluate({}))
        self.assertEqual(seen, [True])

    def test_inner_snapshot_override_is_restored(self):
        ctx = engine.Context(default_timezone=NYC)
        rule = engine.Rule('$now.year', context=ctx)
        snap_2020 = temporal.TemporalSnapshot.frozen('2020-06-01T00:00:00-04:00', NYC)
        snap_2022 = temporal.TemporalSnapshot.frozen('2022-03-13T12:30:00-04:00', NYC)

        def resolver(thing, name):
            if name == 'check':
                return rule.evaluate({}, snapshot=snap_2020)
            return thing[name]
        ctx._Context__resolver = resolver
        outer = engine.Rule('check', context=ctx)
        with ctx.temporal_snapshot(snapshot=snap_2022):
            self.assertEqual(outer.evaluate({}), 2020)   # 内层覆盖
            self.assertEqual(rule.evaluate({}), 2022)    # 外层已恢复

    def test_nested_snapshot_stacks_restore_in_order(self):
        ctx = engine.Context(default_timezone=NYC)
        rule = engine.Rule('$now.year', context=ctx)
        with ctx.temporal_snapshot(moment=_nyc(2022, 1, 1)):
            self.assertEqual(rule.evaluate({}), 2022)
            with ctx.temporal_snapshot(moment=_nyc(2021, 1, 1)):
                self.assertEqual(rule.evaluate({}), 2021)
            self.assertEqual(rule.evaluate({}), 2022)


class CustomTemporalFunctionTests(unittest.TestCase):
    def test_custom_function_reads_snapshot(self):
        def decision_year_gen(blts):
            snap = blts.get_snapshot()
            return lambda: (snap.now.year if snap is not None else None)

        ctx = engine.Context(default_timezone=NYC)
        ctx.builtins = builtins.Builtins.from_defaults(
                {'decision_year': builtins.BuiltinValueGenerator(decision_year_gen)},
                value_types={'decision_year': __import__('rule_engine').DataType.FUNCTION(
                        'decision_year', return_type=__import__('rule_engine').DataType.FLOAT, minimum_arguments=0)},
                timezone=ctx.default_timezone,
                snapshot_provider=ctx._current_snapshot,
        )
        rule = engine.Rule('$decision_year() == 2019', context=ctx)
        self.assertTrue(rule.matches({}, moment=datetime.datetime(2019, 6, 1, tzinfo=UTC)))
        self.assertFalse(rule.matches({}, moment=datetime.datetime(2020, 6, 1, tzinfo=UTC)))


class SnapshotThreadTests(unittest.TestCase):
    def test_concurrent_evaluations_use_isolated_snapshots(self):
        import decimal
        ctx = engine.Context(default_timezone=NYC)
        rule = engine.Rule('$now.year', context=ctx)
        results: dict[str, decimal.Decimal] = {}

        def run(label, moment):
            results[label] = rule.evaluate({}, moment=moment)

        t1 = threading.Thread(target=run, args=('a', _nyc(2019, 1, 1, 12)))
        t2 = threading.Thread(target=run, args=('b', _nyc(2024, 1, 1, 12)))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(results, {'a': decimal.Decimal(2019), 'b': decimal.Decimal(2024)})
        # 线程结束后主线程没有残留帧
        self.assertIsNone(ctx.current_temporal_snapshot())


class ContextSerializationWithCalendarTests(unittest.TestCase):
    def test_pickle_preserves_calendar_and_policies(self):
        cal = temporal.BusinessCalendar(HOLIDAYS_2022, version='hol-2022.1')
        ctx = engine.Context(
                default_timezone=NYC,
                calendar=cal,
                disambiguation=temporal.DSTDisambiguation.LATER,
                gap_policy=temporal.DSTDisambiguation.FORWARD,
        )
        restored = pickle.loads(pickle.dumps(ctx))
        self.assertEqual(restored.calendar, cal)
        self.assertEqual(restored.disambiguation, temporal.DSTDisambiguation.LATER)
        self.assertEqual(restored.gap_policy, temporal.DSTDisambiguation.FORWARD)
        rule = engine.Rule('$is_holiday(d"2022-03-14 10:00:00-04:00")', context=restored)
        self.assertTrue(rule.matches({}, moment=_nyc(2022, 3, 13, 12)))
