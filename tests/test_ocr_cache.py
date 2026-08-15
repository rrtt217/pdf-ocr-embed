"""OCR cache: keying, round-trip, expiry, corruption, and service wiring."""
from __future__ import annotations

import os
import time
from pathlib import Path

from backend import ocr_cache as cache
from backend import ocr_service
from backend.models import OcrBlock, OcrPage


CFG = {"ocr_cache_enabled": "true", "ocr_cache_max_age_hours": "720"}


def _use_tmp_entry_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, 'ENTRY_DIR', tmp_path / 'cache' / 'ocr')
    monkeypatch.setattr(cache, 'resolve', lambda: dict(CFG))


def test_build_key_deterministic_and_sensitive():
    ctx = {'pdf_sha': 'a', 'page': 1, 'adapter': {'model': 'm'}}
    k1 = cache.build_key(ctx)
    k2 = cache.build_key(dict(ctx))
    assert k1 == k2 and len(k1) == 64
    assert k1 != cache.build_key({**ctx, 'page': 2})
    assert k1 != cache.build_key({**ctx, 'adapter': {'model': 'x'}})


def test_put_get_roundtrip(monkeypatch, tmp_path):
    _use_tmp_entry_dir(monkeypatch, tmp_path)
    page = OcrPage(0, 100, 200, [OcrBlock('text', [1, 2, 3, 4], '你好',
                                           conf=0.9)]).to_dict()
    cache.put_page(cache.build_key({'pdf_sha': 'x', 'page': 0}), page)
    assert cache.get_page(cache.build_key({'pdf_sha': 'x', 'page': 0})) == page
    assert cache.status()['entries'] == 1


def test_missing_key_is_a_miss(monkeypatch, tmp_path):
    _use_tmp_entry_dir(monkeypatch, tmp_path)
    assert cache.get_page('f' * 64) is None


def test_corrupt_entry_is_miss_and_removed(monkeypatch, tmp_path):
    _use_tmp_entry_dir(monkeypatch, tmp_path)
    key = 'a' * 64
    p = cache._entry_path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{not json', encoding='utf-8')
    assert cache.get_page(key) is None
    assert not p.exists()


def test_expired_entry_is_miss_and_removed(monkeypatch, tmp_path):
    _use_tmp_entry_dir(monkeypatch, tmp_path)
    key = cache.build_key({'pdf_sha': 'e', 'page': 0})
    cache.put_page(key, {'page_index': 0, 'width': 1, 'height': 1,
                        'blocks': []})
    os.utime(cache._entry_path(key), (time.time() - 100 * 24 * 3600,) * 2)
    assert cache.get_page(key) is None
    assert not cache._entry_path(key).exists()


def test_disabled_cache_is_transparent_noop(monkeypatch, tmp_path):
    _use_tmp_entry_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(cache, 'resolve',
                        lambda: {'ocr_cache_enabled': 'false'})
    key = cache.build_key({'pdf_sha': 'd', 'page': 0})
    cache.put_page(key, {'x': 1})
    assert cache.get_page(key) is None
    assert cache.status()['enabled'] is False
    assert not cache.ENTRY_DIR.exists()


def test_clear_removes_everything(monkeypatch, tmp_path):
    _use_tmp_entry_dir(monkeypatch, tmp_path)
    for i in range(3):
        cache.put_page(cache.build_key({'pdf_sha': str(i), 'page': 0}),
                       {'page_index': i})
    assert cache.status()['entries'] == 3
    result = cache.clear()
    assert result['removed'] == 3 and cache.status()['entries'] == 0


def test_purge_expired_only_touches_stale(monkeypatch, tmp_path):
    _use_tmp_entry_dir(monkeypatch, tmp_path)
    fresh = cache.build_key({'pdf_sha': 'f', 'page': 0})
    stale = cache.build_key({'pdf_sha': 's', 'page': 0})
    cache.put_page(fresh, {'page_index': 1})
    cache.put_page(stale, {'page_index': 2})
    os.utime(cache._entry_path(stale), (time.time() - 999 * 3600,) * 2)
    result = cache.purge_expired()
    assert result['removed'] == 1
    assert cache.get_page(fresh) is not None
    assert cache.get_page(stale) is None


def test_setting_parsers():
    assert cache._num('24', 720.0) == 24.0
    assert cache._num('junk', 720.0) == 720.0
    assert cache._num('0', 720.0) == 720.0     # <=0 falls back
    assert cache._truthy('True') and cache._truthy('1') and cache._truthy('on')
    assert not cache._truthy('false') and not cache._truthy('OFF')
    assert not cache._truthy('')


class CountingAdapter:
    # Dep-free fake engine: counts calls, returns distinguishable pages.
    name = 'fake'

    def __init__(self, tag):
        self.tag = tag
        self.calls = 0

    def cache_fingerprint(self):
        return {'fake': True, 'tag': self.tag}

    def recognize_pixels(self, image_path, width, height, page_index):
        self.calls += 1
        return OcrPage(page_index=page_index, width=width, height=height,
                       blocks=[OcrBlock(kind='text', bbox=[0, 0, 10, 10],
                                        text=self.tag + '-' + str(page_index),
                                        conf=0.9)])

    def close(self):
        pass


def test_recognize_page_hits_cache_second_time(monkeypatch, tmp_path):
    _use_tmp_entry_dir(monkeypatch, tmp_path)
    job = {'id': 'j1', 'pdf_sha256': 'abc'}
    spec = {'page_index': 0, 'img_path': 'x.png', 'w': 100, 'h': 200}

    adapter = CountingAdapter('A')
    p1 = ocr_service._recognize_page(job, adapter, spec)
    p2 = ocr_service._recognize_page(job, adapter, spec)
    assert adapter.calls == 1                       # second serve was a hit
    assert p1.blocks[0].text == p2.blocks[0].text
    assert cache.status()['entries'] == 1


def test_recognize_page_misses_on_changed_settings(monkeypatch, tmp_path):
    _use_tmp_entry_dir(monkeypatch, tmp_path)
    job = {'id': 'j2', 'pdf_sha256': 'abc'}
    spec = {'page_index': 1, 'img_path': 'x.png', 'w': 100, 'h': 200}

    other = CountingAdapter('B')
    ocr_service._recognize_page(job, other, spec)
    ocr_service._recognize_page(job, other, spec)
    assert other.calls == 1


def test_recognize_page_page_index_isolates_entries(monkeypatch, tmp_path):
    _use_tmp_entry_dir(monkeypatch, tmp_path)
    job = {'id': 'j3', 'pdf_sha256': 'abc'}
    adapter = CountingAdapter('C')
    for i in range(3):
        spec = {'page_index': i, 'img_path': 'x.png', 'w': 100, 'h': 200}
        ocr_service._recognize_page(job, adapter, spec)
    assert adapter.calls == 3                         # 3 distinct pages
    assert cache.status()['entries'] == 3
    # re-running page 1 only hits the cache for that page
    spec1 = {'page_index': 1, 'img_path': 'x.png', 'w': 100, 'h': 200}
    ocr_service._recognize_page(job, adapter, spec1)
    assert adapter.calls == 3
    assert cache.status()['hits'] >= 1

def test_page_cache_key_stable_and_sensitive():
    job = {'pdf_sha256': 'abc'}
    fp = {'model': 'm', 'api_key_sha': 'x'}
    k1 = ocr_service._page_cache_key(job, fp, 0)
    k2 = ocr_service._page_cache_key(job, dict(fp), 0)
    assert k1 == k2 and len(k1) == 64
    # any semantic change must miss
    assert k1 != ocr_service._page_cache_key(job, fp, 1)
    assert k1 != ocr_service._page_cache_key({'pdf_sha256': 'other'}, fp, 0)
    assert k1 != ocr_service._page_cache_key(job, {'model': 'other'}, 0)


def test_cache_precheck_splits_hits_and_misses(monkeypatch, tmp_path):
    _use_tmp_entry_dir(monkeypatch, tmp_path)
    job = {'id': 'j4', 'pdf_sha256': 'abc'}
    fp = {'tag': 'A'}
    seed = OcrPage(0, 10, 10, []).to_dict()
    for i in (0, 2):
        cache.put_page(ocr_service._page_cache_key(job, fp, i), seed)
    hits_before = cache.status()['hits']
    misses_before = cache.status()['misses']
    hits, misses = ocr_service._cache_precheck(job, fp, [0, 1, 2, 3])
    assert set(hits) == {0, 2} and misses == [1, 3]
    assert cache.status()['hits'] == hits_before + 2
    # precheck probes must not pollute the miss counter (double counting)
    assert cache.status()['misses'] == misses_before


def test_recognize_page_prefers_explicit_fingerprint(monkeypatch, tmp_path):
    _use_tmp_entry_dir(monkeypatch, tmp_path)
    job = {'id': 'j5', 'pdf_sha256': 'abc'}
    spec = {'page_index': 0, 'img_path': 'x.png', 'w': 100, 'h': 200}

    # Fingerprint computed once per run must win over per-instance values,
    # otherwise parallel workers could key differently and miss the cache.
    a1 = CountingAdapter('A')
    p1 = ocr_service._recognize_page(job, a1, spec, fingerprint={'tag': 'Z'})
    assert a1.calls == 1
    a2 = CountingAdapter('WRONG')  # its own fingerprint would miss;
    p2 = ocr_service._recognize_page(job, a2, spec, fingerprint={'tag': 'Z'})
    assert a2.calls == 0           # the explicit fingerprint still hit
    assert p1.blocks[0].text == p2.blocks[0].text


def test_run_ocr_second_job_served_from_cache_without_rendering(
        monkeypatch, tmp_path):
    _use_tmp_entry_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(ocr_service, 'WORK_DIR', tmp_path / 'work')
    import fitz

    doc = fitz.open()
    for _ in range(3):
        page = doc.new_page(width=200, height=200)
        page.insert_text(fitz.Point(50, 100), 'hello smoke page')
    pdf_path = tmp_path / 'doc.pdf'
    doc.save(str(pdf_path))
    doc.close()
    pdf_bytes = pdf_path.read_bytes()

    adapter = CountingAdapter('A')
    monkeypatch.setattr(ocr_service, '_make_adapter',
                        lambda *a, **k: adapter)

    # -- first upload: renders + engine calls for every page --
    job1 = ocr_service.create_job('doc.pdf', pdf_bytes)
    ocr_service.run_ocr(job1['id'], 'fake', None, 2)
    assert job1['status'] == 'done'
    assert sum(1 for p in job1['pages'] if p) == 3
    assert adapter.calls == 3
    ev1 = ocr_service.drain_events(job1['id'])
    phases1 = [e.get('phase') for e in ev1 if e.get('type') == 'progress']
    assert 'render' in phases1 and 'ocr' in phases1   # both phases on SSE
    img_dir1 = Path(job1['img_dir'])
    assert (img_dir1 / 'page_0000.png').exists()
    assert not list(img_dir1.glob('.page_*.tmp'))     # atomic write, no temp

    # -- second upload of the same PDF: zero renders, zero engine calls --
    job2 = ocr_service.create_job('doc.pdf', pdf_bytes)
    ocr_service.run_ocr(job2['id'], 'fake', None, 2)
    assert job2['status'] == 'done'
    assert adapter.calls == 3                         # nothing ran again
    ev2 = ocr_service.drain_events(job2['id'])
    phases2 = [e.get('phase') for e in ev2 if e.get('type') == 'progress']
    assert 'render' not in phases2                    # skipped entirely
    cached = [e for e in ev2 if e.get('cached')]
    assert len(cached) == 3                           # 3 cache-hit markers
    img_dir2 = Path(job2['img_dir'])
    assert not (img_dir2 / 'page_0000.png').exists()  # not rendered yet

    # -- the preview is rendered lazily on first view --
    p = ocr_service.ensure_page_image(job2['id'], 0)
    assert p is not None and Path(p).exists()
    assert (img_dir2 / 'page_0000.png').exists()
    assert ocr_service.ensure_page_image(job2['id'], 99) is None

    ocr_service.clear_job(job1['id'])
    ocr_service.clear_job(job2['id'])
