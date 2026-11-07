import json
import pathlib
import re
import shutil
import subprocess
from unittest.mock import Mock, patch
from urllib.parse import quote

import pytest

import action_api
import actions
import meeting_store
from web_sources import sources_html

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "supabase/functions/smart-action/index.ts"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("enabled,privacy,mode,expected", [
    (False, False, "live_assist", False), (True, False, "live_assist", True),
    (True, True, "live_assist", False), (True, False, "meeting_chat", True),
    (True, False, "live_rule", False), (True, False, "live_people", False),
    (True, False, "live_recap", False),
])
def test_managed_opt_in(stream, enabled, privacy, mode, expected):
    source = [{"title": " Example ", "url": "https://example.com"}]
    resp = Mock(status_code=200, headers={"Content-Type": "application/json", "X-Web-Sources": quote(json.dumps(source))})
    resp.json.return_value = {"text": "answer"}
    info = {"sources": [{"title": "old", "url": "https://old.com"}]}
    with patch.object(action_api.requests, "post", return_value=resp) as post:
        kwargs = dict(web_search=enabled, web_query="  latest news?  ", config={"privacy_mode": privacy}, info=info)
        if stream:
            result = action_api.run_managed_action_stream("latest news?", mode, "token", lambda _: None, **kwargs)
        else:
            result = action_api.run_managed_action("latest news?", mode, "token", **kwargs)
        assert result == "answer"
        body = post.call_args.kwargs["json"]
        assert (body.get("web_search") is True) == expected
        if not expected:
            assert "web_search" not in body
            assert "web_query" not in body
        else:
            assert body["web_query"] == "latest news?"
        assert info["sources"] == [{"title": "Example", "url": "https://example.com"}]


@pytest.mark.parametrize("value", [None, "garbled", "%FF", "{}", "null", '[{"url":"javascript:alert(1)"}]'])
def test_bad_source_headers(value):
    assert action_api.parse_web_sources(value) == []


def test_sources_filtered_bounded_and_escaped():
    rows = [{"title": " <title> ", "url": "javascript:alert(1)"},
            {"title": " <title> ", "url": "https://example.com/?a=1&b=2"}] * 8
    parsed = action_api.parse_web_sources(quote(json.dumps(rows)))
    assert len(parsed) == 5
    html = sources_html(parsed)
    assert "&lt;title&gt;" in html and "&amp;" in html
    assert "javascript:" not in html
    assert sources_html([]) == ""
    assert meeting_store.clean_chat([{"role": "assistant", "text": "a", "status": "done", "sources": rows}])[0]["sources"] == parsed


def test_source_url_percent_encoding_survives():
    rows = [{"title": "A page", "url": "https://example.com/a%20page?q=%25"}]
    assert action_api.parse_web_sources(quote(json.dumps(rows))) == rows
    assert action_api.sanitize_web_sources(rows) == rows
    assert rows[0]["url"] in sources_html(rows)


@pytest.mark.parametrize("query,expected", [(" " + "x" * 500, "x" * 400), ("", None), (None, None)])
def test_web_query_bounded(query, expected):
    resp = Mock(status_code=200, headers={})
    resp.json.return_value = {"text": "answer"}
    with patch.object(action_api.requests, "post", return_value=resp) as post:
        action_api.run_managed_action("context", "live_assist", "token", web_search=True, web_query=query)
    assert post.call_args.kwargs["json"].get("web_query") == expected


@pytest.mark.parametrize("mode", ["live_rule", "live_people", "live_recap", "live_assist", "meeting_chat"])
def test_actions_only_questions_opt_in(mode):
    model = next(k for k, v in actions.ACTION_MODELS.items() if v.get("kind") == "managed")
    with patch.object(action_api, "run_managed_action_stream", return_value="answer") as run:
        actions.process_stream("question", mode, lambda _: None, model=model,
                               config={"_managed_token": "token", "_web_search": True, "_web_query": "latest news?"})
    assert run.call_args.kwargs["web_search"] == (mode in ("live_assist", "meeting_chat"))
    assert run.call_args.kwargs["web_query"] == "latest news?"


def test_server_contract():
    src = SERVER.read_text()
    for name in ("Linkup_web", "LINKUP_API_KEY", "LINKUP_KEY", "LINKUP_TOKEN", "LINKUP_TIMEOUT_MS", "LINKUP_DEPTH", "LINKUP_DECIDE_TIMEOUT_MS"):
        assert name in src
    assert '["Linkup_web", "LINKUP_API_KEY", "LINKUP_KEY", "LINKUP_TOKEN"]' in src
    assert 'body.web_search === true' in src
    assert 'AbortSignal.timeout(limits.ms)' in src and 'fast: { ms: LINKUP_TIMEOUT_MS' in src   # per depth (fast: the setting)
    assert 'catch { results = []; }' in src
    assert 'AbortSignal.timeout(ms)' in src
    assert '"Access-Control-Expose-Headers": "X-Web-Sources, X-Answer-Family"' in src
    assert 'encodeURIComponent(JSON.stringify(sources))' in src
    assert 'JSON.stringify({ text, sources })' in src
    for cue in ("exchange rate", "interest rate", "inflation", "forecast", "market cap"):
        assert cue in src
    assert 'liveData.test(q)' in src


def test_meeting_chat_baseline_has_no_web_exception():
    messages = action_api.build_messages("<question>Who is the current CEO of OpenAI?</question>", "meeting_chat")
    assert "Answer only from this meeting." in messages[0]["content"]
    assert "Never guess or invent names, numbers, dates or quotes." in messages[0]["content"]
    assert "Linkup" not in messages[0]["content"]
    assert "web results" not in messages[0]["content"]


def test_heuristic_runtime(tmp_path):
    deno = shutil.which("deno")
    if not deno:
        pytest.skip("Deno unavailable")
    src = SERVER.read_text()
    fn = re.search(r'export function webSearchQuery\(question: string\): string \{.*?\n\}', src, re.S).group(0)
    cases = {"": False, "hi": False, "Summarize what we agreed": False,
             "What is Acme?": True, "Who is the CEO of Acme?": True,
             "What are the latest releases?": True, "Check example.com please": True,
             "What happened in 2024?": True, "look up the weather today": True,
             "What should I say now?": False, "Can you summarize the current discussion?": False,
             "Please search our discussion": False, "What's the latest iPhone price?": True,
             "Who is the CEO of Linkup?": True, "Search the web for Acme": True,
             "What's the current USD/EUR exchange rate?": True,
             "What is the interest rate?": True, "Tell me about inflation": True,
             "What is the forecast?": True, "What is Apple's market cap?": True,
             "What is the current server status?": True,
             "Who is the current US president?": True,
             "Can you summarize the current project discussion?": False}
    script = tmp_path / "heuristic.mts"
    script.write_text(fn + "\nconsole.log(JSON.stringify(" + json.dumps(list(cases)) + ".map(webSearchQuery)));")
    out = subprocess.run([deno, "run", str(script)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert [bool(v) for v in json.loads(out.stdout)] == list(cases.values())


def test_web_decisions_deno(tmp_path):
    deno = shutil.which("deno")
    if not deno:
        pytest.skip("Deno unavailable")
    src = SERVER.read_text()
    functions = src[src.index("export function webSearchQuery"):src.index("type WebResult")]
    script = tmp_path / "decisions.ts"
    script.write_text(functions + r'''
function equal(a: unknown, b: unknown) {
  if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(JSON.stringify({actual:a, expected:b}));
}
const explicit = [
  ["Please search the web for Acme, thanks", "Acme"],
  ["search online for Acme", "Acme"], ["search internet for Acme", "Acme"],
  ["Can you please google Acme?", "Acme?"], ["look up Acme please", "Acme"],
  ["check online Acme", "Acme"], ["find online Acme", "Acme"],
  ["can you search Acme", "Acme"], ["search for Acme", "Acme"],
  ["can you google Acme", "Acme"], ["please google it", "it"],
  ["can you please search Acme", "Acme"],
  ["browse Acme", "Acme"], ["what does the internet say about Acme", "Acme"],
  ["cherche sur internet Acme", "Acme"], ["busca en internet Acme", "Acme"],
  ["such im Internet Acme bitte", "Acme"], ["search the web", "search the web"],
];
for (const [question, query] of explicit) {
  equal(explicitWebQuery(question), query);
  const d = await decideWebSearch(question, "secret context", () => { throw new Error("must not classify"); });
  equal([d.source, d.query, d.classifier_ms], ["explicit", query, 0]);
}
equal(explicitWebQuery("Summarize the discussion"), "");
for (const question of ["search for where John mentioned the budget", "look up John's latest budget", "find today's action items"]) {
  equal(explicitWebQuery(question, "meeting_chat"), "");
  let calls = 0;
  const d = await decideWebSearch(question, "private notes", async () => {
    calls++; return {search:false, query:""};
  }, "meeting_chat");
  equal([d.source, d.query, calls], ["model", "", 1]);
  equal((await decideWebSearch(question, "", async () => null, "meeting_chat")).query, "");
}
for (const question of ["search for Acme", "look up Acme"]) {
  equal((await decideWebSearch(question, "", () => { throw new Error("must not classify"); }, "live_assist")).source, "explicit");
}
for (const question of ["search the web for Acme", "google Acme", "look up online Acme", "check online Acme",
                       "find online Acme", "browse the web Acme", "what does the internet say about Acme",
                       "cherche sur internet Acme", "busca en internet Acme", "such im Internet Acme"]) {
  equal(explicitWebQuery(question, "meeting_chat"), "Acme");
  equal((await decideWebSearch(question, "", () => { throw new Error("must not classify"); }, "meeting_chat")).source, "explicit");
}
for (const mode of ["live_assist", "meeting_chat"]) {
  for (const question of ["search the web for how to advertise on Google", "google how to advertise on Google",
                         "look up online how to advertise on Google"]) {
    equal(explicitWebQuery(question, mode), "how to advertise on Google");
    const d = await decideWebSearch(question, "", () => { throw new Error("must not classify"); }, mode);
    equal([d.source, d.query], ["explicit", "how to advertise on Google"]);
  }
  for (const question of ["search for Acme on the web", "search for Acme online", "search for Acme on the internet",
                         "search for Acme on google", "look up Acme online please", "find Acme online",
                         "cherche Acme sur internet", "cherche Acme en ligne", "busca Acme en internet",
                         "busca Acme en línea", "such Acme im Internet", "suche Acme im Netz"]) {
    equal(explicitWebQuery(question, mode), "Acme");
    const d = await decideWebSearch(question, "", () => { throw new Error("must not classify"); }, mode);
    equal([d.source, d.query], ["explicit", "Acme"]);
  }
}
for (const question of ["What is Google stock price?", "Who is Google's CEO?", "How do we look up the budget?"]) {
  equal(explicitWebQuery(question), "");
  let classified = false;
  const d = await decideWebSearch(question, "", async () => { classified = true; return null; });
  equal(classified, true);
  equal(d.query, webSearchQuery(question));
  if (question.includes("Google")) equal(d.query, question);
}
equal(explicitWebQuery("google " + "x".repeat(500)).length, 400);
equal(parseWebDecision('```json\n{"search":true,"query":"Acme facts"}\n```'), {search:true, query:"Acme facts"});
for (const raw of ["garbage", "{}", "null", '{"search":"true","query":"x"}', '{"search":true}']) equal(parseWebDecision(raw), null);
equal(parseWebDecision(JSON.stringify({search:true, query:"word ".repeat(20)}))?.query.split(" ").length, 12);
let calls = 0;
const yes = await decideWebSearch("How big is Acme?", "x".repeat(2000), async (q, context) => {
  calls++; equal(q, "How big is Acme?"); equal(context.length, 1200);
  return {search:true, query:"Acme size"};
});
equal([yes.source, yes.query, calls], ["model", "Acme size", 1]);
for (const q of ["What should I say now?", "Summarize our decisions", "Who mentioned today's price?"]) {
  const d = await decideWebSearch(q, "meeting", async () => ({search:false, query:""}));
  equal([d.source, d.query], ["model", ""]);
}
const empty = await decideWebSearch("How big is Acme?", "", async () => ({search:true, query:""}));
equal(empty.query, "How big is Acme?");
for (const fail of [async () => { throw new Error("network"); },
                   async () => { throw new DOMException("timeout", "TimeoutError"); },
                   async () => parseWebDecision("garbage")]) {
  const d = await decideWebSearch("What is the latest price?", "", fail);
  equal([d.source, d.query], ["heuristic", "What is the latest price?"]);
  equal((await decideWebSearch("Summarize this discussion", "", fail)).source, "none");
}
for (const q of ["", " hi ", "short?"]) {
  equal(await decideWebSearch(q, "", () => { throw new Error("must not classify"); }), {query:"", source:"none", classifier_ms:0});
}
console.log("Deno web decision regressions passed");
''')
    out = subprocess.run([deno, "run", str(script)], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert "Deno web decision regressions passed" in out.stdout


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("parts", [False, True])
@pytest.mark.parametrize("keys", [
    {"Linkup_web": " test-linkup ", "LINKUP_API_KEY": "other", "LINKUP_KEY": "other"},
    {"Linkup_web": " ", "LINKUP_API_KEY": " ", "LINKUP_KEY": " test-linkup "},
])
def test_server_linkup_runtime(tmp_path, stream, parts, keys):
    from tests.test_smart_action_routes import HARNESS, STUB_SUPABASE, STUB_ANTHROPIC, req
    deno = shutil.which("deno")
    if not deno:
        pytest.skip("Deno unavailable")
    src = SERVER.read_text()
    src = re.sub(r'from "https://esm\.sh/@supabase/supabase-js@2"', 'from "./stub_supabase.mjs"', src)
    src = re.sub(r'from "npm:@anthropic-ai/sdk@[\d.]+"', 'from "./stub_anthropic.mjs"', src)
    src = src.replace("Deno.", "globalThis.__testDeno.")
    (tmp_path / "proxy.mts").write_text(src)
    (tmp_path / "stub_supabase.mjs").write_text(STUB_SUPABASE)
    (tmp_path / "stub_anthropic.mjs").write_text(STUB_ANTHROPIC)
    harness = HARNESS.replace('const text = r.empty', '''if (body?.max_completion_tokens === 60 || body?.max_tokens === 60) {
      return new Response(JSON.stringify({choices:[{message:{content:r.decision ?? "garbage"}}]}));
    }
    if (host === "other") return new Response(JSON.stringify({results: [
      {type:"image", name:"no", url:"https://example.com/img"},
      {type:"text", name:"unsafe", url:"javascript:alert(1)", content:"no"},
      {type:"text", name:"Example", url:"https://example.com/news", content:"facts"}]}));
    const text = r.empty''').replace('family: res.headers.get("X-Answer-Family"),',
        'family: res.headers.get("X-Answer-Family"), sources: res.headers.get("X-Web-Sources"),')
    harness = harness.replace("globalThis.Deno =", "globalThis.__testDeno =")
    msg = [{"role": "user", "content": [{"type": "text", "text": "Private meeting notes\nUser's question: latest Acme news?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]}]
    if not parts:
        msg[0]["content"] = msg[0]["content"][0]["text"]
    meeting_messages = action_api.build_messages(
        "Private transcript\n<question>Who is the current CEO of OpenAI?</question>", "meeting_chat")
    requests = [req("live_assist", stream=stream, messages=msg, web_search=True),
                req("live_assist", stream=stream, messages=msg),
                req("live_assist", stream=stream, messages=msg, web_search=True),
                req("live_assist", stream=stream, messages=[{"role": "user", "content": "Private latest news in transcript"}], web_search=True),
                req("meeting_chat", stream=stream, messages=[{"role": "user", "content": "Private latest news in transcript"}], web_search=True),
                req("live_assist", stream=stream, messages=[{"role": "user", "content": "Private transcript and docs"}], web_search=True, web_query=" latest Acme news? "),
                req("meeting_chat", stream=stream, messages=[{"role": "user", "content": "Private transcript\n<question>latest Acme news?</question>"}], web_search=True),
                req("meeting_chat", stream=stream, messages=meeting_messages, web_search=True,
                    web_query="Who is the current CEO of OpenAI?"),
                req("meeting_chat", stream=stream, messages=meeting_messages),
                req("meeting_chat", stream=stream, messages=meeting_messages, web_search=True),
                req("meeting_chat", stream=stream, messages=meeting_messages, web_search=True),
                req("live_assist", stream=stream, messages=msg, web_search=True, web_query="How big is Acme?"),
                req("meeting_chat", stream=stream, messages=meeting_messages, web_search=True,
                    web_query="Who mentioned today's price?"),
                req("live_assist", stream=stream, messages=msg, web_search=True),
                req("live_assist", stream=stream, messages=msg, web_search=True),
                req("live_assist", stream=stream, messages=msg, web_search=True),
                req("live_assist", stream=stream, messages=msg, web_search=True, web_query="Please google Acme size")]
    requests[2]["behave"] = {"other": [{"status": 500}]}
    requests[9]["behave"] = {"other": [{"status": 500}]}
    requests[10]["behave"] = {"other": [{"empty_results": True}]}
    requests[11]["behave"] = {"openai": [{"decision": '{"search":true,"query":"Acme size"}'}]}
    requests[12]["behave"] = {"openai": [{"decision": '{"search":false,"query":""}'}]}
    requests[13]["behave"] = {"openai": [{"status": 503}, {}]}
    requests[14]["behave"] = {"openai": [{"throw": "private network failure"}, {}]}
    requests[15]["behave"] = {"openai": [{"stall": True}, {}]}
    for request in requests:
        for key in ("web_search", "web_query"):
            if key in request:
                request["body"][key] = request.pop(key)
    harness = harness.replace('if (host === "other") return',
        'if (host === "other" && r.empty_results) return new Response(JSON.stringify({results: []}));\n    if (host === "other") return')
    (tmp_path / "harness.mjs").write_text('import process from "node:process";\n' + harness)
    scenarios = [{"id": "web", "env": {"OPENAI_TOKEN": "test-openai", "LINKUP_DECIDE_TIMEOUT_MS": "300", **keys}, "requests": requests},
                 {"id": "no_key", "env": {"OPENAI_TOKEN": "test-openai"}, "requests": [requests[7]]}]
    (tmp_path / "scenarios.json").write_text(json.dumps(scenarios))
    out = subprocess.run([deno, "run", "--allow-all", str(tmp_path / "harness.mjs"),
                          tmp_path.as_uri(), str(tmp_path / "scenarios.json")], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    results = json.loads(out.stdout)
    (good, old, failed, no_question_live, no_question_chat, explicit, tagged, meeting_web,
     meeting_off, meeting_failed, meeting_empty, model_yes, model_no, unavailable,
     network_error, timeout, direct) = results["web"]
    assert good["status"] == old["status"] == failed["status"] == 200
    calls = good["calls"]
    search = next(c for c in calls if "linkup.so" in c["url"])
    assert search["auth"] == "Bearer test-linkup"
    assert search["body"] == {"q": "latest Acme news?", "depth": "flash", "outputType": "searchResults", "maxResults": 5}
    sources = [{"title": "Example", "url": "https://example.com/news"}]
    assert action_api.parse_web_sources(good["sources"]) == sources
    upstream = next(c for c in calls if c["host"] == "openai" and c["body"].get("max_completion_tokens") != 60)
    assert len(upstream["body"]["messages"]) == len(msg)
    content = upstream["body"]["messages"][-1]["content"]
    if parts:
        assert content[1:] == msg[0]["content"]
        assert "Web search results (Linkup)" in content[0]["text"]
    else:
        assert content.endswith("\n\n" + msg[0]["content"])
        assert content.startswith("Web search results (Linkup)")
    assert not any("linkup.so" in c["url"] for c in old["calls"])
    assert action_api.parse_web_sources(failed["sources"]) == []
    for response in (no_question_live, no_question_chat):
        assert not any("linkup.so" in c["url"] for c in response["calls"])
    for response in (explicit, tagged):
        assert next(c for c in response["calls"] if "linkup.so" in c["url"])["body"]["q"] == "latest Acme news?"
    meeting_upstream = next(c for c in meeting_web["calls"] if c["host"] == "openai" and c["body"].get("max_completion_tokens") != 60)["body"]["messages"]
    assert len(meeting_upstream) == len(meeting_messages)
    system = meeting_upstream[0]["content"]
    assert system.startswith(action_api.MEETING_CHAT_SYSTEM + "\n\n")
    assert "exception to the answer-only-from-this-meeting rule" in system
    assert "cite each web-supported fact inline as [1], [2]" in system
    assert "prefer the meeting transcript and notes" in system
    assert "Never invent meeting content or attribute web facts to the meeting" in system
    assert "Use only the supplied results for outside facts" in system
    assert "Web search results (Linkup)" in meeting_upstream[-1]["content"]
    for response in (meeting_off, meeting_failed, meeting_empty, results["no_key"][0]):
        sent = next(c for c in response["calls"] if c["host"] == "openai" and c["body"].get("max_completion_tokens") != 60)["body"]["messages"]
        assert sent == meeting_messages
        assert action_api.parse_web_sources(response["sources"]) == []
    for response in (old, no_question_live, no_question_chat, results["no_key"][0], direct):
        assert not any(c["body"].get("max_completion_tokens") == 60 for c in response["calls"] if c["body"])
    assert next(c for c in model_yes["calls"] if "linkup.so" in c["url"])["body"]["q"] == "Acme size"
    assert not any("linkup.so" in c["url"] for c in model_no["calls"])
    for response in (unavailable, network_error, timeout):
        assert response["status"] == 200
        assert next(c for c in response["calls"] if "linkup.so" in c["url"])["body"]["q"] == "latest Acme news?"
    for response, source in ((good, "heuristic"), (model_yes, "model"), (model_no, "model"),
                             (direct, "explicit"), (timeout, "heuristic")):
        log = [line for line in response["logs"] if line.startswith("Linkup decision ")]
        assert len(log) == 1
        info = json.loads(log[0].split("Linkup decision ")[1])
        assert info["source"] == source
        assert set(info) == {"source", "classifier_ms", "search_ms", "results"}
    classifier = [c for c in good["calls"] if c["host"] == "openai" and c["body"].get("max_completion_tokens") == 60]
    assert len(classifier) == 1
    assert classifier[0]["body"]["stream"] is False
    classifier_input = json.loads(classifier[0]["body"]["messages"][-1]["content"])
    assert classifier_input["question"] == "latest Acme news?"
    assert len(classifier_input["context"]) <= 1200
    assert "image_url" not in classifier_input["context"]
    assert not any("private network failure" in line for line in network_error["logs"])
    assert "exception to the answer-only-from-this-meeting rule" not in json.dumps(upstream["body"]["messages"])
    assert "test-linkup" not in "".join(good["logs"])
    assert "latest Acme news" not in "".join(good["logs"])
    if not stream:
        assert good["json"]["sources"] == sources


def test_web_search_is_off_until_the_person_turns_it_on():
    """The founder's call (2026-11-07): Linkup searches only for someone who switched it on. Every place that reads
    the setting falls back to off."""
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert re.search(r'"live_assist_web_search":\s*False', main_src)
    for rel in ("main.py", "meeting_chat.py", "ui/live_assist.py", "ui/pages/live_assistant.py", "ui/settings.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert 'get("live_assist_web_search", True)' not in text, rel


# ── Smart Actions (2026-11-07) ────────────────────────────────────────────────────────────────
import web_sources


@pytest.mark.parametrize("text,wanted", [
    ("search the web for the population of Lyon", True),
    ("look up the opening hours of the Louvre", True),
    ("what's the current EUR to USD rate", True),
    ("write an email with the exchange rate of the dollar", True),
    ("cherche sur internet le prix du gaz", True),
    ("write an email to John saying the release is today", False),      # dictation: "release", "today" never search
    ("find the train times to Lyon online", True),                       # a search command with "online" at the end
    ("okay so google is a big company", False),                          # "google" not at the start: not a command
    ("make it shorter and in English", False),
    ("the latest version is ready", False),
    ("hi", False),
])
def test_smart_actions_search_only_when_asked_or_for_live_figures(text, wanted):
    assert web_sources.smart_search_wanted(text) is wanted


def test_the_app_and_the_server_use_the_same_smart_rule():
    """The pill says "Searching the web" before the answer: the app copies the server's patterns exactly."""
    source = SERVER.read_text(encoding="utf-8")
    assert re.search(r"const SMART_LIVE_DATA = /(.+?)/i;", source).group(1) == web_sources.SMART_LIVE
    assert re.search(r"const trigger = /(.+?)/i\.exec\(command\)", source).group(1) == web_sources.SMART_ASK
    assert re.search(r"const trailingWeb = /(.+?)/i;", source).group(1) == web_sources.SMART_TRAILING_WEB
    assert re.search(r"\?\? \(trailingWeb\.test\(command\) \? /(.+?)/i\.exec", source).group(1) == web_sources.SMART_ASK_TRAILING
    assert re.search(r"const command = q\.replace\(/(.+?)/i, \"\"\)", source).group(1) == web_sources.SMART_POLITE


def test_the_server_searches_for_smart_actions_without_an_ai_check_and_keeps_pasted_text_clean():
    source = SERVER.read_text(encoding="utf-8")
    assert 'body.mode === "smart_auto")' in source
    assert 'mode === "smart_auto"\n    ? { query: smartWebQuery(question), source: "smart", classifier_ms: 0 }' in source
    assert "with no [1]-style marks and no list of sources" in source
    assert action_api.server_mode("smart_selection") == "smart_auto"
    assert set(action_api.WEB_SEARCH_MODES) == {"live_assist", "meeting_chat", "smart_auto", "smart_selection"}


@pytest.mark.parametrize("mode", [actions.ACTION_SMART_AUTO, actions.ACTION_SMART_SELECTION])
def test_a_smart_action_sends_the_search_and_gets_the_sources_back(mode):
    source = [{"title": "ECB", "url": "https://www.ecb.europa.eu/rates"}]
    resp = Mock(status_code=200, headers={"Content-Type": "application/json", "X-Web-Sources": quote(json.dumps(source))})
    resp.json.return_value = {"text": "1 EUR = 1.08 USD"}
    info = {}
    cfg = {"_managed_token": "tok", "_web_search": True, "_web_query": "look up the euro dollar rate", "_answer_info": info}
    with patch.object(action_api.requests, "post", return_value=resp) as post:
        out = actions.process("look up the euro dollar rate", mode, model=actions.API_MANAGED_ID, config=cfg)
    body = post.call_args.kwargs["json"]
    assert out.endswith("1 EUR = 1.08 USD")
    assert body["web_search"] is True and body["web_query"] == "look up the euro dollar rate"
    assert body["mode"] == "smart_auto"
    assert info["sources"] == [{"title": "ECB", "url": "https://www.ecb.europa.eu/rates"}]


def test_a_smart_action_without_the_setting_never_asks_to_search():
    resp = Mock(status_code=200, headers={"Content-Type": "application/json"})
    resp.json.return_value = {"text": "ok"}
    with patch.object(action_api.requests, "post", return_value=resp) as post:
        actions.process("look up the euro dollar rate", actions.ACTION_SMART_AUTO, model=actions.API_MANAGED_ID,
                        config={"_managed_token": "tok"})
    assert "web_search" not in post.call_args.kwargs["json"]


def test_the_pill_says_searching_the_web_and_counts_the_sources():
    import pill_copy
    title, sub = pill_copy._writing(None, "", {"writer": "Gusan Cloud", "web": True}, 0.3)
    assert sub == "Searching the web · Gusan Cloud"
    assert pill_copy.web_sources_line(1) == "1 web source" and pill_copy.web_sources_line(3) == "3 web sources"


def test_classifier_live_discovery_deno(tmp_path):
    from tests.test_smart_action_routes import HARNESS, STUB_SUPABASE, STUB_ANTHROPIC, req
    deno = shutil.which("deno")
    if not deno:
        pytest.skip("Deno unavailable")
    src = SERVER.read_text()
    src = re.sub(r'from "https://esm\.sh/@supabase/supabase-js@2"', 'from "./stub_supabase.mjs"', src)
    src = re.sub(r'from "npm:@anthropic-ai/sdk@[\d.]+"', 'from "./stub_anthropic.mjs"', src)
    (tmp_path / "proxy.mts").write_text(src.replace("Deno.", "globalThis.__testDeno."))
    (tmp_path / "stub_supabase.mjs").write_text(STUB_SUPABASE)
    (tmp_path / "stub_anthropic.mjs").write_text(STUB_ANTHROPIC)
    harness = HARNESS.replace("globalThis.Deno =", "globalThis.__testDeno =")
    harness = harness.replace("const out = {};", "const signals = new WeakMap(); let signalCount = 0; const out = {};")
    harness = harness.replace("budget: active.size", "signal: init.signal ? (signals.get(init.signal) ?? (signals.set(init.signal, ++signalCount), signalCount)) : null, budget: active.size")
    harness = harness.replace('const text = r.empty', '''if (u.endsWith("/models")) {
      return new Response(JSON.stringify({data:[{id:"deepseek-ai/test"}]}));
    }
    if (host === "other") return new Response(JSON.stringify({results:[]}));
    if (body?.max_tokens === 60 || body?.max_completion_tokens === 60) {
      return new Response(JSON.stringify({choices:[{message:{content:'{"search":true,"query":"Acme size"}'}}]}));
    }
    const text = r.empty''')
    (tmp_path / "harness.mjs").write_text('import process from "node:process";\n' + harness)
    env = {"LIVE_LLM_URL": "https://live.test", "LIVE_LLM_TOKEN": "test-live", "Linkup_web": "test-linkup",
           "LINKUP_DECIDE_TIMEOUT_MS": "300"}
    question = req("live_assist", messages=[{"role": "user", "content": "Private notes"}])
    question["body"].update(web_search=True, web_query="How big is Acme?")
    latest = json.loads(json.dumps(question))
    latest["body"]["web_query"] = "latest Acme news?"
    scenarios = [
        {"id": "cold", "env": env, "requests": [question, question]},
        {"id": "timeout", "env": env, "requests": [latest], "behave": {"live": [{"stall": True}, {}]}},
        {"id": "backup", "env": {**env, "OPENAI_TOKEN": "test-openai"}, "requests": [latest],
         "behave": {"live": [{"stall": True}, {}]}},
        {"id": "no_route", "env": {"Linkup_web": "test-linkup"}, "requests": [latest]},
    ]
    (tmp_path / "scenarios.json").write_text(json.dumps(scenarios))
    out = subprocess.run([deno, "run", "--allow-all", str(tmp_path / "harness.mjs"),
                          tmp_path.as_uri(), str(tmp_path / "scenarios.json")], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    results = json.loads(out.stdout)
    first, cached = results["cold"]
    for result in (first, cached):
        assert result["status"] == 200
        classifier = [c for c in result["calls"] if c["body"] and c["body"].get("max_tokens") == 60]
        assert len(classifier) == 1
        assert classifier[0]["host"] == "live"
        assert classifier[0]["body"]["model"] == "deepseek-ai/test"
        assert classifier[0]["body"]["temperature"] == 0
        assert classifier[0]["body"]["stream"] is False
        assert next(c for c in result["calls"] if "linkup.so" in c["url"])["body"]["q"] == "Acme size"
    discovery = [c for c in first["calls"] if c["url"].endswith("/models")]
    assert len(discovery) == 1
    classifier = next(c for c in first["calls"] if c["body"] and c["body"].get("max_tokens") == 60)
    assert discovery[0]["signal"] == classifier["signal"]
    assert not any(c["url"].endswith("/models") for c in cached["calls"])
    timeout = results["timeout"][0]
    assert timeout["status"] == 200
    assert len([c for c in timeout["calls"] if c["url"].endswith("/models")]) == 2
    for result in (timeout, results["no_route"][0]):
        assert next(c for c in result["calls"] if "linkup.so" in c["url"])["body"]["q"] == "latest Acme news?"
        assert any('"source":"heuristic"' in line for line in result["logs"])
    assert results["no_route"][0]["status"] == 503
    backup = results["backup"][0]
    assert backup["status"] == 200
    classifier = [c for c in backup["calls"] if c["body"] and
                  (c["body"].get("max_tokens") == 60 or c["body"].get("max_completion_tokens") == 60)]
    assert len(classifier) == 1
    assert classifier[0]["host"] == "openai"
    assert classifier[0]["body"]["model"] == "gpt-5.4-nano"
    assert next(c for c in backup["calls"] if "linkup.so" in c["url"])["body"]["q"] == "Acme size"
    assert any('"source":"model"' in line for line in backup["logs"])


# ── how deep: the person's words decide (2026-11-07) ─────────────────────────────────────────
@pytest.mark.parametrize("text,mode,depth", [
    ("do a deep search on the EU AI act fines", "smart_auto", "deep"),
    ("research the best CRM for a 10 person team", "smart_auto", "deep"),
    ("look up the euro dollar rate in depth", "live_assist", "deep"),
    ("quick search the weather in Paris", "smart_auto", "fast"),
    ("just check online if the store is open", "smart_auto", "fast"),
    ("look up the euro dollar rate", "smart_auto", "standard"),
    ("look up the euro dollar rate", "live_assist", "fast"),
])
def test_the_depth_follows_what_the_person_says(text, mode, depth):
    assert web_sources.search_depth(text, mode) == depth


def test_do_a_search_on_is_a_search_request():
    assert web_sources.smart_search_wanted("do a deep search on the EU AI act fines")
    assert web_sources.smart_search_wanted("please run a quick web search about train strikes")
    assert not web_sources.smart_search_wanted("do the research part of the report")      # dictation, not a command


def test_the_depth_patterns_are_the_servers():
    source = SERVER.read_text(encoding="utf-8")
    assert re.search(r"const DO_SEARCH = /(.+?)/i;", source).group(1) == web_sources.SMART_DO_SEARCH
    assert re.search(r"const DEEP_SEARCH = /(.+?)/i;", source).group(1) == web_sources.DEEP_SEARCH
    assert re.search(r"const QUICK_SEARCH = /(.+?)/i;", source).group(1) == web_sources.QUICK_SEARCH


def test_the_server_gives_each_depth_its_time_pages_and_reading_and_the_answer_its_own_budget():
    source = SERVER.read_text(encoding="utf-8")
    assert "standard: { ms: 6000, results: 6, chars: 1500 }" in source
    assert "deep: { ms: 60000, results: 8, chars: 1500 }" in source
    assert "body: JSON.stringify({ q: query, depth, outputType: \"searchResults\", maxResults: limits.results })" in source
    assert "signal: AbortSignal.timeout(limits.ms)" in source
    sources_at = source.index("const sources = web.results.map(")
    assert source.index("const startedAt = Date.now();", sources_at) > sources_at   # the answer's clock starts after
    # the AI check: GPT-5.4 nano first, no wait for the self-hosted model's discovery, 0.7 s
    assert "const route = OPENAI_KEY ? openaiRoute(OPENAI_LIVE_MODEL" in source
    assert "const knownLiveModel = !OPENAI_KEY && LIVE_URL" in source
    assert 'Deno.env.get("LINKUP_DECIDE_TIMEOUT_MS") || "700"' in source


def test_a_deep_search_waits_up_to_two_minutes_and_the_pill_says_researching():
    import pill_copy
    assert action_api._web_timeout(True, "do a deep search on X", "smart_auto", 45) == 120
    assert action_api._web_timeout(True, "look up X", "smart_auto", 45) == 45
    assert action_api._web_timeout(False, "do a deep search on X", "smart_auto", 45) == 45
    _t, sub = pill_copy._writing(None, "", {"writer": "Gusan Cloud", "web": "deep"}, 0.3)
    assert sub == "Researching the web · Gusan Cloud"
