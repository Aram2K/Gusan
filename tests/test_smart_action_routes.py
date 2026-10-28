"""The managed Smart Actions proxy (supabase/functions/smart-action/index.ts): the OpenAI engines.

The REAL source runs under Node (type stripping) with Deno, Supabase, the Anthropic SDK and fetch
faked, the way tests/test_live_speech.py runs the transcription proxy. Every scenario gets a fresh
copy of the module (its own env and its own per-isolate state); one Node process runs them all.
What is checked: the order the engines are tried in per mode, what is sent to OpenAI for each model
family, the model-name secrets, the key names, the reasoning_effort fallback, screenshots, and that
the OpenAI key never reaches a log line or a response.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "supabase" / "functions" / "smart-action" / "index.ts"

OPENAI_KEY = "sk-test-OPENKEY-0001"
HOSTS = ["live", "openai", "anthropic", "mistral", "gemini"]


def _node_can_strip_types():
    node = shutil.which("node")
    if not node:
        return None
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20).stdout
        major, minor = [int(x) for x in out.strip().lstrip("v").split(".")[:2]]
    except Exception:
        return None
    return node if (major, minor) >= (22, 6) else None


STUB_SUPABASE = r"""
export function createClient() {
  const S = () => globalThis.__state;
  return {
    auth: { getUser: async () => ({ data: { user: { id: "u1" } }, error: null }) },
    rpc: async (name) => name === "is_pro" ? { data: true, error: null }
                       : name === "use_smart_action_quota" ? { data: true, error: null } : { data: null, error: {} },
  };
}
"""

# The Anthropic SDK reduced to what the function uses; every request goes through the faked fetch so the
# Claude steps show up in the sequence of URLs like any other engine.
STUB_ANTHROPIC = r"""
export class APIError extends Error { constructor(status, msg) { super(msg); this.status = status; } }
export class AuthenticationError extends APIError {}
export class PermissionDeniedError extends APIError {}
export class NotFoundError extends APIError {}
async function post(key, params, signal) {
  const r = await globalThis.fetch("https://api.anthropic.com/v1/messages", {
    method: "POST", headers: { Authorization: `Bearer ${key}` }, body: JSON.stringify(params), signal });
  if (!r.ok) throw new APIError(r.status, await r.text());
  return r.json();
}
export default class Anthropic {
  static APIError = APIError;
  static AuthenticationError = AuthenticationError;
  static PermissionDeniedError = PermissionDeniedError;
  static NotFoundError = NotFoundError;
  constructor(o) {
    this.models = { retrieve: async (id) => ({ id }) };
    this.messages = {
      create: (params, opts) => post(o.apiKey, params, opts?.signal),
      stream: (params, opts) => ({
        errored: false,
        abort() {},
        [Symbol.asyncIterator]() {
          let n = 0, d = null;
          return { next: async () => {
            if (n === 0) d = await post(o.apiKey, params, opts?.signal);
            const evs = [{ type: "content_block_delta", delta: { type: "text_delta", text: d.content[0].text } },
                         { type: "message_delta", delta: { stop_reason: "end_turn" } }];
            return n < evs.length ? { done: false, value: evs[n++] } : { done: true };
          } };
        },
      }),
    };
  }
}
"""

HARNESS = r"""
import { readFileSync } from "node:fs";
import util from "node:util";
const [, , modDir, scenariosPath] = process.argv;
const scenarios = JSON.parse(readFileSync(scenariosPath, "utf-8"));
const HOST = { "live.test": "live", "api.openai.com": "openai", "api.anthropic.com": "anthropic",
               "api.mistral.ai": "mistral", "generativelanguage.googleapis.com": "gemini" };
const realLog = { log: console.log, warn: console.warn, error: console.error };
const realSetTimeout = globalThis.setTimeout, realClearTimeout = globalThis.clearTimeout;

// Which timers are armed right now (an attempt arms exactly one: its budget) - read when a request is sent.
const active = new Map();
globalThis.setTimeout = (fn, ms, ...a) => {
  const id = realSetTimeout(() => { active.delete(id); fn(...a); }, Math.min(ms, 250), ...a);
  active.set(id, ms);
  return id;
};
globalThis.clearTimeout = (id) => { active.delete(id); realClearTimeout(id); };

const out = {};
for (let i = 0; i < scenarios.length; i++) {
  const sc = scenarios[i];
  const ENV = { SUPABASE_URL: "http://x", SUPABASE_ANON_KEY: "k", ...sc.env };
  globalThis.Deno = { env: { get: (k) => ENV[k] }, serve: (fn) => { globalThis.__handler = fn; } };
  const logs = [];
  for (const k of ["log", "warn", "error"]) console[k] = (...a) => logs.push(util.format(...a));
  const calls = [];
  let queues = {};
  globalThis.fetch = async (url, init) => {
    const u = String(url);
    const host = HOST[new URL(u).host] ?? "other";
    const body = typeof init.body === "string" ? JSON.parse(init.body) : null;
    calls.push({ host, url: u, body, auth: init.headers?.Authorization ?? null,
                 budget: active.size ? [...active.values()][0] : 0 });
    const q = queues[host];
    const r = q && q.length ? (q.length > 1 ? q.shift() : q[0]) : {};
    if (r.throw) throw new Error(r.throw);
    if (r.stall) {   // accepts the connection and never answers: only the abort signal ends it
      return new Promise((_res, rej) => init.signal?.addEventListener("abort", () => rej(new Error("aborted"))));
    }
    const status = r.status ?? 200;
    if (status !== 200) return new Response(r.text ?? "boom", { status });
    if (host === "anthropic") {
      return new Response(JSON.stringify({ content: [{ type: "text", text: "anthropic words" }], stop_reason: "end_turn" }));
    }
    const text = r.empty ? "" : `${host} words`;
    if (body?.stream) {
      return new Response(`data: ${JSON.stringify({ choices: [{ delta: { content: text } }] })}\n\ndata: [DONE]\n\n`,
                          { headers: { "Content-Type": "text/event-stream" } });
    }
    return new Response(JSON.stringify({ choices: [{ message: { role: "assistant", content: text },
                                                       finish_reason: r.finish ?? "stop" }] }));
  };
  await import(`${modDir}/proxy.mts?s=${i}`);
  const handler = globalThis.__handler;
  const results = [];
  for (const rq of sc.requests) {
    calls.length = 0;
    queues = structuredClone(rq.behave ?? sc.behave ?? {});
    const sent = rq.pad ? { ...rq.body, messages: [...rq.body.messages, { role: "user", content: "x".repeat(rq.pad) }] }
                        : rq.body;
    const res = await handler(new Request("http://proxy/", {
      method: "POST", headers: { Authorization: rq.auth ?? "Bearer aaa.bbb.ccc", "Content-Type": "application/json" },
      body: JSON.stringify(sent) }));
    const text = await res.text();
    let json = null; try { json = JSON.parse(text); } catch (_e) { /* an event stream */ }
    results.push({ status: res.status, text, json, family: res.headers.get("X-Answer-Family"),
                   calls: calls.map((c) => ({ ...c })), logs: logs.splice(0) });
  }
  out[sc.id] = results;
}
Object.assign(console, realLog);
// Leave once the output has been handed over: on Linux a pipe is written asynchronously, so exiting
// at once cuts the answer at 64 KiB (Windows pipes don't).
process.stdout.write(JSON.stringify(out), () => process.exit(0));
"""

MSG = [{"role": "system", "content": "You rewrite text."}, {"role": "user", "content": "hello there"}]
SHOT = [{"role": "system", "content": "Answer from the screen."},
        {"role": "user", "content": [{"type": "text", "text": "What is this?"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]}]

FULL_ENV = {"MISTRAL_STT_KEY": "mistral-key-0001", "GOOGLE_STT_KEY": "gemini-key-0001",
            "CLAUDE_LLM_KEY": "claude-key-0001", "LIVE_LLM_URL": "https://live.test",
            "LIVE_LLM_TOKEN": "wk-live.ws-live-0001", "LIVE_LLM_MODEL": "deepseek-ai/test",
            "OPENAI_TOKEN": OPENAI_KEY}
NO_OPENAI_ENV = {k: v for k, v in FULL_ENV.items() if k != "OPENAI_TOKEN"}
ONLY_OPENAI = {"OPENAI_TOKEN": OPENAI_KEY}
ALL_FAIL = {h: [{"status": 500}] for h in HOSTS}


def req(mode=None, stream=False, messages=MSG, max_tokens=300, **extra):
    body = {"messages": messages, "max_tokens": max_tokens, "stream": stream}
    if mode:
        body["mode"] = mode
    return {"body": body, **extra}


# the four kinds of request, the way the app sends them
SMART = req("translate")
LIVE = req("live_assist", stream=True)
NOTES = req("meeting_notes")
OTHER = req("live_recap")
OTHER_STREAM = req("live_recap", stream=True)
NO_MODE = req()
SMART_STREAM = req("translate", stream=True)

# (model secret, reasoning_effort sent, temperature a Smart Action sends)
FAMILIES = [
    ("gpt-5.4-mini", "none", None), ("gpt-5.4-nano", "none", None), ("gpt-5.5", "none", None),
    ("gpt-5.6-sol", "none", None), ("gpt-6-sol", "none", None),
    ("gpt-5-mini", "minimal", None), ("gpt-5-nano", "minimal", None), ("gpt-5", "minimal", None),
    ("o3-mini", "low", None),
    ("gpt-4.1", None, 0.1), ("gpt-4.1-nano", None, 0.1), ("gpt-4o-mini", None, 0.1),
]
BAD_NAMES = ["gpt-5.4-mini/../evil", "gpt 5", "x", "-gpt-5", "gpt-5.4?model=evil", "a" * 70,
             "gpt-5.4\nmini", "", "   ", "gpt-5;rm", "gpt-5.4-minié"]


def build_scenarios():
    s = []

    def add(id, env, requests, behave=None):
        s.append({"id": id, "env": env, "requests": requests, "behave": behave or {}})

    # every engine fails: the URLs fetched are the whole chain, in order
    all_modes = [SMART, req("summarize"), req("smart_auto"), LIVE, NOTES, OTHER, OTHER_STREAM, NO_MODE, SMART_STREAM]
    add("order", FULL_ENV, all_modes, ALL_FAIL)
    add("order_no_openai", NO_OPENAI_ENV, all_modes, ALL_FAIL)
    add("only_openai", ONLY_OPENAI, [SMART, LIVE, NOTES, OTHER, NO_MODE])
    add("nothing", {}, [SMART])
    # everything works: who answers first
    add("all_ok", FULL_ENV, [SMART, LIVE, NOTES, OTHER, NO_MODE])
    # the first engine fails, the next answers
    add("live_fails", FULL_ENV, [SMART, LIVE], {"live": [{"status": 500}]})
    add("live_openai_fail", FULL_ENV, [SMART, LIVE],
        {"live": [{"status": 500}], "openai": [{"status": 500}]})
    add("notes_openai_fails", FULL_ENV, [NOTES], {"openai": [{"status": 500}]})
    add("other_mistral_fails", FULL_ENV, [OTHER], {"mistral": [{"status": 500}]})
    # what goes to OpenAI
    add("defaults", ONLY_OPENAI, [SMART, LIVE, NOTES, NO_MODE, req("translate", max_tokens=5000)])
    for model, _effort, _temp in FAMILIES:
        add(f"family:{model}", {**ONLY_OPENAI, "OPENAI_FAST_MODEL": model, "OPENAI_LIVE_MODEL": model,
                                "OPENAI_NOTES_MODEL": model}, [SMART, LIVE, NOTES, NO_MODE])
    add("custom_models", {**ONLY_OPENAI, "OPENAI_FAST_MODEL": " gpt-5.5 ", "OPENAI_LIVE_MODEL": "gpt-4.1-nano",
                          "OPENAI_NOTES_MODEL": "gpt-5.5"}, [SMART, LIVE, NOTES])
    for i, bad in enumerate(BAD_NAMES):
        add(f"bad_name:{i}", {**ONLY_OPENAI, "OPENAI_FAST_MODEL": bad, "OPENAI_LIVE_MODEL": bad,
                              "OPENAI_NOTES_MODEL": bad}, [SMART, LIVE, NOTES, NO_MODE])
    # which secret holds the key
    add("key:all_three", {"OPENAI_TOKEN": "tok-AAAA-0001", "OPENAI_API_KEY": "api-BBBB-0002",
                          "OPENAI_KEY": "key-CCCC-0003"}, [SMART])
    add("key:blank_token", {"OPENAI_TOKEN": "   ", "OPENAI_API_KEY": "api-BBBB-0002",
                            "OPENAI_KEY": "key-CCCC-0003"}, [SMART])
    add("key:api_key", {"OPENAI_API_KEY": "api-BBBB-0002"}, [SMART])
    add("key:key", {"OPENAI_KEY": " key-CCCC-0003 "}, [SMART])
    add("key:blank_all", {"OPENAI_TOKEN": "", "OPENAI_API_KEY": "  ", "MISTRAL_STT_KEY": "m-0001"}, [SMART])
    # reasoning_effort refused: the same request without it
    reject = {"status": 400, "text": '{"error":{"message":"Unsupported parameter: reasoning_effort"}}'}
    add("retry", ONLY_OPENAI, [SMART, LIVE], {"openai": [reject, {}]})
    add("retry_422", {**ONLY_OPENAI, "OPENAI_NOTES_MODEL": "gpt-5.5"}, [NOTES], {"openai": [{**reject, "status": 422}, {}]})
    add("retry_gpt4", {**ONLY_OPENAI, "MISTRAL_STT_KEY": "m-0001", "OPENAI_FAST_MODEL": "gpt-4.1"},
        [SMART], {"openai": [reject]})
    add("retry_both_rejected", {**ONLY_OPENAI, "MISTRAL_STT_KEY": "m-0001"}, [SMART], {"openai": [reject]})
    # the live model's variant state is the live model's alone
    add("live_state", FULL_ENV, [SMART, SMART, LIVE], {"live": [{"status": 500}], "openai": [reject, {}]})
    add("live_variant_kept", FULL_ENV,
        [{**SMART, "behave": {"live": [{"status": 400, "text": "bad field"}, {}]}},
         {**SMART, "behave": {"live": [{}]}},
         {**SMART, "behave": {"live": [{"status": 500}], "openai": [reject, {}]}},
         {**SMART, "behave": {"live": [{}]}}])
    # screenshots
    add("image", FULL_ENV, [req("live_assist", stream=True, messages=SHOT), req("translate", messages=SHOT),
                            req("meeting_notes", messages=SHOT)],
        {"live": [{"status": 500}], "openai": [{}]})
    # an answer that is empty or cut off goes on to the next engine
    add("empty", {**ONLY_OPENAI, "MISTRAL_STT_KEY": "m-0001"}, [SMART], {"openai": [{"empty": True}]})
    # cut off at max_tokens: kept from OpenAI (a usable answer), thrown away only from the think-model live route
    add("cut_off", {**ONLY_OPENAI, "MISTRAL_STT_KEY": "m-0001"}, [SMART, NOTES], {"openai": [{"finish": "length"}]})
    add("cut_off_live", FULL_ENV, [SMART], {"live": [{"finish": "length"}]})
    # no mode / a non-streamed live answer: the same empty check for every engine
    add("empty_generic", {**ONLY_OPENAI, "GOOGLE_STT_KEY": "gemini-key-0001"},
        [NO_MODE, OTHER, req("live_assist")], {"openai": [{"empty": True}]})
    add("empty_stream", {**ONLY_OPENAI, "MISTRAL_STT_KEY": "m-0001"}, [LIVE], {"openai": [{"empty": True}]})
    # the key must not leak
    add("leak_http", ONLY_OPENAI, [SMART, LIVE, NOTES],
        {"openai": [{"status": 401, "text": f"Incorrect API key provided: {OPENAI_KEY}. See the docs."}]})
    add("leak_throw", ONLY_OPENAI, [SMART, LIVE], {"openai": [{"throw": f"connect failed for {OPENAI_KEY}"}]})
    # a stalled engine never holds the chain: its timer fires and the next one answers
    add("stall_generic", FULL_ENV, [OTHER], {"mistral": [{"stall": True}]})
    add("stall_notes", FULL_ENV, [NOTES], {"openai": [{"stall": True}], "anthropic": [{"stall": True}]})
    add("stall_notes_tail", FULL_ENV, [NOTES], {"openai": [{"status": 500}], "anthropic": [{"status": 500}],
                                                 "mistral": [{"stall": True}]})
    add("stall_smart_stream", FULL_ENV, [SMART_STREAM], {"live": [{"stall": True}], "openai": [{"stall": True}]})
    # what the app may send
    add("max_tokens", ONLY_OPENAI,
        [req("translate", max_tokens=v) for v in (-5, 0, 0.5, "-Infinity", "Infinity", "abc", None, 300.7, 1e9)])
    add("size", ONLY_OPENAI, [{**SMART, "pad": 9_000_000}, {**SMART, "pad": 1_000_000},
                              {**SMART, "pad": 10}, {**SMART, "auth": "Bearer garbage"}])
    shot_urls = [{"type": "text", "text": "What is this?"},
                 {"type": "image_url", "image_url": {"url": "http://169.254.169.254/latest"}},
                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]
    only_bad = [{"type": "image_url", "image_url": {"url": "https://internal.test/x.png"}}]
    add("image_urls", ONLY_OPENAI, [req("live_assist", stream=True, messages=[
        {"role": "system", "content": "s"}, {"role": "user", "content": shot_urls}]),
        req("translate", messages=[{"role": "user", "content": "hi"}, {"role": "user", "content": only_bad}])])
    # one bad image does not switch the live model off for good, and a failed model lookup is not repeated
    add("lookup_down", {k: v for k, v in FULL_ENV.items() if k != "LIVE_LLM_MODEL"}, [SMART, SMART])
    # what the upstream says stays out of the answer and, when it can hold the user's words, out of the log
    add("leak_masked", ONLY_OPENAI, [SMART, NOTES],
        {"openai": [{"status": 401, "text": "Incorrect API key provided: sk-proj-****************abcd. org-SECRET"}]})
    add("leak_straddle", ONLY_OPENAI, [SMART],
        {"openai": [{"status": 500, "text": "x" * 148 + f" {OPENAI_KEY} tail"}]})
    add("leak_input", ONLY_OPENAI, [SMART], {"openai": [{"status": 422, "text": json.dumps({"detail": [{
        "type": "string_type", "loc": ["body", "messages", 0, "content"], "msg": "Input should be a valid string",
        "input": "SECRET USER WORDS"}]})}]})
    return s


@unittest.skipUnless(_node_can_strip_types(), "needs Node 22.6+ (type stripping) to run the function")
class TestSmartActionRoutes(unittest.TestCase):
    """The real function, fetch / Supabase / the Anthropic SDK faked."""

    @classmethod
    def setUpClass(cls):
        src = SOURCE.read_text(encoding="utf-8")
        cls.tmp = tempfile.mkdtemp(prefix="gusan-smart-")
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        for name, text in (("stub_supabase.mjs", STUB_SUPABASE), ("stub_anthropic.mjs", STUB_ANTHROPIC),
                           ("harness.mjs", HARNESS)):
            with open(os.path.join(cls.tmp, name), "w", encoding="utf-8") as f:
                f.write(text)
        patched = re.sub(r'from "https://esm\.sh/@supabase/supabase-js@2"', 'from "./stub_supabase.mjs"', src)
        patched = re.sub(r'from "npm:@anthropic-ai/sdk@[\d.]+"', 'from "./stub_anthropic.mjs"', patched)
        assert patched.count("./stub_") == 2, "the imports of the function changed"
        with open(os.path.join(cls.tmp, "proxy.mts"), "w", encoding="utf-8") as f:
            f.write(patched)
        scenarios = os.path.join(cls.tmp, "scenarios.json")
        with open(scenarios, "w", encoding="utf-8") as f:
            json.dump(build_scenarios(), f)
        node = _node_can_strip_types()
        # the same Node flags the other proxy tests use
        p = subprocess.run([node, "--no-warnings", "--experimental-strip-types",
                            os.path.join(cls.tmp, "harness.mjs"), pathlib.Path(cls.tmp).as_uri(), scenarios],
                           capture_output=True, text=True, timeout=120)
        assert p.returncode == 0, p.stderr[-1500:]
        cls.out = json.loads(p.stdout)

    # ── helpers ────────────────────────────────────────────────────────────
    def hosts(self, r):
        return [c["host"] for c in r["calls"]]

    def calls_to(self, r, host):
        return [c for c in r["calls"] if c["host"] == host]

    def sc(self, id):
        return self.out[id]

    # ── the order engines are tried in ─────────────────────────────────────
    def test_every_mode_walks_its_chain_in_order(self):
        g = "gemini"
        want = {
            "smart": ["live", "openai", "anthropic", "mistral", g, g],
            "live": ["live", "openai", "anthropic", "mistral", g, g],
            "notes": ["openai", "anthropic", "mistral", g, g],
            "other": ["mistral", "openai", g, g],
        }
        r = self.sc("order")
        for i, key in ((0, "smart"), (1, "smart"), (2, "smart"), (3, "live"), (4, "notes"), (5, "other"),
                       (6, "other"), (7, "other"), (8, "smart")):
            self.assertEqual(self.hosts(r[i]), want[key], f"request {i} ({key})")
            self.assertEqual(r[i]["status"], 502)
            self.assertEqual(r[i]["json"]["error"], "action_failed")

    def test_the_openai_urls_are_the_chat_completions_endpoint(self):
        for res in self.sc("order"):
            for c in self.calls_to(res, "openai"):
                self.assertEqual(c["url"], "https://api.openai.com/v1/chat/completions")

    def test_without_an_openai_key_nothing_changes(self):
        g = "gemini"
        want = {
            0: ["live", "anthropic", "mistral", g, g], 3: ["live", "anthropic", "mistral", g, g],
            4: ["anthropic", "mistral", g, g], 5: ["mistral", g, g], 7: ["mistral", g, g],
        }
        r = self.sc("order_no_openai")
        for i, hosts in want.items():
            self.assertEqual(self.hosts(r[i]), hosts, f"request {i}")

    def test_openai_alone_is_enough_to_answer_every_kind_of_request(self):
        for i, res in enumerate(self.sc("only_openai")):
            self.assertEqual(res["status"], 200, i)
            self.assertEqual(self.hosts(res), ["openai"], i)
        self.assertEqual(self.sc("only_openai")[0]["json"]["text"], "openai words")

    def test_no_engine_at_all_is_not_configured(self):
        r = self.sc("nothing")[0]
        self.assertEqual((r["status"], r["json"]["error"]), (503, "actions_not_configured"))
        self.assertEqual(r["calls"], [])

    def test_the_live_model_still_answers_first_when_it_works(self):
        smart, live, notes, other, no_mode = self.sc("all_ok")
        self.assertEqual(self.hosts(smart), ["live"])
        self.assertEqual(smart["json"]["text"], "live words")
        self.assertEqual(self.hosts(live), ["live"])
        self.assertIn("live words", live["text"])
        # meeting notes start with OpenAI; the generic fallbacks start with Mistral, as before
        self.assertEqual(self.hosts(notes), ["openai"])
        self.assertEqual(self.hosts(other), ["mistral"])
        self.assertEqual(self.hosts(no_mode), ["mistral"])

    def test_openai_answers_when_the_live_model_fails(self):
        smart, live = self.sc("live_fails")
        self.assertEqual(self.hosts(smart), ["live", "openai"])
        self.assertEqual(smart["json"]["text"], "openai words")
        self.assertEqual(self.hosts(live), ["live", "openai"])
        self.assertIn("openai words", live["text"])
        self.assertEqual(live["status"], 200)

    def test_claude_answers_when_openai_fails_too(self):
        smart, live = self.sc("live_openai_fail")
        self.assertEqual(self.hosts(smart), ["live", "openai", "anthropic"])
        self.assertEqual(smart["json"]["text"], "anthropic words")
        self.assertEqual(self.hosts(live), ["live", "openai", "anthropic"])
        self.assertIn("anthropic words", live["text"])

    def test_notes_fall_to_claude_and_the_generic_fallback_to_openai(self):
        self.assertEqual(self.hosts(self.sc("notes_openai_fails")[0]), ["openai", "anthropic"])
        r = self.sc("other_mistral_fails")[0]
        self.assertEqual(self.hosts(r), ["mistral", "openai"])
        self.assertEqual(r["json"]["text"], "openai words")

    def test_budgets_per_route(self):
        r = self.sc("order")

        def budgets(i):
            return [c["budget"] for c in r[i]["calls"]]
        # Smart Action (not streamed): live 15 s, OpenAI 12 s like Claude; Mistral/Gemini the 20 s cap
        self.assertEqual(budgets(0), [15_000, 12_000, 12_000, 20_000, 20_000, 20_000])
        # a streamed Smart Action (what the app sends): the live model 8 s, the others 6 s to first words
        self.assertEqual(budgets(8), [8_000, 6_000, 6_000, 6_000, 6_000, 6_000])
        # Live Assistant (streamed): the live model 8 s to first words, the others 12 s
        self.assertEqual(budgets(3), [8_000, 12_000, 12_000, 12_000, 12_000, 12_000])
        # notes: OpenAI 18 s and Claude 14 s for the whole notes, the tail 10 s each
        self.assertEqual(budgets(4), [18_000, 14_000, 10_000, 10_000, 10_000])
        # the generic fallback streamed: 30 s to first words everywhere
        self.assertEqual(budgets(6), [30_000, 30_000, 30_000, 30_000])
        # ... not streamed: every engine has a limit (Mistral and Gemini the 20 s cap, OpenAI 12 s)
        self.assertEqual(budgets(5), [20_000, 12_000, 20_000, 20_000])

    def test_the_notes_chain_reaches_mistral_inside_the_apps_45_seconds(self):
        notes = [c["budget"] for c in self.sc("order")[4]["calls"]]
        self.assertLess(sum(notes[:3]), 45_000)        # OpenAI + Claude + Mistral
        self.assertTrue(all(b > 0 for b in notes))

    def test_a_non_streamed_request_never_runs_an_engine_without_a_timer(self):
        for id in ("order", "order_no_openai"):
            for i, res in enumerate(self.sc(id)):
                self.assertTrue(all(c["budget"] > 0 for c in res["calls"]), f"{id} {i}")

    def test_a_stalled_engine_falls_through_to_the_next_one(self):
        r = self.sc("stall_generic")[0]
        self.assertEqual(self.hosts(r), ["mistral", "openai"])
        self.assertEqual(r["json"]["text"], "openai words")
        r = self.sc("stall_notes")[0]
        self.assertEqual(self.hosts(r), ["openai", "anthropic", "mistral"])
        self.assertEqual(r["json"]["text"], "mistral words")
        r = self.sc("stall_notes_tail")[0]
        self.assertEqual(self.hosts(r), ["openai", "anthropic", "mistral", "gemini"])
        self.assertEqual(r["json"]["text"], "gemini words")
        r = self.sc("stall_smart_stream")[0]
        self.assertEqual(self.hosts(r), ["live", "openai", "anthropic"])
        self.assertIn("anthropic words", r["text"])

    # ── what is sent to OpenAI ─────────────────────────────────────────────
    def openai_body(self, res):
        calls = self.calls_to(res, "openai")
        self.assertTrue(calls, "no OpenAI request")
        return calls[0]["body"]

    def test_the_default_models_and_their_requests(self):
        smart, live, notes, no_mode, big = self.sc("defaults")
        b = self.openai_body(smart)
        self.assertEqual(b, {"model": "gpt-5.4-mini", "messages": MSG, "max_completion_tokens": 300,
                             "stream": False, "reasoning_effort": "none"})
        b = self.openai_body(live)
        self.assertEqual(b, {"model": "gpt-5.4-nano", "messages": MSG, "max_completion_tokens": 300,
                             "stream": True, "reasoning_effort": "none"})
        b = self.openai_body(notes)
        self.assertEqual(b, {"model": "gpt-4.1", "messages": MSG, "max_completion_tokens": 300, "stream": False})
        b = self.openai_body(no_mode)
        self.assertEqual((b["model"], b["reasoning_effort"]), ("gpt-5.4-mini", "none"))
        self.assertEqual(self.openai_body(big)["max_completion_tokens"], 2000)    # the existing ceiling

    def test_a_request_never_carries_max_tokens_for_openai(self):
        for id in ("defaults", "custom_models", "family:gpt-5-mini", "family:gpt-4.1"):
            for res in self.sc(id):
                self.assertNotIn("max_tokens", self.openai_body(res), id)
                self.assertIn("max_completion_tokens", self.openai_body(res), id)

    def test_each_model_family_gets_its_own_reasoning_setting(self):
        for model, effort, temp in FAMILIES:
            smart, live, notes, no_mode = self.sc(f"family:{model}")
            for res in (smart, live, notes, no_mode):
                b = self.openai_body(res)
                self.assertEqual(b["model"], model)
                if effort is None:
                    self.assertNotIn("reasoning_effort", b, model)
                else:
                    self.assertEqual(b["reasoning_effort"], effort, model)
            # only the gpt-4 family takes a temperature, and only for a Smart Action
            self.assertEqual(self.openai_body(smart).get("temperature"), temp, model)
            for res in (live, notes, no_mode):
                self.assertNotIn("temperature", self.openai_body(res), model)
            self.assertEqual(self.openai_body(live)["stream"], True)
            self.assertEqual(self.openai_body(smart)["stream"], False)

    def test_the_model_secrets_pick_the_model_for_each_job(self):
        smart, live, notes = self.sc("custom_models")
        self.assertEqual(self.openai_body(smart)["model"], "gpt-5.5")          # trimmed
        self.assertEqual(self.openai_body(live)["model"], "gpt-4.1-nano")
        self.assertNotIn("reasoning_effort", self.openai_body(live))
        self.assertEqual(self.openai_body(notes)["model"], "gpt-5.5")
        self.assertEqual(self.openai_body(notes)["reasoning_effort"], "none")

    def test_a_model_name_that_is_not_a_plain_id_falls_back_to_the_default(self):
        for i, bad in enumerate(BAD_NAMES):
            smart, live, notes, no_mode = self.sc(f"bad_name:{i}")
            self.assertEqual(self.openai_body(smart)["model"], "gpt-5.4-mini", repr(bad))
            self.assertEqual(self.openai_body(live)["model"], "gpt-5.4-nano", repr(bad))
            self.assertEqual(self.openai_body(notes)["model"], "gpt-4.1", repr(bad))
            self.assertEqual(self.openai_body(no_mode)["model"], "gpt-5.4-mini", repr(bad))
            for res in (smart, live, notes, no_mode):
                self.assertEqual(res["status"], 200, repr(bad))

    # ── which secret holds the key ─────────────────────────────────────────
    def test_openai_token_comes_before_the_other_key_names(self):
        auth = lambda id: self.calls_to(self.sc(id)[0], "openai")[0]["auth"]
        self.assertEqual(auth("key:all_three"), "Bearer tok-AAAA-0001")
        self.assertEqual(auth("key:blank_token"), "Bearer api-BBBB-0002")
        self.assertEqual(auth("key:api_key"), "Bearer api-BBBB-0002")
        self.assertEqual(auth("key:key"), "Bearer key-CCCC-0003")

    def test_blank_key_secrets_mean_no_openai_route(self):
        r = self.sc("key:blank_all")[0]
        self.assertEqual(self.hosts(r), ["mistral"])

    # ── reasoning_effort refused ───────────────────────────────────────────
    def test_a_400_or_422_on_reasoning_effort_retries_without_it(self):
        smart, live = self.sc("retry")
        for res in (smart, live):
            first, second = self.calls_to(res, "openai")
            self.assertEqual(first["body"]["reasoning_effort"], "none")
            self.assertNotIn("reasoning_effort", second["body"])
            self.assertEqual({k: v for k, v in first["body"].items() if k != "reasoning_effort"}, second["body"])
            self.assertEqual(res["status"], 200)
        self.assertEqual(smart["json"]["text"], "openai words")
        self.assertIn("openai words", live["text"])
        first, second = self.calls_to(self.sc("retry_422")[0], "openai")
        self.assertIn("reasoning_effort", first["body"])
        self.assertNotIn("reasoning_effort", second["body"])

    def test_a_model_without_reasoning_effort_has_nothing_to_retry(self):
        r = self.sc("retry_gpt4")[0]
        self.assertEqual(self.hosts(r), ["openai", "mistral"])
        self.assertEqual(r["json"]["text"], "mistral words")

    def test_when_both_forms_are_rejected_the_next_engine_answers(self):
        r = self.sc("retry_both_rejected")[0]
        self.assertEqual(self.hosts(r), ["openai", "openai", "mistral"])
        self.assertEqual(r["json"]["text"], "mistral words")

    def test_an_openai_retry_never_touches_the_live_models_variant(self):
        for res in self.sc("live_state"):
            live_calls = self.calls_to(res, "live")
            self.assertEqual(len(live_calls), 1)
            b = live_calls[0]["body"]
            self.assertEqual(b["reasoning_effort"], "none")
            self.assertEqual(b["chat_template_kwargs"], {"thinking": False, "enable_thinking": False})
        # the one that did succeed was OpenAI's second form
        self.assertEqual(self.sc("live_state")[0]["json"]["text"], "openai words")

    def test_the_live_models_own_variant_memory_still_works(self):
        r = self.sc("live_variant_kept")
        first = self.calls_to(r[0], "live")
        self.assertEqual(len(first), 2)                                  # rejected, then the next form
        self.assertIn("reasoning_effort", first[0]["body"])
        self.assertNotIn("reasoning_effort", first[1]["body"])           # kept: chat_template_kwargs only
        self.assertIn("chat_template_kwargs", first[1]["body"])
        # the next request starts with the form the endpoint took ...
        second = self.calls_to(r[1], "live")[0]["body"]
        self.assertNotIn("reasoning_effort", second)
        # ... and an OpenAI fallback (and its own retry) in between does not move it
        third = self.calls_to(r[2], "live")[0]["body"]
        self.assertNotIn("reasoning_effort", third)
        self.assertEqual(self.hosts(r[2]), ["live", "openai", "openai"])
        fourth = self.calls_to(r[3], "live")[0]["body"]
        self.assertNotIn("reasoning_effort", fourth)
        self.assertIn("chat_template_kwargs", fourth)

    # ── screenshots ────────────────────────────────────────────────────────
    def test_screenshots_pass_through_to_openai_unchanged(self):
        for res in self.sc("image"):
            self.assertEqual(self.openai_body(res)["messages"], SHOT)
            self.assertEqual(res["status"], 200)
        live = self.sc("image")[0]
        self.assertIn("data:image/png;base64,QUJD", json.dumps(self.openai_body(live)["messages"]))

    # ── an answer that is no answer ────────────────────────────────────────
    def test_an_empty_openai_answer_goes_to_the_next_engine(self):
        r = self.sc("empty")[0]
        self.assertEqual(self.hosts(r), ["openai", "mistral"])
        self.assertEqual(r["json"]["text"], "mistral words")
        r = self.sc("empty_stream")[0]
        self.assertEqual(self.hosts(r), ["openai", "mistral"])
        self.assertIn("mistral words", r["text"])

    def test_an_answer_cut_off_at_max_tokens_is_kept_from_openai(self):
        for res in self.sc("cut_off"):          # a Smart Action and meeting notes
            self.assertEqual(self.hosts(res), ["openai"])
            self.assertEqual(res["status"], 200)
        self.assertEqual(self.sc("cut_off")[0]["json"]["text"], "openai words")

    def test_a_cut_off_answer_from_the_think_model_still_goes_on(self):
        r = self.sc("cut_off_live")[0]
        self.assertEqual(self.hosts(r), ["live", "openai"])

    def test_every_non_streamed_engine_gets_the_empty_check(self):
        for res in self.sc("empty_generic"):
            self.assertEqual(self.hosts(res), ["openai", "gemini"])
            self.assertEqual(res["json"]["text"], "gemini words")

    # ── what the app may send ──────────────────────────────────────────────
    def test_max_tokens_is_a_whole_number_from_1_to_2000_or_the_default(self):
        want = [600, 600, 600, 600, 600, 600, 600, 300, 2000]
        got = [self.openai_body(res)["max_completion_tokens"] for res in self.sc("max_tokens")]
        self.assertEqual(got, want)

    def test_an_oversized_or_unauthenticated_request_is_refused_first(self):
        big, long_one, small, garbage = self.sc("size")
        self.assertEqual((big["status"], big["json"]["error"]), (413, "too_large"))
        self.assertEqual(big["calls"], [])
        self.assertEqual(long_one["status"], 200)          # a 1 MB transcript still goes through
        self.assertEqual(small["status"], 200)
        self.assertEqual((garbage["status"], garbage["calls"]), (401, []))

    def test_only_inline_images_go_upstream(self):
        live, translate = self.sc("image_urls")
        sent = json.dumps(self.openai_body(live)["messages"])
        self.assertNotIn("169.254", sent)
        self.assertIn("data:image/png;base64,QUJD", sent)
        self.assertIn("What is this?", sent)
        # a message that held nothing but a foreign image is dropped, the rest is answered
        self.assertEqual(self.openai_body(translate)["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(translate["status"], 200)

    def test_a_failed_live_model_lookup_is_not_repeated_on_every_request(self):
        first, second = self.sc("lookup_down")
        lookups = lambda r: [c for c in r["calls"] if c["url"].endswith("/models")]
        self.assertEqual(len(lookups(first)), 1)
        self.assertEqual(lookups(second), [])
        self.assertEqual(first["status"], 200)             # the other engines answer meanwhile

    # ── what the founder can see ───────────────────────────────────────────
    def test_the_log_names_the_route_and_the_model(self):
        smart = self.sc("only_openai")[0]
        line = next(x for x in smart["logs"] if x.startswith("smart action"))
        info = json.loads(line.split(" ", 2)[2])
        self.assertEqual((info["route"], info["model"]), ("openai", "gpt-5.4-mini"))
        notes = self.sc("only_openai")[2]
        info = json.loads(next(x for x in notes["logs"] if x.startswith("meeting notes")).split(" ", 2)[2])
        self.assertEqual((info["route"], info["model"]), ("openai", "gpt-4.1"))
        live = self.sc("only_openai")[1]
        line = next(x for x in live["logs"] if x.startswith("live answer"))
        info = json.loads(line.split(" ", 2)[2])
        self.assertEqual((info["route"], info["model"]), ("openai", "gpt-5.4-nano"))

    # ── the key stays secret ───────────────────────────────────────────────
    def test_the_key_is_in_the_authorization_header_and_nowhere_else(self):
        for res in self.sc("order"):
            for c in self.calls_to(res, "openai"):
                self.assertEqual(c["auth"], f"Bearer {OPENAI_KEY}")
                self.assertNotIn(OPENAI_KEY, json.dumps(c["body"]))

    def test_an_error_that_echoes_the_key_does_not_reach_the_log_or_the_app(self):
        for id in ("leak_http", "leak_throw"):
            for res in self.sc(id):
                self.assertEqual(res["status"], 502, id)
                self.assertEqual(res["json"]["error"], "action_failed", id)
                self.assertTrue(any(x.startswith("openai ") for x in res["logs"]), id)   # it was logged ...
                self.assertNotIn(OPENAI_KEY, res["text"], id)                              # ... without the key
                self.assertNotIn(OPENAI_KEY, "\n".join(res["logs"]), id)

    def test_the_app_gets_no_upstream_text(self):
        for id in ("leak_masked", "leak_http", "leak_straddle"):
            for res in self.sc(id):
                self.assertEqual(res["status"], 502, id)
                self.assertEqual(res["json"], {"error": "action_failed"}, id)
        for res in self.sc("leak_masked"):
            self.assertNotIn("org-SECRET", res["text"])

    def test_a_masked_or_cut_key_does_not_reach_the_log(self):
        for res in self.sc("leak_masked"):
            blob = "\n".join(res["logs"])
            self.assertNotIn("sk-proj", blob)
            self.assertTrue(any(x.startswith("openai failed 401") for x in res["logs"]))
        blob = "\n".join(self.sc("leak_straddle")[0]["logs"])
        self.assertNotIn("sk-test", blob)                  # not even the part before a 160-character cut
        self.assertNotIn(OPENAI_KEY[:12], blob)

    def test_a_rejected_request_logs_its_kind_never_the_users_words(self):
        r = self.sc("leak_input")[0]
        blob = "\n".join(r["logs"])
        self.assertNotIn("SECRET USER WORDS", blob)
        self.assertNotIn("Input should be", blob)
        self.assertIn("string_type", blob)

    def test_the_key_never_appears_in_any_log_or_response(self):
        secrets = [OPENAI_KEY, "tok-AAAA-0001", "api-BBBB-0002", "key-CCCC-0003", "claude-key-0001",
                   "mistral-key-0001", "gemini-key-0001"]
        for id, results in self.out.items():
            for res in results:
                blob = res["text"] + "\n" + "\n".join(res["logs"])
                for secret in secrets:
                    self.assertNotIn(secret, blob, id)


class TestSmartActionSource(unittest.TestCase):
    """Cheap checks on the source itself (these run even without Node)."""

    @classmethod
    def setUpClass(cls):
        cls.src = SOURCE.read_text(encoding="utf-8")

    def test_the_header_lists_the_secrets_and_the_routing(self):
        head = self.src.split("import ", 1)[0]
        for name in ("OPENAI_TOKEN", "OPENAI_FAST_MODEL", "OPENAI_LIVE_MODEL", "OPENAI_NOTES_MODEL"):
            self.assertIn(name, head)

    def test_the_openai_route_uses_the_name_the_log_lines_show(self):
        self.assertIn('name: "openai"', self.src)

    def test_the_live_model_is_not_switched_off_for_good_by_one_bad_image(self):
        self.assertNotIn("liveNoVision = true", self.src)
        self.assertIn("liveNoVisionUntil = Date.now()", self.src)

    def test_the_live_timeout_is_clamped(self):
        self.assertIn("n >= 1_000 && n <= 60_000", self.src)

    def test_the_default_models(self):
        for line in ('Deno.env.get("OPENAI_FAST_MODEL"), "gpt-5.4-mini"',
                     'Deno.env.get("OPENAI_LIVE_MODEL"), "gpt-5.4-nano"',
                     'Deno.env.get("OPENAI_NOTES_MODEL"), "gpt-4.1"'):
            self.assertIn(line, self.src)


if __name__ == "__main__":
    unittest.main()
