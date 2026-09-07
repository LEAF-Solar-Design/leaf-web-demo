"""Canonical release recovery uses current authority and bounded process ownership."""
from types import SimpleNamespace
from unittest.mock import Mock
import logging
import threading

import pytest

import campaign_release_worker as worker


def row(name, status='active', wait_kind=None):
    return dict(org_id='org', project_id='project', campaign_id='campaign-' + name,
                release_id=name, status=status,
                next_action={'wait_kind': wait_kind} if wait_kind else None)


def runtime(rows):
    store = SimpleNamespace(runnable_releases=Mock(return_value=rows))
    return SimpleNamespace(_store=lambda: store, actor_for_release=Mock(return_value='current-actor'),
                           advance=Mock(), transition=Mock())


def test_current_actor_failure_cannot_execute_or_starve_another_release(caplog):
    service = runtime([row('revoked'), row('ready'), row('queued', 'queued')])
    service.actor_for_release.side_effect = [RuntimeError('secret-provider-response'), 'fresh-member', 'fresh-member']
    with caplog.at_level(logging.WARNING):
        counts = worker.run_once(service)
    assert counts == dict(considered=3, advanced=1, resumed=1, skipped=0, failed=1)
    service.advance.assert_called_once_with('fresh-member', 'project', 'campaign-ready', 'ready')
    service.transition.assert_called_once_with('fresh-member', 'project', 'campaign-queued', 'queued', 'resume')
    assert 'RuntimeError' in caplog.text and 'secret-provider-response' not in caplog.text


def test_restart_reconciles_same_release_through_runtime_and_never_mints_authority():
    service = runtime([row('existing')])
    assert worker.run_once(service)['advanced'] == 1
    assert worker.run_once(service)['advanced'] == 1
    assert service.actor_for_release.call_count == 2
    assert service.advance.call_args_list[0] == service.advance.call_args_list[1]
    service.transition.assert_not_called()


@pytest.mark.parametrize('state,kind', [('paused', None), ('cancelled', None), ('finished', None),
                                      ('needs_approach', None), ('waiting', 'authority'), ('waiting', None)])
def test_terminal_paused_and_authority_waits_never_resume(state, kind):
    service = runtime([row('held', state, kind)])
    assert worker.run_once(service)['skipped'] == 1
    service.actor_for_release.assert_not_called()
    service.advance.assert_not_called()
    service.transition.assert_not_called()


@pytest.mark.parametrize('kind', ['authoring', 'job', 'capacity', 'publication', 'approval'])
def test_supported_pending_waits_resume_through_existing_runtime(kind):
    service = runtime([row('pending', 'waiting', kind), row('ready')])
    assert worker.run_once(service)['resumed'] == 1
    service.transition.assert_called_once_with('current-actor', 'project', 'campaign-pending', 'pending', 'resume')
    service.advance.assert_called_once()


def test_bounded_batch_does_not_spin_on_duplicate_human_wait():
    pending = row('approval', 'waiting', 'approval')
    service = runtime([pending, pending, row('ready')])
    counts = worker.run_once(service, limit=3)
    assert counts['resumed'] == 1 and counts['advanced'] == 1
    service._store().runnable_releases.assert_called_once_with(limit=3)


def test_missing_current_actor_does_not_execute():
    service = runtime([row('existing')])
    service.actor_for_release.return_value = None
    assert worker.run_once(service)['skipped'] == 1
    service.advance.assert_not_called()


def test_serve_waits_between_scans_and_stops_cleanly(monkeypatch):
    stop = Mock()
    stop.is_set.return_value = False
    stop.wait.side_effect = [False, True]
    scan = Mock(side_effect=[RuntimeError('provider-secret'), {}])
    monkeypatch.setattr(worker, 'run_once', scan)
    worker.serve(stop, interval_seconds=5)
    assert scan.call_count == 2
    assert stop.wait.call_args_list[0].args == (5,)
    assert stop.wait.call_count == 2
    stopped = threading.Event()
    stopped.set()
    worker.serve(stopped)
    assert scan.call_count == 2


@pytest.mark.parametrize('interval', [0, -1, float('nan'), True])
def test_serve_refuses_busy_loop_intervals(interval):
    with pytest.raises(ValueError):
        worker.serve(threading.Event(), interval)


@pytest.fixture
def app_worker(monkeypatch):
    import app as application
    import platform_link

    created = []
    class Thread:
        def __init__(self, **kwargs):
            self.kwargs, self.alive = kwargs, False
            created.append(self)
        def start(self): self.alive = True
        def is_alive(self): return self.alive
        def join(self, timeout): self.alive = False
    monkeypatch.setattr(application.threading, 'Thread', Thread)
    monkeypatch.setattr(application, '_campaign_release_worker_stop', None)
    monkeypatch.setattr(application, '_campaign_release_worker_thread', None)
    monkeypatch.setattr(application.deps, 'auth_live', lambda: True)
    monkeypatch.setattr(application.job_store, 'job_store_mode', lambda: 'postgres')
    monkeypatch.setattr(platform_link, '_db_configured', lambda: True)
    monkeypatch.setattr(platform_link, 'postgres_startup_required', lambda: False)
    monkeypatch.delenv('LEAF_CAMPAIGN_RELEASE_WORKER_DISABLED', raising=False)
    return application, platform_link, created


def test_app_starts_one_daemon_and_signals_stop(app_worker):
    application, _, created = app_worker
    application.initialize_campaign_release_worker()
    application.initialize_campaign_release_worker()
    assert len(created) == 1 and created[0].kwargs['daemon'] is True
    stop = created[0].kwargs['kwargs']['stop_event']
    assert not stop.is_set()
    assert created[0].kwargs['target'] is worker.serve
    application.stop_campaign_release_worker()
    assert stop.is_set() and application._campaign_release_worker_thread is None


@pytest.mark.parametrize('disabled', ['auth', 'maintenance', 'sqlite', 'database'])
def test_app_keeps_legacy_and_maintenance_modes_inert(app_worker, monkeypatch, disabled):
    application, platform_link, created = app_worker
    if disabled == 'auth': monkeypatch.setattr(application.deps, 'auth_live', lambda: False)
    elif disabled == 'maintenance': monkeypatch.setenv('LEAF_CAMPAIGN_RELEASE_WORKER_DISABLED', '1')
    elif disabled == 'sqlite': monkeypatch.setattr(application.job_store, 'job_store_mode', lambda: 'legacy')
    else: monkeypatch.setattr(platform_link, '_db_configured', lambda: False)
    application.initialize_campaign_release_worker()
    assert created == []


def test_app_does_not_restart_while_old_worker_is_still_stopping(app_worker):
    application, _, created = app_worker
    application.initialize_campaign_release_worker()
    created[0].join = lambda timeout: None
    application.stop_campaign_release_worker()
    application.initialize_campaign_release_worker()
    assert len(created) == 1
