"""Job persistence: work/<job>/job.json survives restarts and resumes cleanly."""
from __future__ import annotations

import json

import fitz

from backend import ocr_service
from backend import pdf_processing
from backend.models import OcrBlock, OcrPage


def _use_tmp_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr_service, 'WORK_DIR', tmp_path / 'work')
    monkeypatch.setattr(ocr_service, 'UPLOAD_DIR', tmp_path / 'uploads')
    monkeypatch.setattr(pdf_processing, 'OUTPUT_DIR', tmp_path / 'output')


def _simulate_restart():
    """Drop all in-memory state as if the process just restarted."""
    ocr_service._JOBS.clear()
    ocr_service._STREAMS.clear()


def _state_of(job):
    return (ocr_service._state_path(job)).read_text(encoding='utf-8')


def test_create_job_persists_state(monkeypatch, tmp_path):
    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('doc.pdf', b'%PDF-fake-bytes')
    data = json.loads(_state_of(job))
    assert data['id'] == job['id'] and data['filename'] == 'doc.pdf'
    assert data['pages'] == [] and data['status'] == 'uploaded'
    assert (ocr_service._state_path(job)).exists()


def test_update_page_persists_and_restore_roundtrip(monkeypatch, tmp_path):
    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('doc.pdf', b'%PDF-fake-bytes')
    p0 = {'page_index': 0, 'width': 10, 'height': 10, 'blocks': []}
    p2 = {'page_index': 2, 'width': 10, 'height': 10, 'blocks': []}
    ocr_service.update_page(job['id'], 0, p0)
    ocr_service.update_page(job['id'], 2, p2)
    ocr_service._set(job, status='stopped', num_pages=3)

    _simulate_restart()
    assert ocr_service.restore_jobs() == 1

    revived = ocr_service.get_job(job['id'])
    assert revived is not None
    assert revived['status'] == 'stopped'
    assert [p is not None for p in revived['pages']] == [True, False, True]
    assert revived['current'] == 2 and revived['num_pages'] == 3
    assert revived['pages'][0]['page_index'] == 0
    assert revived['pages'][2]['page_index'] == 2
    assert hasattr(revived['cancel_event'], 'set')   # rebuilt, not serialized


def test_restore_normalizes_crashed_status(monkeypatch, tmp_path):
    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('doc.pdf', b'%PDF-fake-bytes')
    ocr_service._set(job, status='running', num_pages=5)
    _simulate_restart()
    ocr_service.restore_jobs()
    assert ocr_service.get_job(job["id"])["status"] == "stopped"


def test_restore_skips_corrupt_and_missing_pdf(monkeypatch, tmp_path):
    _use_tmp_dirs(monkeypatch, tmp_path)
    ok = ocr_service.create_job('ok.pdf', b'%PDF-fake-bytes')

    # corrupt state
    bad_dir = tmp_path / 'work' / 'bad000000000'
    bad_dir.mkdir(parents=True)
    (bad_dir / 'job.json').write_text('{not valid json', encoding='utf-8')
    # valid state but the source PDF vanished
    gone_dir = tmp_path / 'work' / 'bad000000001'
    gone_dir.mkdir(parents=True)
    (gone_dir / 'job.json').write_text(json.dumps({
        'id': 'bad000000001', 'filename': 'gone.pdf',
        'pdf_path': str(gone_dir / 'gone.pdf'),
        'img_dir': str(gone_dir), 'pages': [], 'status': 'done',
    }), encoding='utf-8')

    _simulate_restart()
    assert ocr_service.restore_jobs() == 1
    assert ocr_service.get_job(ok['id']) is not None
    assert ocr_service.get_job('bad000000000') is None
    assert ocr_service.get_job('bad000000001') is None


def test_clear_job_removes_state_file(monkeypatch, tmp_path):
    _use_tmp_dirs(monkeypatch, tmp_path)
    job = ocr_service.create_job('doc.pdf', b'%PDF-fake-bytes')
    state = ocr_service._state_path(job)
    assert state.exists()
    assert ocr_service.clear_job(job['id']) is True
    assert not state.exists() and not state.parent.exists()
    assert ocr_service.get_job(job['id']) is None


def test_embed_persists_embedded_path(monkeypatch, tmp_path):
    _use_tmp_dirs(monkeypatch, tmp_path)
    doc = fitz.open()
    page = doc.new_page(width=200, height=200)
    page.insert_text(fitz.Point(20, 100), 'persist me')
    pdf_bytes = doc.tobytes()
    doc.close()

    job = ocr_service.create_job('real.pdf', pdf_bytes)
    ocr_page = OcrPage(page_index=0, width=200, height=200, blocks=[
        OcrBlock(kind='text', bbox=[10, 90, 100, 110], text='persist me')])
    out_path, stats = ocr_service.embed_job(job['id'], [ocr_page.to_dict()])
    assert out_path.exists()

    data = json.loads(_state_of(job))
    assert data['status'] == 'embedded'
    assert data['embedded_path'] == str(out_path)
    assert data['thumb_path'] and __import__('pathlib').Path(data['thumb_path']).exists()
    _simulate_restart()
    ocr_service.restore_jobs()
    revived = ocr_service.get_job(job['id'])
    assert revived['status'] == 'embedded'
    assert revived['embedded_path'] == str(out_path)


def test_snapshot_keys_exclude_runtime_objects():
    job = {'id': 'x', 'pdf_path': 'p', 'img_dir': 'd', 'pages': [],
           'status': 'done', 'cancel_event': object()}
    snap = ocr_service._snapshot(job)
    assert 'cancel_event' not in snap
    assert snap['id'] == 'x' and snap['status'] == 'done'


def test_batched_pages_split_on_request_failure(monkeypatch, tmp_path):
    """A failing multi-page request (e.g. a read timeout) is split in half
    and retried; only single pages that still fail are marked failed."""
    import threading
    from collections import deque
    from backend.models import OcrBlock, OcrPage as OP

    _use_tmp_dirs(monkeypatch, tmp_path)

    class _FakeAdapter:
        max_batch_pages = 4
        def __init__(self):
            self.calls = 0
        def recognize_pages(self, specs):
            self.calls += 1
            if len(specs) > 1:
                raise RuntimeError("simulated batch timeout")
            s = specs[0]
            return [OP(page_index=s.page_index, width=s.width, height=s.height,
                       blocks=[OcrBlock(kind="text", bbox=[0, 0, 10, 10],
                                        text=f"p{s.page_index}")])]

    adapter = _FakeAdapter()
    job = {"id": "split-j", "current": 0, "pages": [],
           "status": "running", "adapter": "unlimited",
           "img_dir": str(tmp_path / "work" / "split-j"),
           "num_pages": 3, "error": None}
    ocr_service._JOBS[job["id"]] = job
    ocr_service._STREAMS[job["id"]] = deque(maxlen=1000)
    specs = [{"img_path": "x.png", "w": 100, "h": 100, "page_index": i}
             for i in range(3)]
    ocr_service._ocr_pages_batched(
        job, job["id"], adapter, specs, 3, threading.Event(), None)
    assert adapter.calls >= 5  # 3+2 then three singles
    assert all(job["pages"][i] is not None for i in range(3))
    assert job["pages"][0]["blocks"][0]["text"] == "p0"
    assert job["pages"][2]["blocks"][0]["text"] == "p2"
    assert job["current"] == 3


def test_batched_pages_run_in_parallel(monkeypatch, tmp_path):
    """concurrency>1 must run several multi-page batches at the same time."""
    import threading
    import time
    from collections import deque
    from backend.models import OcrBlock, OcrPage as OP

    _use_tmp_dirs(monkeypatch, tmp_path)
    inflight: list = [0]
    peak: list = [0]
    lock = threading.Lock()

    class _A:
        max_batch_pages = 2
        def recognize_pages(self, specs):
            with lock:
                inflight[0] += 1
                peak[0] = max(peak[0], inflight[0])
            try:
                time.sleep(0.2)
            finally:
                with lock:
                    inflight[0] -= 1
            return [OP(page_index=s.page_index, width=s.width, height=s.height,
                       blocks=[OcrBlock(kind="text", bbox=[0, 0, 10, 10],
                                        text=f"p{s.page_index}")])
                    for s in specs]

    adapter = _A()
    job = {"id": "par-b", "current": 0, "pages": [],
           "status": "running", "adapter": "unlimited",
           "img_dir": str(tmp_path / "work" / "par-b"),
           "num_pages": 4, "error": None}
    ocr_service._JOBS[job["id"]] = job
    ocr_service._STREAMS[job["id"]] = deque(maxlen=1000)
    specs = [{"img_path": "x.png", "w": 100, "h": 100, "page_index": i}
             for i in range(4)]
    ocr_service._ocr_pages_batched(job, job["id"], adapter, specs, 4,
                                   threading.Event(), None, concurrency=2)
    assert peak[0] >= 2, f"expected overlapping batches, peak concurrency={peak[0]}"
    assert all(job["pages"][i] is not None for i in range(4))


def test_batched_inflight_cap_clamps_batch_size(monkeypatch, tmp_path):
    """batch x concurrency beyond max_inflight_pages clamps the batch size
    (parallelism kept) instead of letting a huge number of pages go in flight."""
    import threading
    from collections import deque
    from backend.models import OcrBlock, OcrPage as OP

    _use_tmp_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr("backend.config.resolve",
                        lambda: {"max_inflight_pages": "4"})
    sizes: list = []

    class _A:
        max_batch_pages = 10
        def recognize_pages(self, specs):
            sizes.append(len(specs))
            return [OP(page_index=s.page_index, width=s.width, height=s.height,
                       blocks=[OcrBlock(kind="text", bbox=[0, 0, 10, 10],
                                        text="x")])
                    for s in specs]

    adapter = _A()
    job = {"id": "cap-b", "current": 0, "pages": [],
           "status": "running", "adapter": "unlimited",
           "img_dir": str(tmp_path / "work" / "cap-b"),
           "num_pages": 5, "error": None}
    ocr_service._JOBS[job["id"]] = job
    ocr_service._STREAMS[job["id"]] = deque(maxlen=1000)
    specs = [{"img_path": "x.png", "w": 100, "h": 100, "page_index": i}
             for i in range(5)]
    ocr_service._ocr_pages_batched(job, job["id"], adapter, specs, 5,
                                   threading.Event(), None, concurrency=2)
    # batch=10 x workers=2 = 20 > 4 -> batch clamped to 4//2 = 2 pages:
    # groups of [2, 2, 1]
    assert sorted(sizes) == [1, 2, 2]
    assert all(job["pages"][i] is not None for i in range(5))
