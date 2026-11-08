"""Optional AI email drafting with the client's own key (Gemini / Claude / OpenAI). AI never scores leads."""
from __future__ import annotations

import requests

from . import crypto

PROVIDERS = {
    "gemini": {"label": "Google Gemini (free tier available)", "model": "gemini-2.0-flash"},
    "claude": {"label": "Anthropic Claude", "model": "claude-haiku-4-5"},
    "openai": {"label": "OpenAI", "model": "gpt-4o-mini"},
}


class AIError(Exception):
    pass


def _complete(provider: str, model: str, key: str, prompt: str, max_tokens: int = 700) -> str:
    model = model or PROVIDERS[provider]["model"]
    try:
        if provider == "gemini":
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              headers={"x-goog-api-key": key}, timeout=60,
                              json={"contents": [{"parts": [{"text": prompt}]}],
                                    "generationConfig": {"maxOutputTokens": max_tokens}})
            if r.status_code != 200:
                raise AIError(f"Gemini error {r.status_code}: {r.text[:200]}")
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        if provider == "claude":
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=60,
                              headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                              json={"model": model, "max_tokens": max_tokens,
                                    "messages": [{"role": "user", "content": prompt}]})
            if r.status_code != 200:
                raise AIError(f"Claude error {r.status_code}: {r.text[:200]}")
            return r.json()["content"][0]["text"]
        if provider == "openai":
            r = requests.post("https://api.openai.com/v1/chat/completions", timeout=60,
                              headers={"Authorization": f"Bearer {key}"},
                              json={"model": model, "max_tokens": max_tokens,
                                    "messages": [{"role": "user", "content": prompt}]})
            if r.status_code != 200:
                raise AIError(f"OpenAI error {r.status_code}: {r.text[:200]}")
            return r.json()["choices"][0]["message"]["content"]
    except requests.RequestException as e:
        raise AIError(f"Network error: {e}") from e
    except (KeyError, IndexError, ValueError) as e:
        raise AIError("Unexpected AI response") from e
    raise AIError("Unknown provider")


def test_key(provider: str, model: str, key: str) -> str:
    out = _complete(provider, model, key, "Reply with the single word: OK", max_tokens=10)
    return f"Key works. Model replied: {out.strip()[:40]}"


def draft_email(org, product: str, market: str, instructions: str = "") -> tuple[str, str]:
    key = crypto.decrypt(org["ai_key_enc"])
    if not org["ai_provider"] or not key:
        raise AIError("Add your AI key in Settings > AI first.")
    prompt = (
        "Write a short, personal B2B cold outreach email (max 110 words). Plain text, no markdown, no emojis, "
        "no exaggerated claims. Use only these placeholders where useful: {{first_name}}, {{company_name}}, "
        "{{city}}, {{country}}. Do NOT write a signature or unsubscribe line (added automatically).\n"
        f"Our product/service: {product or 'n/a'}\nTarget market: {market or 'n/a'}\n"
        f"Extra instructions: {instructions or 'none'}\n"
        "Format exactly:\nSUBJECT: <subject line>\n\n<email body>")
    text = _complete(org["ai_provider"], org["ai_model"], key, prompt).strip()
    subject, body = "", text
    if text.upper().startswith("SUBJECT:"):
        first, _, rest = text.partition("\n")
        subject, body = first.split(":", 1)[1].strip(), rest.strip()
    return subject[:150], body[:3000]
