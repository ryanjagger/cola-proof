"""API tests: upload -> process -> review -> purge, against the real
pipeline on one easy sample."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SAMPLES = Path(__file__).parent.parent / "sample-forms" / "registry"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("data")
    from server import config

    config.settings.data_dir = data_dir
    # app builds its Store at import time; import after pointing DATA_DIR
    # at the test dir.
    from server import app as app_module

    app_module.store = app_module.Store(
        config.settings.db_path, config.settings.media_dir
    )
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture(scope="module")
def processed_batch(client):
    pdf = (SAMPLES / "12207001000539.pdf").read_bytes()
    resp = client.post(
        "/api/batches",
        files=[("files", ("12207001000539.pdf", pdf, "application/pdf"))],
    )
    assert resp.status_code == 200
    batch_id = resp.json()["batch"]["id"]
    deadline = time.time() + 120
    while time.time() < deadline:
        summary = client.get(f"/api/batches/{batch_id}").json()["summary"]
        if summary["processed"] == summary["total"]:
            return batch_id
        time.sleep(0.5)
    pytest.fail("batch did not finish processing")


def test_record_processed_and_auto_approved(client, processed_batch):
    records = client.get(f"/api/batches/{processed_batch}/records").json()
    assert len(records) == 1
    r = records[0]
    assert r["state"] == "done"
    assert r["ttb_id"] == "12207001000539"
    assert r["auto_status"] == "Pass"
    assert r["disposition"] == "Approved"
    assert r["dispositioned_by"] == "system"
    assert r["form"]["brand_name"] == "VIEJO TONEL"
    assert {v["field"] for v in r["verdicts"]} == {
        "brand_name", "net_contents", "alcohol_content", "class_type",
    }
    assert r["warning"]["status"] == "exact"


def test_crop_served(client, processed_batch):
    records = client.get(f"/api/batches/{processed_batch}/records").json()
    r = records[0]
    assert len(r["crops"]) == 2
    resp = client.get(f"/api/records/{r['id']}/crops/0")
    assert resp.status_code == 200
    assert resp.headers["content-type"] in ("image/jpeg", "image/png")
    assert len(resp.content) > 1000


def test_source_pdf_served_inline(client, processed_batch):
    records = client.get(f"/api/batches/{processed_batch}/records").json()
    r = records[0]
    resp = client.get(f"/api/records/{r['id']}/pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"].startswith("inline")
    assert resp.content[:5] == b"%PDF-"


def test_disposition_roundtrip(client, processed_batch):
    records = client.get(f"/api/batches/{processed_batch}/records").json()
    rid = records[0]["id"]
    resp = client.post(
        f"/api/records/{rid}/disposition",
        json={"disposition": "Rejected", "by": "agent.jones", "note": "check art"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["disposition"] == "Rejected"
    assert body["auto_status"] == "Pass"  # audit split preserved

    resp = client.post(
        f"/api/records/{rid}/disposition",
        json={"disposition": "Approved", "by": "system"},
    )
    assert resp.status_code == 422  # system can never disposition


def test_sse_events_stream(client, processed_batch):
    with client.stream(
        "GET", f"/api/batches/{processed_batch}/events"
    ) as resp:
        assert resp.status_code == 200
        body = ""
        for chunk in resp.iter_text():
            body += chunk
            if "event: done" in body:
                break
    assert "event: record" in body
    assert '"ttb_id": "12207001000539"' in body


def test_delete_purges_media(client, processed_batch):
    from server import app as app_module

    media = app_module.store.batch_media_dir(processed_batch)
    assert media.exists()
    resp = client.delete(f"/api/batches/{processed_batch}")
    assert resp.status_code == 200
    assert not media.exists()
    assert client.get(f"/api/batches/{processed_batch}").status_code == 404


def test_restart_recovers_inflight_records(client):
    """A record stranded 'processing' by a restart is re-enqueued on
    startup and finishes; without recovery it would hang forever."""
    from fastapi.testclient import TestClient

    from server import app as app_module

    pdf = (SAMPLES / "12207001000539.pdf").read_bytes()
    resp = client.post(
        "/api/batches",
        files=[("files", ("12207001000539.pdf", pdf, "application/pdf"))],
    )
    batch_id = resp.json()["batch"]["id"]
    record_id = resp.json()["record_ids"][0]
    deadline = time.time() + 120
    while time.time() < deadline:
        if client.get(f"/api/records/{record_id}").json()["state"] == "done":
            break
        time.sleep(0.5)

    # Simulate the restart having caught it mid-flight.
    with app_module.store._conn() as c:
        c.execute(
            "UPDATE records SET state='processing', auto_status=NULL WHERE id=?",
            (record_id,),
        )

    # A fresh client context re-runs the startup hook on the same store.
    with TestClient(app_module.app) as restarted:
        deadline = time.time() + 120
        while time.time() < deadline:
            r = restarted.get(f"/api/records/{record_id}").json()
            if r["state"] == "done":
                assert r["auto_status"] == "Pass"
                break
            time.sleep(0.5)
        else:
            pytest.fail("orphaned record was not recovered on startup")

    client.delete(f"/api/batches/{batch_id}")
