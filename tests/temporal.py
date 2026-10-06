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

"""时态快照（TemporalSnapshot）回归测试。"""

import datetime
import json
import pickle
import threading
import unittest

import rule_engine.builtins as builtins
import rule_engine.engine as engine
import rule_engine.errors as errors
from rule_engine import Calendar, Context, DstResolutionPolicy, Rule, TemporalSnapshot, temporal_function

import dateutil.tz

UTC = datetime.timezone.utc
NEW_YORK = 'America/New_York'
HAS_NEW_YORK = dateutil.tz.gettz(NEW_YORK) is not None


def utc(year, *parts):
    return datetime.datetime(year, *parts, tzinfo=UTC)


class TemporalSnapshotTests(unittest.TestCase):
    def test_business_instant_in_business_timezone(self):
        # 12:00Z on 2024-11-03 is 07:00 in New York (EST, UTC-5)
        snapshot = TemporalSnapshot(utc(2024, 11, 3, 12), timezone=NEW_YORK)
        self.assertEqual(snapshot.now.utcoffset(), datetime.timedelta(hours=-5))
        self.assertEqual((snapshot.now.hour, snapshot.now.day), (7, 3))
        # the absolute instant is unchanged
        self.assertEqual(snapshot.now.astimezone(UTC), utc(2024, 11, 3, 12))
        self.assertEqual(snapshot.timezone_name, NEW_YORK)

    def test_naive_instant_localized_with_policy(self):
        # a naive business instant is a wall time in the policy timezone
        snapshot = TemporalSnapshot(datetime.datetime(2024, 11, 3, 7, 30), timezone=NEW_YORK)
        self.assertEqual(snapshot.now.utcoffset(), datetime.timedelta(hours=-5))

    def test_today_truncates_in_business_timezone(self):
        snapshot = TemporalSnapshot(utc(2024, 11, 3, 4, 30), timezone=NEW_YORK)  # 00:30 NY
        today = snapshot.today()
        self.assertEqual((today.hour, today.minute, today.second, today.microsecond), (0, 0, 0, 0))
        self.assertEqual(today.astimezone(UTC), utc(2024, 11, 3, 4))

    def test_default_timezone_is_local(self):
        snapshot = TemporalSnapshot(utc(2024, 1, 1))
        self.assertEqual(snapshot.timezone_name, 'utc')  # the instant is UTC and no policy zone given

    def test_unknown_timezone_raises(self):
        with self.assertRaises(errors.EvaluationError):
            TemporalSnapshot(utc(2024, 1, 1), timezone='Mars/Olympus_Mons')

    def test_fingerprint_is_deterministic_and_sensitive(self):
        calendar = Calendar([datetime.date(2024, 1, 1)], version='v1')
        a = TemporalSnapshot(utc(2024, 1, 1), timezone=NEW_YORK, calendar=calendar,
                             timezone_database_version='tzdata/2024.1')
        b = TemporalSnapshot(utc(2024, 1, 1), timezone=NEW_YORK, calendar=calendar,
                             timezone_database_version='tzdata/2024.1')
        self.assertEqual(a.fingerprint, b.fingerprint)
        c = a.replace(timezone_database_version='tzdata/2025.1')
        self.assertNotEqual(a.fingerprint, c.fingerprint)
        d = a.replace(now=utc(2024, 1, 2))
        self.assertNotEqual(a.fingerprint, d.fingerprint)


class ReplayRuleTests(unittest.TestCase):
    def test_now_and_today_are_replayable(self):
        context = Context(default_timezone=NEW_YORK)
        snapshot = context.make_snapshot(utc(2024, 11, 3, 12))
        rule = Rule('$now.hour == 7 and $today.hour == 0 and $now.year == 2024', context=context)
        self.assertTrue(rule.matches({}, at=snapshot))
        self.assertTrue(rule.matches({}, at=snapshot))  # minutes apart on the wall clock: same answer

    def test_at_accepts_plain_datetime(self):
        context = Context(default_timezone='utc')
        rule = Rule('$now.year == 2024', context=context)
        self.assertTrue(rule.matches({}, at=utc(2024, 1, 1)))

    def test_window_is_half_open(self):
        context = Context(default_timezone='utc')
        snapshot = context.make_snapshot(utc(2024, 12, 24, 12))
        rule = Rule('$within(expires_at, $window(t"P30D")[0], $window(t"P30D")[1])', context=context)
        # end (now) is excluded, start (now - 30d) is included
        self.assertFalse(rule.matches({'expires_at': utc(2024, 12, 24, 12)}, at=snapshot))
        self.assertTrue(rule.matches({'expires_at': utc(2024, 11, 24, 12)}, at=snapshot))
        self.assertTrue(rule.matches({'expires_at': utc(2024, 12, 10)}, at=snapshot))
        self.assertFalse(rule.matches({'expires_at': utc(2024, 11, 24, 11, 59, 59)}, at=snapshot))

    def test_window_days_aligns_to_business_timezone_day_boundary(self):
        context = Context(default_timezone=NEW_YORK)
        # exactly midnight New York on 2024-11-03
        snapshot = context.make_snapshot(utc(2024, 11, 3, 4))
        rule = Rule(
                '$window_days(1)[0] == $parse_datetime("2024-11-02T00:00") and '
                '$window_days(1)[1] == $parse_datetime("2024-11-03T00:00")',
                context=context)
        self.assertTrue(rule.matches({}, at=snapshot))

    def test_window_with_explicit_anchor(self):
        snapshot = TemporalSnapshot(utc(2024, 1, 1), timezone='utc')
        start, end = snapshot.window(datetime.timedelta(days=7), anchor=utc(2025, 1, 10))
        self.assertEqual((start, end), (utc(2025, 1, 3), utc(2025, 1, 10)))

    def test_start_of_units(self):
        context = Context(default_timezone='utc')
        snapshot = context.make_snapshot(utc(2024, 6, 15, 13, 45, 30))
        thing = {'moment': utc(2024, 6, 15, 13, 45, 30)}
        cases = {
                'day': utc(2024, 6, 15),
                'week': utc(2024, 6, 10),  # Monday
                'month': utc(2024, 6, 1),
                'year': utc(2024, 1, 1),
        }
        for unit, expected in cases.items():
            rule = Rule('$start_of(moment, "{0}") == d"{1}"'.format(unit, expected.date().isoformat()), context=context)
            self.assertTrue(rule.matches(thing, at=snapshot), msg=unit)

    def test_start_of_invalid_unit(self):
        context = Context(default_timezone='utc')
        rule = Rule('$start_of($now, "fortnight")', context=context)
        with self.assertRaises(errors.FunctionCallError):
            rule.evaluate({}, at=context.make_snapshot(utc(2024, 1, 1)))

    def test_batch_filter_uses_one_snapshot(self):
        context = Context(default_timezone='utc')
        snapshot = context.make_snapshot(utc(2024, 12, 24, 12))
        rule = Rule('$within(expires_at, $window(t"P30D")[0], $window(t"P30D")[1])', context=context)
        things = (
                {'id': 'a', 'expires_at': utc(2024, 12, 10)},
                {'id': 'b', 'expires_at': utc(2024, 8, 1)},
                {'id': 'c', 'expires_at': utc(2024, 11, 24, 12)},
        )
        self.assertEqual(tuple(thing['id'] for thing in rule.filter(things, at=snapshot)), ('a', 'c'))

    def test_temporal_builtins_without_snapshot_raise(self):
        for expression in ('$is_holiday()', '$window(t"P1D")', '$window_days(1)',
                           '$within($now, $now, $now)', '$start_of($now)', '$add_business_days($now, 1)'):
            with self.assertRaises(errors.EvaluationError, msg=expression):
                Rule(expression).evaluate({})

    def test_legacy_evaluation_path_is_unchanged(self):
        # without a snapshot, $now still reads the wall clock and naive input values attach the default zone
        self.assertTrue(Rule('$now > d"2000-01-01"').matches({}))
        context = Context(default_timezone='utc')
        rule = Rule('event.hour == 2', context=context)
        self.assertTrue(rule.matches({'event': datetime.datetime(2024, 3, 10, 2)}))


@unittest.skipUnless(HAS_NEW_YORK, 'America/New_York timezone data is required for DST tests')
class DaylightSavingTests(unittest.TestCase):
    GAP_WALL = datetime.datetime(2024, 3, 10, 2, 30)   # 02:30 does not exist (spring forward)
    FOLD_WALL = datetime.datetime(2024, 11, 3, 1, 30)  # 01:30 happens twice (fall back)

    def test_gap_raises_by_default(self):
        snapshot = TemporalSnapshot(utc(2024, 3, 10, 12), timezone=NEW_YORK)
        with self.assertRaises(errors.EvaluationError):
            snapshot.localize(self.GAP_WALL)

    def test_gap_shift_forward(self):
        snapshot = TemporalSnapshot(utc(2024, 3, 10, 12), timezone=NEW_YORK,
                                    dst_policy=DstResolutionPolicy.SHIFT_FORWARD)
        localized = snapshot.localize(self.GAP_WALL)
        self.assertEqual(localized.replace(tzinfo=None), datetime.datetime(2024, 3, 10, 3, 30))
        self.assertEqual(localized.utcoffset(), datetime.timedelta(hours=-4))

    def test_gap_shift_backward(self):
        snapshot = TemporalSnapshot(utc(2024, 3, 10, 12), timezone=NEW_YORK,
                                    dst_policy=DstResolutionPolicy.SHIFT_BACKWARD)
        localized = snapshot.localize(self.GAP_WALL)
        self.assertEqual(localized.utcoffset(), datetime.timedelta(hours=-5))

    def test_fold_raises_by_default(self):
        snapshot = TemporalSnapshot(utc(2024, 11, 3, 5), timezone=NEW_YORK)
        with self.assertRaises(errors.EvaluationError):
            snapshot.localize(self.FOLD_WALL)

    def test_fold_raise_on_gap_takes_daytime_side(self):
        snapshot = TemporalSnapshot(utc(2024, 11, 3, 5), timezone=NEW_YORK,
                                    dst_policy=DstResolutionPolicy.RAISE_ON_GAP)
        localized = snapshot.localize(self.FOLD_WALL)
        self.assertEqual(localized.fold, 0)
        self.assertEqual(localized.utcoffset(), datetime.timedelta(hours=-4))

    def test_fold_shift_backward_takes_standard_side(self):
        snapshot = TemporalSnapshot(utc(2024, 11, 3, 5), timezone=NEW_YORK,
                                    dst_policy=DstResolutionPolicy.SHIFT_BACKWARD)
        localized = snapshot.localize(self.FOLD_WALL)
        self.assertEqual(localized.fold, 1)
        self.assertEqual(localized.utcoffset(), datetime.timedelta(hours=-5))

    def test_parse_datetime_respects_gap_policy(self):
        context = Context(default_timezone=NEW_YORK, dst_policy=DstResolutionPolicy.SHIFT_FORWARD)
        snapshot = context.make_snapshot(utc(2024, 3, 10, 12))
        rule = Rule('$parse_datetime("2024-03-10T02:30").hour == 3', context=context)
        self.assertTrue(rule.matches({}, at=snapshot))

    def test_parse_datetime_gap_raises(self):
        context = Context(default_timezone=NEW_YORK, dst_policy=DstResolutionPolicy.RAISE)
        snapshot = context.make_snapshot(utc(2024, 3, 10, 12))
        rule = Rule('$parse_datetime("2024-03-10T02:30")', context=context)
        with self.assertRaises(errors.EvaluationError):
            rule.evaluate({}, at=snapshot)

    def test_naive_input_symbol_respects_gap_policy(self):
        context = Context(default_timezone=NEW_YORK, dst_policy=DstResolutionPolicy.SHIFT_FORWARD)
        snapshot = context.make_snapshot(utc(2024, 3, 10, 12))
        rule = Rule('event.hour == 3', context=context)
        self.assertTrue(rule.matches({'event': self.GAP_WALL}, at=snapshot))

    def test_naive_business_instant_in_gap_raises_at_construction(self):
        with self.assertRaises(errors.EvaluationError):
            TemporalSnapshot(self.GAP_WALL, timezone=NEW_YORK, dst_policy=DstResolutionPolicy.RAISE)

    def test_truncation_respects_midnight_gap(self):
        # Sao Paulo sprang forward at midnight on 2018-11-04 (00:00 did not exist)
        if dateutil.tz.gettz('America/Sao_Paulo') is None:
            self.skipTest('America/Sao_Paulo timezone data is required')
        instant = datetime.datetime(2018, 11, 4, 10, tzinfo=UTC)
        snap = TemporalSnapshot(instant, timezone='America/Sao_Paulo', dst_policy=DstResolutionPolicy.SHIFT_FORWARD)
        truncated = snap.start_of_day()
        self.assertEqual(truncated.replace(tzinfo=None), datetime.datetime(2018, 11, 4, 1, 0))
        snap_raise = TemporalSnapshot(instant, timezone='America/Sao_Paulo')
        with self.assertRaises(errors.EvaluationError):
            snap_raise.start_of_day()

    def test_invalid_policy_string_raises(self):
        with self.assertRaises(ValueError):
            TemporalSnapshot(utc(2024, 1, 1), timezone='utc', dst_policy='rewind')


class CalendarTests(unittest.TestCase):
    def setUp(self):
        # 2024-11-04 is a Monday; 2024-11-05 (Tuesday) is a holiday
        self.calendar = Calendar([datetime.date(2024, 11, 5)], business_days=range(5), version='hol-2024.11')
        self.context = Context(default_timezone='utc', calendar=self.calendar)
        self.snapshot = self.context.make_snapshot(utc(2024, 11, 4, 9))

    def test_calendar_version_and_fingerprint(self):
        self.assertEqual(self.calendar.version, 'hol-2024.11')
        self.assertTrue(self.calendar.fingerprint.startswith('hol-2024.11:'))
        # same content, same explicit version: equal
        same = Calendar([datetime.date(2024, 11, 5)], business_days=range(5), version='hol-2024.11')
        self.assertEqual(same, self.calendar)
        # different content with same version: not equal
        changed = Calendar([datetime.date(2024, 11, 6)], business_days=range(5), version='hol-2024.11')
        self.assertNotEqual(changed, self.calendar)
        # implicit content version
        implicit = Calendar([datetime.date(2024, 11, 5)])
        self.assertTrue(implicit.version.startswith('content-'))

    def test_holiday_and_business_day_builtins(self):
        rule = Rule('$is_business_day() and not $is_holiday()', context=self.context)
        self.assertTrue(rule.matches({}, at=self.snapshot))
        holiday_snapshot = self.context.make_snapshot(utc(2024, 11, 5, 9))
        rule = Rule('$is_holiday() and not $is_business_day()', context=self.context)
        self.assertTrue(rule.matches({}, at=holiday_snapshot))
        # Saturday is neither a business day nor a declared holiday
        weekend_snapshot = self.context.make_snapshot(utc(2024, 11, 9, 9))
        rule = Rule('not $is_business_day() and not $is_holiday()', context=self.context)
        self.assertTrue(rule.matches({}, at=weekend_snapshot))

    def test_add_business_days_skips_weekend_and_holidays(self):
        rule = Rule(
                '$add_business_days($parse_datetime("2024-11-04T09:00"), 1).date == $parse_datetime("2024-11-06T09:00").date',
                context=self.context)
        self.assertTrue(rule.matches({}, at=self.snapshot))
        # skipping a full week with the Tuesday holiday lands on the following Tuesday
        rule = Rule(
                '$add_business_days($parse_datetime("2024-11-04T09:00"), 5).date == $parse_datetime("2024-11-12T09:00").date',
                context=self.context)
        self.assertTrue(rule.matches({}, at=self.snapshot))
        # negative counts walk backwards
        rule = Rule(
                '$add_business_days($parse_datetime("2024-11-06T09:00"), -1).date == $parse_datetime("2024-11-04T09:00").date',
                context=self.context)
        self.assertTrue(rule.matches({}, at=self.snapshot))

    def test_calendar_direct_arithmetic(self):
        self.assertEqual(self.calendar.add_business_days(datetime.date(2024, 11, 4), 1), datetime.date(2024, 11, 6))
        self.assertFalse(self.calendar.is_business_day(datetime.date(2024, 11, 5)))

    def test_calendar_required_for_business_day_functions(self):
        snapshot = TemporalSnapshot(utc(2024, 11, 4), timezone='utc')
        with self.assertRaises(errors.FunctionCallError):
            snapshot.is_holiday()
        with self.assertRaises(errors.FunctionCallError):
            snapshot.add_business_days(None, 1)

    def test_calendar_serialization_round_trip(self):
        restored = Calendar.from_dict(self.calendar.to_dict())
        self.assertEqual(restored, self.calendar)
        self.assertEqual(restored.holidays, self.calendar.holidays)
        self.assertEqual(restored.business_days, self.calendar.business_days)


class SnapshotSerializationTests(unittest.TestCase):
    def setUp(self):
        self.calendar = Calendar([datetime.date(2024, 12, 25)], business_days=range(5), version='v1')
        self.snapshot = TemporalSnapshot(utc(2024, 12, 24, 12), timezone=NEW_YORK,
                                         calendar=self.calendar, timezone_database_version='tzdata/2024.1')

    def test_dict_and_json_round_trip(self):
        restored = TemporalSnapshot.from_dict(self.snapshot.to_dict())
        self.assertEqual(restored.fingerprint, self.snapshot.fingerprint)
        self.assertEqual(restored.now, self.snapshot.now)
        self.assertEqual(restored.timezone_name, NEW_YORK)
        self.assertEqual(restored.calendar, self.calendar)
        restored.compatible_with(self.snapshot)
        from_json = TemporalSnapshot.from_json(self.snapshot.to_json())
        self.assertEqual(from_json.fingerprint, self.snapshot.fingerprint)

    def test_fingerprint_detects_tampering(self):
        data = json.loads(self.snapshot.to_json())
        data['now'] = '2024-12-24T13:00:00+00:00'
        with self.assertRaises(errors.EvaluationError):
            TemporalSnapshot.from_dict(data)

    def test_missing_field_raises(self):
        data = self.snapshot.to_dict()
        del data['now']
        with self.assertRaises(errors.EvaluationError):
            TemporalSnapshot.from_dict(data)

    def test_compatible_with_allows_different_instant(self):
        later = self.snapshot.replace(now=utc(2025, 1, 1))
        later.compatible_with(self.snapshot)  # no exception

    def test_compatible_with_detects_version_mismatches(self):
        for kwargs in (
                {'timezone': 'utc'},
                {'timezone_database_version': 'tzdata/2020.1'},
                {'dst_policy': DstResolutionPolicy.SHIFT_FORWARD},
        ):
            with self.assertRaises(errors.EvaluationError, msg=str(kwargs)):
                self.snapshot.compatible_with(self.snapshot.replace(**kwargs))
        # calendar removed vs present
        with self.assertRaises(errors.EvaluationError):
            self.snapshot.compatible_with(self.snapshot.replace(calendar=None))
        # same version label, different holiday content: rejected when content is required
        same_version_different_content = Calendar([datetime.date(2024, 12, 26)], business_days=range(5), version='v1')
        other = self.snapshot.replace(calendar=same_version_different_content)
        with self.assertRaises(errors.EvaluationError):
            self.snapshot.compatible_with(other)
        # version-only comparison passes when content requirement is relaxed
        self.snapshot.compatible_with(other, require_calendar_content=False)

    def test_versions_report(self):
        versions = self.snapshot.versions
        self.assertEqual(versions['timezone'], NEW_YORK)
        self.assertEqual(versions['timezone_database'], 'tzdata/2024.1')
        self.assertEqual(versions['calendar'], 'v1')
        self.assertEqual(versions['dst_policy'], 'raise')

    def test_pickle_round_trip(self):
        restored = pickle.loads(pickle.dumps(self.snapshot))
        self.assertEqual(restored.fingerprint, self.snapshot.fingerprint)
        restored.compatible_with(self.snapshot)

    def test_detect_database_version_returns_string(self):
        version = engine.temporal.detect_timezone_database_version()
        self.assertIsInstance(version, str)
        self.assertTrue(version)


class NestedEvaluationTests(unittest.TestCase):
    def test_nested_snapshot_isolates_and_restores_outer(self):
        context = Context(default_timezone='utc')
        outer_snapshot = context.make_snapshot(utc(2024, 1, 1))
        inner_snapshot = context.make_snapshot(utc(2025, 6, 6))
        inner_rule = Rule('$now.year == 2025', context=context)

        @temporal_function
        def nested(snapshot):
            self.assertEqual(snapshot.now.year, 2024)
            inner_result = inner_rule.matches({}, at=inner_snapshot)
            # after the inner call the outer snapshot is visible again
            self.assertEqual(engine.temporal.get_current_snapshot(), outer_snapshot)
            return inner_result

        context.builtins = builtins.Builtins.from_defaults({'nested': nested}, timezone=dateutil.tz.tzutc())
        outer_rule = Rule('$nested() and $now.year == 2024', context=context)
        self.assertTrue(outer_rule.matches({}, at=outer_snapshot))
        self.assertIsNone(engine.temporal.get_current_snapshot())

    def test_nested_evaluation_inherits_snapshot_without_at(self):
        context = Context(default_timezone='utc')
        snapshot = context.make_snapshot(utc(2024, 6, 1))
        inner_rule = Rule('$now.year == 2024', context=context)

        @temporal_function
        def check(snapshot):
            return inner_rule.matches({})  # no at=: inherits the active snapshot

        context.builtins = builtins.Builtins.from_defaults({'check': check}, timezone=dateutil.tz.tzutc())
        self.assertTrue(Rule('$check()', context=context).matches({}, at=snapshot))

    def test_temporal_function_without_injected_snapshot(self):
        context = Context(default_timezone='utc')
        snapshot = context.make_snapshot(utc(2024, 1, 1))

        @temporal_function(needs_snapshot=False)
        def declared_temporal():
            return True

        context.builtins = builtins.Builtins.from_defaults({'declared_temporal': declared_temporal},
                                                           timezone=dateutil.tz.tzutc())
        self.assertTrue(Rule('$declared_temporal()', context=context).matches({}, at=snapshot))
        with self.assertRaises(errors.FunctionCallError):
            Rule('$declared_temporal()', context=context).matches({})

    def test_custom_temporal_function_requires_snapshot(self):
        context = Context(default_timezone='utc')

        @temporal_function
        def grace_end(snapshot, days):
            return snapshot.now + datetime.timedelta(days=int(days))

        context.builtins = builtins.Builtins.from_defaults({'grace_end': grace_end}, timezone=dateutil.tz.tzutc())
        rule = Rule('$grace_end(30) == $now + t"P30D"', context=context)
        self.assertTrue(rule.matches({}, at=context.make_snapshot(utc(2024, 1, 1))))
        with self.assertRaises(errors.FunctionCallError):
            rule.matches({})

    def test_threads_get_independent_snapshots(self):
        context = Context(default_timezone='utc')
        rule = Rule('$now.year == year', context=context)
        results = {}

        def worker(year):
            snapshot = context.make_snapshot(utc(year, 1, 1))
            results[year] = rule.matches({'year': year}, at=snapshot)

        threads = [threading.Thread(target=worker, args=(year,)) for year in (2020, 2024, 2030)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results, {2020: True, 2024: True, 2030: True})


class ContextTemporalConfigTests(unittest.TestCase):
    def test_iana_default_timezone(self):
        context = Context(default_timezone=NEW_YORK)
        snapshot = context.make_snapshot(utc(2024, 11, 3, 12))
        self.assertEqual(snapshot.timezone_name, NEW_YORK)
        with self.assertRaises(ValueError):
            Context(default_timezone='Not/A_Zone')

    def test_calendar_and_policy_pickle(self):
        calendar = Calendar([datetime.date(2024, 12, 25)], version='v1')
        context = Context(default_timezone=NEW_YORK, calendar=calendar, dst_policy='shift_forward')
        restored = pickle.loads(pickle.dumps(context))
        self.assertEqual(restored.calendar, calendar)
        self.assertEqual(restored.dst_policy, DstResolutionPolicy.SHIFT_FORWARD)
        snapshot = restored.make_snapshot(utc(2024, 12, 24, 12))
        self.assertEqual(snapshot.timezone_name, NEW_YORK)
        self.assertTrue(Rule('$now.year == 2024', context=restored).matches({}, at=snapshot))

    def test_invalid_calendar_type_raises(self):
        with self.assertRaises(TypeError):
            Context(calendar=['2024-12-25'])


if __name__ == '__main__':
    unittest.main()
