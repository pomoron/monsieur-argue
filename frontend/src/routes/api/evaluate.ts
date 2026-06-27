/**
 * /api/evaluate — proxy to Python backend.
 *
 * Python uses Legora's scoring pipeline (with LLM fallback) to produce
 * {overall_score, grade, dimensions, strengths, weaknesses, addressed_terms, missed_terms}.
 */
import { createFileRoute } from "@tanstack/react-router";

const PYTHON_API = process.env["PYTHON_API_URL"] ?? "http://localhost:8000";

export const Route = createFileRoute("/api/evaluate")({
  server: {
    handlers: {
      POST: async ({ request }) => {
        const body = await request.json();
        const upstream = await fetch(`${PYTHON_API}/api/evaluate`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!upstream.ok) {
          const msg = await upstream.text().catch(() => "");
          return new Response(`Evaluate error: ${upstream.status} ${msg}`, {
            status: upstream.status,
          });
        }
        const data = await upstream.json();
        return Response.json(data);
      },
    },
  },
});
