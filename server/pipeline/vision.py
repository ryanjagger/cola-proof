"""Tier B extraction: self-hosted vision model behind an OpenAI-compatible API.

The client only knows VISION_BASE_URL — llama-server in dev (Metal), the
llama.cpp sidecar on Railway (CPU, private networking), or whatever
runtime an agency stands up (vLLM, Ollama). Same code everywhere; no
outbound network anywhere.

The model transcribes — it never judges. Output is JSON-schema-constrained
(llama.cpp grammar enforcement) structured transcription: brand text, ABV
statement, net contents statement, and the *verbatim* warning text. The
deterministic validator and matchers always re-run on this output;
Tier B failures and timeouts degrade to "couldn't read clearly", which
keeps the record in Needs Review — escalate on doubt, never reject on it.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass

import httpx

# No concrete example values in this prompt: small models parrot them
# into the transcription when the label is hard to read. Observed with a
# sample "Alc. 42% by Vol" being returned for a 47.5% label.
PROMPT = (
    "Transcribe this alcohol beverage label EXACTLY as printed. Return JSON:\n"
    "- brand_text: the brand/product name as printed\n"
    "- abv_text: the alcohol content statement, word for word as printed\n"
    "- net_contents_text: the net contents / volume statement as printed\n"
    "- warning_text: the complete government warning paragraph verbatim, "
    "every word exactly as printed on THIS label\n"
    "- full_text: all other readable text on the label, briefly — skip "
    "decorative flourishes and repeated text\n"
    "Rules: copy only what is visible in the image. Write words normally — "
    "never insert spaces between the letters of a word. If something is "
    "not printed on the label or you cannot read it, return null for that "
    "field. NEVER guess, complete from memory, or correct spelling."
)

# maxLength is load-bearing, not cosmetic: llama.cpp compiles it into the
# decoding grammar, which is the only reliable brake on greedy-decode
# repetition loops (observed: a fabricated warning sentence repeated for
# ~2800 tokens, ~50s of CPU decode per crop). warning_text's budget is
# ~3x the statutory paragraph (~280 chars) because the model sometimes
# letter-spaces transcriptions; a cut-short warning lands in review,
# never a false pass.
SCHEMA = {
    "type": "object",
    "properties": {
        "brand_text": {"type": ["string", "null"], "maxLength": 200},
        "abv_text": {"type": ["string", "null"], "maxLength": 120},
        "net_contents_text": {"type": ["string", "null"], "maxLength": 120},
        "warning_text": {"type": ["string", "null"], "maxLength": 1000},
        "full_text": {"type": ["string", "null"], "maxLength": 600},
    },
    "required": [
        "brand_text", "abv_text", "net_contents_text", "warning_text", "full_text",
    ],
}


_FIELD_KEYS = (
    "brand_text", "abv_text", "net_contents_text", "warning_text", "full_text",
)


def _parse_lenient(content: str) -> dict | None:
    """Salvage complete string fields from truncated/malformed JSON.

    Constrained decoding can hit the token cap mid-string, leaving an
    unterminated JSON document. Any field whose string literal completed
    is still good evidence — losing the whole read over the last field is
    what stranded records in review.
    """
    out = {}
    for key in _FIELD_KEYS:
        m = re.search(rf'"{key}"\s*:\s*("(?:[^"\\]|\\.)*")', content)
        if m:
            try:
                out[key] = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    return out or None


@dataclass
class VisionResult:
    ok: bool
    brand_text: str | None = None
    abv_text: str | None = None
    net_contents_text: str | None = None
    warning_text: str | None = None
    full_text: str | None = None
    error: str | None = None
    elapsed_ms: int = 0

    @property
    def combined_text(self) -> str:
        """All transcribed text, for the matchers' text pool."""
        parts = [
            self.brand_text, self.abv_text, self.net_contents_text,
            self.warning_text, self.full_text,
        ]
        return "\n".join(p for p in parts if p)


class VisionClient:
    """Minimal OpenAI-compatible chat-completions client (vision input,
    JSON-schema-constrained output)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._http = httpx.Client(transport=transport, timeout=timeout)

    def available(self) -> bool:
        if not self.base_url:
            return False
        try:
            r = self._http.get(f"{self.base_url}/models", timeout=5.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def read_crop(self, image_data: bytes, ext: str) -> VisionResult:
        import time

        if not self.base_url:
            return VisionResult(ok=False, error="vision tier not configured")
        mime = "image/png" if ext == "png" else "image/jpeg"
        b64 = base64.b64encode(image_data).decode()
        payload = {
            "model": self.model,
            # Ceiling above the grammar's worst case (~1000 tokens when
            # every field maxes out letter-spaced), not a working budget;
            # _parse_lenient salvages the rare overrun.
            "temperature": 0,
            "max_tokens": 1200,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
                    ],
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "label_transcription", "schema": SCHEMA},
            },
        }
        start = time.monotonic()
        error = None
        try:
            resp = self._http.post(
                f"{self.base_url}/chat/completions",
                json=payload,
            )
            resp.raise_for_status()
            choice = resp.json()["choices"][0]
            content = choice["message"]["content"]
            truncated = choice.get("finish_reason") == "length"
            try:
                data = json.loads(content)
                if truncated:
                    error = "output hit token cap (parsed cleanly)"
            except json.JSONDecodeError as e:
                data = _parse_lenient(content)
                if data is None:
                    raise e
                error = (
                    f"{'truncated' if truncated else 'malformed'} output; "
                    f"salvaged {len(data)} field(s)"
                )
        except (httpx.HTTPError, KeyError, json.JSONDecodeError) as e:
            return VisionResult(
                ok=False,
                error=f"{type(e).__name__}: {e}",
                elapsed_ms=int((time.monotonic() - start) * 1000),
            )
        return VisionResult(
            ok=True,
            brand_text=data.get("brand_text"),
            abv_text=data.get("abv_text"),
            net_contents_text=data.get("net_contents_text"),
            warning_text=data.get("warning_text"),
            full_text=data.get("full_text"),
            error=error,
            elapsed_ms=int((time.monotonic() - start) * 1000),
        )
