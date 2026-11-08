"""Optional AI email drafting and live lead discovery with the client's own key (Gemini / Claude / OpenAI). AI never scores leads."""
from __future__ import annotations

import json
import re
import requests

from . import crypto

PROVIDERS = {
    "gemini": {"label": "Google Gemini (free tier available)", "model": "gemini-2.0-flash"},
    "claude": {"label": "Anthropic Claude", "model": "claude-haiku-4-5"},
    "openai": {"label": "OpenAI", "model": "gpt-4o-mini"},
}


class AIError(Exception):
    pass


def normalize_model(provider: str, model: str = "") -> str:
    """Ensure a valid model name is used; sanitize Google Cloud project IDs if mistakenly entered."""
    m = (model or "").strip()
    if provider == "gemini":
        if not m or "gen-lang-client" in m or not m.startswith("gemini"):
            return "gemini-2.0-flash"
    elif provider == "claude":
        if not m or not m.startswith("claude"):
            return "claude-haiku-4-5"
    elif provider == "openai":
        if not m or not (m.startswith("gpt-") or m.startswith("o")):
            return "gpt-4o-mini"
    return m or PROVIDERS.get(provider, {}).get("model", "")


def _complete(provider: str, model: str, key: str, prompt: str, max_tokens: int = 700) -> str:
    model = normalize_model(provider, model)
    try:
        if provider == "gemini":
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              headers={"x-goog-api-key": key, "Content-Type": "application/json"}, timeout=60,
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


def _parse_companies_json(text: str, default_country: str = "", default_city: str = "") -> list[dict]:
    """Robustly extract JSON array of companies from AI text output."""
    raw = text.strip()
    match = re.search(r"\[\s*\{.*\}\s*\]", raw, re.DOTALL)
    candidate = match.group(0) if match else raw
    candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\s*```$", "", candidate).strip()

    data = None
    try:
        data = json.loads(candidate)
    except Exception:
        fixed = re.sub(r",\s*([\]}])", r"\1", candidate)
        try:
            data = json.loads(fixed)
        except Exception:
            pass

    if not isinstance(data, list):
        return []

    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        site = str(item.get("website") or "").strip()
        if not name or not site:
            continue
        out.append({
            "name": name[:120],
            "website": site,
            "email": str(item.get("email") or "").strip()[:120],
            "phone": str(item.get("phone") or "").strip()[:60],
            "city": str(item.get("city") or default_city or "").strip()[:80],
            "country": str(item.get("country") or default_country or "").strip()[:80],
            "description": str(item.get("description") or "").strip()[:300],
        })
    return out


def search_leads_with_ai(org, keyword: str, country: str, city: str = "", company_type: str = "", target: int = 20) -> list[dict]:
    """Search for real commercial companies using Gemini with live Google Search grounding."""
    key = crypto.decrypt(org["ai_key_enc"]) if org and org["ai_key_enc"] else ""
    if not org or not org["ai_provider"] or not key:
        raise AIError("Add your AI key in Settings > AI first.")

    provider = org["ai_provider"]
    model = normalize_model(provider, org["ai_model"])
    limit = max(5, min(target, 40))

    prompt = (
        f"You are a professional B2B lead generation researcher with live access to Google Search.\n"
        f"Find up to {limit} real, registered, currently operational businesses/companies matching these details:\n"
        f"- Product / Keyword: {keyword}\n"
        f"- Company Type: {company_type or 'manufacturers, exporters, distributors, suppliers'}\n"
        f"- City: {city or 'any'}\n"
        f"- Country: {country}\n\n"
        f"Search Google live to verify real company names and their actual official websites.\n"
        f"Do NOT invent fake names or fake URLs. If a company has no website, skip it.\n"
        f"Return ONLY a valid JSON array of objects without any markdown code fences or conversational text.\n"
        f"Format schema:\n"
        f'[\n'
        f'  {{\n'
        f'    "name": "Exact business/company name",\n'
        f'    "website": "example.com (or full URL)",\n'
        f'    "email": "contact email if found, or empty string",\n'
        f'    "phone": "phone number with country code if found, or empty string",\n'
        f'    "city": "{city or "City"}",\n'
        f'    "country": "{country}",\n'
        f'    "description": "Short description of what they manufacture or sell (max 150 chars)"\n'
        f'  }}\n'
        f']'
    )

    if provider == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
        payload_grounded = {
            "contents": [{"parts": [{"text": prompt}]}],
            "tools": [{"googleSearch": {}}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4000}
        }
        try:
            r = requests.post(url, headers=headers, json=payload_grounded, timeout=90)
            if r.status_code == 200:
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            else:
                # Fallback without googleSearch tool if model/region rejects it
                r2 = requests.post(url, headers=headers, timeout=60,
                                   json={"contents": [{"parts": [{"text": prompt}]}],
                                         "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4000}})
                if r2.status_code != 200:
                    raise AIError(f"Gemini API error {r2.status_code}: {r2.text[:200]}")
                text = r2.json()["candidates"][0]["content"]["parts"][0]["text"]
        except requests.RequestException as e:
            raise AIError(f"Network error contacting Gemini: {e}") from e
    else:
        text = _complete(provider, model, key, prompt, max_tokens=3500)

    return _parse_companies_json(text, default_country=country, default_city=city)
