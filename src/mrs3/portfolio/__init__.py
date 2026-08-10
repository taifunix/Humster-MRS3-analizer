"""Анализатор портфеля.

Спецификация: `docs/specs/2026-08-09-portfolio-analyzer-v04.md`.

Два входа:
  1. CSV результатов стратегий (лоты уже приведены к целевой просадке);
  2. база сделок DuckDB — журналы по каждой стратегии отдельно.

Слой A (`layer_a`) — дешёвый отсев по занятости слотов и асимметрии, работает
без журналов. Слой B (`simulator`, `lots`, `search`) — событийная симуляция
сета, подбор множителя лотов, маржа с висящими ордерами, Парето и проверка на
второй половине истории.
"""

from .layer_a import Candidate, CombinationScreen, LayerAError, screen_all, screen_combination
from .models import RunConfig, SetResult, StrategyInput, TradeRecord
from .pipeline import PortfolioError, PortfolioInputs, run_portfolio
from .search import correlation_matrix, enumerate_sets, pareto_front, split_validation
from .simulator import simulate_set

__all__ = [
    "Candidate",
    "CombinationScreen",
    "LayerAError",
    "PortfolioError",
    "PortfolioInputs",
    "RunConfig",
    "SetResult",
    "StrategyInput",
    "TradeRecord",
    "correlation_matrix",
    "enumerate_sets",
    "pareto_front",
    "run_portfolio",
    "screen_all",
    "screen_combination",
    "simulate_set",
    "split_validation",
]
