import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

logger = logging.getLogger("uvicorn.error.salestrackr")
active_save = ContextVar("active_save", default=None)


@contextmanager
def save_timing(kind, action):
    metrics = {"entityType": kind, "action": action, "outcome": "ok", "driveCalls": 0, "confirmedReuses": 0}
    token = active_save.set(metrics)
    started = perf_counter()
    try:
        yield
    except Exception as error:
        metrics["outcome"] = "error"
        metrics["errorCode"] = error.code if hasattr(error, "code") else type(error).__name__
        raise
    finally:
        metrics["totalMs"] = round((perf_counter() - started) * 1000, 1)
        active_save.reset(token)
        logger.info("save_timing %s", json.dumps(metrics, sort_keys=True))


@contextmanager
def timed_phase(name):
    metrics = active_save.get()
    started = perf_counter()
    try:
        yield
    except Exception:
        if metrics is not None:
            metrics["failedPhase"] = name
        raise
    finally:
        if metrics is not None:
            metrics[name] = round(metrics.get(name, 0) + (perf_counter() - started) * 1000, 1)


@contextmanager
def timed_lock(lock):
    with timed_phase("lockWaitMs"):
        lock.acquire()
    try:
        yield
    finally:
        lock.release()


def drive_execute(request, *, num_retries):
    metrics = active_save.get()
    if metrics is not None:
        metrics["driveCalls"] += 1
    with timed_phase("driveMs"):
        return request.execute(num_retries=num_retries)


def confirmed_reuse():
    metrics = active_save.get()
    if metrics is not None:
        metrics["confirmedReuses"] += 1
