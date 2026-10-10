from datetime import UTC, datetime, timedelta

import pytest

from vydpobot.alerts import AlertEvent, AlertState, evaluate_soc, evaluate_staleness

THR, STEP, HYST = 50.0, 5.0, 5.0


def run(socs, state=None, thr=THR, step=STEP, hyst=HYST):
    """Feed a SOC sequence; return (final state, list of per-step events)."""
    state = state or AlertState()
    out = []
    for soc in socs:
        state, ev = evaluate_soc(state, soc, thr, step, hyst)
        out.append(ev)
    return state, out


def drops(events):
    return [(e.kind, e.level) for step_ev in events for e in step_ev]


@pytest.mark.parametrize(
    ("soc", "level"),
    [(50, 50), (49.9, 50), (47, 50), (45, 45), (44, 45), (31, 35), (7, 10), (5, 5), (4, 5), (0, 5)],
)
def test_first_drop_level(soc, level):
    state, ev = evaluate_soc(AlertState(), soc, THR, STEP, HYST)
    assert ev == [AlertEvent("drop", soc=soc, level=level)]
    assert state.in_alert
    assert state.next_level == level - STEP


@pytest.mark.parametrize("soc", [50.01, 51, 52, 54.9])
def test_above_threshold_no_event(soc):
    state, ev = evaluate_soc(AlertState(), soc, THR, STEP, HYST)
    assert ev == [] and state == AlertState()


def test_none_soc_noop():
    s = AlertState(next_level=30, in_alert=True)
    assert evaluate_soc(s, None, THR, STEP, HYST) == (s, [])


def test_levels_in_sequence():
    _, ev = run([52, 49, 48, 46, 44, 43, 40, 39, 35])
    assert [e.level for evs in ev for e in evs] == [50, 45, 40, 35]


def test_skipped_levels_single_event():
    state, ev = run([60, 28])
    assert [e.level for evs in ev for e in evs] == [30]
    assert state.next_level == 25
    assert len([e for evs in ev for e in evs]) == 1


def test_no_repeat_at_same_level():
    _, ev = run([49, 48, 47, 46])
    assert len([e for evs in ev for e in evs]) == 1


def test_soc_zero_no_spam():
    state, ev = run([0, 0, 0, 0])
    assert [e.level for evs in ev for e in evs] == [5]
    assert state.next_level == 0


def test_lowest_level_then_zero_after_drop_chain():
    _, ev = run([8, 4, 0, 0])
    assert [e.level for evs in ev for e in evs] == [10, 5]


def test_full_recovery_emits_and_resets():
    state, ev = run([40, 56])
    assert drops(ev) == [("drop", 40), ("recovered", None)]
    assert state == AlertState()


def test_recovery_without_alert_silent():
    state, ev = evaluate_soc(AlertState(), 80, THR, STEP, HYST)
    assert ev == [] and state == AlertState()


def test_recovery_keeps_stale_flag():
    s = AlertState(next_level=30, in_alert=True, stale_notified=True)
    state, ev = evaluate_soc(s, 70, THR, STEP, HYST)
    assert state == AlertState(stale_notified=True)
    assert [e.kind for e in ev] == ["recovered"]


def test_partial_rearm_example():
    state, _ = run([30])
    assert state.next_level == 25
    state, ev = evaluate_soc(state, 45, THR, STEP, HYST)
    assert ev == [] and state.next_level == 40 and state.in_alert
    state, ev = evaluate_soc(state, 40, THR, STEP, HYST)
    assert [(e.kind, e.level) for e in ev] == [("drop", 40)]
    assert state.next_level == 35


def test_oscillation_never_realerts():
    _, ev = run([30] + [31, 30] * 10)
    assert len([e for evs in ev for e in evs]) == 1


@pytest.mark.parametrize(
    ("soc", "expected_next"),
    [(30, 25), (31, 25), (35, 30), (39, 30), (40, 35), (45, 40), (54, 45)],
)
def test_partial_rearm_levels(soc, expected_next):
    state = AlertState(next_level=25, in_alert=True)
    state, ev = evaluate_soc(state, soc, THR, STEP, HYST)
    assert ev == []
    assert state.next_level == expected_next


def test_rearm_from_zero_sentinel():
    state, _ = run([0])
    state, ev = evaluate_soc(state, 12, THR, STEP, HYST)
    assert ev == [] and state.next_level == 5
    state, ev = evaluate_soc(state, 5, THR, STEP, HYST)
    assert [e.level for e in ev] == [5]


def test_hysteresis_zero():
    state, ev = run([44, 46, 49.9], hyst=0)
    assert drops(ev) == [("drop", 45)]
    assert state.next_level == 45  # rearmed immediately
    _, ev = run([44, 50], hyst=0)
    assert drops(ev) == [("drop", 45), ("recovered", None)]


def test_float_safety():
    state, ev = evaluate_soc(AlertState(), 45.0000001, 50, 5, 5)
    assert [e.level for e in ev] == [45]
    state, ev = evaluate_soc(AlertState(), 0.1 + 0.2 + 44.7, 50, 5, 5)  # 45.0000000000x
    assert [e.level for e in ev] == [45]


def test_non_integer_step():
    _, ev = run([47], thr=50, step=2.5)
    assert [e.level for evs in ev for e in evs] == [47.5]


def test_threshold_100():
    _, ev = run([100, 96, 91, 89], thr=100, step=10)
    assert [e.level for evs in ev for e in evs] == [100, 90]


def test_next_level_above_threshold_clamped():
    s = AlertState(next_level=90, in_alert=True)
    _, ev = evaluate_soc(s, 52, THR, STEP, HYST)
    assert ev == []
    _, ev = evaluate_soc(s, 48, THR, STEP, HYST)
    assert [e.level for e in ev] == [50]


def test_dict_roundtrip():
    s = AlertState(next_level=25.0, in_alert=True, stale_notified=True)
    assert AlertState.from_dict(s.to_dict()) == s
    assert AlertState.from_dict(AlertState().to_dict()) == AlertState()
    assert AlertState.from_dict(None) == AlertState()
    assert AlertState.from_dict({}) == AlertState()


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("age_min", "notified", "after", "kinds", "flag"),
    [
        (10, False, 30, [], False),
        (30, False, 30, [], False),
        (31, False, 30, ["stale"], True),
        (90, True, 30, [], True),
        (5, True, 30, ["back_online"], False),
        (5, False, 30, [], False),
        (500, False, 0, [], False),
        (500, True, 0, [], True),
    ],
)
def test_staleness(age_min, notified, after, kinds, flag):
    s = AlertState(next_level=30, in_alert=True, stale_notified=notified)
    new, ev = evaluate_staleness(s, NOW - timedelta(minutes=age_min), NOW, after)
    assert [e.kind for e in ev] == kinds
    assert new.stale_notified is flag
    assert new.next_level == 30 and new.in_alert


def test_stale_seconds():
    _, ev = evaluate_staleness(AlertState(), NOW - timedelta(minutes=45), NOW, 30)
    assert ev[0].stale_seconds == 45 * 60
