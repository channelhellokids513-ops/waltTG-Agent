"""
Client Python untuk OAK Research Remote MCP Server (https://oakresearch.io/api/mcp).

Protokol:
  - MCP Streamable HTTP (JSON-RPC 2.0, mendukung respons `application/json` dan `text/event-stream`)
  - OAuth 2.1 (Dynamic Client Registration + PKCE S256 + refresh_token)
  - Semua tool bersifat read-only.

Tool gratis (akun OAK Research gratis):
  `search`, `get_crypto`, `get_tradfi`, `get_project`, `get_market_overview`,
  `list_articles`, `get_article`, `get_news`, `get_calendar`

Tool Premium (opsional bila `OAK_MCP_PREMIUM=true`):
  `get_alpha_feed`, `get_daily_recap`, `get_oak_index`, `get_portfolios`,
  `get_yields`, `get_hyperliquid_smart_money`, `get_unusual_activity`

Fail-safe:
  - Jika kredensial belum dikonfigurasi (`OAK_MCP_ACCESS_TOKEN` atau `OAK_MCP_CLIENT_ID` +
    `OAK_MCP_REFRESH_TOKEN`) atau `OAK_MCP_ENABLED=off`, seluruh helper langsung mengembalikan
    `{"available": False}` dengan nol panggilan jaringan agar jalur trading tidak pernah terganggu.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

import httpx

log = logging.getLogger("oak_mcp")

OAK_MCP_URL = os.getenv("OAK_MCP_URL", "https://oakresearch.io/api/mcp").strip()
OAK_OAUTH_ISSUER = "https://oakresearch.io"
OAK_AUTHORIZE_URL = f"{OAK_OAUTH_ISSUER}/mcp/authorize"
OAK_TOKEN_URL = f"{OAK_OAUTH_ISSUER}/api/mcp/oauth/token"
OAK_REGISTER_URL = f"{OAK_OAUTH_ISSUER}/api/mcp/oauth/register"

_MCP_PROTOCOL_VERSION = "2025-03-26"

# Cache in-process agar market overview & kalender makro hanya ditarik 1x per siklus bot
_GLOBAL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_ASSET_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
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
    _GLOBAL_CACHE.clear()
    _ASSET_CACHE.clear()


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
            self._maybe_sync_rotated_refresh_token(new_rt)
        return self.access_token

    def _maybe_sync_rotated_refresh_token(self, new_refresh_token: str) -> None:
        """Sinkronisasi opsional refresh token OAK yang di-rotate ke GitHub Secret bila tersedia."""
        try:
            gh_pat = os.getenv("GH_PAT", "").strip()
            repo = os.getenv("GITHUB_REPOSITORY", "").strip()
            if not (gh_pat and repo):
                return
            import walt_bot

            sync_fn = getattr(walt_bot, "_sync_github_secret", None)
            if callable(sync_fn):
                sync_fn(repo, "OAK_MCP_REFRESH_TOKEN", new_refresh_token, gh_pat)
                log.info("OAK_MCP_REFRESH_TOKEN ter-rotate dan disinkronkan ke GitHub Secrets.")
        except Exception as exc:
            log.debug(f"Sync OAK_MCP_REFRESH_TOKEN dilewati: {exc}")

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


def _extract_headlines_from_oak_text(text: str, limit: int = 8) -> list[str]:
    """Ekstrak daftar headline berita dari output teks/JSON tool `get_news` OAK."""
    if not text:
        return []
    titles: list[str] = []
    try:
        parsed = json.loads(text)
        items = parsed if isinstance(parsed, list) else (
            parsed.get("news") or parsed.get("items") or parsed.get("articles") or parsed.get("data") or []
        )
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    t = str(it.get("title") or it.get("headline") or it.get("summary") or "").strip()
                    if t:
                        titles.append(t)
                elif isinstance(it, str) and it.strip():
                    titles.append(it.strip())
    except Exception:
        for line in text.splitlines():
            clean = line.strip().lstrip("-*•0123456789.) ").strip()
            if 18 <= len(clean) <= 220 and not clean.startswith(("{", "}", "[", "]", "http")):
                titles.append(clean)
    deduped: list[str] = []
    seen: set[str] = set()
    for t in titles:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            deduped.append(t)
        if len(deduped) >= limit:
            break
    return deduped


def fetch_oak_global_brief(client: Optional[OakMCPClient] = None) -> dict[str, Any]:
    """Ambil ringkasan pasar global & kalender makro/unlocks 1x per 15 menit (cache in-process)."""
    now = time.time()
    cached = _GLOBAL_CACHE.get("global")
    if cached and (now - cached[0]) < _CACHE_TTL_SEC:
        return dict(cached[1])

    cli = client or get_oak_client()
    if cli is None:
        return {"available": False, "reason": "not_configured"}

    out: dict[str, Any] = {
        "available": True,
        "market_overview": "",
        "calendar": "",
        "alpha_feed": "",
        "errors": [],
    }
    for tool_name, field_key, args in (
        ("get_market_overview", "market_overview", {}),
        ("get_calendar", "calendar", {}),
    ):
        try:
            txt = cli.call_tool(tool_name, args)
            out[field_key] = txt[:1400]
        except Exception as exc:
            out["errors"].append(f"{tool_name}:{type(exc).__name__}")
            log.warning(f"OAK MCP {tool_name} gagal: {exc}")

    if is_oak_mcp_premium():
        try:
            out["alpha_feed"] = cli.call_tool("get_alpha_feed", {})[:1000]
        except Exception as exc:
            out["errors"].append(f"get_alpha_feed:{type(exc).__name__}")

    if not out["market_overview"] and not out["calendar"] and not out["alpha_feed"]:
        out["available"] = False
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
      - Crypto: `get_crypto` + `get_news` (+ `get_hyperliquid_smart_money` bila Premium)
      - TradFi (Saham AS / Komoditas): `get_tradfi` + `get_news`
      - Plus konteks makro & kalender global (`get_market_overview`, `get_calendar`).
    """
    sym = str(ticker or "").strip().upper()
    if not sym:
        return {"available": False, "reason": "empty_ticker"}

    now = time.time()
    cached = _ASSET_CACHE.get(sym)
    if cached and (now - cached[0]) < _CACHE_TTL_SEC:
        return dict(cached[1])

    cli = client or get_oak_client()
    if cli is None:
        return {"available": False, "reason": "not_configured", "ticker": sym, "headlines": []}

    crypto_set = {"BTC", "ETH", "SOL", "XRP", "BNB", "LINK", "AVAX", "AAVE", "SUI", "NEAR", "HYPE", "VVV"}
    asset_is_crypto = (sym in crypto_set) if is_crypto is None else bool(is_crypto)

    global_brief = fetch_oak_global_brief(client=cli)
    out: dict[str, Any] = {
        "available": False,
        "ticker": sym,
        "asset_type": "crypto" if asset_is_crypto else "tradfi",
        "asset_data": "",
        "headlines": [],
        "news_raw": "",
        "market_overview": global_brief.get("market_overview", ""),
        "calendar": global_brief.get("calendar", ""),
        "smart_money": "",
        "errors": list(global_brief.get("errors", [])),
    }

    primary_tool = "get_crypto" if asset_is_crypto else "get_tradfi"
    try:
        out["asset_data"] = cli.call_tool(primary_tool, {"query": sym})[:1200]
        out["available"] = True
    except Exception as exc:
        out["errors"].append(f"{primary_tool}:{type(exc).__name__}")
        log.debug(f"OAK MCP {primary_tool}({sym}) gagal: {exc}")

    try:
        news_txt = cli.call_tool("get_news", {"query": sym})
        out["news_raw"] = news_txt[:1400]
        out["headlines"] = _extract_headlines_from_oak_text(news_txt, limit=8)
        if news_txt:
            out["available"] = True
    except Exception as exc:
        out["errors"].append(f"get_news:{type(exc).__name__}")
        log.debug(f"OAK MCP get_news({sym}) gagal: {exc}")

    if is_oak_mcp_premium() and asset_is_crypto:
        try:
            out["smart_money"] = cli.call_tool("get_hyperliquid_smart_money", {"symbol": sym})[:900]
            out["available"] = True
        except Exception as exc:
            out["errors"].append(f"get_hyperliquid_smart_money:{type(exc).__name__}")

    if out["market_overview"] or out["calendar"]:
        out["available"] = True

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
        blocks.append(f"- Kalender Makro / Earnings / Token Unlocks: {oak_ctx['calendar'][:450]}")
    if oak_ctx.get("smart_money"):
        blocks.append(f"- Hyperliquid Smart Money Top 100: {oak_ctx['smart_money'][:400]}")
    return "\n".join(blocks) if len(blocks) > 1 else ""
