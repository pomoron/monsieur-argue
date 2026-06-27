/**
 * /api/chat — thin proxy to the Python FastAPI backend.
 *
 * The Python backend (api_server.py) receives raw scenario/persona/norms data
 * and builds the system prompt using Percy's richer build_system_prompt() with
 * full kill-switch and tactical-awareness logic. It streams the response back.
 */
import { createFileRoute } from "@tanstack/react-router";

const PYTHON_API = process.env["PYTHON_API_URL"] ?? "http://localhost:8000";

export const Route = createFileRoute("/api/chat")({
  server: {
    handlers: {
      POST: async ({ request }) => {
        const body = await request.json();
        const upstream = await fetch(`${PYTHON_API}/api/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: request.signal,
        });
        if (!upstream.ok) {
          const msg = await upstream.text().catch(() => "");
          return new Response(`Chat backend error: ${upstream.status} ${msg}`, {
            status: upstream.status,
          });
        }
        // Stream the response straight through
        return new Response(upstream.body, {
          status: 200,
          headers: { "Content-Type": "text/plain; charset=utf-8" },
        });
      },
    },
  },
});
