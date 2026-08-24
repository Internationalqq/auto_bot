from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

from autobot import estimate_excel_analysis, pdf_estimate_adapter, web_ui


def test_pdf_adapter_reports_both_ocr_passes(monkeypatch) -> None:
    class FakeImage:
        shape = (100, 200, 3)

        def reshape(self, *_shape):
            return self

    class FakePixmap:
        samples = b""
        height = 100
        width = 200
        n = 3

    class FakePage:
        def get_pixmap(self, **_kwargs):
            return FakePixmap()

    class FakeDocument:
        def __len__(self):
            return 1

        def __iter__(self):
            return iter([FakePage()])

        def close(self):
            return None

    empty_ocr = {key: [] for key in ("text", "conf", "left", "top", "width", "height")}
    fake_pytesseract = SimpleNamespace(
        Output=SimpleNamespace(DICT="dict"),
        image_to_data=lambda *_args, **_kwargs: empty_ocr,
    )
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace(COLOR_RGB2BGR=1, cvtColor=lambda image, _mode: image))
    monkeypatch.setitem(sys.modules, "fitz", SimpleNamespace(Matrix=lambda *_args: None, open=lambda **_kwargs: FakeDocument()))
    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace(uint8="uint8", frombuffer=lambda *_args, **_kwargs: FakeImage()))
    monkeypatch.setitem(sys.modules, "pytesseract", fake_pytesseract)
    events: list[tuple[int, str, str]] = []

    records = pdf_estimate_adapter.PdfEstimateAdapter().to_position_records(
        b"%PDF-test",
        progress_cb=lambda percent, stage, detail: events.append((percent, stage, detail)),
    )

    assert records == []
    assert [30, 60, 90, 94, 100] == sorted(set(percent for percent, _, _ in events if percent))
    assert any("проход 1 из 2" in detail.casefold() for _, _, detail in events)
    assert any("проход 2 из 2" in detail.casefold() for _, _, detail in events)


def test_sparse_pdf_progress_is_mapped_into_upload_pipeline(monkeypatch) -> None:
    adapter_events = [
        (0, "Подготавливаю OCR PDF", "Страниц: 2"),
        (25, "OCR PDF: страница 1 из 2", "Проход 1 из 2"),
        (100, "Строки PDF собраны", "Найдено позиций: 1"),
    ]

    def fake_records(_path, *, progress_cb=None):
        for event in adapter_events:
            progress_cb(*event)
        return [
            {
                "name": "Устройство бетонной отмостки",
                "qty": 12.5,
                "unit": "м2",
                "code": "ГЭСН-01-01-001",
                "position": "1",
                "page": 1,
            }
        ]

    monkeypatch.setattr(pdf_estimate_adapter, "pdf_to_position_records", fake_records)
    pipeline_events: list[tuple[int, str, str]] = []

    rows = estimate_excel_analysis._read_pdf_sparse_position_rows(
        Path("estimate.pdf"),
        progress_cb=lambda percent, stage, detail: pipeline_events.append((percent, stage, detail)),
    )

    assert len(rows) == 1
    assert [event[0] for event in pipeline_events] == [38, 48, 77]
    assert pipeline_events[1][1] == "OCR PDF: страница 1 из 2"


def test_upload_job_state_survives_memory_reset(tmp_path, monkeypatch) -> None:
    job_id = "0123456789abcdef"
    jobs_dir = tmp_path / ".upload_jobs"
    monkeypatch.setattr(web_ui, "ESTIMATE_UPLOAD_JOBS_DIR", jobs_dir)
    job = {
        "job_id": job_id,
        "running": True,
        "ok": False,
        "progress": 43,
        "stage": "OCR PDF: страница 1 из 2",
        "detail": "Проход 1 из 2",
        "log_lines": [],
    }

    try:
        with web_ui.estimate_upload_lock:
            web_ui.estimate_upload_jobs[job_id] = job
            web_ui._estimate_upload_persist_locked(job)
            web_ui.estimate_upload_jobs.pop(job_id, None)
            restored = web_ui._estimate_upload_load_locked(job_id)

        assert restored is not None
        assert restored["progress"] == 43
        assert restored["stage"] == "OCR PDF: страница 1 из 2"
    finally:
        with web_ui.estimate_upload_lock:
            web_ui.estimate_upload_jobs.pop(job_id, None)
            web_ui.estimate_upload_workers.discard(job_id)


def test_upload_heartbeat_advances_long_ocr(tmp_path, monkeypatch) -> None:
    job_id = "fedcba9876543210"
    monkeypatch.setattr(web_ui, "ESTIMATE_UPLOAD_JOBS_DIR", tmp_path / ".upload_jobs")

    class OneBeat:
        calls = 0

        def wait(self, _seconds: float) -> bool:
            self.calls += 1
            return self.calls > 1

    try:
        with web_ui.estimate_upload_lock:
            web_ui.estimate_upload_jobs[job_id] = {
                "job_id": job_id,
                "running": True,
                "progress": 38,
                "log_lines": [],
            }

        web_ui._estimate_upload_heartbeat(job_id, OneBeat())

        with web_ui.estimate_upload_lock:
            job = dict(web_ui.estimate_upload_jobs[job_id])
        assert job["progress"] == 39
        assert job["progress_estimated"] is True
        assert job["elapsed_seconds"] >= 0
    finally:
        with web_ui.estimate_upload_lock:
            web_ui.estimate_upload_jobs.pop(job_id, None)
            web_ui.estimate_upload_workers.discard(job_id)


def test_status_endpoint_resumes_persisted_running_job(tmp_path, monkeypatch) -> None:
    job_id = "aaaabbbbccccdddd"
    monkeypatch.setattr(web_ui, "ESTIMATE_UPLOAD_JOBS_DIR", tmp_path / ".upload_jobs")
    recovery_calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        web_ui,
        "_start_estimate_upload_worker",
        lambda requested_job_id, *, recovering=False: recovery_calls.append((requested_job_id, recovering)) or True,
    )
    job = {
        "job_id": job_id,
        "running": True,
        "ok": False,
        "progress": 61,
        "progress_estimated": True,
        "stage": "OCR PDF: страница 1 из 2",
        "detail": "Распознаю строки",
        "elapsed_seconds": 84,
        "log_lines": [],
    }

    try:
        with web_ui.estimate_upload_lock:
            web_ui.estimate_upload_jobs[job_id] = job
            web_ui._estimate_upload_persist_locked(job)
            web_ui.estimate_upload_jobs.pop(job_id, None)

        response = web_ui.app.test_client().get(f"/api/estimates/upload-status/{job_id}")

        assert response.status_code == 200
        assert response.get_json()["progress"] == 61
        assert response.get_json()["elapsed_seconds"] == 84
        assert recovery_calls == [(job_id, True)]
    finally:
        with web_ui.estimate_upload_lock:
            web_ui.estimate_upload_jobs.pop(job_id, None)
            web_ui.estimate_upload_workers.discard(job_id)


def test_health_endpoint_is_lightweight() -> None:
    response = web_ui.app.test_client().get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "service": "autobot"}
