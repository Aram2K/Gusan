// AI assistants (Claude, ChatGPT, ... through the Gusan connector, docs/mcp/gusan-connector-spec.md) sign in
// with Supabase Auth's OAuth 2.1 server. Their access token is a normal user token for the person who
// allowed them, plus a `client_id` claim. They may only read the person's Gusan meetings (the `mcp`
// function); every function that acts for the signed-in person refuses such a token outright, before
// doing anything.
//
// The claim is read without checking the signature: this only ever refuses. A token that passes still goes
// through auth.getUser() as before, and a forged token with client_id is refused anyway.

export function isAssistantToken(authHeader: string | null | undefined): boolean {
  const token = String(authHeader ?? "").replace(/^Bearer\s+/i, "").trim();
  const payload = token.split(".")[1];
  if (!payload) return false;
  try {
    const b64 = payload.replace(/-/g, "+").replace(/_/g, "/");
    const claims = JSON.parse(atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4)));
    return typeof claims?.client_id === "string" && claims.client_id.length > 0;
  } catch {
    return false;
  }
}

/** The answer to an assistant's token: 403, JSON, with the CORS headers the function already sends. */
export function assistantRefusal(headers: Record<string, string> = {}): Response {
  return new Response(
    JSON.stringify({ error: "assistant_token", message: "An AI assistant connected to Gusan can only read Gusan meetings." }),
    { status: 403, headers: { ...headers, "Content-Type": "application/json" } },
  );
}
