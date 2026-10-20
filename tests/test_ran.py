"""The radio side of a session: RAN entries from the gNB metrics tap and the
E2 xApp, projected into chartable columns. Pure functions, no database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mec_cast_logging.sessions import build_ran

START = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
T0_NS = int(START.timestamp() * 1e9)


def tap(i, rows, service="mec-cast-ran"):
    """One gNB report as the collector posts it: norm rows, recv_ns."""
    return {
        "timestamp": START + timedelta(seconds=i),
        "service": service,
        "recv_ns": str(T0_NS + i * 1_000_000_000),
        "norm": [{"cell": "1", "unit": u, "gnb_ts_ns": 0, **r} for r, u in rows],
        "control": None,
    }


def ul(ue, value):
    return ({"ue": ue, "metric": "ue.ul_throughput_bps", "value": value}, "bit/s")


def control(i, action, outcome, max_ratio):
    return {
        "timestamp": START + timedelta(seconds=i),
        "service": "mec-cast-ran-kpm",
        "recv_ns": None,
        "norm": None,
        "control": {
            "ts_ns": T0_NS + i * 1_000_000_000,
            "node": "gnb-1",
            "ue_id": "0",
            "action": action,
            "min_prb_ratio": "0",
            "max_prb_ratio": str(max_ratio),
            "dedicated_prb_ratio": "0",
            "outcome": outcome,
            "detail": "",
        },
    }


def test_no_ran_entries_is_an_empty_answer_not_an_error():
    ran = build_ran("run-1", [])
    assert ran.sources == [] and ran.controls == []


def test_series_are_aligned_with_the_report_times_and_gaps_stay_null():
    rows = [
        tap(0, [ul("17921", 5e6)]),
        tap(1, [ul("17921", 6e6), ul("17922", 1e6)]),  # a second UE attaches
        tap(2, [ul("17921", 7e6)]),
    ]
    (src,) = build_ran("run-1", rows).sources
    assert src.source == "json" and src.service == "mec-cast-ran"
    assert src.t == [START.timestamp() + i for i in range(3)]
    assert src.ues == ["17921", "17922"]
    by_ue = {s.ue: s for s in src.series}
    assert by_ue["17921"].values == [5e6, 6e6, 7e6]
    assert by_ue["17922"].values == [None, 1e6, None], "absent is null, never zero"
    assert by_ue["17921"].unit == "bit/s"


def test_cell_rows_are_kept_but_not_listed_as_a_ue():
    rows = [tap(0, [({"ue": "", "metric": "cell.pusch_prbs_used_mean", "value": 12}, "prb")])]
    (src,) = build_ran("run-1", rows).sources
    assert src.ues == []
    assert src.series[0].metric == "cell.pusch_prbs_used_mean"


def test_both_sources_come_back_apart_tap_first():
    rows = [
        tap(0, [ul("e2:0", 5.7e6)], service="mec-cast-ran-kpm"),
        tap(0, [ul("17921", 5.8e6)]),
    ]
    sources = build_ran("run-1", rows).sources
    assert [s.source for s in sources] == ["json", "kpm"]
    assert sources[1].ues == ["e2:0"]


def test_time_falls_back_to_the_database_stamp_without_recv_ns():
    row = tap(5, [ul("17921", 1.0)])
    row["recv_ns"] = None
    (src,) = build_ran("run-1", [row]).sources
    assert src.t == [(START + timedelta(seconds=5)).timestamp()]


def test_controls_are_listed_in_order_with_their_outcome():
    rows = [control(30, "step@30s", "ack", 100), control(1, "apply", "ack", 30)]
    ran = build_ran("run-1", rows)
    assert [(c.action, c.max_prb_ratio, c.outcome) for c in ran.controls] == [
        ("apply", 30, "ack"),
        ("step@30s", 100, "ack"),
    ]
    assert ran.controls[0].ue_id == 0
    assert ran.sources == [], "a control is not a report"


def test_entries_from_other_services_are_ignored():
    rows = [tap(0, [ul("1", 1.0)], service="mec-cast-edge")]
    assert build_ran("run-1", rows).sources == []
