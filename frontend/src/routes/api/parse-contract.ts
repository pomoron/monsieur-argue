/**
 * /api/parse-contract — proxy to Python backend.
 *
 * Python uses pypdf + Claude to extract contract terms, then maps to
 * {key_terms: [{term, favours, buyer_fear, seller_fear, clause_ref}], summary}.
 */
import { createFileRoute } from "@tanstack/react-router";

const PYTHON_API = process.env["PYTHON_API_URL"] ?? "http://localhost:8000";

export const Route = createFileRoute("/api/parse-contract")({
  server: {
    handlers: {
      POST: async ({ request }) => {
        const body = await request.json();
        const upstream = await fetch(`${PYTHON_API}/api/parse-contract`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!upstream.ok) {
          const msg = await upstream.text().catch(() => "");
          return new Response(`Parse-contract error: ${upstream.status} ${msg}`, {
            status: upstream.status,
          });
        }
        const data = await upstream.json();
        return Response.json(data);
      },
    },
  },
});
