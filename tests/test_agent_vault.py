"""Run the vault edge function with isolated Supabase and H fakes."""
import json
import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "supabase/functions/agent-vault/index.ts"
ID = "11111111-1111-1111-1111-111111111111"
LOGIN = "22222222-2222-2222-2222-222222222222"
NONCE = "33333333-3333-3333-3333-333333333333"
VALUES = {"username": "private-user-987", "password": "private-password-654"}

HARNESS = r'''
import {readFileSync} from 'node:fs';
const scenarios=JSON.parse(readFileSync(process.argv[3],'utf8')), results=[], log=console.log;
for(let i=0;i<scenarios.length;i++){
 const s=scenarios[i], calls=[], logs=[], writes=[], queries=[];
 console.log=(...args)=>logs.push(args.join(' '));
 const id='11111111-1111-1111-1111-111111111111', login='22222222-2222-2222-2222-222222222222', nonce='33333333-3333-3333-3333-333333333333';
 const row={id,user_id:'u1',title:'Sign in',status:'awaiting_login',login_request:{hostname:'example.com'},...s.row};
 const run={task_id:id,h_session_id:'session1',h_vault_id:'vault1',release_nonce:nonce,release_nonce_expires_at:new Date(Date.now()+900000).toISOString(),release_count:0,tool_req:{call:{id:'call1',tool_name:'request_login',args:{hostname:'example.com'}},hostname:'example.com',login_id:login,page_url:'https://example.com/login'},...s.run};
 const tables={agent_tasks:s.tasks??[row],agent_task_runs:[run],vault_logins:s.logins??[{id:login,user_id:'u1',hostname:'example.com',username_hint:null,has_totp:false,...s.login}],vault_events:[]};
 globalThis.__db={auth:{getUser:async()=>({data:{user:s.noUser?null:{id:'u1'}},error:null})},rpc:async()=>({data:s.pro??true,error:s.proError?{}:null}),from:table=>{
  let mode='select',patch=null, filters=[],single=false,limit=null,columns='*';
  const q={select:c=>{columns=c??'*';return q},eq:(k,v)=>{filters.push(r=>r[k]===v);return q},lt:(k,v)=>{filters.push(r=>r[k]<v);return q},gt:(k,v)=>{filters.push(r=>r[k]>v);return q},
   update:p=>{mode='update';patch=p;return q},insert:p=>{mode='insert';patch=p;return q},upsert:p=>{mode='upsert';patch=p;return q},delete:()=>{mode='delete';return q},order:()=>q,limit:n=>{limit=n;return q},
   maybeSingle:()=>{single=true;return execute()},single:()=>{single=true;return execute()},then:(resolve,reject)=>execute().then(resolve,reject)};
  async function execute(){
   queries.push({table,mode,columns});
   let rows=tables[table].filter(r=>filters.every(f=>f(r)));
   if(mode!=='select')writes.push({table,mode,patch:structuredClone(patch)});
   if(mode==='update')rows.forEach(r=>Object.assign(r,patch));
   if(mode==='delete')tables[table]=tables[table].filter(r=>!rows.includes(r));
   if(mode==='insert'){rows=[structuredClone(patch)];tables[table].push(...rows)}
   if(mode==='upsert'){
    let old=tables[table].find(r=>r.user_id===patch.user_id&&r.hostname===patch.hostname);
    if(old)Object.assign(old,patch);else{old={id:login,created_at:'2026-11-08T00:00:00Z',last_used_at:null,...patch};tables[table].push(old)}rows=[old];
   }
   if(limit!==null)rows=rows.slice(0,limit);
   const project=r=>columns==='*'?r:Object.fromEntries(columns.split(',').map(k=>[k,r[k]]));
   return {data:structuredClone(single?(rows[0]?project(rows[0]):null):rows.map(project)),error:null};
  }return q;
 }};
 const environment={SUPABASE_URL:'https://supa.test',SUPABASE_ANON_KEY:'anon',SUPABASE_SERVICE_ROLE_KEY:'service',H_company_computer_agents:'test-key',AGENT_VAULT_SWEEP_SECRET:'sweep-test',...s.env};
 globalThis.Deno={env:{get:k=>environment[k]},serve:fn=>globalThis.handler=fn};
 globalThis.fetch=async(url,init)=>{
  calls.push({url,method:init.method,body:init.body?JSON.parse(init.body):null});
  if(s.hFailure&&url.includes(s.hFailure))return new Response('untrusted',{status:500});
  if(url.endsWith('/pause'))return new Response(null,{status:s.pauseStatus??202});
  if(init.method==='GET'){
   const offset=Number(new URL(url).searchParams.get('offset'));
   return new Response(JSON.stringify(s.pages?.[offset/100]??{total:(s.vaults??[]).length,vaults:s.vaults??[]}));
  }
  return new Response(null,{status:init.method==='DELETE'?(s.deleteStatus??204):202});
 };
 await import(`${process.argv[2]}/proxy.mts?${i}`);
 const replies=[];
 for(const r of s.requests??[{body:s.body??{action:'release',task_id:id,login_id:login,release_nonce:nonce,values:{username:'private-user-987',password:'private-password-654'}}}]){
  const raw=r.raw??JSON.stringify(r.body);
  const headers={'Content-Type':'application/json',Authorization:s.auth??'Bearer aaa.bbb.ccc',...s.headers,...r.headers};
  const respond=async()=>{
   const response=await handler(new Request('https://edge.test/',{method:r.method??'POST',headers,body:raw}));
   return {status:response.status,body:await response.json()};
  };
  replies.push(...await Promise.all(Array.from({length:r.concurrent??1},respond)));
 }
 results.push({replies,calls,logs,writes,tables,queries});
}
console.log=log;log(JSON.stringify(results));
'''


@pytest.fixture(scope="module")
def run():
    node = shutil.which("node")
    if not node or tuple(map(int, subprocess.check_output([node, "--version"], text=True).strip().lstrip("v").split(".")[:2])) < (22, 6):
        pytest.skip("Node >=22.6 required")

    def execute(*scenarios):
        with tempfile.TemporaryDirectory() as directory:
            p = pathlib.Path(directory)
            source = re.sub(r'import \{ createClient \} from "[^"]+";',
                            'const createClient = (...args: any[]) => globalThis.__db;', SOURCE.read_text())
            (p / "proxy.mts").write_text(source)
            (p / "harness.mjs").write_text(HARNESS)
            (p / "scenarios.json").write_text(json.dumps(scenarios))
            result = subprocess.run([node, "--experimental-strip-types", str(p / "harness.mjs"), str(p), str(p / "scenarios.json")], capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)
    return execute


def release(**overrides):
    return {"action": "release", "task_id": ID, "login_id": LOGIN, "release_nonce": NONCE, "values": VALUES, **overrides}


@pytest.mark.parametrize("scenario,status,error", [
    ({"auth": ""}, 401, "unauthorized"), ({"auth": "Bearer bad"}, 401, "unauthorized"),
    ({"noUser": True}, 401, "unauthorized"), ({"pro": False}, 403, "pro_required"),
    ({"proError": True}, 500, "entitlement_check_failed"),
    ({"headers": {"X-Gusan-Privacy-Mode": "true"}}, 403, "privacy_mode"),
    ({"headers": {"Content-Type": "text/plain"}}, 415, "unsupported_media_type"),
    ({"requests": [{"raw": "x" * 65537}]}, 413, "body_too_large"),
    ({"headers": {"Content-Length": "65537"}}, 413, "body_too_large"),
    ({"requests": [{"raw": "[]"}]}, 400, "bad_request"),
    ({"requests": [{"raw": "{"}]}, 400, "bad_request"),
])
def test_refusals(run, scenario, status, error):
    out, = run(scenario)
    assert out["replies"] == [{"status": status, "body": {"error": error}}]
    assert not out["calls"] and not out["writes"]


@pytest.mark.parametrize("host,expected", [("HTTPS://EXAMPLE.COM/login?token=x#hash", "example.com"), ("Sub.Example.com", "sub.example.com"), ("example.com.", "example.com")])
def test_add_normalizes(run, host, expected):
    out, = run({"body": {"action": "add", "hostname": host, "username_hint": "a…", "has_totp": True}})
    assert out["replies"][0]["body"]["hostname"] == expected
    assert out["tables"]["vault_events"][0]["event"] == "added"


@pytest.mark.parametrize("host", ["127.0.0.1", "http://127.1", "https://[::1]", "example.com:443", "https://example.com:443/path", "localhost", "bad_host.com", "ftp://example.com", "https://user:pass@example.com", "-bad.com", "", None])
def test_add_rejects(run, host):
    out, = run({"body": {"action": "add", "hostname": host}})
    assert out["replies"][0]["status"] == 400
    assert not out["writes"]


def test_add_limit_and_upsert(run):
    rows = [{"id": str(i), "user_id": "u1", "hostname": f"site{i}.com"} for i in range(50)]
    blocked, updated = run({"logins": rows, "body": {"action": "add", "hostname": "new.com"}},
                           {"logins": rows, "body": {"action": "add", "hostname": "site0.com"}})
    assert blocked["replies"][0]["status"] == 409
    assert updated["replies"][0]["status"] == 200
    assert len(updated["tables"]["vault_logins"]) == 50


def test_release_only_h_receives_values(run):
    out, = run({})
    assert out["replies"][0]["body"]["status"] == "running"
    item, result = out["calls"]
    assert item["url"].endswith("/vaults/vault1/items")
    assert item["body"]["values"] == VALUES
    assert item["body"]["urls"] == ["https://example.com"]
    assert result["body"]["kind"] == "tool_result" and result["body"]["tool_req"]["id"] == "call1"
    assert "fill_secret_at" in result["body"]["result"]
    assert out["tables"]["agent_task_runs"][0]["release_count"] == 1
    assert out["tables"]["agent_task_runs"][0]["release_nonce"] is None
    assert out["tables"]["vault_events"][0]["event"] == "released"
    for secret in VALUES.values():
        assert secret not in json.dumps([out["logs"], out["replies"], out["writes"], out["tables"]])


@pytest.mark.parametrize("scenario,error", [
    ({"body": release(release_nonce="44444444-4444-4444-4444-444444444444")}, "invalid_nonce"),
    ({"run": {"release_nonce_expires_at": "2000-01-01T00:00:00Z"}}, "invalid_nonce"),
    ({"run": {"release_nonce": None}}, "invalid_nonce"),
    ({"run": {"release_count": 3}}, "invalid_nonce"),
    ({"row": {"status": "running"}}, "not_awaiting_login"),
    ({"login": {"hostname": "other.com"}}, "login_mismatch"),
    ({"login": {"user_id": "other"}}, "login_mismatch"),
    ({"row": {"user_id": "other"}}, "not_found"),
    ({"body": release(values={"unknown": "private"})}, "invalid_fields"),
    ({"body": release(values={})}, "invalid_fields"),
    ({"body": release(values={"password": ""})}, "invalid_fields"),
    ({"body": release(values={"password": "x" * 4097})}, "invalid_fields"),
    ({"body": release(values={"password": 42})}, "invalid_fields"),
])
def test_release_guards(run, scenario, error):
    out, = run(scenario)
    assert out["replies"][0]["body"]["error"] == error
    assert not out["calls"] and not out["writes"]


def test_nonce_reuse(run):
    out, = run({"requests": [{"body": release()}, {"body": release()}]})
    assert [r["status"] for r in out["replies"]] == [200, 409]
    assert len(out["calls"]) == 2


def test_concurrent_release_claims_once(run):
    out, = run({"requests": [{"body": release(), "concurrent": 2}]})
    assert sorted(r["status"] for r in out["replies"]) == [200, 409]
    assert len(out["calls"]) == 2
    assert out["tables"]["agent_task_runs"][0]["release_count"] == 1


@pytest.mark.parametrize("patch", [{"username_hint": "x" * 65}, {"username_hint": 42}, {"has_totp": "true"}])
def test_add_metadata_validation(run, patch):
    out, = run({"body": {"action": "add", "hostname": "example.com", **patch}})
    assert out["replies"][0]["status"] == 400
    assert not out["writes"]


def test_delete_404_and_foreign_pending(run):
    missing, foreign = run({"body": {"action": "delete", "login_id": ID}},
                           {"row": {"user_id": "other"}, "body": {"action": "pending"}})
    assert missing["replies"][0]["status"] == 404
    assert foreign["replies"][0]["body"] == {"pending": []}


@pytest.mark.parametrize("status,event", [(404, "vault_deleted"), (500, "vault_delete_failed")])
def test_sweep_delete_outcomes(run, status, event):
    out, = run({"body": {"action": "sweep"}, "headers": {"X-Gusan-Sweep-Secret": "sweep-test"},
                "row": {"status": "failed"}, "deleteStatus": status,
                "vaults": [{"id": "vault1", "name": f"gusan-task-{ID}"}]})
    assert out["replies"][0]["status"] == 200
    assert out["tables"]["vault_events"][0]["event"] == event


@pytest.mark.parametrize("path", ["/items", "/tool_results"])
def test_h_failure_cleans_vault_without_values(run, path):
    out, = run({"hFailure": path})
    assert out["replies"][0]["body"]["error_code"] == "vault_error"
    assert out["tables"]["agent_task_runs"][0]["h_vault_id"] is None
    assert out["calls"][-1]["method"] == "DELETE"
    assert out["tables"]["vault_events"][-1]["event"] == "vault_deleted"
    for value in VALUES.values():
        assert value not in json.dumps([out["logs"], out["writes"], out["replies"]])


def test_deny(run):
    out, = run({"body": {"action": "deny", "task_id": ID, "release_nonce": NONCE}})
    assert out["replies"][0]["body"]["status"] == "running"
    assert len(out["calls"]) == 1
    assert out["calls"][0]["body"]["kind"] == "error_event"
    assert out["calls"][0]["body"]["origin"] == "custom_tools"
    assert out["tables"]["vault_events"][0]["event"] == "denied"
    assert out["tables"]["agent_task_runs"][0]["release_nonce"] is None


def test_list_pending_delete(run):
    listed, pending, deleted, empty = run({"body": {"action": "list"}}, {"body": {"action": "pending"}},
                                         {"body": {"action": "delete", "login_id": LOGIN}}, {"logins": [], "body": {"action": "list"}})
    assert listed["replies"][0]["body"]["logins"][0]["login_id"] == LOGIN
    assert pending["replies"][0]["body"]["pending"][0]["release_nonce"] == NONCE
    assert deleted["replies"][0]["body"] == {"deleted": True}
    assert deleted["tables"]["vault_events"][0]["event"] == "deleted"
    assert empty["replies"][0]["body"] == {"logins": [], "events": []}


@pytest.mark.parametrize("pause_status", [202, 404, 409])
def test_sweep_retries_pending_session_even_without_vault(run, pause_status):
    out, = run({"body": {"action": "sweep"}, "headers": {"X-Gusan-Sweep-Secret": "sweep-test"},
                "row": {"status": "cancelled"}, "run": {"session_cleanup_pending": True, "h_vault_id": None},
                "pauseStatus": pause_status})
    assert [(c["method"], c["url"].split("/api/v2")[-1]) for c in out["calls"][:2]] == [
        ("POST", "/sessions/session1/pause"), ("DELETE", "/sessions/session1")]
    assert out["tables"]["agent_task_runs"][0]["session_cleanup_pending"] is False


@pytest.mark.parametrize("failure", ["pause", "delete"])
def test_sweep_failed_session_cleanup_retains_marker(run, failure):
    out, = run({"body": {"action": "sweep"}, "headers": {"X-Gusan-Sweep-Secret": "sweep-test"},
                "row": {"status": "cancelled"}, "run": {"session_cleanup_pending": True},
                **({"pauseStatus": 500} if failure == "pause" else {"deleteStatus": 500})})
    assert out["replies"][0]["status"] == 200
    assert out["tables"]["agent_task_runs"][0]["session_cleanup_pending"] is True
    assert out["tables"]["agent_task_runs"][0]["h_session_id"] == "session1"
    assert "agent-vault session_cleanup_failed" in out["logs"]


def test_sweep_does_not_stop_active_or_unmarked_sessions(run):
    active, unmarked = run(
        {"body": {"action": "sweep"}, "headers": {"X-Gusan-Sweep-Secret": "sweep-test"},
         "run": {"session_cleanup_pending": True}},
        {"body": {"action": "sweep"}, "headers": {"X-Gusan-Sweep-Secret": "sweep-test"},
         "row": {"status": "cancelled"}})
    for out in [active, unmarked]:
        assert not any("/sessions/" in c["url"] for c in out["calls"])


def test_sweep_auth_and_scope(run):
    vaults = [{"id": "terminal", "name": f"gusan-task-{ID}", "created_at": "2099-01-01T00:00:00Z"},
              {"id": "foreign", "name": "customer-vault", "created_at": "2000-01-01T00:00:00Z"},
              {"id": "invalid", "name": "gusan-task-not-a-uuid", "created_at": "2000-01-01T00:00:00Z"}]
    no_secret, wrong_secret, deleted, active = run(
        {"body": {"action": "sweep"}, "env": {"AGENT_VAULT_SWEEP_SECRET": ""}},
        {"body": {"action": "sweep"}},
        {"body": {"action": "sweep"}, "headers": {"X-Gusan-Sweep-Secret": "sweep-test"}, "row": {"status": "completed"}, "vaults": vaults},
        {"body": {"action": "sweep"}, "headers": {"X-Gusan-Sweep-Secret": "sweep-test"}, "vaults": vaults})
    assert no_secret["replies"][0]["status"] == 404 and wrong_secret["replies"][0]["status"] == 401
    assert not no_secret["calls"] and not wrong_secret["calls"]
    assert deleted["replies"][0]["body"] == {"scanned": 3, "deleted": 1, "failed": 0}
    assert deleted["calls"][-1]["url"].endswith("/vaults/terminal")
    assert len(active["calls"]) == 1


def test_sweep_pagination_missing_and_old(run):
    out, = run({"body": {"action": "sweep"}, "headers": {"X-Gusan-Sweep-Secret": "sweep-test"},
                "row": {"deadline_at": "2000-01-01T00:00:00Z"},
                "pages": [{"total": 101, "vaults": [{"id": "old", "name": f"gusan-task-{ID}", "created_at": "2000-01-01T00:00:00Z"}] * 100},
                          {"total": 101, "vaults": [{"id": "missing", "name": f"gusan-task-{LOGIN}", "created_at": "2099-01-01T00:00:00Z"}]}]})
    assert out["replies"][0]["body"]["deleted"] == 101
    assert "offset=100" in out["calls"][1]["url"]


def test_sweep_keeps_old_vault_of_task_within_deadline(run):
    out, = run({"body": {"action": "sweep"}, "headers": {"X-Gusan-Sweep-Secret": "sweep-test"},
                "row": {"status": "running", "deadline_at": "2099-01-01T00:00:00Z"},
                "pages": [{"total": 1, "vaults": [{"id": "old", "name": f"gusan-task-{ID}", "created_at": "2000-01-01T00:00:00Z"}]}]})
    assert out["replies"][0]["body"]["deleted"] == 0
    assert not any(c["method"] == "DELETE" for c in out["calls"])


def test_add_without_optional_fields_keeps_metadata(run):
    rows = [{"id": LOGIN, "user_id": "u1", "hostname": "example.com", "username_hint": "a•••@x.com", "has_totp": True}]
    out, = run({"logins": rows, "body": {"action": "add", "hostname": "example.com"}})
    body = out["replies"][0]["body"]
    assert body["username_hint"] == "a•••@x.com" and body["has_totp"] is True


def test_static_security_contract():
    source = SOURCE.read_text()
    assert not re.search(r"console\.\w+\s*\([^;]*(?:\bbody\b|\bvalues\b|\braw\b)", source, re.I)
    sql = (ROOT / "supabase/migrations/202611080001_agent_vault.sql").read_text().lower()
    login_columns = sql.split("create table public.vault_logins (")[1].split(");")[0]
    assert not re.search(r"\b(?:username|password|values|email|api_key)\b", login_columns)
    assert "revoke all on public.vault_events from public, anon, authenticated, service_role" in sql
    assert "grant insert on public.vault_events to service_role" in sql
    assert not re.search(r"grant (?:all|update|delete).*vault_events", sql)
    assert "'awaiting_login'" in sql and "add column release_nonce uuid" in sql
