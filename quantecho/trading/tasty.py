"""tastytrade Open API client (OAuth2 refresh-token flow). Sandbox and production are separate environments
with separate credentials.

Verified against developer.tastytrade.com (2026): base URLs, POST /oauth/token (grant_type=refresh_token,
refresh_token, client_secret; 15-minute JWT access tokens; refresh token does not rotate), mandatory
User-Agent, order JSON (dasherized keys, decimal strings), POST /accounts/{n}/orders[/dry-run],
DELETE/GET /accounts/{n}/orders/{id}, GET .../orders/live, positions, balances, /market-data/by-type.

NOT verified on the docs pages read (taken from tastytrade's SDK conventions; confirm on first sandbox run):
nested option-chain field names, and the accounts list endpoint (/customers/me/accounts).

Safety: the API does not deduplicate retries. Every order carries a unique `external-identifier`; a
submit that times out is NEVER blindly resent: we look the identifier up first (see place_order).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

from ..logsetup import log
from ..providers.base import AuthError, ProviderError, RateLimited, http_error
from .occ import OptionContract, parse_occ

URLS = {"sandbox": "https://api.cert.tastyworks.com", "live": "https://api.tastyworks.com"}
USER_AGENT = "quantecho/0.7"
SOURCE = "quantecho"


class OrderUncertain(ProviderError):
    """Submit outcome unknown (timeout/5xx) and the order could not be found by its identifier."""


@dataclass
class Expiration:
    expiry: date
    dte: int
    strikes: Dict[float, Dict[str, str]] = field(default_factory=dict)  # strike -> {"C": symbol, "P": symbol}


def parse_nested_chain(payload: Any) -> List[Expiration]:
    """Tolerant parser for GET /option-chains/{symbol}/nested (field names per tastytrade SDK conventions)."""
    items = ((payload or {}).get("data") or {}).get("items") or []
    out: List[Expiration] = []
    for item in items:
        for e in item.get("expirations") or []:
            ds = e.get("expiration-date") or e.get("expiration_date")
            if not ds:
                continue
            exp = date.fromisoformat(ds)
            dte = int(e.get("days-to-expiration", e.get("days_to_expiration", (exp - date.today()).days)))
            ex = Expiration(exp, dte)
            for s in e.get("strikes") or []:
                try:
                    k = float(s.get("strike-price", s.get("strike_price")))
                except (TypeError, ValueError):
                    continue
                legs = {}
                if s.get("call"):
                    legs["C"] = s["call"]
                if s.get("put"):
                    legs["P"] = s["put"]
                if legs:
                    ex.strikes[k] = legs
            if ex.strikes:
                out.append(ex)
    return sorted(out, key=lambda x: x.expiry)


def build_option_order(symbol: str, action: str, quantity: int, limit: float, ext_id: str,
                       tif: str = "Day") -> Dict[str, Any]:
    """Single-leg equity-option LIMIT order. Buying is a Debit, selling a Credit (API rejects mismatches)."""
    if action not in ("Buy to Open", "Sell to Close"):
        raise ValueError("long-options-only bot: actions are 'Buy to Open' and 'Sell to Close'")
    if quantity < 1 or int(quantity) != quantity:
        raise ValueError("quantity must be a positive whole number")
    if not limit or limit <= 0:
        raise ValueError("limit price required")
    parse_occ(symbol)  # validates the symbol
    return {
        "time-in-force": tif,
        "order-type": "Limit",
        "price": f"{limit:.2f}",
        "price-effect": "Debit" if action.startswith("Buy") else "Credit",
        "source": SOURCE,
        "external-identifier": ext_id,
        "legs": [{"instrument-type": "Equity Option", "symbol": symbol, "quantity": int(quantity), "action": action}],
    }


def new_external_id(prefix: str = "qe") -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


class TastytradeClient:
    def __init__(self, env: str, client_secret: str, refresh_token: str, account: str = "",
                 http: Any = None, timeout: float = 15.0):
        import httpx

        if env not in URLS:
            raise ValueError("env must be 'sandbox' or 'live'")
        self.env, self.base = env, URLS[env]
        self._secret, self._refresh = client_secret.strip(), refresh_token.strip()
        if not self._secret or not self._refresh:
            raise AuthError("tastytrade client secret and refresh token are required")
        self.account = account.strip()
        self.http = http or httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT,
                                                                  "Accept": "application/json"})
        self._token: Optional[str] = None
        self._token_exp = 0.0

    # ---------------------------------------------------------------- auth + transport
    def _redact(self, text: str) -> str:
        for s in (self._secret, self._refresh, self._token or ""):
            if s:
                text = text.replace(s, "***")
        return text

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = self.http.post(f"{self.base}/oauth/token",
                           json={"grant_type": "refresh_token", "refresh_token": self._refresh,
                                 "client_secret": self._secret},
                           headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"})
        if r.status_code >= 400:
            raise http_error(r.status_code, self._redact(r.text or ""))
        data = r.json()
        tok = data.get("access_token") or (data.get("data") or {}).get("access_token")
        if not tok:
            raise AuthError("tastytrade: no access_token in token response")
        self._token = tok
        self._token_exp = time.time() + float(data.get("expires_in") or 900)  # documented lifetime: 15 min
        return tok

    def _request(self, method: str, path: str, *, json: Any = None, params: Any = None, retries: int = 0) -> Any:
        """retries > 0 only for idempotent reads; order submission always uses retries=0."""
        import httpx

        attempt = 0
        while True:
            headers = {"Authorization": f"Bearer {self._access_token()}", "User-Agent": USER_AGENT,
                       "Accept": "application/json"}
            try:
                r = self.http.request(method, f"{self.base}{path}", json=json, params=params, headers=headers)
            except httpx.TimeoutException as e:
                raise TimeoutError(self._redact(str(e))) from None
            except httpx.HTTPError as e:
                raise ProviderError(self._redact(f"{type(e).__name__}: {e}")) from None
            if r.status_code == 401 and attempt == 0:
                self._token = None            # expired token: refresh once and retry
                attempt += 1
                continue
            if r.status_code in (429, 502, 503, 504) and attempt < retries:
                attempt += 1
                time.sleep(min(2 ** attempt, 20))
                continue
            if r.status_code >= 400:
                raise http_error(r.status_code, self._redact(r.text or ""), r.headers.get("Retry-After"))
            return r.json() if r.content else {}

    # ---------------------------------------------------------------- reads
    def accounts(self) -> List[str]:
        p = self._request("GET", "/customers/me/accounts", retries=2)
        out = []
        for it in (p.get("data") or {}).get("items") or []:
            acc = it.get("account") or it
            if acc.get("account-number"):
                out.append(acc["account-number"])
        return out

    def ensure_account(self) -> str:
        if not self.account:
            accs = self.accounts()
            if not accs:
                raise ProviderError("tastytrade: no accounts found for these credentials")
            self.account = accs[0]
        return self.account

    def balances(self) -> Dict[str, Any]:
        return (self._request("GET", f"/accounts/{self.ensure_account()}/balances", retries=2) or {}).get("data") or {}

    def positions(self) -> List[Dict[str, Any]]:
        p = self._request("GET", f"/accounts/{self.ensure_account()}/positions", params={"include-marks": "true"},
                          retries=2)
        return (p.get("data") or {}).get("items") or []

    def option_chain(self, underlying: str) -> List[Expiration]:
        return parse_nested_chain(self._request("GET", f"/option-chains/{underlying.upper()}/nested", retries=2))

    def quotes(self, equities: List[str] = (), options: List[str] = ()) -> Dict[str, Dict[str, Any]]:
        """REST snapshot (PRODUCTION only; the sandbox returns 502 for market data). Max 100 symbols."""
        params = {}
        if equities:
            params["equity"] = ",".join(equities)
        if options:
            params["equity-option"] = ",".join(options)
        p = self._request("GET", "/market-data/by-type", params=params, retries=1)
        return {it["symbol"]: it for it in (p.get("data") or {}).get("items") or [] if it.get("symbol")}

    def order(self, order_id: Any) -> Dict[str, Any]:
        return (self._request("GET", f"/accounts/{self.ensure_account()}/orders/{order_id}", retries=2)
                .get("data") or {})

    def live_orders(self) -> List[Dict[str, Any]]:
        p = self._request("GET", f"/accounts/{self.ensure_account()}/orders/live", retries=2)
        return (p.get("data") or {}).get("items") or []

    def find_by_external_id(self, ext_id: str) -> Optional[Dict[str, Any]]:
        for o in self.live_orders():
            if o.get("external-identifier") == ext_id:
                return o
        p = self._request("GET", f"/accounts/{self.ensure_account()}/orders",
                          params={"start-date": date.today().isoformat()}, retries=2)
        for o in (p.get("data") or {}).get("items") or []:
            if o.get("external-identifier") == ext_id:
                return o
        return None

    # ---------------------------------------------------------------- writes (never auto-retried)
    def dry_run(self, order: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", f"/accounts/{self.ensure_account()}/orders/dry-run", json=order).get("data") or {}

    def place_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """Submit once. On timeout/5xx, look the order up by external-identifier instead of resending."""
        acct, ext = self.ensure_account(), order["external-identifier"]
        try:
            return (self._request("POST", f"/accounts/{acct}/orders", json=order).get("data") or {}).get("order") or {}
        except (TimeoutError, ProviderError) as e:
            if isinstance(e, (AuthError, RateLimited)) or ("HTTP 4" in str(e)):
                raise                                   # definitely not placed
            log.warning(f"tastytrade submit uncertain ({e}); checking for {ext}")
            for _ in range(3):
                found = self.find_by_external_id(ext)
                if found:
                    return found
                time.sleep(1.5)
            raise OrderUncertain(f"order {ext} status unknown after {e}; NOT resent. Check the account.") from None

    def cancel(self, order_id: Any) -> None:
        self._request("DELETE", f"/accounts/{self.ensure_account()}/orders/{order_id}")


def contract_from_symbol(symbol: str) -> OptionContract:
    return parse_occ(symbol)
