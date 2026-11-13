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

const PAGE = "https://gusan.aibuben.xyz/confirmed";

Deno.serve((req) => {
  const search = new URL(req.url).search;
  return new Response(null, {
    status: 302,
    headers: {
      "Location": PAGE + search,
      "Cache-Control": "no-store",
      "Referrer-Policy": "no-referrer",
    },
  });
});
