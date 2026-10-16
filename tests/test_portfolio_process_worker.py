import os
import threading
from pathlib import Path

import pytest

import mrs3._portfolio_process_worker as worker
from mrs3._portfolio_process_worker import _ProcessBatchEvaluator, _ProcessBatchFailure, shared_process_pool


def _pid_and_context(members, context):
    return {"pid": os.getpid(), "context": context, "task": members[0]}


def _crash_on_poison(members, context):
    if members[0] == "poison":
        os._exit(3)
    return {"pid": os.getpid(), "context": context}


def _run_with_deadline(function, seconds=120):
    outcome = {}

    def target():
        try:
            outcome["value"] = function()
        except BaseException as error:  # surfaced to the test thread
            outcome["error"] = error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "process bridge hung"
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def test_context_is_shared_through_a_file_and_removed_on_close(monkeypatch):
    created = {}
    real_pool = worker.ProcessPoolExecutor

    def recording_pool(**kwargs):
        created.update(kwargs)
        return real_pool(**kwargs)

    monkeypatch.setattr(worker, "ProcessPoolExecutor", recording_pool)
    context = ("large", tuple(range(1000)))
    bridge = _ProcessBatchEvaluator(_pid_and_context, context, 2, 2)
    path = Path(bridge.context_path)
    try:
        results = _run_with_deadline(lambda: bridge(((0, ("a",)), (1, ("b",)))))
    finally:
        bridge.close()

    assert "initargs" not in created and "initializer" not in created
    assert [item[1]["context"] for item in results] == [context, context]
    assert [item[1]["task"] for item in results] == ["a", "b"]
    assert not path.exists()


def test_a_child_crash_fails_closed_instead_of_hanging():
    bridge = _ProcessBatchEvaluator(_crash_on_poison, "ctx", 2, 2)
    try:
        with pytest.raises(_ProcessBatchFailure) as error:
            _run_with_deadline(lambda: bridge(((0, ("poison",)), (1, ("poison",)))))
    finally:
        bridge.close()
    assert error.value.exception_type == "BrokenProcessPool"


def test_shared_pool_is_reused_across_bridges_and_recovers_after_a_crash():
    with shared_process_pool(2):
        first = _ProcessBatchEvaluator(_pid_and_context, "first", 2, 2)
        first_results = _run_with_deadline(lambda: first(((0, ("a",)), (1, ("b",)))))
        first.close()
        second = _ProcessBatchEvaluator(_pid_and_context, "second", 2, 2)
        second_results = _run_with_deadline(lambda: second(((0, ("a",)), (1, ("b",)))))
        second.close()
        crashing = _ProcessBatchEvaluator(_crash_on_poison, "third", 2, 1)
        with pytest.raises(_ProcessBatchFailure):
            _run_with_deadline(lambda: crashing(((0, ("poison",)),)))
        crashing.close()
        after = _ProcessBatchEvaluator(_pid_and_context, "after", 2, 1)
        after_results = _run_with_deadline(lambda: after(((0, ("x",)),)))
        after.close()

    assert [item[1]["context"] for item in first_results] == ["first", "first"]
    assert [item[1]["context"] for item in second_results] == ["second", "second"]
    assert {item[1]["pid"] for item in first_results} & {item[1]["pid"] for item in second_results}
    assert after_results[0][1]["context"] == "after"
    assert worker._SHARED_POOL is None


def test_shared_pool_is_ignored_by_other_threads():
    seen = {}
    with shared_process_pool(2):
        def other_thread():
            bridge = _ProcessBatchEvaluator(_pid_and_context, "other", 2, 1)
            seen["shared"] = bridge._shared
            bridge.close()
        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(60)
        owner = _ProcessBatchEvaluator(_pid_and_context, "owner", 2, 1)
        seen["owner"] = owner._shared
        owner.close()
    assert seen["shared"] is None
    assert seen["owner"] is not None


def test_context_files_live_in_a_dedicated_directory_and_stale_ones_are_swept(tmp_path, monkeypatch):
    monkeypatch.setattr(worker.tempfile, "gettempdir", lambda: str(tmp_path))
    directory = tmp_path / "mrs3-process-context"
    directory.mkdir()
    stale = directory / "stale.pkl"
    stale.write_bytes(b"x")
    os.utime(stale, (0, 0))
    fresh = directory / "fresh.pkl"
    fresh.write_bytes(b"x")

    bridge = _ProcessBatchEvaluator(_pid_and_context, "ctx", 1, 1)
    try:
        assert Path(bridge.context_path).parent == directory
    finally:
        bridge.close()

    assert not stale.exists() and fresh.exists()


def test_worker_context_cache_is_released_before_the_next_load(tmp_path, monkeypatch):
    import pickle

    first, second = tmp_path / "a.pkl", tmp_path / "b.pkl"
    first.write_bytes(pickle.dumps("first"))
    second.write_bytes(pickle.dumps("second"))
    assert worker._load_context(str(first)) == "first"
    observed = []
    real_load = worker.pickle.load
    monkeypatch.setattr(worker.pickle, "load", lambda handle: (observed.append(worker._CONTEXT_CACHE), real_load(handle))[1])

    assert worker._load_context(str(second)) == "second"
    assert observed == [None]
