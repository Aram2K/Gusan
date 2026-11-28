// Where Gusan's "Confirm your email" and "Reset your password" links land (Supabase Auth's Site URL: the app
// sends no redirect address of its own).
//
// It used to answer with an HTML page, but Supabase serves web pages from its own *.supabase.co address as
// plain text (an anti-phishing rule): people saw the page's source code. Now it only forwards the browser to
// the real page on the Gusan site, https://gusan.aibuben.xyz/confirmed (aibuben repo,
// transcribe-site/public/confirmed.html). What follows "#" in the address (the sign-in details, a reset link's
// key, or the error of a link already used: error_code=otp_expired) never reaches this function; the browser
// keeps it across the redirect, and the page reads it there. A query string, when Supabase sends one, is passed on unchanged.
// Links already sent keep working: they still point here.
//
// The Gusan connector (docs/mcp/gusan-connector-spec.md): Supabase Auth's OAuth 2.1 server sends a person who
// is connecting an AI assistant to Site URL + "/oauth/consent" (the Authorization Path), i.e.
// .../functions/v1/confirmed/oauth/consent?authorization_id=... That goes to the consent page on the site,
// https://gusan.aibuben.xyz/oauth/consent, with the same query.

const PAGE = "https://gusan.aibuben.xyz/confirmed";
const CONSENT = "https://gusan.aibuben.xyz/oauth/consent";

export function target(requestUrl: string): string {
  const url = new URL(requestUrl);
  const page = /\/oauth\/consent\/?$/.test(url.pathname) ? CONSENT : PAGE;
  return page + url.search;
}

Deno.serve((req) => {
  return new Response(null, {
    status: 302,
    headers: {
      "Location": target(req.url),
      "Cache-Control": "no-store",
      "Referrer-Policy": "no-referrer",
    },
  });
});
