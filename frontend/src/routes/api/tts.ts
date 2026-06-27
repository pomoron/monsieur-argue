/**
 * /api/tts — proxy to Python backend.
 *
 * Python uses Google Cloud TTS (Neural2 voice) with edge-tts as fallback.
 * Returns audio/mpeg bytes.
 *
 * Note: The Python backend returns raw MP3 bytes (not SSE/PCM like the original
 * Loveable route). tts-player.ts's streamSpeech() is updated to handle MP3 blobs.
 */
import { createFileRoute } from "@tanstack/react-router";

const PYTHON_API = process.env["PYTHON_API_URL"] ?? "http://localhost:8000";

export const Route = createFileRoute("/api/tts")({
  server: {
    handlers: {
      POST: async ({ request }) => {
        const body = await request.json();
        const upstream = await fetch(`${PYTHON_API}/api/tts`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: request.signal,
        });
        if (!upstream.ok) {
          const msg = await upstream.text().catch(() => "");
          return new Response(`TTS error: ${upstream.status} ${msg}`, {
            status: upstream.status,
          });
        }
        return new Response(upstream.body, {
          status: 200,
          headers: { "Content-Type": "audio/mpeg" },
        });
      },
    },
  },
});
