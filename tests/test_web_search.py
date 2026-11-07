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
        kwargs = dict(web_search=enabled, config={"privacy_mode": privacy}, info=info)
        if stream:
            result = action_api.run_managed_action_stream("latest news?", mode, "token", lambda _: None, **kwargs)
        else:
            result = action_api.run_managed_action("latest news?", mode, "token", **kwargs)
        assert result == "answer"
        body = post.call_args.kwargs["json"]
        assert (body.get("web_search") is True) == expected
        if not expected:
            assert "web_search" not in body
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


@pytest.mark.parametrize("mode", ["live_rule", "live_people", "live_recap", "live_assist", "meeting_chat"])
def test_actions_only_questions_opt_in(mode):
    model = next(k for k, v in actions.ACTION_MODELS.items() if v.get("kind") == "managed")
    with patch.object(action_api, "run_managed_action_stream", return_value="answer") as run:
        actions.process_stream("question", mode, lambda _: None, model=model,
                               config={"_managed_token": "token", "_web_search": True})
    assert run.call_args.kwargs["web_search"] == (mode in ("live_assist", "meeting_chat"))


def test_server_contract():
    src = SERVER.read_text()
    for name in ("LINKUP_API_KEY", "LINKUP_KEY", "LINKUP_TOKEN", "LINKUP_TIMEOUT_MS", "LINKUP_DEPTH"):
        assert name in src
    assert 'body.web_search === true' in src
    assert 'AbortSignal.timeout(LINKUP_TIMEOUT_MS)' in src
    assert 'catch { console.log("Linkup unavailable - answering without web search")' in src
    assert '"Access-Control-Expose-Headers": "X-Web-Sources, X-Answer-Family"' in src
    assert 'encodeURIComponent(JSON.stringify(sources))' in src
    assert 'JSON.stringify({ text, sources })' in src


def test_heuristic_runtime(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable; Deno unavailable in this environment")
    src = SERVER.read_text()
    fn = re.search(r'export function webSearchQuery\(question: string\): string \{.*?\n\}', src, re.S).group(0)
    cases = {"": False, "hi": False, "Summarize what we agreed": False,
             "What is Acme?": True, "Who is the CEO of Acme?": True,
             "What are the latest releases?": True, "Check example.com please": True,
             "What happened in 2024?": True, "look up the weather today": True}
    script = tmp_path / "heuristic.mts"
    script.write_text(fn + "\nconsole.log(JSON.stringify(" + json.dumps(list(cases)) + ".map(webSearchQuery)));")
    out = subprocess.run([node, "--experimental-strip-types", str(script)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert [bool(v) for v in json.loads(out.stdout)] == list(cases.values())


@pytest.mark.parametrize("stream", [False, True])
def test_server_linkup_runtime(tmp_path, stream):
    from tests.test_smart_action_routes import HARNESS, STUB_SUPABASE, STUB_ANTHROPIC, req
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    src = SERVER.read_text()
    src = re.sub(r'from "https://esm\.sh/@supabase/supabase-js@2"', 'from "./stub_supabase.mjs"', src)
    src = re.sub(r'from "npm:@anthropic-ai/sdk@[\d.]+"', 'from "./stub_anthropic.mjs"', src)
    (tmp_path / "proxy.mts").write_text(src)
    (tmp_path / "stub_supabase.mjs").write_text(STUB_SUPABASE)
    (tmp_path / "stub_anthropic.mjs").write_text(STUB_ANTHROPIC)
    harness = HARNESS.replace('const text = r.empty', '''if (host === "other") return new Response(JSON.stringify({results: [
      {type:"image", name:"no", url:"https://example.com/img"},
      {type:"text", name:"unsafe", url:"javascript:alert(1)", content:"no"},
      {type:"text", name:"Example", url:"https://example.com/news", content:"facts"}]}));
    const text = r.empty''').replace('family: res.headers.get("X-Answer-Family"),',
        'family: res.headers.get("X-Answer-Family"), sources: res.headers.get("X-Web-Sources"),')
    (tmp_path / "harness.mjs").write_text(harness)
    msg = [{"role": "user", "content": [{"type": "text", "text": "Private meeting notes\nUser's question: latest Acme news?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]}]
    requests = [req("live_assist", stream=stream, messages=msg, web_search=True),
                req("live_assist", stream=stream, messages=msg),
                req("live_assist", stream=stream, messages=msg, web_search=True)]
    requests[2]["behave"] = {"other": [{"status": 500}]}
    scenarios = [{"id": "web", "env": {"OPENAI_TOKEN": "test-openai", "LINKUP_API_KEY": " ", "LINKUP_KEY": " test-linkup "}, "requests": requests}]
    (tmp_path / "scenarios.json").write_text(json.dumps(scenarios))
    out = subprocess.run([node, "--no-warnings", "--experimental-strip-types", str(tmp_path / "harness.mjs"),
                          tmp_path.as_uri(), str(tmp_path / "scenarios.json")], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    good, old, failed = json.loads(out.stdout)["web"]
    assert good["status"] == old["status"] == failed["status"] == 200
    calls = good["calls"]
    search = next(c for c in calls if "linkup.so" in c["url"])
    assert search["auth"] == "Bearer test-linkup"
    assert search["body"] == {"q": "latest Acme news?", "depth": "flash", "outputType": "searchResults", "maxResults": 5}
    sources = [{"title": "Example", "url": "https://example.com/news"}]
    assert action_api.parse_web_sources(good["sources"]) == sources
    upstream = next(c for c in calls if c["host"] == "openai")
    assert upstream["body"]["messages"][-1] == msg[0]
    assert "Web search results (Linkup)" in upstream["body"]["messages"][-2]["content"]
    assert not any("linkup.so" in c["url"] for c in old["calls"])
    assert action_api.parse_web_sources(failed["sources"]) == []
    assert "test-linkup" not in "".join(good["logs"])
    assert "latest Acme news" not in "".join(good["logs"])
    if not stream:
        assert good["json"]["sources"] == sources
