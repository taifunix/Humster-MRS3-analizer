"""Pure adapter from frozen Panel facts to sized multi-pair candidates."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .liquidity import ReferenceSnapshot
from .minute_capacity import MinuteCapacityResult


CAMPAIGN_CONTRACT_VERSION = "PORTFOLIO_WEIGHTED_CAMPAIGN_V1"
CAMPAIGN_SEARCH_MODE = "WEIGHTED_V1"
CAMPAIGN_LEGACY_SEARCH_MODE = "PRETEST_PROXY"
CAMPAIGN_WEIGHTED_ALGO_VERSION = "WS1.1"
WEIGHTED_SEARCH_NOT_IMPLEMENTED = "WEIGHTED_SEARCH_NOT_IMPLEMENTED"


class CampaignContractError(ValueError):
    """A stable fail-closed Campaign contract error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def validate_campaign_contract(campaign: Any) -> None:
    """Validate the exact weighted Campaign envelope before any I/O."""
    if not isinstance(campaign, Mapping):
        raise CampaignContractError("CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED")
    versions = campaign.get("versions")
    if "stage1_mode" in campaign or (isinstance(versions, Mapping) and "stage1_mode" in versions):
        raise CampaignContractError("CAMPAIGN_LEGACY_STAGE1_MODE_UNSUPPORTED")

    contract_version = campaign.get("campaign_contract_version")
    if not isinstance(contract_version, str) or not contract_version or contract_version != CAMPAIGN_CONTRACT_VERSION:
        raise CampaignContractError("CAMPAIGN_CONTRACT_VERSION_UNSUPPORTED")

    search_mode = campaign.get("search_mode")
    if not isinstance(search_mode, str) or not search_mode:
        raise CampaignContractError("CAMPAIGN_SEARCH_MODE_REQUIRED")
    if search_mode == CAMPAIGN_LEGACY_SEARCH_MODE:
        raise CampaignContractError("CAMPAIGN_LEGACY_SEARCH_MODE_UNSUPPORTED")
    if search_mode != CAMPAIGN_SEARCH_MODE:
        raise CampaignContractError("CAMPAIGN_SEARCH_MODE_UNSUPPORTED")

    algo_version = campaign.get("weighted_algo_version")
    if not isinstance(algo_version, str) or not algo_version:
        raise CampaignContractError("CAMPAIGN_WEIGHTED_ALGO_VERSION_REQUIRED")
    if algo_version != CAMPAIGN_WEIGHTED_ALGO_VERSION:
        raise CampaignContractError("CAMPAIGN_WEIGHTED_ALGO_VERSION_UNSUPPORTED")

    if (
        not isinstance(versions, Mapping)
        or any(
            versions.get(key) != value
            for key, value in (
                ("campaign_contract_version", contract_version),
                ("search_mode", search_mode),
                ("weighted_algo_version", algo_version),
            )
        )
    ):
        raise CampaignContractError("CAMPAIGN_VERSIONS_MISMATCH")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class AdapterResult:
    status: str
    variants: tuple[Mapping[str, Any], ...] = ()
    excluded: tuple[Mapping[str, Any], ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "variants", tuple(_freeze(item) for item in self.variants))
        object.__setattr__(self, "excluded", tuple(_freeze(item) for item in self.excluded))
        object.__setattr__(self, "blockers", tuple(dict.fromkeys(self.blockers)))
        object.__setattr__(self, "warnings", tuple(dict.fromkeys(self.warnings)))

def _adapter_gate(campaign: Any) -> AdapterResult:
    try:
        validate_campaign_contract(campaign)
    except CampaignContractError as error:
        return AdapterResult("FAIL", blockers=(error.code,))
    return AdapterResult("FAIL", blockers=(WEIGHTED_SEARCH_NOT_IMPLEMENTED,))


def build_portfolio_candidates(
    selected_rows: Sequence[Mapping[str, Any]],
    campaign: Mapping[str, Any],
    *,
    capacities: Mapping[str, MinuteCapacityResult],
    reference: ReferenceSnapshot,
    mark_prices: Mapping[str, Any],
    spread_observations: Mapping[str, Sequence[Mapping[str, Any]]],
    spread_history_statuses: Mapping[str, str],
    now_ms: int,
    workers: int = 1,
) -> AdapterResult:
    """Validate the frozen Campaign before the weighted implementation exists."""
    return _adapter_gate(campaign)


def run_portfolio_adapter(
    selected_rows: Sequence[Mapping[str, Any]],
    campaign: Mapping[str, Any],
    *,
    workspace_root: str | Path,
    market_fetcher: Any = None,
    archive_fetcher: Any = None,
    workers: int = 1,
) -> AdapterResult:
    """Validate the frozen Campaign before loading local or public facts."""
    return _adapter_gate(campaign)


__all__ = [
    "CAMPAIGN_CONTRACT_VERSION",
    "CAMPAIGN_LEGACY_SEARCH_MODE",
    "CAMPAIGN_SEARCH_MODE",
    "CAMPAIGN_WEIGHTED_ALGO_VERSION",
    "CampaignContractError",
    "WEIGHTED_SEARCH_NOT_IMPLEMENTED",
    "AdapterResult",
    "build_portfolio_candidates",
    "run_portfolio_adapter",
    "validate_campaign_contract",
]
