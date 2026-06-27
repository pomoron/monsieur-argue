"""
api_server.py
-------------
FastAPI backend that bridges the Loveable TypeScript frontend with Percy's
Python negotiation engine, Legora scoring, and voice I/O.

Run with:
    uvicorn api_server:app --reload --port 8000

Endpoints
---------
  POST /api/chat           — streaming AI negotiation turns
  POST /api/parse-contract — PDF → {key_terms, summary} (Percy's extraction)
  POST /api/evaluate       — transcript → Loveable evaluation schema
  POST /api/transcribe     — audio file → {text}  (voice_io STT)
  POST /api/tts            — {text} → audio stream (voice_io TTS)
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from typing import Any, Optional

# ── Path setup: ensure negotiation root is importable ────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from engine import build_system_prompt
from contract_parser import extract_pdf_text, extract_contract_terms

app = FastAPI(title="Monsieur Argue API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Config loader ─────────────────────────────────────────────────────────────

def _load_config() -> dict:
    path = os.path.join(_HERE, "config.json")
    if not os.path.exists(path):
        raise HTTPException(500, "config.json not found. Run setup.bat first.")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _get_anthropic_client(config: dict):
    try:
        import anthropic
    except ImportError:
        raise HTTPException(500, "anthropic package not installed. Run: pip install anthropic")
    api_key = (
        config.get("claude", {}).get("api_key")
        or os.environ.get("ANTHROPIC_API_KEY", "")
    )
    if not api_key:
        raise HTTPException(500, "No Anthropic API key found in config.json or ANTHROPIC_API_KEY env var.")
    return anthropic.Anthropic(api_key=api_key)


# ── Schema helpers ────────────────────────────────────────────────────────────

def _apply_extracted_terms_to_scenario(scenario: dict, extracted_terms: list) -> dict:
    """
    Merge Loveable ExtractedTerm[] into scenario.contested_points so Percy's
    build_system_prompt can use them.
    """
    if not extracted_terms:
        return scenario
    scenario = json.loads(json.dumps(scenario))  # deep copy
    existing = {cp["issue"].lower() for cp in scenario.get("contested_points", [])}
    for t in extracted_terms:
        term = t.get("term", "")
        if term.lower() in existing:
            continue
        scenario.setdefault("contested_points", []).append({
            "issue":            term,
            "clause":           t.get("clause_ref", ""),
            "buyer_position":   t.get("buyer_fear", ""),
            "seller_position":  t.get("seller_fear", ""),
            "notes":            f"Favours {t.get('favours', 'neutral')}",
        })
    return scenario


def _apply_past_learning_to_persona(persona: dict, past_learning: Optional[dict]) -> dict:
    """
    Convert Loveable pastLearning {weaknesses, missed_terms} into Percy's
    tactical_awareness block on the persona.
    """
    if not past_learning:
        return persona
    weaknesses_raw = past_learning.get("weaknesses", [])
    missed_terms   = past_learning.get("missed_terms", [])
    if not weaknesses_raw and not missed_terms:
        return persona

    persona = json.loads(json.dumps(persona))  # deep copy
    weakness_entries = [
        {
            "pattern":             w,
            "severity":            "MEDIUM",
            "exploit_instruction": f"Push hard on: {w}",
        }
        for w in weaknesses_raw
    ] + [
        {
            "pattern":             f"Missed term: {t}",
            "severity":            "HIGH",
            "exploit_instruction": f"Exploit their failure to address this term: {t}",
        }
        for t in missed_terms
    ]

    persona["tactical_awareness"] = {
        "trainee_weaknesses": weakness_entries,
        "trainee_strengths":  [],
        "session_count":      1,
    }
    return persona


def _map_favours(party_key: str) -> str:
    """Map Percy's PARTY_A/PARTY_B/NEUTRAL to Loveable's buyer/seller/neutral."""
    return {"PARTY_A": "buyer", "PARTY_B": "seller", "NEUTRAL": "neutral"}.get(
        party_key.upper(), "neutral"
    )


# ── POST /api/chat ────────────────────────────────────────────────────────────

@app.post("/api/chat")
async def chat(request: Request):
    """
    Stream an AI negotiation turn.

    Accepts:
        scenario       — Scenario JSON (matches inputs/scenario.json)
        persona        — Persona JSON (matches inputs/persona.json)
        norms          — CompanyNorms JSON
        extractedTerms — ExtractedTerm[] or null
        contractSummary— string or null
        pastLearning   — {weaknesses: string[], missed_terms: string[]} or null
        messages       — [{role, content|text}]

    Returns:
        text/plain streaming response (the AI's reply, possibly ending with [[END:...]])
    """
    body: dict[str, Any] = await request.json()
    config = _load_config()

    scenario        = body.get("scenario", {})
    persona         = body.get("persona", {})
    norms           = body.get("norms", {})
    extracted_terms = body.get("extractedTerms") or []
    past_learning   = body.get("pastLearning")
    messages_raw    = body.get("messages", [])

    # Enrich inputs with contract terms and past learning
    scenario = _apply_extracted_terms_to_scenario(scenario, extracted_terms)
    persona  = _apply_past_learning_to_persona(persona, past_learning)

    system_prompt = build_system_prompt(norms, scenario, persona)

    # Normalise messages to Anthropic format
    messages = [
        {"role": m["role"], "content": m.get("content") or m.get("text", "")}
        for m in messages_raw
    ]

    client = _get_anthropic_client(config)
    model  = config.get("claude", {}).get("model", "claude-opus-4-5")
    max_tokens = config.get("negotiation", {}).get("max_tokens", 1000)

    def generate():
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                yield text

    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8")


# ── POST /api/parse-contract ──────────────────────────────────────────────────

@app.post("/api/parse-contract")
async def parse_contract(request: Request):
    """
    Parse a contract PDF and return {key_terms, summary} in Loveable's schema.

    Accepts:
        pdf_base64 — base64-encoded PDF bytes
        filename   — original filename (informational)
        mime       — MIME type

    Returns:
        {
          key_terms: [{term, favours("buyer"|"seller"|"neutral"), buyer_fear, seller_fear, clause_ref?}],
          summary:   string
        }
    """
    body: dict = await request.json()
    config = _load_config()

    pdf_b64 = body.get("pdf_base64", "")
    if not pdf_b64:
        raise HTTPException(400, "pdf_base64 is required")

    try:
        pdf_bytes = base64.b64decode(pdf_b64)
    except Exception:
        raise HTTPException(400, "Invalid base64 data")

    # Write to temp file
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        tmp_path = f.name

    try:
        pdf_text     = extract_pdf_text(tmp_path)
        contract_data = extract_contract_terms(config, pdf_text)
    except Exception as e:
        raise HTTPException(500, f"Contract extraction failed: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # Map Percy's contested_terms[] → Loveable's key_terms[]
    key_terms = [
        {
            "term":        t.get("issue", ""),
            "favours":     _map_favours(t.get("favours", "NEUTRAL")),
            "buyer_fear":  t.get("party_a_concern", ""),
            "seller_fear": t.get("party_b_concern", ""),
            "clause_ref":  t.get("clause", "") or None,
        }
        for t in contract_data.get("contested_terms", [])
    ]

    title      = contract_data.get("contract_title", "Contract")
    n_agreed   = len(contract_data.get("agreed_terms", []))
    n_cont     = len(contract_data.get("contested_terms", []))
    party_a    = contract_data.get("party_a_label", "Party A")
    party_b    = contract_data.get("party_b_label", "Party B")
    summary    = (
        f"{title}. Parties: {party_a} and {party_b}. "
        f"{n_agreed} agreed term(s) and {n_cont} contested point(s) identified."
    )

    return JSONResponse({"key_terms": key_terms, "summary": summary})


# ── POST /api/evaluate ────────────────────────────────────────────────────────

@app.post("/api/evaluate")
async def evaluate(request: Request):
    """
    Evaluate a negotiation transcript against the rubric.

    Accepts:
        transcript     — [{role, text}]
        playbook       — Playbook JSON
        scenario       — Scenario JSON
        extractedTerms — ExtractedTerm[] or null
        userSide       — string (e.g. "Buyer (Meridian...)")

    Returns Loveable Evaluation schema:
        {overall_score, grade, dimensions, strengths, weaknesses,
         addressed_terms, missed_terms}
    """
    body: dict = await request.json()
    config = _load_config()

    transcript     = body.get("transcript", [])
    playbook       = body.get("playbook", {})
    scenario       = body.get("scenario", {})
    extracted_terms = body.get("extractedTerms") or []
    user_side      = body.get("userSide", "Buyer")

    # Attempt to use Legora's scorer first (richer, dimension-aware)
    try:
        eval_result = _evaluate_with_legora(
            config, transcript, playbook, scenario, extracted_terms, user_side
        )
        return JSONResponse(eval_result)
    except Exception as legora_err:
        # Fall back to a direct LLM evaluation if Legora is unavailable
        pass

    # Fallback: direct Claude evaluation
    try:
        eval_result = _evaluate_with_llm(
            config, transcript, playbook, scenario, extracted_terms, user_side
        )
        return JSONResponse(eval_result)
    except Exception as e:
        raise HTTPException(500, f"Evaluation failed: {e}")


def _evaluate_with_legora(
    config, transcript, playbook, scenario, extracted_terms, user_side
) -> dict:
    """Try Legora's scoring pipeline. Raises if unavailable."""
    sys.path.insert(0, os.path.join(_HERE, "integrated", "Legora"))
    from assessor.scoring import score_session
    from assessor.signals import extract_signals

    # Build a minimal session dict that the scorer understands
    dialogue = [
        {"role": m["role"].upper(), "content": m.get("text", m.get("content", ""))}
        for m in transcript
    ]
    signals  = extract_signals(dialogue, scenario, playbook)
    raw      = score_session(config, dialogue, scenario, playbook, signals)

    # Map Legora output → Loveable schema
    dims = []
    for d in raw.get("dimensions", []):
        dims.append({
            "id":      d.get("id", ""),
            "name":    d.get("name", ""),
            "score":   round(d.get("score_5", 0) * 20),   # 0-5 → 0-100 → normalise to 0-5 * 20
            "comment": d.get("comment", ""),
        })

    # overall_score from score_5 average → 0-100
    overall = raw.get("overall_score", 0)
    if overall <= 5:
        overall = round(overall * 20)

    grade_map = {"A": "A", "B": "B", "C": "C", "D": "D", "E": "E"}
    grade = raw.get("grade", _score_to_grade(overall))

    return {
        "overall_score":   overall,
        "grade":           grade_map.get(grade, grade),
        "dimensions":      dims,
        "strengths":       raw.get("strengths", []),
        "weaknesses":      raw.get("weaknesses", raw.get("improvements", [])),
        "addressed_terms": raw.get("addressed_terms", []),
        "missed_terms":    raw.get("missed_terms", []),
    }


def _evaluate_with_llm(
    config, transcript, playbook, scenario, extracted_terms, user_side
) -> dict:
    """Direct Claude evaluation — identical schema to Loveable's evaluate.ts."""
    client = _get_anthropic_client(config)
    model  = config.get("claude", {}).get("model", "claude-opus-4-5")

    convo = "\n\n".join(
        f"{m['role'].upper()}: {m.get('text', m.get('content', ''))}"
        for m in transcript
    )

    prompt = f"""You are a senior negotiation coach reviewing a junior lawyer's performance.

USER PLAYED THE SIDE OF: {user_side}

SCENARIO:
{json.dumps(scenario, indent=2)}

EXTRACTED CONTRACT TERMS:
{json.dumps(extracted_terms, indent=2)}

RUBRIC (apply only to the USER's turns):
{json.dumps(playbook, indent=2)}

TRANSCRIPT:
{convo}

Return ONLY valid JSON matching this exact schema:
{{
  "overall_score": <number 0-100>,
  "grade": "A"|"B"|"C"|"D"|"E",
  "dimensions": [{{"id":"<str>","name":"<str>","score":<number 0-5>,"comment":"<str>"}}],
  "strengths": ["<str>"],
  "weaknesses": ["<str>"],
  "addressed_terms": ["<str>"],
  "missed_terms": ["<str>"]
}}"""

    response = client.messages.create(
        model=model,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def _score_to_grade(score: int) -> str:
    if score >= 85: return "A"
    if score >= 70: return "B"
    if score >= 55: return "C"
    if score >= 40: return "D"
    return "E"


# ── POST /api/transcribe ──────────────────────────────────────────────────────

@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...)):
    """
    STT: audio file → {text}.
    Uses voice_io.py's STT pipeline (Gemini STT / faster-whisper fallback).
    """
    config  = _load_config()
    content = await file.read()

    suffix = os.path.splitext(file.filename or "audio.wav")[1] or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(content)
        tmp_path = f.name

    try:
        text = _stt(config, tmp_path)
        return JSONResponse({"text": text})
    except Exception as e:
        raise HTTPException(500, f"STT failed: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _stt(config: dict, audio_path: str) -> str:
    """Try voice_io STT; fall back to faster-whisper if needed."""
    try:
        from voice_io import record_user_input  # noqa: F401 (import check)
        # voice_io.record_user_input records live — we need transcription from file
        # Use faster-whisper directly (already in requirements)
        from faster_whisper import WhisperModel
        model = WhisperModel(
            config.get("voice", {}).get("stt_model", "tiny.en"),
            device="cpu",
            compute_type="int8",
        )
        segments, _ = model.transcribe(audio_path)
        return " ".join(s.text.strip() for s in segments).strip()
    except ImportError:
        raise RuntimeError(
            "faster-whisper not installed. Run: pip install faster-whisper"
        )


# ── POST /api/tts ─────────────────────────────────────────────────────────────

@app.post("/api/tts")
async def tts(request: Request):
    """
    TTS: {text, voice?} → audio/mpeg stream.
    Uses Google Cloud TTS if credentials are present; falls back to edge-tts.
    """
    body   = await request.json()
    config = _load_config()
    text   = body.get("text", "").strip()
    if not text:
        raise HTTPException(400, "text is required")

    voice_cfg = config.get("voice", {})

    # Try Google Cloud TTS first (highest quality)
    audio_bytes = _tts_google(text, voice_cfg) or _tts_edge(text, voice_cfg)
    if audio_bytes is None:
        raise HTTPException(500, "TTS unavailable: no provider succeeded.")

    return Response(content=audio_bytes, media_type="audio/mpeg")


def _tts_google(text: str, voice_cfg: dict) -> Optional[bytes]:
    """Google Cloud TTS → MP3 bytes, or None if unavailable."""
    try:
        from google.cloud import texttospeech
        from google.oauth2 import service_account

        key_path = voice_cfg.get("service_account_key_path", "service_account_key.json")
        if not os.path.exists(os.path.join(_HERE, key_path)):
            return None

        creds  = service_account.Credentials.from_service_account_file(
            os.path.join(_HERE, key_path),
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        client = texttospeech.TextToSpeechClient(credentials=creds)

        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice = texttospeech.VoiceSelectionParams(
            language_code=voice_cfg.get("language_code", "en-GB"),
            name=voice_cfg.get("voice_name", "en-GB-Neural2-B"),
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=voice_cfg.get("speaking_rate", 1.0),
        )
        response = client.synthesize_speech(
            input=synthesis_input, voice=voice, audio_config=audio_config
        )
        return response.audio_content
    except Exception:
        return None


def _tts_edge(text: str, voice_cfg: dict) -> Optional[bytes]:
    """edge-tts fallback → MP3 bytes, or None if unavailable."""
    try:
        import asyncio
        import edge_tts

        voice = voice_cfg.get("edge_tts_voice", "en-GB-RyanNeural")

        async def _synthesize():
            communicate = edge_tts.Communicate(text, voice)
            chunks = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
            return b"".join(chunks)

        return asyncio.run(_synthesize())
    except Exception:
        return None


# ── Root: serve the self-contained frontend ───────────────────────────────────

@app.get("/")
async def serve_frontend():
    path = os.path.join(_HERE, "frontend.html")
    if not os.path.exists(path):
        raise HTTPException(404, "frontend.html not found. Place it in the negotiation/ folder.")
    return FileResponse(path, media_type="text/html")


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "monsieur-argue-api"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
