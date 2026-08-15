"""OCR cache: keying, round-trip, expiry, corruption, and service wiring."""
from __future__ import annotations

import os
import time

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
