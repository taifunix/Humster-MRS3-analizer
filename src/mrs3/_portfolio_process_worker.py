"""Small spawn-safe worker support for portfolio calculations.

Keep this module independent of ``mrs3.portfolio``: importing that package
eagerly imports the full optimizer and its SciPy stack on Windows spawn.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation, localcontext
import math
import os
import time
from typing import Any, Callable, Mapping, Sequence

import numpy as np


_PROCESS_EVALUATOR: Callable[..., Any] | None = None
_PROCESS_CONTEXT: Any = None


class _ProcessBatchFailure(RuntimeError):
    """Process bridge failure with successfully completed results preserved."""

    def __init__(self, results: Sequence[tuple[int, Any]], exception_type: str) -> None:
        super().__init__("WORKER_FAILURE")
        self.results = tuple(results)
        self.exception_type = exception_type


def _process_peak_rss_bytes() -> int:
    import psutil

    info = psutil.Process(os.getpid()).memory_info()
    if os.name == "nt":
        peak_wset = getattr(info, "peak_wset", None)
        if peak_wset:
            return int(peak_wset)
    return int(getattr(info, "rss", 0) or 0)


def _process_initializer(evaluator: Callable[..., Any], context: Any) -> None:
    global _PROCESS_EVALUATOR, _PROCESS_CONTEXT
    _PROCESS_EVALUATOR = evaluator
    _PROCESS_CONTEXT = context


def _process_task(task: tuple[int, tuple[Mapping[str, Any], ...]]) -> tuple[int, Any]:
    index, members = task
    evaluator = _PROCESS_EVALUATOR
    if evaluator is None:
        return index, {"status": "UNKNOWN", "reason": "PROCESS_EVALUATOR_UNAVAILABLE"}
    try:
        return index, evaluator(members, _PROCESS_CONTEXT)
    except (ArithmeticError, KeyError, TypeError, ValueError, OSError) as error:
        return index, {
            "status": "UNKNOWN",
            "reason": "PRETEST_EVALUATION_INVALID",
            "exception_type": type(error).__name__,
        }


class _ProcessBatchEvaluator:
    """Small bounded process bridge; the parent owns ordering and accounting."""

    def __init__(self, evaluator: Callable[..., Any], context: Any, workers: int, task_count: int) -> None:
        width = min(max(1, int(workers)), max(1, int(task_count)), os.cpu_count() or 1, 61 if os.name == "nt" else 2**31 - 1)
        self.width = width
        self.pool = ProcessPoolExecutor(
            max_workers=width,
            initializer=_process_initializer,
            initargs=(evaluator, context),
        )

    def __call__(self, tasks: Sequence[tuple[int, tuple[Mapping[str, Any], ...]]]) -> tuple[tuple[int, Any], ...]:
        futures: dict[Any, int] = {}
        results: list[tuple[int, Any]] = []
        failure_type: str | None = None
        for task in tasks:
            try:
                futures[self.pool.submit(_process_task, task)] = task[0]
            except (KeyboardInterrupt, SystemExit):
                self.close()
                raise
            except BaseException as error:
                failure_type = type(error).__name__
                break
        try:
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except (KeyboardInterrupt, SystemExit):
                    self.close()
                    raise
                except BaseException as error:
                    failure_type = failure_type or type(error).__name__
            if failure_type is not None:
                raise _ProcessBatchFailure(results, failure_type)
            return tuple(sorted(results, key=lambda item: item[0]))
        except (KeyboardInterrupt, SystemExit):
            self.close()
            raise

    def close(self) -> None:
        self.pool.shutdown(wait=True)


def _decimal(value: Any, field: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (Decimal, int, str)):
        raise TypeError(f"{field} requires an exact Decimal-compatible value")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field} must be finite") from error
    if not result.is_finite() or (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError(f"{field} must be finite and valid")
    return result


def _precision_for(*values: Any) -> int:
    """Choose stable working precision without trusting the caller context."""
    maximum_requirement = 0
    value_count = 0

    def collect(value: Any) -> None:
        nonlocal maximum_requirement, value_count
        if isinstance(value, Decimal):
            digits = len(value.as_tuple().digits)
            value_count += 1
            exponent = value.as_tuple().exponent
            integer_span = max(0, value.adjusted() + 1) if value else 0
            fractional_span = digits + max(0, -exponent)
            maximum_requirement = max(maximum_requirement, integer_span, fractional_span)
        elif isinstance(value, (tuple, list)):
            for item in value:
                collect(item)

    for value in values:
        collect(value)
    count_digits = len(str(max(1, value_count)))
    return max(64, maximum_requirement + count_digits + 32)


def _sum_products(left: Sequence[Decimal], right: Sequence[Decimal]) -> Decimal:
    if len(left) != len(right):
        raise ValueError("VECTOR_SHAPE_MISMATCH")
    with localcontext() as context:
        context.prec = _precision_for(left, right)
        return sum((first * second for first, second in zip(left, right)), Decimal(0))


def bank_for_path(path: Sequence[Any], max_dd: Any) -> Decimal:
    """Return the smallest B making every peak-equity DD no larger than m."""
    drawdown = _decimal(max_dd, "max_dd")
    if not Decimal(0) < drawdown < Decimal(1):
        raise ValueError("max_dd must be between zero and one")
    values = tuple(_decimal(value, "path") for value in path)
    if not values:
        raise ValueError("path must not be empty")
    with localcontext() as context:
        context.prec = _precision_for(values, drawdown)
        high = Decimal(0)
        required = Decimal(0)
        for gain in values:
            high = max(high, gain)
            required = max(required, ((Decimal(1) - drawdown) * high - gain) / drawdown)
        return max(Decimal(0), required)


def _path(normalized_delta: Sequence[Sequence[Any]], x: Sequence[Decimal]) -> tuple[Decimal, ...]:
    weights = tuple(_decimal(value, "x", nonnegative=True) for value in x)
    rows = tuple(tuple(_decimal(delta, "normalized_delta") for delta in row) for row in normalized_delta)
    with localcontext() as context:
        context.prec = _precision_for(rows, weights)
        totals = [Decimal(0)] * len(weights)
        result: list[Decimal] = []
        for row in rows:
            if len(row) != len(weights):
                raise ValueError("NORMALIZED_DELTA_SHAPE_MISMATCH")
            totals = [total + delta * weight for total, delta, weight in zip(totals, row, weights)]
            result.append(sum(totals, Decimal(0)))
        if not result:
            raise ValueError("NORMALIZED_DELTA_EMPTY")
        return tuple(result)


def _bootstrap_seed(seed: Any, block_ordinal: Any, scenario_index: Any) -> tuple[int, int, int]:
    if any(type(value) is not int or value < 0 for value in (seed, block_ordinal, scenario_index)):
        raise ValueError("BOOTSTRAP_SEED_INVALID")
    return seed, block_ordinal, scenario_index


def _bootstrap_indices_with_stats(
    T: int,
    D: Decimal,
    *,
    history_step_minutes: Decimal,
    seed: int,
    block_ordinal: int,
    scenario_index: int,
) -> tuple[tuple[int, ...], int]:
    with localcontext() as decimal_context:
        decimal_context.prec = _precision_for(history_step_minutes, D)
        probability = history_step_minutes / (Decimal(1440) * D)
    if not Decimal(0) < probability <= Decimal(1):
        raise ValueError("BOOTSTRAP_PROBABILITY_INVALID")
    rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence(_bootstrap_seed(seed, block_ordinal, scenario_index))))
    indices = np.empty(T, dtype=np.int64)
    indices[0] = rng.integers(T)
    restart_count = 0
    probability_float = float(probability)
    for index in range(1, T):
        if rng.random() < probability_float:
            indices[index] = rng.integers(T)
            restart_count += 1
        else:
            indices[index] = (indices[index - 1] + 1) % T
    return tuple(int(value) for value in indices), restart_count


def _bootstrap_scenario_batch(task: Mapping[str, Any], context: tuple[Any, ...]) -> Mapping[str, Any]:
    """Evaluate one bounded scenario batch using initializer-owned increments."""
    increment_array, T, families, step, seed, drawdown, precision = context
    ordinal = int(task["family_ordinal"])
    start = int(task["scenario_start"])
    count = int(task["scenario_count"])
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    family_days = families[ordinal]
    banks: list[list[Decimal]] = [[] for _ in range(len(increment_array))]
    coverage_mask = 0
    restart_count = 0
    peak_rss_bytes = _process_peak_rss_bytes()
    for scenario in range(start, start + count):
        indices, restarts = _bootstrap_indices_with_stats(
            T,
            family_days,
            history_step_minutes=step,
            seed=seed,
            block_ordinal=ordinal,
            scenario_index=scenario,
        )
        restart_count += restarts
        for index in indices:
            coverage_mask |= 1 << index
        with localcontext() as decimal_context:
            decimal_context.prec = precision
            sampled_paths = np.cumsum(increment_array[:, indices], axis=1, dtype=object)
        for vector_index, path in enumerate(sampled_paths):
            banks[vector_index].append(bank_for_path(path, drawdown))
    peak_rss_bytes = max(peak_rss_bytes, _process_peak_rss_bytes())
    return {
        "task_id": task["task_id"],
        "family_ordinal": ordinal,
        "scenario_start": start,
        "scenario_count": count,
        "banks": tuple(tuple(values) for values in banks),
        "restart_count": restart_count,
        "coverage_mask": coverage_mask,
        "wall_seconds": time.perf_counter() - started_wall,
        "process_cpu_seconds": time.process_time() - started_cpu,
        "peak_rss_bytes": peak_rss_bytes,
    }


def _bootstrap_process_evaluator(members: Sequence[Mapping[str, Any]], context: tuple[Any, ...]) -> Mapping[str, Any]:
    return _bootstrap_scenario_batch(members[0], context)
