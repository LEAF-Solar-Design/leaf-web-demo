"""Sequential execution, durable terminal results, independent heartbeat cadence."""
from __future__ import annotations

from datetime import datetime, timezone
import logging
from logging.handlers import RotatingFileHandler
import multiprocessing
import os
import sqlite3
import json
import threading
import time
from uuid import UUID

from .client import ClientError, Revoked
from .ops import Dispatcher, failure

LOG = logging.getLogger("leaf_agent")
HEARTBEAT_SECONDS = 30  # P0 requires at least one heartbeat each 30 seconds.
OP_TIMEOUTS = {"pkg.install": 1500, "pkg.upgrade": 1500, "pkg.uninstall": 120}
DEFAULT_OP_TIMEOUT = 30


def configure_logging(config):
    config.home.mkdir(parents=True, exist_ok=True, mode=0o700)
    for existing in list(LOG.handlers):
        if isinstance(existing, RotatingFileHandler):
            if existing.baseFilename == str((config.home / "agent.log").resolve()):
                return
            LOG.removeHandler(existing)
            existing.close()
    handler = RotatingFileHandler(config.home / "agent.log", maxBytes=1048576, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _worker(send, dispatcher, op, args):
    try:
        send.send(dispatcher.dispatch(op, args))
    except Exception:
        send.send(failure("upstream_error", status="error"))
    finally:
        send.close()


def isolated(dispatcher, op, args, timeout, tick):
    # spawn works on all supported platforms and prevents inheriting device keys
    # or the Studio client into op workers. Handlers contain local package state.
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(send, dispatcher, op, args), daemon=True)
    deadline = time.monotonic() + timeout
    timed_out = threading.Event()
    def stop_expired_worker():
        timed_out.set()
        try:
            if process.is_alive():
                process.terminate()
        except OSError:
            pass  # The worker may have exited between is_alive and terminate.
    timer = threading.Timer(timeout, stop_expired_worker)
    timer.daemon = True
    try:
        process.start()
        # Enforce the worker deadline even if a heartbeat is blocked on network
        # I/O/backoff in the parent. Never leave timed-out work running locally.
        timer.interval = max(0, deadline - time.monotonic())
        timer.start()
        send.close()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or timed_out.is_set():
                return failure("timeout", status="error")
            if receive.poll(min(1.0, remaining)):
                try:
                    result = receive.recv()
                    return failure("timeout", status="error") if timed_out.is_set() else result
                except EOFError:
                    return failure("timeout" if timed_out.is_set() else "upstream_error", status="error")
            tick()
    finally:
        timer.cancel()
        if timer.ident is not None:
            timer.join()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join()
        receive.close()
        send.close()


def free_memory_mb():
    try:
        return max(0, os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") // 1048576)
    except (AttributeError, ValueError, OSError):
        return 0


class Agent:
    def __init__(self, config, client, registry, *, clock=time.monotonic, sleep=time.sleep, executor=isolated):
        self.config, self.client, self.registry = config, client, registry
        self.clock, self.sleep, self.executor = clock, sleep, executor
        self.started = clock()
        self.next_heartbeat = self.started
        config.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(config.home / "results.sqlite3")
        self.db.execute("CREATE TABLE IF NOT EXISTS results (id TEXT PRIMARY KEY, body TEXT NOT NULL, pending INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS identity (id TEXT PRIMARY KEY)")
        device_id = config.load()["device_id"]
        previous = self.db.execute("SELECT id FROM identity").fetchone()
        if previous != (device_id,):
            self.db.execute("DELETE FROM results")
            self.db.execute("DELETE FROM identity")
            self.db.execute("INSERT INTO identity VALUES (?)", (device_id,))
        self.db.commit()

    def heartbeat(self):
        if self.clock() >= self.next_heartbeat:
            self.client.heartbeat(self.registry.states(), free_memory_mb(), max(0, int(self.clock() - self.started)))
            self.next_heartbeat = self.clock() + HEARTBEAT_SECONDS

    def operation_tick(self):
        try:
            self.heartbeat()
        except Revoked:
            raise
        except ClientError:
            # A temporary Studio failure must not interrupt a local install or
            # cause it to repeat. The transport already applied its backoff.
            LOG.warning("Heartbeat deferred during operation")

    def execute(self, command):
        command_id = command["command_id"]
        saved = self.db.execute("SELECT body FROM results WHERE id=?", (command_id,)).fetchone()
        if saved:
            return json.loads(saved[0])
        try:
            UUID(command_id)
            if self.config.disabled:
                outcome = failure("paused")
            else:
                if set(command) != {"command_id", "op", "args", "issued_at", "expires_at"}:
                    raise ValueError("Invalid envelope")
                issued = datetime.fromisoformat(command["issued_at"].replace("Z", "+00:00"))
                expires = datetime.fromisoformat(command["expires_at"].replace("Z", "+00:00"))
                if issued.tzinfo is None or expires.tzinfo is None or expires <= issued:
                    raise ValueError("Invalid expiry")
                if expires <= datetime.now(timezone.utc):
                    outcome = failure("command_expired")
                else:
                    timeout = OP_TIMEOUTS.get(command["op"], DEFAULT_OP_TIMEOUT)
                    outcome = self.executor(Dispatcher(self.config, self.registry), command["op"], command["args"], timeout, self.operation_tick)
        except (ValueError, TypeError, KeyError, AttributeError):
            outcome = failure("invalid_args")
        result = {"command_id": command_id, **outcome, "finished_at": utc_now()}
        self.db.execute("INSERT INTO results VALUES (?, ?, 1)", (command_id, json.dumps(result, allow_nan=False)))
        self.db.commit()
        return result

    def flush_results(self):
        rows = self.db.execute("SELECT id, body FROM results WHERE pending=1 ORDER BY rowid LIMIT 8").fetchall()
        if rows:
            response = self.client.post_results([json.loads(row[1]) for row in rows])
            if response != {"accepted": len(rows)}:
                self.client.backoff.wait()
                raise ClientError("Invalid result acknowledgement")
            self.db.executemany("UPDATE results SET pending=0 WHERE id=?", [(row[0],) for row in rows])
            self.db.commit()

    def run(self, *, max_cycles=None):
        cycles = 0
        try:
            while max_cycles is None or cycles < max_cycles:
                cycles += 1
                try:
                    self.heartbeat()
                    self.flush_results()
                    # Leave headroom for the 30-second heartbeat rule.
                    wait = min(25, max(0, int(self.next_heartbeat - self.clock()) - 1))
                    commands = self.client.poll(wait=wait)
                    for command in commands:
                        self.heartbeat()
                        result = self.execute(command)
                        self.db.execute("UPDATE results SET pending=1 WHERE id=?", (result["command_id"],))
                        self.db.commit()
                    self.flush_results()
                    # A server returning immediately (including an empty 200/204)
                    # must not turn the poll loop into a busy loop.
                    self.sleep(1)
                except ClientError as exc:
                    if isinstance(exc, Revoked):
                        raise
                    LOG.warning("Studio request deferred")
            return 0
        except Revoked:
            self.config.wipe_identity()
            # Old-device result retries must not be sent after reenrollment.
            self.db.execute("DELETE FROM results")
            self.db.commit()
            LOG.info("Device revoked; local identity removed")
            return 3
        finally:
            self.db.close()
