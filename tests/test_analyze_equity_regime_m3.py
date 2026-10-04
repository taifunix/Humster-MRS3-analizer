from datetime import datetime, timedelta, timezone
from decimal import Decimal

import hashlib
import json

from scripts.analyze_equity_regime_m3 import curve_facts, direction, classify, group_stream, summarize, _canonical, _classify_history


T = datetime(2026, 10, 1, tzinfo=timezone.utc)


def row(day, equity, index):
    return (index, T + timedelta(days=day), Decimal(str(equity)))


def test_source_order_is_checked_before_deduplication():
    points = [row(-44, 100, 0), row(-42, 101, 1), row(-43, 102, 2)]
    facts = curve_facts(points, T - timedelta(days=45), T)
    assert facts['status'] == 'NOT_EVALUATED'
    assert facts['reason'] == 'source_invalid_chronology'


def test_pre28_exact_14_days_and_full_hwm_carry():
    points = [row(-42, 100, 0), row(-28, 110, 1), row(-15, 120, 2),
              row(-14, 90, 3), row(-7, 92, 4), row(0, 100, 5)]
    facts = curve_facts(points, T - timedelta(days=42), T)
    assert facts['pre28_days'] == 14
    assert facts['pre28'] is not None
    assert facts['hwm']['7']['value'] == '120'
    assert Decimal(facts['dd']['7']) >= Decimal('23')


def test_weekly_breakout_uses_frozen_ath_and_close():
    points = [row(-42, 100, 0), row(-28, 105, 1), row(-14, 108, 2),
              row(-7, 110, 3), row(-5, 112, 4), row(-2, 115, 5), row(0, 111, 6)]
    facts = curve_facts(points, T - timedelta(days=42), T)
    assert facts['hwm']['7']['value'] == '110'
    assert facts['stages'][2] >= 2
    assert facts['held_weekly_breakout'] is True
    assert facts['stage_ath'][2]['first']['value'] == '112'
    assert facts['stage_ath'][2]['last']['value'] == '115'
    assert facts['stage_ath'][2]['first']['time'] < facts['stage_ath'][2]['last']['time']


def test_held_breakout_uses_strict_decimal_comparison_without_direction_epsilon():
    base = [row(-42, 100, 0), row(-28, 105, 1), row(-14, 108, 2),
            row(-7, 110, 3), row(-5, 112, 4), row(-2, 115, 5)]
    equal = curve_facts(base + [row(0, 110, 6)], T - timedelta(days=42), T)
    above = curve_facts(base + [row(0, '110.000000000001', 6)], T - timedelta(days=42), T)
    below = curve_facts(base + [row(0, '109.999999999999', 6)], T - timedelta(days=42), T)
    assert all(f['stages'][2] >= 1 for f in (equal, above, below))
    assert equal['held_weekly_breakout'] is False
    assert above['held_weekly_breakout'] is True
    assert below['held_weekly_breakout'] is False


def test_h1_prefix_pause_can_prove_later_resumed():
    points = [(i, T + timedelta(days=day), Decimal(value)) for i, (day, value)
              in enumerate([(-56, '100'), (-42, '105'), (-28, '110'),
                            (-21, '112'), (-14, '115'), (-12, '117'),
                            (-10, '120'), (-7, '115'), (-5, '122'),
                            (-3, '125'), (0, '123')])]
    facts = curve_facts(points, T - timedelta(days=56), T, prefixes=True)
    assert facts['prefixes']['7']['stages'][2] > 0
    assert facts['prefixes']['7']['held_weekly_breakout'] is False
    assert facts['held_weekly_breakout'] is True
    history, current = _classify_history(facts, Decimal('0.5'), 'abs', Decimal(1))
    assert [x['status'] for x in history] == ['WEAKENING', 'STALLED']
    assert current['status'] == 'RESUMED'


def test_direction_and_two_slowdowns():
    def metric(speed):
        return {'trend30': str(speed), 'endpoint30': str(speed)}
    facts = {'status': 'READY', 'pre28': None, 'windows': {'28': metric(10),
             '14': metric(9), '7': metric(12)}, 'stages': [1, 1, 1],
             'dd': {'14': '0', '7': '0'}, 'held_weekly_breakout': True}
    assert direction(metric(0.2), Decimal('0.25')) == 'FLAT'
    assert classify(facts, Decimal('0.25'), 'abs', Decimal('0.5'))['status'] == 'GROWING'
    facts['windows']['7'] = metric(8)
    assert classify(facts, Decimal('0.25'), 'abs', Decimal('0.5'))['status'] == 'WEAKENING'
    assert classify(facts, Decimal('0.25'), 'rel', Decimal('0.3'))['status'] == 'GROWING'


def test_all_up_and_staged_ath_with_lost_weekly_breakout_is_stalled():
    def metric(speed):
        return {'trend30': str(speed), 'endpoint30': str(speed)}
    facts = {'status': 'READY', 'pre28': None,
             'windows': {'28': metric(10), '14': metric(9), '7': metric(8)},
             'stages': [1, 1, 1], 'dd': {'14': '5', '7': '5'},
             'held_weekly_breakout': False}
    result = classify(facts, Decimal('0.5'), 'abs', Decimal(1))
    assert result['status'] == 'STALLED'
    assert result['rank'] == 'RESERVED'


def test_group_stream_counts_empty_and_final_group():
    rows = [(1, *row(-1, 100, 0)), (1, *row(0, 101, 1)), (3, *row(0, 99, 0))]
    groups = list(group_stream([rows[:1], rows[1:]], {1, 2, 3}))
    assert [(key, len(group)) for key, group in groups] == [(1, 2), (2, 0), (3, 1)]


def test_nonpositive_and_chronology_precedence():
    points = [row(-42, 100, 0), row(-40, 0, 1), row(-41, 100, 2)]
    facts = curve_facts(points, T - timedelta(days=42), T)
    assert facts['reason'] == 'source_invalid_chronology'
    points[-1] = row(-39, 100, 2)
    facts = curve_facts(points, T - timedelta(days=42), T)
    assert facts['reason'] == 'nonpositive_equity'


def test_drawdown_boundary_equal_23_drops_after_growth():
    points = [row(-42, 100, 0), row(-28, 110, 1), row(-14, 100, 2),
              row(-7, 100, 3), row(-3, 77, 4), row(0, 100, 5)]
    facts = curve_facts(points, T - timedelta(days=42), T)
    # Peak 110 implies a 30% fall to 77, including the old peak in DD.
    assert Decimal(facts['dd']['7']) == 30
    assert classify(facts, Decimal('0.5'), 'abs', Decimal(1))['status'] == 'DECLINING'
    points[4] = row(-3, '84.7', 4)
    facts = curve_facts(points, T - timedelta(days=42), T)
    assert Decimal(facts['dd']['7']) == 23
    assert classify(facts, Decimal('0.5'), 'abs', Decimal(1))['status'] == 'DECLINING'


def test_prefixes_never_use_future_points():
    base = [row(-50, 100, 0), row(-42, 102, 1), row(-35, 104, 2),
            row(-28, 106, 3), row(-21, 108, 4), row(-14, 110, 5),
            row(-7, 112, 6), row(0, 114, 7)]
    original = curve_facts(base, T - timedelta(days=50), T, prefixes=True)
    altered = base[:-1] + [row(0, 500, 7)]
    changed = curve_facts(altered, T - timedelta(days=50), T, prefixes=True)
    assert original['prefixes'] == changed['prefixes']
    assert original['hwm']['0'] != changed['hwm']['0']


def test_a_late_first_record_does_not_retroactively_prove_prior_growth():
    points = [row(-42, 100, 0), row(-14, 100, 1), row(-7, 70, 2),
              row(-3, 100, 3), row(0, 110, 4)]
    facts = curve_facts(points, T - timedelta(days=42), T)
    assert Decimal(facts['dd']['14']) == 30
    assert facts['strict_ath_count'] == 1
    assert facts['severe_dd_after_growth'] is False
    outcome = classify(facts, Decimal('0.5'), 'abs', Decimal(1))
    assert outcome['status'] == 'UNRESOLVED'
    assert outcome['decision'] == 'DROP'


def test_resumed_remains_resumed_until_growth_geometry_is_complete():
    def metric(speed):
        return {'trend30': str(speed), 'endpoint30': str(speed)}
    facts = {'status': 'READY', 'pre28': None,
             'windows': {'28': metric(10), '14': metric(0), '7': metric(5)},
             'stages': [1, 0, 1], 'dd': {'14': '5', '7': '3'},
             'held_weekly_breakout': True}
    assert classify(facts, Decimal('0.5'), 'abs', Decimal(1),
                    ('STALLED', 'RESUMED'))['status'] == 'RESUMED'
    facts['windows']['14'] = metric(6)
    facts['stages'][1] = 1
    assert classify(facts, Decimal('0.5'), 'abs', Decimal(1),
                    ('STALLED', 'RESUMED'))['status'] in {'GROWING', 'WEAKENING'}


def test_reclassification_keeps_frozen_panel_projection(tmp_path):
    points = [row(-42, 100, 0), row(-28, 105, 1), row(-14, 110, 2),
              row(-7, 115, 3), row(0, 120, 4)]
    facts = curve_facts(points, T - timedelta(days=42), T, prefixes=True)
    (tmp_path/'facts.jsonl').write_text(_canonical({'result_id': 7, 'symbol': 'X',
        'side': 'LONG', 'old_state': 'GROWING', 'facts': facts}) + '\n', encoding='utf-8')
    manifest = {'seed': 'fixed'}
    first = summarize(tmp_path, manifest, {7})
    again = summarize(tmp_path, manifest, {7})
    assert first['latest_panel_anchor'] == again['latest_panel_anchor']
    assert sum(first['latest_panel_anchor'].values()) == 1
    assert sum(first['anchor_actions'].values()) == 1
    assert sum(first['anchor_reasons'].values()) == 1


def test_summary_facts_sha_matches_raw_crlf_artifact(tmp_path):
    points = [row(-42, 100, 0), row(-28, 105, 1), row(-14, 110, 2),
              row(-7, 115, 3), row(0, 120, 4)]
    facts = curve_facts(points, T - timedelta(days=42), T, prefixes=True)
    payload = (_canonical({'result_id': 7, 'symbol': 'X', 'side': 'LONG',
                           'old_state': 'GROWING', 'facts': facts}) + '\r\n').encode()
    (tmp_path/'facts.jsonl').write_bytes(payload)
    result = summarize(tmp_path, {'seed': 'fixed'}, {7})
    assert result['facts_sha256'] == hashlib.sha256(payload).hexdigest()


def test_duplicate_timestamp_keeps_last_effective_value_and_raw_risk():
    points = [row(-42, 100, 0), row(-28, 110, 1), row(-14, 120, 2),
              row(-7, 120, 3),
              (4, T - timedelta(days=3) + timedelta(hours=1), Decimal(80)),
              (5, T - timedelta(days=3) + timedelta(hours=1), Decimal(130)),
              (6, T - timedelta(days=3) + timedelta(hours=2), Decimal(100)),
              row(0, 122, 7)]
    facts = curve_facts(points, T - timedelta(days=42), T)
    assert facts['status'] == 'READY'
    assert Decimal(facts['dd']['7']) > 30
    assert facts['hwm']['0']['value'] == '130'
    assert facts['raw_peak_missed_by_grid'] is True


def test_ath_on_boundary_belongs_to_earlier_stage():
    points = [row(-42, 100, 0), row(-28, 101, 1), row(-14, 102, 2),
              row(-7, 103, 3), row(0, 104, 4)]
    facts = curve_facts(points, T - timedelta(days=42), T)
    assert facts['stages'] == [1, 1, 1]
    assert facts['hwm']['7']['value'] == '103'
    assert facts['held_weekly_breakout'] is True


def test_invalid_source_order_cannot_be_repaired_by_timestamp_sorting():
    points = [row(-42, 100, 0), row(-40, 101, 1), row(-41, 102, 2)]
    assert curve_facts(points, T - timedelta(days=42), T)['reason'] == 'source_invalid_chronology'
    # Sorting by timestamp would make this source appear valid; M3 checks the original indices.
    assert [p[0] for p in sorted(points, key=lambda p: p[1])] == [0, 2, 1]


def test_invalid_duplicate_index_and_nonfinite_equity_precede_metrics():
    repeated = [row(-42, 100, 0), row(-40, 101, 1), row(-40, 102, 1)]
    assert curve_facts(repeated, T - timedelta(days=42), T)['reason'] == 'source_invalid_sample_index'
    malformed = [row(-42, 100, 0), row(-40, Decimal('NaN'), 1)]
    assert curve_facts(malformed, T - timedelta(days=42), T)['reason'] == 'source_invalid_equity'


def test_non_utc_timestamp_is_rejected():
    local = datetime(2026, 10, 1, tzinfo=timezone(timedelta(hours=2)))
    points = [(0, local - timedelta(days=42), Decimal(100)), (1, local, Decimal(110))]
    assert curve_facts(points, T - timedelta(days=42), T)['reason'] == 'invalid_utc_timestamp'
