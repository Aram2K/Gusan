// speed-bench, switched off. The Gusan Interpreter benchmark's function (bench/interpreter/server/
// index.ts) is deployed only for a run; right after the run this stub replaces it, so the project's
// server keys can no longer be reached through it. Deploy with verify_jwt=false, like the real one.
Deno.serve(() => new Response(JSON.stringify({ error: "gone" }), {
  status: 410,
  headers: { "Content-Type": "application/json" },
}));
