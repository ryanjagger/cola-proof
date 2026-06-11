"""State machine and lifecycle tests for the store (spec phase-4)."""

import pytest

from server.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "test.db", tmp_path / "media")


def _finish(store, record_id, auto_status):
    store.record_done(
        record_id,
        ttb_id="12345678901234",
        auto_status=auto_status,
        form={"brand_name": "X"},
        crops=[],
        verdicts=[],
        warning=None,
        escalation=[],
    )


def test_pass_auto_approves_by_system(store):
    b = store.create_batch("batch")
    rid = store.add_record(b["id"], "a.pdf")
    _finish(store, rid, "Pass")
    r = store.get_record(rid)
    assert r["disposition"] == "Approved"
    assert r["dispositioned_by"] == "system"
    assert r["dispositioned_at"] is not None


def test_pass_stays_editable(store):
    b = store.create_batch("batch")
    rid = store.add_record(b["id"], "a.pdf")
    _finish(store, rid, "Pass")
    r = store.set_disposition(rid, "Rejected", by="agent.smith", note="label art swapped")
    assert r["disposition"] == "Rejected"
    assert r["dispositioned_by"] == "agent.smith"
    assert r["note"] == "label art swapped"
    # auto_status is untouched: the disagreement is the audit signal.
    assert r["auto_status"] == "Pass"


def test_fail_is_never_auto_rejected(store):
    b = store.create_batch("batch")
    rid = store.add_record(b["id"], "a.pdf")
    _finish(store, rid, "Fail")
    r = store.get_record(rid)
    assert r["disposition"] is None  # open, awaiting the agent
    assert r["dispositioned_by"] is None


def test_system_cannot_disposition(store):
    b = store.create_batch("batch")
    rid = store.add_record(b["id"], "a.pdf")
    _finish(store, rid, "Fail")
    with pytest.raises(ValueError):
        store.set_disposition(rid, "Rejected", by="system")


def test_invalid_disposition_rejected(store):
    b = store.create_batch("batch")
    rid = store.add_record(b["id"], "a.pdf")
    _finish(store, rid, "Needs Review")
    with pytest.raises(ValueError):
        store.set_disposition(rid, "Maybe", by="agent")


def test_cannot_disposition_unfinished_record(store):
    b = store.create_batch("batch")
    rid = store.add_record(b["id"], "a.pdf")
    with pytest.raises(KeyError):
        store.set_disposition(rid, "Approved", by="agent")


def test_batch_summary_and_completeness(store):
    b = store.create_batch("batch")
    r_pass = store.add_record(b["id"], "a.pdf")
    r_fail = store.add_record(b["id"], "b.pdf")
    r_review = store.add_record(b["id"], "c.pdf")
    _finish(store, r_pass, "Pass")
    _finish(store, r_fail, "Fail")
    _finish(store, r_review, "Needs Review")

    s = store.batch_summary(b["id"])
    assert s == {
        "total": 3, "processed": 3, "failed": 1, "needs_review": 1,
        "passed": 1, "errors": 0, "open": 2, "complete": False,
    }
    store.set_disposition(r_fail, "Rejected", by="agent")
    store.set_disposition(r_review, "Approved", by="agent")
    s = store.batch_summary(b["id"])
    assert s["open"] == 0
    assert s["complete"] is True


def test_error_record_counts_processed_but_not_open(store):
    b = store.create_batch("batch")
    rid = store.add_record(b["id"], "bad.pdf")
    store.record_error(rid, "cannot open PDF")
    s = store.batch_summary(b["id"])
    assert s["processed"] == 1
    assert s["errors"] == 1
    assert s["open"] == 0


def test_delete_batch_purges_media(store):
    b = store.create_batch("batch")
    rid = store.add_record(b["id"], "a.pdf")
    media = store.batch_media_dir(b["id"])
    (media / "a.pdf").write_bytes(b"pdf")
    store.delete_batch(b["id"])
    assert not media.exists()
    assert store.get_batch(b["id"]) is None
    assert store.get_record(rid) is None


def test_record_roundtrip_inflates_json(store):
    b = store.create_batch("batch")
    rid = store.add_record(b["id"], "a.pdf")
    store.record_done(
        rid,
        ttb_id="x",
        auto_status="Needs Review",
        form={"brand_name": "OLD CARTER"},
        crops=[{"index": 0, "kind": "front"}],
        verdicts=[{"field": "brand_name", "outcome": "near_miss"}],
        warning={"status": "near", "score": 97.0},
        escalation=["brand_name: near_miss"],
    )
    r = store.get_record(rid)
    assert r["form"]["brand_name"] == "OLD CARTER"
    assert r["crops"][0]["kind"] == "front"
    assert r["warning"]["status"] == "near"
    assert r["escalation"] == ["brand_name: near_miss"]


def test_old_verdicts_without_source_round_trip(store):
    """Records written before source attribution existed lack the
    source/source_crop keys; the store serves them verbatim — the
    frontend treats the absent keys as unknown."""
    b = store.create_batch("batch")
    rid = store.add_record(b["id"], "a.pdf")
    store.record_done(
        rid,
        ttb_id="x",
        auto_status="Needs Review",
        form={},
        crops=[],
        verdicts=[{"field": "brand_name", "outcome": "near_miss"}],
        warning={"status": "near", "score": 97.0},
        escalation=[],
    )
    r = store.get_record(rid)
    assert "source" not in r["verdicts"][0]
    assert "source" not in r["warning"]


def test_list_unfinished_returns_only_inflight_states(store):
    b = store.create_batch("batch")
    rid_pending = store.add_record(b["id"], "a.pdf")
    rid_processing = store.add_record(b["id"], "b.pdf")
    store.record_processing(rid_processing)
    rid_done = store.add_record(b["id"], "c.pdf")
    _finish(store, rid_done, "Pass")
    rid_error = store.add_record(b["id"], "d.pdf")
    store.record_error(rid_error, "boom")

    unfinished = store.list_unfinished()
    assert {r["id"] for r in unfinished} == {rid_pending, rid_processing}
    assert all(r["batch_id"] == b["id"] for r in unfinished)
