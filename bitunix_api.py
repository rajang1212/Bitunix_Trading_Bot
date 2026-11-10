from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import hashlib
import json
import os
from pathlib import Path
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

VERSION = "1.3.1-multiagent"
BASE_URL = "https://fapi.bitunix.com"
MAX_RESPONSE_BYTES = 1_048_576
REQUEST_TIMEOUT_SECONDS = 15
VAULT_MAGIC = b"BITUNIX-DPAPI-v1\n"
VAULT_MAX_BYTES = 65_536

GET_ENDPOINTS = {
    "contract": "/api/v1/futures/market/trading_pairs",
    "tickers": "/api/v1/futures/market/tickers",
    "kline": "/api/v1/futures/market/kline",
    "depth": "/api/v1/futures/market/depth",
    "funding_batch": "/api/v1/futures/market/funding_rate/batch",
    "account": "/api/v1/futures/account",
    "settings": "/api/v1/futures/account/get_leverage_margin_mode",
    "positions": "/api/v1/futures/position/get_pending_positions",
    "history_positions": "/api/v1/futures/position/get_history_positions",
    "orders": "/api/v1/futures/trade/get_pending_orders",
    "order_detail": "/api/v1/futures/trade/get_order_detail",
    "history_trades": "/api/v1/futures/trade/get_history_trades",
    "tpsl_pending": "/api/v1/futures/tpsl/get_pending_orders",
}
PUBLIC_GETS = {"contract", "tickers", "kline", "depth", "funding_batch"}
POST_ENDPOINTS = {
    "change_margin_mode": "/api/v1/futures/account/change_margin_mode",
    "change_leverage": "/api/v1/futures/account/change_leverage",
    "place_order": "/api/v1/futures/trade/place_order",
    "flash_close": "/api/v1/futures/trade/flash_close_position",
    "modify_position_tpsl": "/api/v1/futures/tpsl/position/modify_order",
}


class ApiError(RuntimeError):
    pass


class VaultError(ApiError):
    pass


def _clean_credential(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise VaultError(f"{label}: invalid saved value.")
    value = value.strip()
    if not value or len(value) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise VaultError(f"{label}: invalid saved value.")
    return value


@dataclass(frozen=True)
class Credentials:
    api_key: str = field(repr=False)
    secret_key: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "api_key", _clean_credential(self.api_key, "API Key"))
        object.__setattr__(self, "secret_key", _clean_credential(self.secret_key, "Secret Key"))


def vault_path() -> Path:
    if os.name != "nt":
        raise VaultError("Saved Bitunix credentials can only be loaded on Windows in this build.")
    base = os.environ.get("LOCALAPPDATA")
    if not base or not Path(base).is_absolute():
        raise VaultError("Windows LOCALAPPDATA could not be resolved.")
    return Path(base) / "BitunixSignalConnector" / "credentials.dpapi"


def _dpapi_decrypt(data: bytes) -> bytes:
    if os.name != "nt":
        raise VaultError("Windows DPAPI is required to load the saved Bitunix key.")
    if not data or len(data) > VAULT_MAX_BYTES:
        raise VaultError("Invalid encrypted credential data size.")
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    unprotect = crypt32.CryptUnprotectData
    unprotect.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.POINTER(DataBlob),
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    unprotect.restype = wintypes.BOOL
    input_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    incoming = DataBlob(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = DataBlob()
    try:
        ok = unprotect(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing))
        if not ok:
            raise VaultError("Windows could not decrypt the saved Bitunix credentials for this user.")
        if outgoing.cbData > VAULT_MAX_BYTES:
            raise VaultError("Windows returned an unexpected credential data size.")
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        ctypes.memset(input_buffer, 0, len(input_buffer))
        if outgoing.pbData:
            ctypes.memset(outgoing.pbData, 0, outgoing.cbData)
            kernel32.LocalFree(ctypes.cast(outgoing.pbData, ctypes.c_void_p))


def load_saved_credentials(path: Path | None = None) -> Credentials:
    path = path if path is not None else vault_path()
    if path.is_symlink() or path.parent.is_symlink():
        raise VaultError("Refusing to read credentials through a symbolic link.")
    try:
        raw = path.read_bytes()
    except OSError:
        raise VaultError("Saved Bitunix credentials were not found. Run the earlier Bitunix connection checker and save the key locally.") from None
    if len(raw) > VAULT_MAX_BYTES or not raw.startswith(VAULT_MAGIC):
        raise VaultError("Saved Bitunix credential file has an unsupported format.")
    clear = _dpapi_decrypt(raw[len(VAULT_MAGIC):])
    try:
        record = json.loads(clear.decode("utf-8"))
        if not isinstance(record, dict) or record.get("schema") != 1:
            raise ValueError
        return Credentials(record["api_key"], record["secret_key"])
    except (ValueError, KeyError, UnicodeError, TypeError, VaultError):
        raise VaultError("Saved Bitunix credential format is invalid. Re-run the Bitunix connection checker.") from None


def canonical_query(params: Mapping[str, str]) -> str:
    return "".join(k + params[k] for k in sorted(params))


def _compact_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _sign(credentials: Credentials, query: Mapping[str, str], body: str, nonce: str, timestamp: str) -> str:
    message = nonce + timestamp + credentials.api_key + canonical_query(query) + body
    first = hashlib.sha256(message.encode("utf-8")).hexdigest()
    return hashlib.sha256((first + credentials.secret_key).encode("utf-8")).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError("HTTP redirect blocked to protect Bitunix credentials.")


def default_opener() -> urllib.request.OpenerDirector:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=context)
    )


def _result_code(value: Any) -> int | None:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isascii() and value.lstrip("-").isdigit() and len(value) < 12:
        return int(value)
    return None


def _decode(body: bytes) -> Any:
    if len(body) > MAX_RESPONSE_BYTES:
        raise ApiError("Bitunix response exceeded the safety size limit.")
    try:
        obj = json.loads(body.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        raise ApiError("Bitunix returned a non-JSON response.") from None
    if not isinstance(obj, dict):
        raise ApiError("Unexpected Bitunix response structure.")
    code = _result_code(obj.get("code"))
    if code != 0:
        msg = str(obj.get("msg") or "")[:220]
        if code == 403:
            raise ApiError("Bitunix API key does not permit trading (API code 403).")
        if code == 10004:
            raise ApiError("This computer/server IP is not in the Bitunix API-key whitelist (API code 10004).")
        if code == 20003:
            raise ApiError("Insufficient Bitunix futures balance (API code 20003).")
        if code == 20005:
            raise ApiError("Bitunix rejected the selected leverage (API code 20005).")
        raise ApiError(f"Bitunix API operation failed with code {code if code is not None else 'UNKNOWN'}{': ' + msg if msg else ''}.")
    return obj.get("data")


def decimal_value(value: Any, name: str = "value") -> Decimal:
    if value is None or isinstance(value, bool):
        raise ApiError(f"Missing numeric value: {name}.")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ApiError(f"Invalid numeric value: {name}.") from None
    if not d.is_finite():
        raise ApiError(f"Invalid numeric value: {name}.")
    return d


def rows(data: Any, keys: tuple[str, ...] = ("list", "data", "orderList", "positionList", "tradeList")) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return [x for x in data[key] if isinstance(x, dict)]
        return [data]
    return []


def round_down(value: Decimal, precision: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)


def fmt_decimal(value: Decimal, precision: int | None = None) -> str:
    if precision is not None:
        return f"{value:.{precision}f}"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


class BitunixClient:
    def __init__(self, credentials: Credentials | None = None, opener=None):
        self.credentials = credentials or load_saved_credentials()
        self.opener = opener if opener is not None else default_opener()

    def _headers(self, query: Mapping[str, str], body: str, private: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "language": "en-US",
            "User-Agent": f"BitunixScalper/{VERSION}",
        }
        if private:
            nonce = secrets.token_hex(16)
            timestamp = str(time.time_ns() // 1_000_000)
            headers.update({
                "api-key": self.credentials.api_key,
                "nonce": nonce,
                "timestamp": timestamp,
                "sign": _sign(self.credentials, query, body, nonce, timestamp),
            })
        return headers

    def get(self, endpoint: str, params: Mapping[str, Any] | None = None) -> Any:
        if endpoint not in GET_ENDPOINTS:
            raise ApiError("Blocked GET endpoint: not on allowlist.")
        query: dict[str, str] = {}
        for key, value in (params or {}).items():
            if value is None:
                continue
            k, v = str(key), str(value)
            if not k.isascii() or not v.isascii():
                raise ApiError("Non-ASCII Bitunix query parameter blocked.")
            query[k] = v
        url = BASE_URL + GET_ENDPOINTS[endpoint]
        if query:
            url += "?" + urllib.parse.urlencode(sorted(query.items()))
        request = urllib.request.Request(
            url,
            data=None,
            headers=self._headers(query, "", endpoint not in PUBLIC_GETS),
            method="GET",
        )
        return self._send(request)

    def post(self, endpoint: str, payload: Mapping[str, Any]) -> Any:
        if endpoint not in POST_ENDPOINTS:
            raise ApiError("Blocked POST endpoint: not on allowlist.")
        body = _compact_json(payload)
        request = urllib.request.Request(
            BASE_URL + POST_ENDPOINTS[endpoint],
            data=body.encode("utf-8"),
            headers=self._headers({}, body, True),
            method="POST",
        )
        return self._send(request)

    def _send(self, request: urllib.request.Request) -> Any:
        try:
            with self.opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                if response.status != 200:
                    raise ApiError(f"Bitunix returned HTTP {int(response.status)}.")
                return _decode(response.read(MAX_RESPONSE_BYTES + 1))
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                body = exc.read(MAX_RESPONSE_BYTES + 1)
            finally:
                exc.close()
            try:
                return _decode(body)
            except ApiError as inner:
                raise inner from None
            except Exception:
                raise ApiError(f"Bitunix returned HTTP {status}.") from None
        except urllib.error.URLError:
            raise ApiError("Could not reach fapi.bitunix.com.") from None
        except (TimeoutError, OSError):
            raise ApiError("Bitunix request timed out.") from None

    def contracts(self) -> list[dict[str, Any]]:
        return rows(self.get("contract", {}))

    def tickers(self) -> list[dict[str, Any]]:
        return rows(self.get("tickers", {}))

    def tickers_for(self, symbols: list[str]) -> list[dict[str, Any]]:
        return rows(self.get("tickers", {"symbols": ",".join(symbols)}))

    def funding_rates(self) -> list[dict[str, Any]]:
        return rows(self.get("funding_batch", {}))

    def kline(self, symbol: str, interval: str, limit: int = 120) -> list[dict[str, Any]]:
        raw = rows(self.get("kline", {"symbol": symbol, "interval": interval, "limit": limit}))
        def key(row: dict[str, Any]) -> int:
            try:
                return int(row.get("time") or row.get("ts") or 0)
            except (TypeError, ValueError):
                return 0
        return sorted(raw, key=key)

    def account(self) -> dict[str, Any]:
        data = self.get("account", {"marginCoin": "USDT"})
        r = rows(data)
        if len(r) != 1:
            raise ApiError("Could not read the USDT futures account uniquely.")
        row = r[0]
        return {
            "available": decimal_value(row.get("available"), "available"),
            "position_mode": str(row.get("positionMode") or "UNKNOWN").upper(),
        }

    def settings(self, symbol: str) -> dict[str, Any]:
        data = self.get("settings", {"marginCoin": "USDT", "symbol": symbol})
        r = [x for x in rows(data) if str(x.get("symbol") or "") == symbol]
        if len(r) != 1:
            raise ApiError("Could not verify Bitunix leverage/margin mode.")
        return {"leverage": int(r[0].get("leverage")), "margin_mode": str(r[0].get("marginMode") or "").upper()}

    def positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"includeSubAccounts": "false"}
        if symbol:
            params["symbol"] = symbol
        out = []
        for row in rows(self.get("positions", params)):
            try:
                if decimal_value(row.get("qty", "0"), "qty") > 0:
                    out.append(row)
            except ApiError:
                continue
        return out

    def pending_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"skip": "0", "limit": "100"}
        if symbol:
            params["symbol"] = symbol
        return rows(self.get("orders", params))

    def history_position(self, position_id: str) -> dict[str, Any] | None:
        r = rows(self.get("history_positions", {"positionId": position_id, "skip": "0", "limit": "20"}))
        matches = [x for x in r if str(x.get("positionId") or "") == position_id]
        return matches[0] if matches else None

    def order_detail(self, order_id: str) -> dict[str, Any]:
        r = rows(self.get("order_detail", {"orderId": order_id}))
        if len(r) != 1:
            raise ApiError("Could not confirm the Bitunix order uniquely.")
        return r[0]

    def ensure_isolated_leverage(self, symbol: str, leverage: int) -> None:
        current = self.settings(symbol)
        if current["margin_mode"] != "ISOLATION":
            self.post("change_margin_mode", {"marginMode": "ISOLATION", "symbol": symbol, "marginCoin": "USDT"})
        current = self.settings(symbol)
        if current["leverage"] != leverage:
            self.post("change_leverage", {"symbol": symbol, "leverage": leverage, "marginCoin": "USDT"})
        verified = self.settings(symbol)
        if verified["margin_mode"] != "ISOLATION" or verified["leverage"] != leverage:
            raise ApiError("Bitunix settings could not be verified after the change.")

    def place_market_with_brackets(self, *, symbol: str, qty: str, direction: str,
                                   take_profit: str, stop_loss: str, client_id: str,
                                   hedge_mode: bool = True) -> tuple[str, str | None]:
        side = "BUY" if direction == "LONG" else "SELL"
        payload: dict[str, Any] = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "orderType": "MARKET",
            "clientId": client_id,
            "reduceOnly": False,
            "tpPrice": take_profit,
            "tpStopType": "MARK_PRICE",
            "tpOrderType": "MARKET",
            "slPrice": stop_loss,
            "slStopType": "MARK_PRICE",
            "slOrderType": "MARKET",
        }
        if hedge_mode:
            payload["tradeSide"] = "OPEN"
        data = self.post("place_order", payload)
        r = rows(data)
        if len(r) != 1:
            raise ApiError("Bitunix place-order response was ambiguous; do not retry blindly.")
        order_id = str(r[0].get("orderId") or "")
        returned_client_id = str(r[0].get("clientId") or "") or None
        if not order_id:
            raise ApiError("Bitunix place-order response omitted orderId; do not retry blindly.")
        return order_id, returned_client_id

    def flash_close(self, position_id: str) -> None:
        self.post("flash_close", {"positionId": position_id})

    def modify_position_tpsl(self, *, symbol: str, position_id: str,
                             take_profit: str | None = None, stop_loss: str | None = None) -> None:
        payload: dict[str, Any] = {"symbol": symbol, "positionId": position_id}
        if take_profit is not None:
            payload.update({"tpPrice": take_profit, "tpStopType": "MARK_PRICE"})
        if stop_loss is not None:
            payload.update({"slPrice": stop_loss, "slStopType": "MARK_PRICE"})
        if take_profit is None and stop_loss is None:
            raise ApiError("At least one TP/SL value is required.")
        self.post("modify_position_tpsl", payload)
