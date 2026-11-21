"""Heavy upload extraction is bounded before source reads and mutation guards."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
import guest_uploads


def assert_available_from_another_thread(lock):
    def probe():
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        return acquired
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(probe).result(timeout=5)


def test_ten_worker_entries_share_one_heavy_allocation_and_guard(monkeypatch):
    lock = threading.Lock()
    slot = guest_uploads._EXTRACTION_LOCK
    attempted = threading.Event()
    heavy_entered = threading.Event()
    release = threading.Event()
    state = {'attempts': 0, 'guards': 0, 'peak_guards': 0, 'bytes': 0, 'peak_bytes': 0}
    completed = []

    class ObservedSlot:
        def __enter__(self):
            with lock:
                state['attempts'] += 1
                if state['attempts'] == 10:
                    attempted.set()
            slot.acquire()

        def __exit__(self, *error):
            slot.release()

    @contextmanager
    def guard():
        with lock:
            state['guards'] += 1
            state['peak_guards'] = max(state['peak_guards'], state['guards'])
        try:
            yield None
        finally:
            with lock:
                state['guards'] -= 1

    def heavy(tenant, drawing, extension):
        allocation = bytearray(256 * 1024)
        with lock:
            state['bytes'] += len(allocation)
            state['peak_bytes'] = max(state['peak_bytes'], state['bytes'])
        heavy_entered.set()
        try:
            assert release.wait(timeout=15)
            with lock:
                completed.append((tenant, drawing, extension))
        finally:
            with lock:
                state['bytes'] -= len(allocation)

    monkeypatch.setattr(guest_uploads, '_EXTRACTION_LOCK', ObservedSlot())
    monkeypatch.setattr(guest_uploads.write_loop, 'upload_mutation_refusal_guard', guard)
    monkeypatch.setattr(guest_uploads, '_run_extraction', heavy)
    inputs = [(f'tenant{i % 2}', f'drawing{i}', '.dwg') for i in range(10)]
    workers = []
    try:
        workers = [guest_uploads.start_extraction_thread(*item) for item in inputs]
        assert attempted.wait(timeout=10) and heavy_entered.wait(timeout=10)
        with lock:
            assert state['guards'] == 1 and state['bytes'] == 256 * 1024
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=15)
    assert all(not worker.is_alive() for worker in workers)
    assert sorted(completed) == sorted(inputs)
    assert state['peak_guards'] == 1 and state['peak_bytes'] == 256 * 1024
    assert state['guards'] == 0 and state['bytes'] == 0


@pytest.mark.parametrize('failure', ['guard', 'body'])
def test_extraction_slot_released_after_exception(failure, monkeypatch):
    slot = threading.RLock()
    error = RuntimeError('synthetic worker failure')

    @contextmanager
    def guard():
        if failure == 'guard':
            raise error
        yield None

    def body(*args):
        raise error

    monkeypatch.setattr(guest_uploads, '_EXTRACTION_LOCK', slot)
    monkeypatch.setattr(guest_uploads.write_loop, 'upload_mutation_refusal_guard', guard)
    monkeypatch.setattr(guest_uploads, '_run_extraction', body)
    with pytest.raises(RuntimeError) as caught:
        guest_uploads.run_extraction('tenant', 'drawing', '.dwg')
    assert caught.value is error
    assert_available_from_another_thread(slot)


def test_fence_refusal_releases_slot_without_starting_heavy_work(monkeypatch):
    slot = threading.RLock()
    refusal = object()

    @contextmanager
    def guard():
        yield refusal

    def forbidden(*args):
        raise AssertionError('Refused extraction cannot read or parse source')

    monkeypatch.setattr(guest_uploads, '_EXTRACTION_LOCK', slot)
    monkeypatch.setattr(guest_uploads.write_loop, 'upload_mutation_refusal_guard', guard)
    monkeypatch.setattr(guest_uploads.write_loop, 'log_mutation_refused', lambda *args, **kwargs: None)
    monkeypatch.setattr(guest_uploads, '_run_extraction', forbidden)
    guest_uploads.run_extraction('tenant', 'drawing', '.dwg')
    assert_available_from_another_thread(slot)


def test_synchronous_retry_reenters_then_releases_worker_lock(monkeypatch):
    slot = threading.RLock()
    visits = []

    @contextmanager
    def guard():
        yield None

    def body(tenant, drawing, extension):
        visits.append(drawing)
        if drawing == 'first':
            guest_uploads.run_extraction(tenant, 'retry', extension)

    monkeypatch.setattr(guest_uploads, '_EXTRACTION_LOCK', slot)
    monkeypatch.setattr(guest_uploads.write_loop, 'upload_mutation_refusal_guard', guard)
    monkeypatch.setattr(guest_uploads, '_run_extraction', body)
    guest_uploads.run_extraction('tenant', 'first', '.dxf')
    assert visits == ['first', 'retry']
    assert_available_from_another_thread(slot)


@pytest.mark.parametrize('status', ['ready', 'failed'])
def test_queued_terminal_attempt_stops_before_source_read(status, monkeypatch):
    monkeypatch.setattr(guest_uploads, 'upload_store_mode', lambda: 'legacy')
    monkeypatch.setattr(guest_uploads.write_loop, 'upload_backend_for_tenant', lambda tenant: object())
    monkeypatch.setattr(guest_uploads.write_loop, 'fence_refusal', lambda: None)
    monkeypatch.setattr(guest_uploads, 'read_marker', lambda *args: {'status': status})

    def forbidden(*args):
        raise AssertionError('A terminal queued attempt cannot read source bytes')

    monkeypatch.setattr(guest_uploads, '_verify_staged_source', forbidden)
    guest_uploads._run_extraction('tenant', 'drawing', '.dwg')
