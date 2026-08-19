#ai_client.py
"""
One shared entry point for every text-generation call in the app
(dev_agent, code_helper, self_maintain, web search summaries, and more),
so a single provider's outage or exhausted quota doesn't take every one of
those features down with it.

Fallback order, first configured-and-working provider wins:
  1. Gemini            — "gemini_api_key"     (primary — also the only one
                                                 that can currently drive the
                                                 real-time voice engine)
  2. Claude (Anthropic) — "anthropic_api_key"
  3. Groq               — "groq_api_key"       — very fast inference for
                                                   open models (Llama, etc.)
                                                   Free tier available.
  4. Custom / local      — "fallback_api_url"   — any OpenAI-compatible
                                                   endpoint: LM Studio,
                                                   Ollama, vLLM, or a local
                                                   NVIDIA inference server

All of this is configured in config/api_keys.json — see
config/api_keys.example.json for the schema. None of it is required; with
nothing configured, calls simply fail with a clear message rather than a
cryptic provider-specific traceback.

Every call site does:

    from core.ai_client import generate_content
    response = generate_content(prompt)
    text = response.text

— identical in shape to the old direct `google.genai` call, so nothing else
at the call site needs to change.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeoutError
from pathlib import Path


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-5"
DEFAULT_GROQ_MODEL   = "llama-3.3-70b-versatile"
GROQ_BASE_URL        = "https://api.groq.com/openai/v1"

GEMINI_TIMEOUT_S     = 8      # hard cap on a single Gemini attempt
GEMINI_COOLDOWN_S    = 90     # after a failure, skip Gemini entirely for this long —
                               # avoids paying the "try it, wait, fail" tax on every
                               # single call while it's known to be down this session

_gemini_down_until = 0.0      # module-level, shared across every call site


class AllProvidersFailedError(Exception):
    """Raised only when every configured provider (or none at all) failed."""
    pass


class _Response:
    """Minimal shape-compatible stand-in for a google-genai response — just
    the `.text` attribute every call site already reads."""
    def __init__(self, text: str, provider: str):
        self.text     = text
        self.provider = provider  # which provider actually answered, for logging


def _load_config() -> dict:
    try:
        return json.loads(API_CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


# ── Providers ────────────────────────────────────────────────────────────────

def _try_gemini(prompt: str, model: str) -> str:
    if time.monotonic() < _gemini_down_until:
        remaining = int(_gemini_down_until - time.monotonic())
        raise RuntimeError(f"Gemini in cooldown after a recent failure ({remaining}s left) — skipping")

    cfg = _load_config()
    key = (cfg.get("gemini_api_key") or "").strip()
    if not key:
        raise RuntimeError("no Gemini key configured")

    from google import genai
    client = genai.Client(api_key=key)

    # Hard timeout — without this, an unreachable (not just quota-exhausted)
    # Gemini can hang far longer than any user is willing to wait before the
    # fallback chain ever gets a chance to answer.
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.models.generate_content, model=model, contents=prompt)
        response = future.result(timeout=GEMINI_TIMEOUT_S)   # raises TimeoutError on expiry

    text = (response.text or "").strip()
    if not text:
        raise ValueError("Gemini returned an empty response")
    return text



def _try_claude(prompt: str) -> str:
    cfg = _load_config()
    key = (cfg.get("anthropic_api_key") or "").strip()
    if not key:
        raise RuntimeError("no Claude key configured")
    model = (cfg.get("anthropic_model") or DEFAULT_CLAUDE_MODEL).strip()

    import anthropic
    client   = anthropic.Anthropic(api_key=key)
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
    text  = "".join(parts).strip()
    if not text:
        raise ValueError("Claude returned an empty response")
    return text


def _try_groq(prompt: str, timeout: int = 30) -> str:
    cfg = _load_config()
    key = (cfg.get("groq_api_key") or "").strip()
    if not key:
        raise RuntimeError("no Groq key configured")
    model = (cfg.get("groq_model") or DEFAULT_GROQ_MODEL).strip()

    import requests
    resp = requests.post(
        f"{GROQ_BASE_URL}/chat/completions",
        json={
            "model":    model,
            "messages": [{"role": "user", "content": prompt}],
            "stream":   False,
        },
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {key}",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    text = (data.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
    if not text:
        raise ValueError("Groq returned an empty response")
    return text


def _try_custom(prompt: str, timeout: int = 30) -> str:
    cfg = _load_config()
    url   = (cfg.get("fallback_api_url") or "").strip().rstrip("/")
    if not url:
        raise RuntimeError("no custom fallback endpoint configured")
    key   = (cfg.get("fallback_api_key") or "").strip()
    model = (cfg.get("fallback_model")   or "").strip() or "default"

    import requests
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    resp = requests.post(
        f"{url}/v1/chat/completions",
        json={
            "model":    model,
            "messages": [{"role": "user", "content": prompt}],
            "stream":   False,
        },
        headers=headers, timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    text = (data.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
    if not text:
        raise ValueError("Custom endpoint returned an empty response")
    return text


# ── Public entry point ───────────────────────────────────────────────────────

def generate_content(prompt: str, model: str = DEFAULT_GEMINI_MODEL) -> _Response:
    """
    Tries Gemini, then Claude, then Groq, then a custom/local endpoint — in
    that order, using whichever are configured — and returns the first
    success. Raises AllProvidersFailedError with every provider's error
    only if all of them (or none configured) failed.
    """
    global _gemini_down_until
    errors: list[str] = []

    try:
        return _Response(_try_gemini(prompt, model), "gemini")
    except Exception as e:
        err_str = str(e)
        # Don't cool down for a config issue (no key set — that's not
        # transient, adding one should work immediately) or for a call we
        # already skipped because cooldown was already active (avoids
        # needlessly re-extending the window every single call).
        already_known = "no Gemini key configured" in err_str or "in cooldown" in err_str
        if not already_known:
            _gemini_down_until = time.monotonic() + GEMINI_COOLDOWN_S
        errors.append(f"Gemini: {e}")
        print(f"[AIClient] ⚠️ Gemini failed ({e}) — trying fallback...")

    try:
        return _Response(_try_claude(prompt), "claude")
    except Exception as e:
        errors.append(f"Claude: {e}")

    try:
        return _Response(_try_groq(prompt), "groq")
    except Exception as e:
        errors.append(f"Groq: {e}")

    try:
        return _Response(_try_custom(prompt), "custom")
    except Exception as e:
        errors.append(f"Custom endpoint: {e}")

    raise AllProvidersFailedError(
        "All configured AI providers failed:\n" + "\n".join(errors)
        + "\n\nAdd a Gemini key, a Claude key (anthropic_api_key), a Groq key "
          "(groq_api_key), and/or a custom endpoint (fallback_api_url) in "
          "config/api_keys.json."
    )
