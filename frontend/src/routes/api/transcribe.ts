/**
 * /api/transcribe — proxy to Python backend.
 *
 * Python uses voice_io.py's STT pipeline (Gemini STT / faster-whisper fallback)
 * and returns {text: string}.
 */
import { createFileRoute } from "@tanstack/react-router";

const PYTHON_API = process.env["PYTHON_API_URL"] ?? "http://localhost:8000";

export const Route = createFileRoute("/api/transcribe")({
  server: {
    handlers: {
      POST: async ({ request }) => {
        // Forward the multipart form data unchanged
        const formData = await request.formData();
        const upstream = await fetch(`${PYTHON_API}/api/transcribe`, {
          method: "POST",
          body: formData,
        });
        if (!upstream.ok) {
          const msg = await upstream.text().catch(() => "");
          return new Response(`STT error: ${upstream.status} ${msg}`, {
            status: upstream.status,
          });
        }
        const data = await upstream.json();
        return Response.json(data);
      },
    },
  },
});
