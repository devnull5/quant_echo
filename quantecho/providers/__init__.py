"""Market-data provider registry. Add a provider: write a module with a SPEC and list it here."""
from __future__ import annotations

from typing import Dict, List, Mapping

from . import alpaca, alphavantage, finnhub, polygon, schwab, simulated, tiingo, twelvedata, yahoo
from .base import AuthError, Field, Provider, ProviderError, ProviderSpec, RateLimited

PROVIDERS: Dict[str, ProviderSpec] = {
    s.id: s
    for s in (
        yahoo.SPEC,
        schwab.SPEC,
        alpaca.SPEC,
        tiingo.SPEC,
        finnhub.SPEC,
        polygon.SPEC,
        twelvedata.SPEC,
        alphavantage.SPEC,
        simulated.SPEC,
    )
}
DEFAULT_PROVIDER = "yahoo"


def all_fields() -> List[Field]:
    return [f for spec in PROVIDERS.values() for f in spec.fields]


def create(provider_id: str, creds: Mapping[str, str]) -> Provider:
    spec = PROVIDERS.get(provider_id)
    if spec is None:
        raise ProviderError(f"Unknown provider {provider_id!r}")
    return spec.factory(creds)


__all__ = ["PROVIDERS", "DEFAULT_PROVIDER", "AuthError", "RateLimited", "Field", "Provider", "ProviderError", "ProviderSpec", "all_fields",
           "create"]
