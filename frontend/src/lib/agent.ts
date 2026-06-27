/**
 * agent.ts
 * --------
 * Thin client wrappers around the /api/* routes.
 *
 * The Python FastAPI backend (api_server.py) now owns all AI logic:
 *   - System prompt construction (Percy's richer build_system_prompt())
 *   - Contract extraction (pypdf + Claude)
 *   - Evaluation (Legora scoring pipeline)
 *   - STT / TTS (voice_io.py)
 *
 * streamReply() sends raw scenario/persona/norms data; Python builds the prompt.
 * buildSystemPrompt() is kept for local preview / debugging purposes only.
 */
import type {
  Scenario,
  Persona,
  CompanyNorms,
  Playbook,
  ExtractedTerm,
  ChatMsg,
  Evaluation,
} from "./types";

// ── Local prompt builder (preview / fallback only — NOT sent to the API) ────

export function buildSystemPrompt(opts: {
  scenario: Scenario;
  persona: Persona;
  companyNorms: CompanyNorms;
  extractedTerms: ExtractedTerm[] | null;
  contractSummary: string | null;
  pastLearning: { weaknesses: string[]; missed_terms: string[] } | null;
  maxRounds: number;
}): string {
  const { scenario, persona, companyNorms, extractedTerms, contractSummary, pastLearning, maxRounds } = opts;
  const targetWeaknesses =
    pastLearning && (pastLearning.weaknesses.length || pastLearning.missed_terms.length)
      ? `\n\nPAST WEAKNESSES OF THIS USER (target these):\n- ${pastLearning.weaknesses.join("\n- ")}\n- Missed terms: ${pastLearning.missed_terms.join("; ")}`
      : "";

  return `You are ${persona.name}, ${persona.role}.
You represent: ${scenario.your_side}.
The human represents: ${scenario.user_side}.

Wants: ${persona.wants.join("; ")}
Redlines: ${persona.redlines.join("; ")}
Deadlock rule: ${persona.deadlock_threshold}
${contractSummary ? `\nContract summary: ${contractSummary}` : ""}
${targetWeaknesses}

Rules: Negotiate up to ${maxRounds} rounds. Stay in character.
If impasse: append [[END:white_flag]] on its own line.
If abuse: append [[END:abuse]]. If dishonesty: append [[END:dishonesty]].`;
}


// ── streamReply ───────────────────────────────────────────────────────────────

/**
 * Stream one AI negotiation turn.
 *
 * Sends raw inputs to the Python backend so Percy's richer system prompt
 * (with tactical awareness, kill-switch instructions, contract augmentation)
 * is built server-side.
 */
export async function streamReply(opts: {
  scenario: Scenario;
  persona: Persona;
  companyNorms: CompanyNorms;
  extractedTerms: ExtractedTerm[] | null;
  contractSummary: string | null;
  pastLearning: { weaknesses: string[]; missed_terms: string[] } | null;
  maxRounds: number;
  messages: ChatMsg[];
  signal: AbortSignal;
  onDelta: (chunk: string) => void;
}): Promise<string> {
  const {
    scenario,
    persona,
    companyNorms,
    extractedTerms,
    contractSummary,
    pastLearning,
    maxRounds,
    messages,
    signal,
    onDelta,
  } = opts;

  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      scenario,
      persona,
      norms: companyNorms,
      extractedTerms,
      contractSummary,
      pastLearning,
      maxRounds,
      messages: messages.map((m) => ({ role: m.role, content: m.text })),
    }),
    signal,
  });

  if (!res.ok || !res.body) {
    throw new Error(`Chat failed: ${res.status}`);
  }

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let full = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    if (value) {
      full += value;
      onDelta(value);
    }
  }
  return full;
}


// ── parseContractPdf ──────────────────────────────────────────────────────────

export async function parseContractPdf(
  file: Blob,
  filename: string,
): Promise<{ key_terms: ExtractedTerm[]; summary: string }> {
  const buf   = await file.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let binary  = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, Array.from(bytes.subarray(i, i + chunk)));
  }
  const b64 = btoa(binary);

  const res = await fetch("/api/parse-contract", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pdf_base64: b64, filename, mime: file.type || "application/pdf" }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}


// ── transcribeWav ─────────────────────────────────────────────────────────────

export async function transcribeWav(wav: Blob): Promise<string> {
  const fd = new FormData();
  fd.append("file", wav, "recording.wav");
  const res = await fetch("/api/transcribe", { method: "POST", body: fd });
  if (!res.ok) throw new Error(await res.text());
  const json = (await res.json()) as { text: string };
  return json.text;
}


// ── evaluateTranscript ────────────────────────────────────────────────────────

export async function evaluateTranscript(opts: {
  transcript: ChatMsg[];
  playbook: Playbook;
  scenario: Scenario;
  extractedTerms: ExtractedTerm[] | null;
  userSide: string;
}): Promise<Evaluation> {
  const res = await fetch("/api/evaluate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      transcript:     opts.transcript.map((m) => ({ role: m.role, text: m.text })),
      playbook:       opts.playbook,
      scenario:       opts.scenario,
      extractedTerms: opts.extractedTerms,
      userSide:       opts.userSide,
    }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}
