"""
Client Python untuk OAK Research Remote MCP Server (https://oakresearch.io/api/mcp).

Protokol:
  - MCP Streamable HTTP (JSON-RPC 2.0, mendukung respons `application/json` dan `text/event-stream`)
  - OAuth 2.1 (Dynamic Client Registration + PKCE S256 + rotating `refresh_token`)
  - Semua tool bersifat read-only.

Skema tool terverifikasi live terhadap `https://oakresearch.io/api/mcp` (16 tools):
  Gratis (akun OAK Research gratis):
    - `get_market_overview(locale="en")`
    - `get_calendar(from="YYYY-MM-DD", to="YYYY-MM-DD")`
    - `get_news(locale="en", limit=15)`
    - `get_tradfi(ticker="NVDA")`
    - `get_crypto(id="bitcoin", locale="en")`
    - `search(query="SOL", locale="en")`
    - `get_project(id=...)`, `list_articles(...)`, `get_article(...)`
  Premium (opsional bila `OAK_MCP_PREMIUM=true`):
    - `get_alpha_feed(locale="en", limit=10)`
    - `get_hyperliquid_smart_money(coin="BTC", limit=20)`
    - `get_daily_recap`, `get_oak_index`, `get_portfolios`, `get_yields`, `get_unusual_activity`

Fail-safe & Rotasi Token:
  - Jika kredensial belum dikonfigurasi atau `OAK_MCP_ENABLED=off`, seluruh helper langsung
    mengembalikan `{"available": False}` dengan nol panggilan jaringan.
  - OAK merotasi `OAK_MCP_REFRESH_TOKEN` setiap refresh; token baru otomatis ditulis balik ke
    GitHub Secret (`token_manager.update_github_secret`) dan/atau `.env` lokal.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

log = logging.getLogger("oak_mcp")

OAK_MCP_URL = os.getenv("OAK_MCP_URL", "https://oakresearch.io/api/mcp").strip()
OAK_OAUTH_ISSUER = "https://oakresearch.io"
OAK_AUTHORIZE_URL = f"{OAK_OAUTH_ISSUER}/mcp/authorize"
OAK_TOKEN_URL = f"{OAK_OAUTH_ISSUER}/api/mcp/oauth/token"
OAK_REGISTER_URL = f"{OAK_OAUTH_ISSUER}/api/mcp/oauth/register"

_MCP_PROTOCOL_VERSION = "2025-03-26"

# Peta ticker -> CoinGecko/OAK id untuk get_crypto (ticker lain otomatis dicari lewat tool `search`)
_CRYPTO_TICKER_TO_OAK_ID: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "ripple",
    "BNB": "binancecoin",
    "LINK": "chainlink",
    "AVAX": "avalanche-2",
    "AAVE": "aave",
    "SUI": "sui",
    "NEAR": "near",
    "HYPE": "hyperliquid",
    "VVV": "venice-token",
    "DOGE": "dogecoin",
    "ADA": "cardano",
}

# Cache in-process agar market overview, kalender makro, & news global hanya ditarik 1x per siklus
_GLOBAL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_ASSET_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_SLUG_CACHE: dict[str, str] = dict(_CRYPTO_TICKER_TO_OAK_ID)
_CACHE_TTL_SEC = 900.0  # 15 menit


def is_oak_mcp_enabled() -> bool:
    """True jika fitur OAK MCP diaktifkan (default: on)."""
    return os.getenv("OAK_MCP_ENABLED", "on").strip().lower() not in (
        "off", "0", "false", "no", "disabled"
    )


def is_oak_mcp_premium() -> bool:
    """True jika akun OAK memiliki langganan Premium untuk tool tambahan."""
    return os.getenv("OAK_MCP_PREMIUM", "false").strip().lower() in (
        "1", "true", "yes", "on"
    )


def is_oak_mcp_configured() -> bool:
    """True jika OAK MCP aktif dan memiliki access_token atau (client_id + refresh_token)."""
    if not is_oak_mcp_enabled():
        return False
    if os.getenv("OAK_MCP_ACCESS_TOKEN", "").strip():
        return True
    cid = os.getenv("OAK_MCP_CLIENT_ID", "").strip()
    rt = os.getenv("OAK_MCP_REFRESH_TOKEN", "").strip()
    return bool(cid and rt)


def reset_oak_cache() -> None:
    """Bersihkan cache in-memory OAK MCP (berguna untuk unit test)."""
    global _SHARED_CLIENT
    _GLOBAL_CACHE.clear()
    _ASSET_CACHE.clear()
    _SLUG_CACHE.clear()
    _SLUG_CACHE.update(_CRYPTO_TICKER_TO_OAK_ID)
    _SHARED_CLIENT = None


def _parse_mcp_http_body(content_type: str, text: str) -> dict[str, Any]:
    """Parse respons MCP Streamable HTTP (JSON langsung atau SSE `data: ...`)."""
    raw = (text or "").strip()
    if not raw:
        return {}
    if "text/event-stream" in (content_type or "").lower() or raw.startswith("event:") or raw.startswith("data:"):
        last_obj: dict[str, Any] = {}
        for line in raw.splitlines():
            s = line.strip()
            if s.startswith("data:"):
                payload_str = s[5:].strip()
                if not payload_str or payload_str == "[DONE]":
                    continue
                try:
                    parsed = json.loads(payload_str)
                    if isinstance(parsed, dict):
                        last_obj = parsed
                except json.JSONDecodeError:
                    continue
        if last_obj:
            return last_obj
    return json.loads(raw)


def _extract_tool_text(result_obj: Any) -> str:
    """Ekstrak teks bersih dari payload `result` MCP (`content: [{type: text, text: ...}]`)."""
    if isinstance(result_obj, str):
        return result_obj.strip()
    if not isinstance(result_obj, dict):
        return ""
    if "structuredContent" in result_obj and result_obj["structuredContent"]:
        try:
            return json.dumps(result_obj["structuredContent"], ensure_ascii=False)
        except Exception:
            pass
    parts: list[str] = []
    for item in result_obj.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            txt = str(item.get("text") or "").strip()
            if txt:
                parts.append(txt)
    if parts:
        return "\n".join(parts)
    return json.dumps(result_obj, ensure_ascii=False)


def _persist_oak_refresh_token(new_refresh_token: str) -> bool:
    """
    Simpan `OAK_MCP_REFRESH_TOKEN` yang baru di-rotate oleh OAK OAuth:
      1. Update `.env` lokal bila ada dan bukan di runner GitHub Actions.
      2. Update GitHub Repository Secret `OAK_MCP_REFRESH_TOKEN` bila `GH_PAT` & `GITHUB_REPOSITORY` ada.
    """
    persisted = False
    try:
        import token_manager

        in_ci = os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"
        env_path = token_manager.env_file_path()
        if not in_ci and env_path.exists():
            raw = env_path.read_text(encoding="utf-8")
            if re.search(r"(?m)^OAK_MCP_REFRESH_TOKEN=", raw):
                upd = re.sub(
                    r"(?m)^OAK_MCP_REFRESH_TOKEN=.*$",
                    f"OAK_MCP_REFRESH_TOKEN={new_refresh_token}",
                    raw,
                )
            else:
                sep = "" if raw.endswith("\n") else "\n"
                upd = f"{raw}{sep}OAK_MCP_REFRESH_TOKEN={new_refresh_token}\n"
            env_path.write_text(upd, encoding="utf-8")
            persisted = True
            log.info("OAK_MCP_REFRESH_TOKEN ter-rotate dan diperbarui di .env lokal.")

        pat = os.environ.get("GH_PAT", "").strip()
        repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
        if pat and repo:
            ok = token_manager.update_github_secret(repo, "OAK_MCP_REFRESH_TOKEN", new_refresh_token, pat)
            if ok:
                persisted = True
                log.info("OAK_MCP_REFRESH_TOKEN ter-rotate dan disinkronkan ke GitHub Secrets.")
    except Exception as exc:
        log.warning(f"Persist OAK_MCP_REFRESH_TOKEN gagal: {exc}")
    return persisted


class OakMCPClient:
    """Client ringan untuk OAK Research Remote MCP Server (Streamable HTTP + OAuth 2.1)."""

    def __init__(
        self,
        mcp_url: Optional[str] = None,
        access_token: Optional[str] = None,
        client_id: Optional[str] = None,
        refresh_token: Optional[str] = None,
        timeout: float = 20.0,
    ):
        self.mcp_url = (mcp_url or os.getenv("OAK_MCP_URL", OAK_MCP_URL)).strip()
        self.access_token = (access_token or os.getenv("OAK_MCP_ACCESS_TOKEN", "")).strip()
        self.client_id = (client_id or os.getenv("OAK_MCP_CLIENT_ID", "")).strip()
        self.refresh_token = (refresh_token or os.getenv("OAK_MCP_REFRESH_TOKEN", "")).strip()
        self.token_expires_at: float = 0.0
        self.session_id: Optional[str] = None
        self._initialized: bool = False
        self._req_id: int = 0
        self.client = httpx.Client(timeout=timeout)

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def refresh_access_token(self) -> str:
        """Tukar `OAK_MCP_REFRESH_TOKEN` menjadi access_token baru via OAuth 2.1 token endpoint."""
        if not (self.client_id and self.refresh_token):
            raise RuntimeError("OAK_MCP_CLIENT_ID dan OAK_MCP_REFRESH_TOKEN belum di-set.")
        r = self.client.post(
            OAK_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": self.refresh_token,
            },
            headers={"Accept": "application/json", "User-Agent": "WaltBot/1.0"},
        )
        r.raise_for_status()
        data = r.json()
        tok = str(data.get("access_token") or "").strip()
        if not tok:
            raise RuntimeError(f"OAK OAuth refresh tidak mengembalikan access_token: {list(data.keys())}")
        self.access_token = tok
        expires_in = float(data.get("expires_in", 3600) or 3600)
        self.token_expires_at = time.time() + max(60.0, expires_in - 60.0)
        new_rt = str(data.get("refresh_token") or "").strip()
        if new_rt and new_rt != self.refresh_token:
            self.refresh_token = new_rt
            os.environ["OAK_MCP_REFRESH_TOKEN"] = new_rt
            _persist_oak_refresh_token(new_rt)
        return self.access_token

    def _ensure_token(self) -> str:
        if self.access_token and (self.token_expires_at == 0.0 or time.time() < self.token_expires_at):
            return self.access_token
        if self.client_id and self.refresh_token:
            return self.refresh_access_token()
        if self.access_token:
            return self.access_token
        raise RuntimeError("Tidak ada kredensial OAK MCP (set OAK_MCP_ACCESS_TOKEN atau OAK_MCP_REFRESH_TOKEN).")

    def _headers(self) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self._ensure_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "WaltBot/1.0",
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _rpc(self, method: str, params: Optional[dict[str, Any]] = None, is_notification: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not is_notification:
            payload["id"] = self._next_id()
        if params is not None:
            payload["params"] = params

        r = self.client.post(self.mcp_url, json=payload, headers=self._headers())
        # Retry 1x jika access_token kadaluarsa (401) dan refresh_token tersedia
        if r.status_code == 401 and self.client_id and self.refresh_token:
            log.info("OAK MCP access_token kadaluarsa (401) -> refresh token & coba ulang...")
            self.refresh_access_token()
            self.session_id = None
            r = self.client.post(self.mcp_url, json=payload, headers=self._headers())

        if r.status_code == 202 and is_notification:
            return {}
        r.raise_for_status()

        sid = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid.strip()
        if is_notification or not r.text.strip():
            return {}
        data = _parse_mcp_http_body(r.headers.get("content-type", ""), r.text)
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise RuntimeError(f"OAK MCP RPC error ({method}): {err}")
        return data

    def initialize(self) -> dict[str, Any]:
        """Handshake MCP initialize + notifications/initialized."""
        if self._initialized:
            return {"already_initialized": True}
        res = self._rpc(
            "initialize",
            {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "walt-trading-bot", "version": "1.0.0"},
            },
        )
        try:
            self._rpc("notifications/initialized", is_notification=True)
        except Exception:
            pass
        self._initialized = True
        return res

    def list_tools(self) -> list[dict[str, Any]]:
        """Daftar seluruh tool read-only yang tersedia di OAK Research MCP Server."""
        self.initialize()
        res = self._rpc("tools/list", {})
        return list((res.get("result") or {}).get("tools") or [])

    def call_tool(self, name: str, arguments: Optional[dict[str, Any]] = None) -> str:
        """Panggil satu tool OAK MCP dan kembalikan teks hasil yang bersih."""
        self.initialize()
        res = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        result_obj = res.get("result") if isinstance(res, dict) else res
        if isinstance(result_obj, dict) and result_obj.get("isError"):
            raise RuntimeError(f"OAK MCP tool '{name}' returned error: {_extract_tool_text(result_obj)[:200]}")
        return _extract_tool_text(result_obj)


_SHARED_CLIENT: Optional[OakMCPClient] = None


def get_oak_client() -> Optional[OakMCPClient]:
    """Dapatkan singleton OakMCPClient bila kredensial tersedia; None bila belum dikonfigurasi."""
    global _SHARED_CLIENT
    if not is_oak_mcp_configured():
        return None
    if _SHARED_CLIENT is None:
        _SHARED_CLIENT = OakMCPClient()
    return _SHARED_CLIENT


def _resolve_crypto_oak_id(cli: OakMCPClient, ticker: str) -> Optional[str]:
    """Petakan ticker crypto (mis. BTC, SOL) ke slug `id` OAK (`bitcoin`, `solana`)."""
    sym = str(ticker or "").strip().upper()
    if sym in _SLUG_CACHE:
        return _SLUG_CACHE[sym]
    try:
        raw = cli.call_tool("search", {"query": sym, "locale": "en"})
        parsed = json.loads(raw)
        for item in parsed.get("cryptocurrencies") or []:
            if isinstance(item, dict) and str(item.get("symbol", "")).strip().upper() == sym:
                slug = str(item.get("id") or "").strip()
                if slug:
                    _SLUG_CACHE[sym] = slug
                    return slug
    except Exception as exc:
        log.debug(f"OAK MCP search({sym}) gagal resolve slug: {exc}")
    return None


def _summarize_market_overview(raw_text: str) -> str:
    try:
        d = json.loads(raw_text)
        parts = []
        mcap = d.get("totalMarketCapUsd") or {}
        if isinstance(mcap, dict) and mcap.get("value"):
            parts.append(f"Crypto MCap=${mcap['value']/1e12:.2f}T (30d {mcap.get('change30dPct', 0):+.1f}%)")
        tvl = d.get("defiTvlUsd") or {}
        if isinstance(tvl, dict) and tvl.get("value"):
            parts.append(f"DeFi TVL=${tvl['value']/1e9:.1f}B (30d {tvl.get('change30dPct', 0):+.1f}%)")
        stb = d.get("stablecoinSupplyUsd") or {}
        if isinstance(stb, dict) and stb.get("value"):
            parts.append(f"Stablecoins=${stb['value']/1e9:.1f}B")
        dom = d.get("btcDominancePct") or d.get("btcDominance")
        if isinstance(dom, dict) and dom.get("value"):
            parts.append(f"BTC.D={dom['value']:.1f}%")
        elif isinstance(dom, (int, float)):
            parts.append(f"BTC.D={dom:.1f}%")
        if parts:
            return " | ".join(parts)
    except Exception:
        pass
    return raw_text[:450]


def _summarize_calendar(raw_text: str, focus_symbol: Optional[str] = None) -> str:
    try:
        d = json.loads(raw_text)
        events = d.get("events") or []
        if not isinstance(events, list):
            return raw_text[:400]
        sym_u = str(focus_symbol or "").strip().upper()
        picked = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            ev_sym = str(ev.get("symbol") or "").strip().upper()
            imp = str(ev.get("importance") or "").strip().lower()
            kind = str(ev.get("kind") or "").strip().lower()
            if (sym_u and ev_sym == sym_u) or imp in ("high", "medium") or kind in ("macro", "earnings"):
                dt = str(ev.get("startsAt") or "")[:10]
                name = str(ev.get("name") or ev.get("title") or ev_sym).strip()
                picked.append(f"{dt} [{kind}/{imp or 'std'}] {name}" + (f" ({ev_sym})" if ev_sym else ""))
            if len(picked) >= 6:
                break
        if picked:
            return "; ".join(picked)
        # fallback 3 event pertama bila tidak ada high/medium
        for ev in events[:3]:
            if isinstance(ev, dict):
                dt = str(ev.get("startsAt") or "")[:10]
                picked.append(f"{dt} [{ev.get('kind', 'event')}] {ev.get('name') or ev.get('symbol', '')}")
        return "; ".join(picked)
    except Exception:
        return raw_text[:400]


def _extract_news_items(raw_text: str) -> list[dict[str, str]]:
    if not raw_text:
        return []
    try:
        parsed = json.loads(raw_text)
        items = parsed if isinstance(parsed, list) else (
            parsed.get("items") or parsed.get("news") or parsed.get("articles") or parsed.get("data") or []
        )
        out = []
        for it in items:
            if isinstance(it, dict):
                title = str(it.get("title") or it.get("headline") or "").strip()
                summary = str(it.get("summary") or "").strip()
                if title:
                    out.append({"title": title, "summary": summary})
            elif isinstance(it, str) and it.strip():
                out.append({"title": it.strip(), "summary": ""})
        return out
    except Exception:
        out = []
        for line in raw_text.splitlines():
            clean = line.strip().lstrip("-*•0123456789.) ").strip()
            if 18 <= len(clean) <= 220 and not clean.startswith(("{", "}", "[", "]", "http")):
                out.append({"title": clean, "summary": ""})
        return out


def _summarize_asset_json(raw_text: str, asset_type: str) -> str:
    try:
        d = json.loads(raw_text)
        if not isinstance(d, dict):
            return raw_text[:500]
        name = d.get("name") or d.get("symbol") or ""
        price = d.get("current_price")
        chg = d.get("price_change_percentage_24h")
        if chg is None:
            chg = d.get("price_change_percentage_24h_in_currency")
        mcap = d.get("market_cap")
        hi, lo = d.get("high_24h"), d.get("low_24h")
        parts = [f"{name} ({asset_type})"]
        if isinstance(price, (int, float)):
            parts.append(f"price=${price:,.4f}" if price < 100 else f"price=${price:,.2f}")
        if isinstance(chg, (int, float)):
            parts.append(f"24h={chg:+.2f}%")
        if isinstance(hi, (int, float)) and isinstance(lo, (int, float)):
            parts.append(f"range24h=${lo:,.2f}-${hi:,.2f}")
        if isinstance(mcap, (int, float)) and mcap > 0:
            parts.append(f"MCap=${mcap/1e9:.1f}B")
        return " | ".join(parts)
    except Exception:
        return raw_text[:500]


def fetch_oak_global_brief(client: Optional[OakMCPClient] = None) -> dict[str, Any]:
    """Ambil ringkasan pasar global, kalender 7 hari (earnings/macro/unlocks), & berita utama 1x per 15 menit."""
    now = time.time()
    cached = _GLOBAL_CACHE.get("global")
    if cached and (now - cached[0]) < _CACHE_TTL_SEC:
        return dict(cached[1])

    cli = client or get_oak_client()
    if cli is None:
        return {"available": False, "reason": "not_configured"}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    next7 = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")

    out: dict[str, Any] = {
        "available": False,
        "market_overview": "",
        "calendar_raw": "",
        "calendar": "",
        "news_items": [],
        "alpha_feed": "",
        "errors": [],
    }
    try:
        ov_raw = cli.call_tool("get_market_overview", {"locale": "en"})
        out["market_overview"] = _summarize_market_overview(ov_raw)
        out["available"] = True
    except Exception as exc:
        out["errors"].append(f"get_market_overview:{type(exc).__name__}")
        log.warning(f"OAK MCP get_market_overview gagal: {exc}")

    try:
        cal_raw = cli.call_tool("get_calendar", {"from": today, "to": next7})
        out["calendar_raw"] = cal_raw
        out["calendar"] = _summarize_calendar(cal_raw)
        out["available"] = True
    except Exception as exc:
        out["errors"].append(f"get_calendar:{type(exc).__name__}")
        log.warning(f"OAK MCP get_calendar gagal: {exc}")

    try:
        news_raw = cli.call_tool("get_news", {"locale": "en", "limit": 15})
        out["news_items"] = _extract_news_items(news_raw)
        if out["news_items"]:
            out["available"] = True
    except Exception as exc:
        out["errors"].append(f"get_news:{type(exc).__name__}")
        log.warning(f"OAK MCP get_news gagal: {exc}")

    if is_oak_mcp_premium():
        try:
            out["alpha_feed"] = cli.call_tool("get_alpha_feed", {"locale": "en", "limit": 10})[:900]
            out["available"] = True
        except Exception as exc:
            out["errors"].append(f"get_alpha_feed:{type(exc).__name__}")

    if not out["available"]:
        out["reason"] = "all_tools_failed"

    _GLOBAL_CACHE["global"] = (now, dict(out))
    return out


def fetch_oak_market_context(
    ticker: str,
    *,
    is_crypto: Optional[bool] = None,
    client: Optional[OakMCPClient] = None,
) -> dict[str, Any]:
    """
    Tarik konteks riset spesifik aset dari OAK Research MCP Server untuk memperkaya 4-Voter Swarm
    dan setiap keputusan trading:
      - Crypto: `get_crypto(id=slug, locale="en")` (+ `get_hyperliquid_smart_money(coin=sym)` bila Premium)
      - TradFi (Saham AS / Komoditas): `get_tradfi(ticker=sym)`
      - Berita relevan & kalender makro/earnings/unlocks dari `fetch_oak_global_brief()`.
    """
    sym = str(ticker or "").strip().upper()
    if not sym:
        return {"available": False, "reason": "empty_ticker", "headlines": []}

    now = time.time()
    cached = _ASSET_CACHE.get(sym)
    if cached and (now - cached[0]) < _CACHE_TTL_SEC:
        return dict(cached[1])

    cli = client or get_oak_client()
    if cli is None:
        return {"available": False, "reason": "not_configured", "ticker": sym, "headlines": []}

    asset_is_crypto = (sym in _CRYPTO_TICKER_TO_OAK_ID) if is_crypto is None else bool(is_crypto)
    global_brief = fetch_oak_global_brief(client=cli)

    cal_summary = (
        _summarize_calendar(global_brief.get("calendar_raw", ""), focus_symbol=sym)
        if global_brief.get("calendar_raw")
        else global_brief.get("calendar", "")
    )
    out: dict[str, Any] = {
        "available": bool(global_brief.get("available")),
        "ticker": sym,
        "asset_type": "crypto" if asset_is_crypto else "tradfi",
        "asset_data": "",
        "headlines": [],
        "market_overview": global_brief.get("market_overview", ""),
        "calendar": cal_summary,
        "smart_money": "",
        "errors": list(global_brief.get("errors", [])),
    }

    slug_name = ""
    if asset_is_crypto:
        slug = _resolve_crypto_oak_id(cli, sym)
        slug_name = slug or ""
        if slug:
            try:
                raw_asset = cli.call_tool("get_crypto", {"id": slug, "locale": "en"})
                out["asset_data"] = _summarize_asset_json(raw_asset, "crypto")
                out["available"] = True
            except Exception as exc:
                out["errors"].append(f"get_crypto:{type(exc).__name__}")
                log.debug(f"OAK MCP get_crypto({slug}) gagal: {exc}")
    else:
        try:
            raw_asset = cli.call_tool("get_tradfi", {"ticker": sym})
            out["asset_data"] = _summarize_asset_json(raw_asset, "tradfi")
            out["available"] = True
        except Exception as exc:
            out["errors"].append(f"get_tradfi:{type(exc).__name__}")
            log.debug(f"OAK MCP get_tradfi({sym}) gagal: {exc}")

    # Filter berita OAK yang relevan dengan aset (cocok ticker atau nama slug)
    keywords = {sym.lower()}
    if slug_name:
        keywords.add(slug_name.lower().replace("-", " "))
    matched_headlines: list[str] = []
    for item in global_brief.get("news_items") or []:
        blob = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        if any(re.search(rf"\b{re.escape(kw)}\b", blob) for kw in keywords if len(kw) >= 2):
            matched_headlines.append(item["title"])
    out["headlines"] = matched_headlines[:6]

    if is_oak_mcp_premium() and asset_is_crypto:
        try:
            out["smart_money"] = cli.call_tool("get_hyperliquid_smart_money", {"coin": sym, "limit": 20})[:900]
            out["available"] = True
        except Exception as exc:
            out["errors"].append(f"get_hyperliquid_smart_money:{type(exc).__name__}")

    _ASSET_CACHE[sym] = (now, dict(out))
    return out


def format_oak_context_for_prompt(oak_ctx: Optional[dict[str, Any]]) -> str:
    """Susun blok konteks OAK Research yang ringkas dan bebas noise untuk prompt LLM."""
    if not oak_ctx or not oak_ctx.get("available"):
        return ""
    blocks: list[str] = ["[DATA REAL-TIME OAK RESEARCH MCP]"]
    if oak_ctx.get("asset_data"):
        blocks.append(f"- Profil & Metrik Aset ({oak_ctx.get('ticker', '')}): {oak_ctx['asset_data'][:600]}")
    if oak_ctx.get("market_overview"):
        blocks.append(f"- Ikhtisar Pasar & Likuiditas Global: {oak_ctx['market_overview'][:450]}")
    if oak_ctx.get("calendar"):
        blocks.append(f"- Kalender Makro / Earnings / Token Unlocks (7h): {oak_ctx['calendar'][:450]}")
    if oak_ctx.get("smart_money"):
        blocks.append(f"- Hyperliquid Smart Money Top 100: {oak_ctx['smart_money'][:400]}")
    return "\n".join(blocks) if len(blocks) > 1 else ""
