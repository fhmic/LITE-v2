#web_search.py
import json
import sys
from pathlib import Path

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


# ── Config / provider selection ─────────────────────────────────────────────
#
# Search never requires any key or account — DuckDuckGo is the always-on
# baseline and needs zero configuration. On top of that, an *optional*
# provider can be configured (in Settings, or api_keys.json) to produce
# nicer, synthesized answers:
#
#   "search_provider": "gemini"   — Google Gemini grounded search (needs
#                                    "gemini_api_key")
#   "search_provider": "custom"   — any OpenAI-compatible chat/completions
#                                    endpoint: LM Studio, Ollama, vLLM,
#                                    LocalAI, or a local NVIDIA inference
#                                    server. Configured via:
#                                      "search_api_url"   e.g. http://localhost:8000
#                                      "search_api_key"   optional, most local
#                                                          servers don't need one
#                                      "search_model"     model name as served
#   "search_provider": "skip"     — DuckDuckGo only, no synthesis step
#
# If no provider is configured at all, or the configured provider fails for
# any reason, everything transparently falls back to raw DuckDuckGo results
# so search and news always return *something* usable.

def _load_config() -> dict:
    try:
        return json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _get_api_key() -> str:
    """Gemini key, if configured. Never raises — returns "" if unset."""
    return (_load_config().get("gemini_api_key") or "").strip()


def _get_search_provider() -> str:
    raw = (_load_config().get("search_provider") or "gemini").strip().lower()
    return raw if raw in ("gemini", "custom", "skip") else "gemini"


def _get_custom_endpoint() -> tuple[str, str, str]:
    """Returns (base_url, api_key, model) for a custom OpenAI-compatible provider."""
    cfg = _load_config()
    url   = (cfg.get("search_api_url") or "").strip().rstrip("/")
    key   = (cfg.get("search_api_key") or "").strip()
    model = (cfg.get("search_model")   or "").strip() or "default"
    return url, key, model


def _gemini_search(query: str) -> str:
    key = _get_api_key()
    if not key:
        raise RuntimeError("No Gemini API key configured.")

    from google import genai

    client   = genai.Client(api_key=key)
    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=query,
        config={"tools": [{"google_search": {}}]},
    )

    text = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            text += part.text

    text = text.strip()
    if not text:
        raise ValueError("Gemini returned an empty response.")
    return text


def _custom_synthesize(prompt: str, timeout: int = 20) -> str:
    """
    Ask a locally-configured OpenAI-compatible endpoint (LM Studio, Ollama,
    vLLM, LocalAI, a local NVIDIA inference server, etc.) to answer/summarize.
    This backend has no built-in web-grounding of its own, so callers should
    feed it context (e.g. DDG results) to work from when accuracy matters.
    """
    url, key, model = _get_custom_endpoint()
    if not url:
        raise RuntimeError("No custom search endpoint configured.")

    import requests

    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    payload = {
        "model":    model,
        "messages": [{"role": "user", "content": prompt}],
        "stream":   False,
    }
    resp = requests.post(
        f"{url}/v1/chat/completions",
        json=payload, headers=headers, timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    text = (
        data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
    ).strip()
    if not text:
        raise ValueError("Custom endpoint returned an empty response.")
    return text


def _ddg_with_retry(fn, *, retries: int = 2, base_delay: float = 1.5):
    """
    Runs a DDG call with a couple of short backoff retries — DDG rate-limits
    (HTTP 429/403) fairly aggressively but usually recovers within a second
    or two, so a brief retry avoids surfacing a hard failure for what's
    normally a transient blip.
    """
    import random
    import time as _time

    last_exc = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            is_ratelimit = "ratelimit" in str(e).lower() or "429" in str(e) or "403" in str(e)
            if attempt < retries and is_ratelimit:
                delay = base_delay * (attempt + 1) + random.uniform(0, 0.5)
                print(f"[WebSearch] ⏳ DDG rate-limited — retrying in {delay:.1f}s "
                      f"({attempt + 1}/{retries})")
                _time.sleep(delay)
                continue
            raise last_exc


def _ddg_search(query: str, max_results: int = 6) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    def _run():
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    "title":   r.get("title",  ""),
                    "snippet": r.get("body",   ""),
                    "url":     r.get("href",   ""),
                })
        return results

    return _ddg_with_retry(_run)


def _ddg_news(query: str, max_results: int = 8) -> list[dict]:
    """DDG news search — returns actual articles, not website homepages."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    def _run():
        results = []
        with DDGS() as ddgs:
            for r in ddgs.news(query, max_results=max_results):
                results.append({
                    "title":   r.get("title",  ""),
                    "snippet": r.get("body",   ""),
                    "url":     r.get("url",    ""),
                    "source":  r.get("source", ""),
                })
        return results

    try:
        return _ddg_with_retry(_run)
    except Exception as e:
        print(f"[WebSearch] ⚠️ DDG news() failed ({e}) — falling back to text search")
        try:
            return _ddg_search(query, max_results=max_results)
        except Exception as e2:
            print(f"[WebSearch] ⚠️ DDG text fallback also failed ({e2})")
            return []


def _format_ddg(query: str, results: list[dict]) -> str:
    if not results:
        return f"No results found for: {query}"

    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        if r.get("title"):   lines.append(f"{i}. {r['title']}")
        if r.get("snippet"): lines.append(f"   {r['snippet']}")
        if r.get("url"):     lines.append(f"   Source: {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_news(query: str, results: list[dict]) -> str:
    if not results:
        return f"No news found for: {query}"

    lines = [f"Latest news: {query}\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        if not title:
            continue
        src = f"  [{r['source']}]" if r.get("source") else ""
        lines.append(f"{i}. {title}{src}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet'][:140]}")
        if r.get("url"):
            lines.append(f"   {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


def _synthesize_from_results(query: str, results_text: str, kind: str = "search") -> str | None:
    """
    Extra resilience layer — after Gemini (grounded or plain) has already
    been tried and failed, this makes one more attempt via whatever other
    provider is configured (Claude, or a custom/local endpoint) before the
    caller falls back to raw DuckDuckGo results. Returns None if nothing
    else is configured or everything fails — never raises.
    """
    try:
        from core.ai_client import generate_content
        prompt = (
            f"Based on these {kind} results, give a concise, well-organized answer "
            f"to: {query}\n\n{results_text}"
        )
        return generate_content(prompt).text
    except Exception as e:
        print(f"[WebSearch] ⚠️ Fallback synthesis unavailable ({e})")
        return None


# ── Modes ──────────────────────────────────────────────────────────────────────

def _search(query: str) -> str:
    """
    DuckDuckGo is the always-available baseline (no key needed). Gemini
    (grounded search) is tried first if configured; if that fails, Claude
    or a custom/local endpoint is tried next via the shared AI-client
    fallback chain, before finally returning raw DDG results.
    """
    provider = _get_search_provider()
    if provider == "gemini" and _get_api_key():
        try:
            return _gemini_search(query)
        except Exception as e:
            print(f"[WebSearch] ⚠️ Gemini failed ({e}) — trying fallback...")

    results      = _ddg_search(query)
    results_text = _format_ddg(query, results)

    if provider != "skip":
        synthesized = _synthesize_from_results(query, results_text, kind="search")
        if synthesized:
            return synthesized

    return results_text


def _news(query: str) -> str:
    """
    DuckDuckGo news is fetched unconditionally — it never requires a key, so
    news always works out of the box. If a provider is configured (Gemini or
    a custom local/NVIDIA endpoint), it's raced in parallel for a nicer
    synthesized answer; DDG's result is used the moment it's ready if the
    provider hasn't responded yet, and always used if the provider fails.
    """
    import threading

    ddg_query = query if query else "world news today"
    provider  = _get_search_provider()

    ddg_box     = [None]
    provider_box = [None]
    lock        = threading.Lock()
    done_evt    = threading.Event()

    def _try_ddg():
        try:
            results = _ddg_news(ddg_query, max_results=8)
            text    = _format_news(ddg_query, results)
        except Exception as e:
            print(f"[WebSearch] ⚠️ DDG news failed ({e})")
            text = ""
        with lock:
            ddg_box[0] = text
        done_evt.set()

    def _try_provider():
        if provider == "skip":
            return
        try:
            if provider == "gemini" and _get_api_key():
                gemini_query = f"latest news today: {query}" if query else "top world news today"
                try:
                    text = _gemini_search(gemini_query)
                except Exception as e:
                    print(f"[WebSearch] ⚠️ Gemini news failed ({e}) — trying fallback...")
                    text = _synthesize_from_results(
                        gemini_query,
                        _format_news(ddg_query, _ddg_news(ddg_query, max_results=8)),
                        kind="news",
                    ) or ""
            elif provider == "custom":
                url, _, _ = _get_custom_endpoint()
                if not url:
                    return
                # Give the custom model DDG context once available, else ask cold.
                text = _custom_synthesize(
                    "Give today's top world news headlines"
                    + (f" about: {query}" if query else "") + "."
                )
            else:
                return
        except Exception as e:
            print(f"[WebSearch] ⚠️ {provider} news failed ({e})")
            return
        if text and len(text) > 60:
            with lock:
                provider_box[0] = text
            done_evt.set()

    threading.Thread(target=_try_ddg,      daemon=True).start()
    threading.Thread(target=_try_provider, daemon=True).start()

    done_evt.wait(timeout=10.0)

    # Prefer the optional provider's answer if it arrived; DDG is the
    # guaranteed fallback and is always attempted regardless of provider.
    with lock:
        if provider_box[0]:
            return provider_box[0]
        if ddg_box[0]:
            return ddg_box[0]

    # Neither responded within the timeout — give DDG a little longer since
    # it never requires a key and should basically always eventually work.
    done_evt.wait(timeout=5.0)
    with lock:
        return ddg_box[0] or provider_box[0] or f"No news found for: {query}"


def _research(query: str) -> str:
    """Deep dive — tries Gemini, then Claude/custom fallback, then DDG."""
    research_query = (
        f"Comprehensive, detailed explanation of: {query}. "
        "Include background context, key facts, current state, and important nuances."
    )
    provider = _get_search_provider()
    if provider == "gemini" and _get_api_key():
        try:
            return _gemini_search(research_query)
        except Exception as e:
            print(f"[WebSearch] ⚠️ Research Gemini failed ({e}) — trying fallback...")

    if provider != "skip":
        results      = _ddg_search(query, max_results=6)
        synthesized  = _synthesize_from_results(
            research_query, _format_ddg(query, results), kind="research"
        )
        if synthesized:
            return synthesized

    results = _ddg_search(query, max_results=10)
    return _format_ddg(query, results)


def _price(query: str) -> str:
    """Product price lookup — tries Gemini, then Claude/custom fallback, then DDG."""
    price_query = f"current price of {query} — how much does it cost today"
    provider = _get_search_provider()
    if provider == "gemini" and _get_api_key():
        try:
            return _gemini_search(price_query)
        except Exception as e:
            print(f"[WebSearch] ⚠️ Price Gemini failed ({e}) — trying fallback...")

    if provider != "skip":
        results     = _ddg_search(f"{query} price buy", max_results=6)
        synthesized = _synthesize_from_results(
            price_query, _format_ddg(query, results), kind="price"
        )
        if synthesized:
            return synthesized

    results = _ddg_search(f"{query} price buy", max_results=6)
    return _format_ddg(query, results)


def _compare(items: list[str], aspect: str) -> str:
    query = (
        f"Compare {', '.join(items)} in terms of {aspect}. "
        "Give specific facts and data."
    )
    provider = _get_search_provider()
    if provider == "gemini" and _get_api_key():
        try:
            return _gemini_search(query)
        except Exception as e:
            print(f"[WebSearch] ⚠️ Gemini compare failed: {e} — trying fallback...")

    all_results: dict[str, list] = {}
    for item in items:
        try:
            all_results[item] = _ddg_search(f"{item} {aspect}", max_results=3)
        except Exception:
            all_results[item] = []

    if provider != "skip":
        combined = "\n\n".join(
            _format_ddg(item, res) for item, res in all_results.items()
        )
        synthesized = _synthesize_from_results(query, combined, kind="comparison")
        if synthesized:
            return synthesized

    lines = [f"Comparison — {aspect.upper()}", "─" * 40]
    for item in items:
        lines.append(f"\n▸ {item}")
        for r in all_results.get(item, [])[:2]:
            if r.get("snippet"):
                lines.append(f"  • {r['snippet']}")
            if r.get("url"):
                lines.append(f"    {r['url']}")
    return "\n".join(lines)


# ── Public entry point ─────────────────────────────────────────────────────────

def web_search(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    query  = params.get("query", "").strip()
    mode   = params.get("mode",  "search").lower().strip()
    items  = params.get("items", [])
    aspect = params.get("aspect", "general").strip() or "general"

    if not query and not items:
        return "Please provide a search query."

    if items and mode not in ("compare",):
        mode = "compare"

    if player:
        player.write_log(f"[Search:{mode}] {query or ', '.join(items)}")

    print(f"[WebSearch] 🔍 mode={mode!r}  query={query!r}")

    try:
        if mode == "compare" and items:
            return _compare(items, aspect)
        if mode == "news":
            return _news(query)
        if mode == "research":
            return _research(query)
        if mode == "price":
            return _price(query)
        return _search(query)

    except Exception as e:
        # Last-resort fallback — DuckDuckGo needs no key and should virtually
        # never fail outright, but guard anyway so search/news never hard-error.
        print(f"[WebSearch] ⚠️ Unexpected error ({e}) — falling back to raw DDG")
        try:
            if mode == "news":
                return _format_news(query or "world news today", _ddg_news(query or "world news today"))
            return _format_ddg(query, _ddg_search(query or " ".join(items)))
        except Exception as e2:
            print(f"[WebSearch] ❌ All backends failed: {e2}")
            return f"Search failed: {e2}"
