"""Artifact store: content sniffing, ownership, quotas, expiry and safe delivery classes."""
import base64
import hashlib
import struct
import zlib

import pytest

from odytor.artifacts import ArtifactLimits, ArtifactStore, sniff
from odytor.errors import ValidationError


def png(width=2, height=2, noise=False):
    def chunk(tag, body):
        return struct.pack('>I', len(body)) + tag + body + struct.pack('>I', zlib.crc32(tag + body))
    import random
    rng = random.Random(7)
    raw = b''.join(b'\x00' + (rng.randbytes(3 * width) if noise else b'\xff\x00\x00' * width) for _ in range(height))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')


class Clock:
    now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return ArtifactStore(tmp_path / 'art', ArtifactLimits(max_artifact_bytes=1 << 20, max_image_delivery_bytes=4096, max_owner_bytes=3 << 20,
                                                          max_total_bytes=5 << 20, retention_hours=1), clock=clock)


CTX = {'run_id': 'test-' + 'a' * 32, 'workspace_id': 'ws-' + 'b' * 32, 'workspace_version': 3, 'tested_tree': 'c' * 40,
       'source_revision': {'head_sha': 'd' * 40}}


def put(store, data, kind='log', name='out.txt', owner='alice'):
    return store.put(owner=owner, repo='o/r', kind=kind, name=name, data=data, context=CTX)


@pytest.mark.parametrize('data,mime,klass', [
    (png(), 'image/png', 'image'), (b'\xff\xd8\xff\xe0jfif', 'image/jpeg', 'image'), (b'RIFF\x00\x00\x00\x00WEBPVP8 ', 'image/webp', 'image'),
    (b'PK\x03\x04zip', 'application/zip', 'archive'), (b'plain text\nlog line\n', 'text/plain', 'text'),
    (b'{"a": 1}', 'text/plain', 'text'), (b'\x00\x01\x02', 'application/octet-stream', 'binary'),
    (b'<svg onload="alert(1)"></svg>', 'text/plain', 'active_text'), (b'<!DOCTYPE html><script>x</script>', 'text/plain', 'active_text'),
    (b'  <html><body>x</body></html>', 'text/plain', 'active_text'), (b'text with <script>alert(1)</script> inside', 'text/plain', 'active_text')])
def test_content_is_classified_by_bytes_never_by_claim(data, mime, klass):
    info = sniff(data)
    assert (info['mime'], info['class']) == (mime, klass)


def test_put_records_exact_provenance_and_integrity(store):
    data = png()
    record = put(store, data, kind='screenshot', name='after sort.png')
    assert record['sha256'] == hashlib.sha256(data).hexdigest() and record['size'] == len(data) and record['mime'] == 'image/png'
    assert record['owner'] == 'alice' and record['run_id'] == CTX['run_id'] and record['tested_tree'] == 'c' * 40
    public = store.public(record)
    assert public['delivery'] == 'native_image' and 'owner' not in public and 'path' not in str(public).lower()
    assert public['expires_at'] > public['created_at']


def test_a_screenshot_must_really_be_an_image_and_names_never_become_paths(store):
    with pytest.raises(ValidationError):
        put(store, b'<svg/>', kind='screenshot')
    record = put(store, b'hello', name='../../etc/passwd')
    assert record['name'] == 'artifact' and '/' not in record['artifact_id']
    assert not list(store.root.glob('**/passwd'))
    with pytest.raises(ValidationError):
        put(store, b'')


@pytest.mark.parametrize('bad', ['../x', 'ar-short', '/etc/passwd', 'ar-' + 'g' * 32, 'ar-' + '0' * 32])
def test_ids_are_opaque_and_unknown_ones_look_identical(store, bad):
    put(store, b'x')
    with pytest.raises(ValidationError) as info:
        store.get(bad, 'alice')
    assert str(info.value) == 'Unknown artifact_id.'


def test_other_callers_cannot_read_or_discover_artifacts(store):
    record = put(store, b'secret')
    for mode in ('auto', 'metadata', 'text', 'base64'):
        with pytest.raises(ValidationError) as info:
            store.content(record['artifact_id'], 'mallory', mode=mode)
        assert str(info.value) == 'Unknown artifact_id.'


def test_images_are_delivered_natively_within_the_limit_and_chunked_beyond_it(store):
    small = put(store, png(), kind='screenshot')
    out = store.content(small['artifact_id'], 'alice')
    assert out['image'][0] == 'image/png' and out['image'][1] == png() and 'base64' not in out
    big = put(store, png(64, 64, noise=True), kind='screenshot')  # larger than the 4096-byte native limit in this fixture
    assert big['size'] > 4096
    fallback = store.content(big['artifact_id'], 'alice')
    assert 'image' not in fallback and 'base64' in fallback['note'] and fallback['window']['returned'] == 0
    chunk = store.content(big['artifact_id'], 'alice', mode='base64')
    assert base64.b64decode(chunk['base64']) == png(64, 64, noise=True)[:chunk['window']['returned']] and chunk['window']['sha256_of_whole'] == big['sha256']


def test_text_is_windowed_and_active_text_is_labelled_untrusted(store):
    body = ('line\n' * 30000).encode()
    record = put(store, body)
    first = store.content(record['artifact_id'], 'alice')
    assert len(first['text']) == 65536 and first['window']['next_offset'] == 65536
    rest = store.content(record['artifact_id'], 'alice', offset=first['window']['next_offset'], length=10)
    assert rest['text'] == 'line\nline' or len(rest['text']) == 10
    active = put(store, b'<html><script>steal()</script></html>', name='page.html')
    out = store.content(active['artifact_id'], 'alice')
    assert out['meta']['untrusted_active_content'] is True and 'Untrusted' in out['warning'] and out['meta']['mime'] == 'text/plain'
    assert 'image' not in out and out['meta']['delivery'] == 'text_window_untrusted'


def test_archives_and_binaries_are_never_returned_as_text_or_images(store):
    trace = put(store, b'PK\x03\x04' + b'\x00' * 100, kind='trace', name='trace.zip')
    assert store.content(trace['artifact_id'], 'alice')['meta']['delivery'] == 'base64_chunks'
    for mode in ('text', 'image'):
        with pytest.raises(ValidationError):
            store.content(trace['artifact_id'], 'alice', mode=mode)
    assert store.content(trace['artifact_id'], 'alice', mode='metadata')['meta']['mime'] == 'application/zip'


def test_quotas_and_per_artifact_limit(store):
    with pytest.raises(ValidationError):
        put(store, b'x' * ((1 << 20) + 1))
    for _ in range(3):
        put(store, b'x' * (1 << 20))
    with pytest.raises(ValidationError):  # alice hit her 3 MiB quota
        put(store, b'x')
    assert put(store, b'x', owner='bob')['owner'] == 'bob'  # another caller is unaffected


def test_expiry_and_purge(store, clock):
    record = put(store, b'soon gone')
    clock.now += 3600 + 1
    with pytest.raises(ValidationError):
        store.get(record['artifact_id'], 'alice')
    assert not list(store.root.glob('ar-*'))  # the lazy purge on access removed it
    assert store.purge(force=True) == 0


def test_delete_for_run_and_restart_persistence(store, tmp_path, clock):
    record = put(store, b'kept')
    again = ArtifactStore(store.root, store.limits, clock=clock)
    assert again.content(record['artifact_id'], 'alice')['text'] == 'kept'
    assert again.delete_for_run(CTX['run_id']) == 1 and not list(again.root.glob('ar-*'))


def test_normalize_artifact_name_handles_extensions_and_duplicates():
    from odytor.artifacts import normalize_artifact_name
    assert normalize_artifact_name('screenshot.png.png', mime='image/png', kind='screenshot') == 'screenshot.png'
    assert normalize_artifact_name('screenshot', mime='image/png', kind='screenshot') == 'screenshot.png'
    assert normalize_artifact_name('screenshot.png', mime='image/png', kind='screenshot') == 'screenshot.png'
    assert normalize_artifact_name('screenshot.txt', mime='image/png', kind='screenshot') == 'screenshot.png'
    assert normalize_artifact_name('pic.jpg.jpg', mime='image/jpeg', kind='screenshot') == 'pic.jpg'
    assert normalize_artifact_name('../../etc/passwd') == 'artifact'
    assert normalize_artifact_name('valid_log.txt', mime='text/plain', kind='log') == 'valid_log.txt'
    # Test length clamping preserving extension
    long_name = 'a' * 100 + '.png'
    clamped = normalize_artifact_name(long_name, mime='image/png')
    assert len(clamped) <= 80 and clamped.endswith('.png')
