"""Regression tests for strict native cron job compare-and-swap guards."""

import json

import cron.jobs as jobs


def _isolated_store(tmp_path, monkeypatch):
    cron_dir = tmp_path / "cron"
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", cron_dir / "output")


def test_expected_cas_rejects_bool_integer_alias(tmp_path, monkeypatch):
    _isolated_store(tmp_path, monkeypatch)
    job = jobs.create_job(prompt="strict CAS", schedule="every 1h")

    assert jobs.update_job(
        job["id"],
        {"state": "paused"},
        expected={"enabled": 1},
    ) is None
    fetched = jobs.get_job(job["id"])
    assert fetched is not None
    assert fetched["enabled"] is True


def test_expected_cas_rejects_absent_nullable_field(tmp_path, monkeypatch):
    _isolated_store(tmp_path, monkeypatch)
    job = jobs.create_job(prompt="strict nullable CAS", schedule="every 1h")
    document = json.loads(jobs.JOBS_FILE.read_text(encoding="utf-8"))
    records = document["jobs"] if isinstance(document, dict) else document
    record = next(item for item in records if item["id"] == job["id"])
    del record["paused_reason"]
    jobs.JOBS_FILE.write_text(json.dumps(document), encoding="utf-8")

    assert jobs.update_job(
        job["id"],
        {"state": "paused"},
        expected={"paused_reason": None},
    ) is None
    stored_doc = json.loads(jobs.JOBS_FILE.read_text(encoding="utf-8"))
    stored_records = stored_doc["jobs"] if isinstance(stored_doc, dict) else stored_doc
    stored = next(item for item in stored_records if item["id"] == job["id"])
    assert "paused_reason" not in stored


def test_expected_cas_accepts_exact_typed_image(tmp_path, monkeypatch):
    _isolated_store(tmp_path, monkeypatch)
    job = jobs.create_job(prompt="strict valid CAS", schedule="every 1h")

    updated = jobs.update_job(
        job["id"],
        {"enabled": False, "state": "paused"},
        expected={"enabled": True, "state": "scheduled"},
    )
    assert updated is not None
    assert updated["state"] == "paused"
