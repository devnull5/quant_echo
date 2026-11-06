"""Market-data provider registry. Add a provider: write a module with a SPEC and list it here."""
from __future__ import annotations

from typing import Dict, List, Mapping

from . import alpaca, alphavantage, coinbase, finnhub, polygon, schwab, simulated, tiingo, twelvedata, yahoo
from .base import AuthError, Field, Provider, ProviderError, ProviderSpec, RateLimited

PROVIDERS: Dict[str, ProviderSpec] = {
    s.id: s
    for s in (
        simulated.SPEC,
        coinbase.SPEC,
        alpaca.SPEC,
        tiingo.SPEC,
        finnhub.SPEC,
        polygon.SPEC,
        twelvedata.SPEC,
        alphavantage.SPEC,
        schwab.SPEC,
        yahoo.SPEC,
    )
}
DEFAULT_PROVIDER = "simulated"  # never opens to an error; first-run picker offers real sources


def visible_providers(show_personal: bool, current: str = "") -> List[ProviderSpec]:
    """Sources to list in the UI: personal-use-only ones only when opted in (or already selected)."""
    return [s for s in PROVIDERS.values() if show_personal or not s.personal_use_only or s.id == current]


def all_fields() -> List[Field]:
    return [f for spec in PROVIDERS.values() for f in spec.fields]


def create(provider_id: str, creds: Mapping[str, str]) -> Provider:
    spec = PROVIDERS.get(provider_id)
    if spec is None:
        raise ProviderError(f"Unknown provider {provider_id!r}")
    return spec.factory(creds)


__all__ = ["PROVIDERS", "DEFAULT_PROVIDER", "AuthError", "RateLimited", "Field", "Provider", "ProviderError", "ProviderSpec", "all_fields", "visible_providers",
           "create"]
