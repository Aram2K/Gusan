"""Exercise the real Calendar function with isolated Supabase and Google fakes."""
import json
import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = r'''
import {readFileSync} from 'node:fs';
const scenarios=JSON.parse(readFileSync(process.argv[3],'utf8')), results=[];
const print=console.log;
for(let index=0;index<scenarios.length;index++) {
 const s=scenarios[index], logs=[], calls=[], tables={calendar_connections:[],calendar_oauth_states:[]}, responses=[];
 console.log=(...x)=>logs.push(x.join(' '));
 const env={SUPABASE_URL:'https://project.supabase.co',SUPABASE_SERVICE_ROLE_KEY:'service',GOOGLE_CALENDAR_CLIENT_ID:'client',GOOGLE_CALENDAR_CLIENT_SECRET:'secret',CALENDAR_ENCRYPTION_KEY:'ab'.repeat(32),...s.env};
 let handler, authURL, cutoff;
 globalThis.Deno={env:{get:k=>env[k]},serve:h=>handler=h};
 globalThis.__db={auth:{getUser:async()=>({data:{user:s.noUser?null:{id:'u1'}},error:null})},rpc:async(name,args)=>{
  if(name==='disconnect_calendar_google'){cutoff=new Date().toISOString();tables.calendar_connections=[];tables.calendar_oauth_states=[];return {data:null,error:null}}
  if(name==='save_calendar_google_connection'){
   if(cutoff&&args.p_state_created_at<=cutoff)return {data:false,error:null};
   tables.calendar_connections=[{user_id:args.p_user_id,provider:'google',encrypted_refresh_token:args.p_encrypted_refresh_token,granted_scope:args.p_scope}];return {data:true,error:null};
  }
  throw Error('unknown RPC');
 },from:table=>{
  let op='select', payload, filters=[];
  const q={select:()=>q,eq:(k,v)=>{filters.push(r=>r[k]===v);return q},gt:(k,v)=>{filters.push(r=>r[k]>v);return q},
   insert:p=>{op='insert';payload=p;return q},upsert:p=>{op='upsert';payload=p;return q},delete:()=>{op='delete';return q},
   maybeSingle:async()=>{const r=execute();return {...r,data:r.data?.[0]??null}},then:(a,b)=>Promise.resolve(execute()).then(a,b)};
  function execute(){
   if(s.storageError)return {data:null,error:{}};
   let rows=tables[table], selected=rows.filter(r=>filters.every(f=>f(r)));
   if(op==='delete')tables[table]=rows.filter(r=>!selected.includes(r));
   if(op==='insert')rows.push({created_at:new Date().toISOString(),...structuredClone(payload)});
   if(op==='upsert'){tables[table]=rows.filter(r=>r.user_id!==payload.user_id);tables[table].push(structuredClone(payload));}
   return {data:structuredClone(selected),error:null};
  }
  return q;
 }};
 globalThis.fetch=async(url,init={})=>{
  url=String(url); calls.push({url,body:init.body instanceof URLSearchParams?Object.fromEntries(init.body):init.body?JSON.parse(init.body):null});
  if(url.endsWith('/revoke'))return new Response('{}',{status:s.revokeFail?500:200});
  if(url.endsWith('/token')){
   const body=Object.fromEntries(init.body);
   if(body.grant_type==='authorization_code') {
    if(s.disconnectRace){cutoff=new Date().toISOString();tables.calendar_connections=[];tables.calendar_oauth_states=[];}
    const challenge=Buffer.from(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(body.code_verifier))).toString('base64url');
    if(challenge!==new URL(authURL).searchParams.get('code_challenge'))throw Error('PKCE mismatch');
    return Response.json(s.exchangeFail?{error:'bad_code'}:{access_token:'ACCESS_PRIVATE',refresh_token:'REFRESH_PRIVATE',scope:s.scopeMissing?'openid':'https://www.googleapis.com/auth/calendar.readonly'},{status:s.exchangeFail?400:200});
   }
   return Response.json(s.invalidGrant?{error:'invalid_grant'}:{access_token:'ACCESS_PRIVATE'},{status:s.invalidGrant?400:200});
  }
  if(url.endsWith('/freeBusy'))return Response.json({calendars:{primary:{busy:[{start:'2026-11-09T10:00:00Z',end:'2026-11-09T11:00:00Z'}]}}});
  if(url.includes('/events?'))return Response.json({items:[
   {start:{dateTime:'2026-11-09T10:00:00Z'},end:{dateTime:'2026-11-09T11:00:00Z'},summary:'PRIVATE_EVENT'},
   {start:{date:'2026-11-10'},end:{date:'2026-11-11'},summary:'All day'},
   ...['transparent','declined','cancelled'].map(x=>({start:{date:'2026-11-10'},end:{date:'2026-11-11'},summary:x,transparency:x,status:x,attendees:[{self:true,responseStatus:x}]}))
  ]});
  throw Error('unexpected fetch');
 };
 await import('file://'+process.argv[2]+'/proxy.mts?'+index);
 const steps=s.steps??['google_start','callback','busy'];
 for(const step of steps){
  const callback=['callback','replay','denied','expired'].includes(step);
  if(step==='expired'&&tables.calendar_oauth_states[0])tables.calendar_oauth_states[0].expires_at='2000-01-01T00:00:00Z';
  const state=authURL?new URL(authURL).searchParams.get('state'):'invalid';
  const url=callback?'https://project.supabase.co/functions/v1/calendar?state='+state+'&'+(step==='denied'?'error=access_denied':'code=CODE_PRIVATE'):'https://project.supabase.co/functions/v1/calendar';
  const body=typeof step==='object'?step:{action:step,...(step==='busy'?{start:'2026-11-09T00:00:00Z',end:s.longWindow?'2026-12-09T00:00:00Z':'2026-11-17T00:00:00Z',include_titles:!!s.titles}:{})};
  const response=await handler(new Request(url,{method:callback?'GET':'POST',headers:s.noBearer?{}:{Authorization:'Bearer test'},...(callback?{}:{body:JSON.stringify(body)})}));
  let data;try{data=await response.json()}catch{}
  if(data?.url)authURL=data.url;
  responses.push({status:response.status,data,location:response.headers.get('location'),cors:response.headers.get('access-control-allow-origin')});
 }
 results.push({responses,tables,logs,calls});
}
print(JSON.stringify(results));
'''


@pytest.fixture(scope="module")
def run():
    node = shutil.which("node")
    assert node, "Node is required for real-function tests"

    def execute(*scenarios):
        with tempfile.TemporaryDirectory() as directory:
            p = pathlib.Path(directory)
            source = (ROOT / "supabase/functions/calendar/index.ts").read_text()
            source = re.sub(r'import \{ createClient \} from "[^"]+";', "const createClient = (...args: any[]) => globalThis.__db;", source)
            (p / "proxy.mts").write_text(source)
            (p / "harness.mjs").write_text(HARNESS)
            (p / "scenarios.json").write_text(json.dumps(scenarios))
            result = subprocess.run([node, "--experimental-strip-types", str(p / "harness.mjs"), str(p), str(p / "scenarios.json")], capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)
    return execute


def test_start_pkce_exchange_storage_and_single_use(run):
    from urllib.parse import urlparse, parse_qs
    result, = run({"steps": ["google_start", "callback", "replay", "status"]})
    first, connected, replay, status = result["responses"]
    parsed = urlparse(first["data"]["url"])
    query = parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    for name, value in {"client_id": "client", "redirect_uri": "https://project.supabase.co/functions/v1/calendar", "response_type": "code", "scope": "https://www.googleapis.com/auth/calendar.readonly", "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true", "code_challenge_method": "S256"}.items():
        assert query[name] == [value]
    assert len(query["state"][0]) == len(query["code_challenge"][0]) == 43
    assert connected["location"] == "gusan://calendar?google=connected"
    assert replay["location"].endswith("reason=denied")
    assert status["data"] == {"google": {"connected": True}}
    assert "REFRESH_PRIVATE" not in json.dumps(result["tables"])
    assert "CODE_PRIVATE" not in json.dumps(result["tables"])
    assert result["tables"]["calendar_oauth_states"] == []
    assert result["logs"] == []
    assert all(r["cors"] is None for r in result["responses"])


@pytest.mark.parametrize("option,step,reason", [({}, "expired", "denied"), ({}, "denied", "denied"), ({"scopeMissing": True}, "callback", "scope"), ({"exchangeFail": True}, "callback", "exchange")])
def test_callback_rejections(run, option, step, reason):
    result, = run({**option, "steps": ["google_start", step, "status"]})
    assert result["responses"][1]["location"].endswith("reason=" + reason)
    assert result["responses"][-1]["data"] == {"google": {"connected": False}}


def test_busy_private_and_clamped(run):
    result, = run({"longWindow": True})
    busy = result["responses"][-1]["data"]["blocks"]
    assert busy == [{"start": "2026-11-09T10:00:00Z", "end": "2026-11-09T11:00:00Z", "all_day": False}]
    call = next(c for c in result["calls"] if c["url"].endswith("/freeBusy"))
    assert call["body"]["timeMax"] == "2026-11-23T00:00:00.000Z"
    assert not any("/events?" in c["url"] for c in result["calls"])


def test_disconnect_wins_over_callback_already_exchanging(run):
    result, = run({"disconnectRace": True, "steps": ["google_start", "callback", "status"]})
    assert result["responses"][1]["location"].endswith("reason=cancelled")
    assert result["responses"][-1]["data"] == {"google": {"connected": False}}
    assert any(c["url"].endswith("/revoke") for c in result["calls"])
    assert result["tables"]["calendar_connections"] == []


def test_busy_titles_skips_free_declined_cancelled(run):
    result, = run({"titles": True})
    blocks = result["responses"][-1]["data"]["blocks"]
    assert [b["title"] for b in blocks] == ["PRIVATE_EVENT", "All day"]
    assert blocks[1]["all_day"] is True
    call = next(c for c in result["calls"] if "/events?" in c["url"])
    assert "singleEvents=true" in call["url"] and "maxResults=250" in call["url"]
    assert result["logs"] == []


def test_invalid_grant_disconnect_and_empty_status(run):
    invalid, disconnected, empty = run({"invalidGrant": True}, {"revokeFail": True, "steps": ["google_start", "callback", "disconnect", "status"]}, {"steps": ["status"]})
    assert invalid["responses"][-1]["status"] == 409
    assert invalid["responses"][-1]["data"] == {"error": "google_reconnect"}
    assert invalid["tables"]["calendar_connections"] == []
    assert disconnected["responses"][-2]["data"] == {"ok": True}
    assert disconnected["responses"][-1]["data"] == {"google": {"connected": False}}
    assert any(c["url"].endswith("/revoke") for c in disconnected["calls"])
    assert empty["responses"][0]["data"] == {"google": {"connected": False}}


@pytest.mark.parametrize("options,status,code", [({"noBearer": True}, 401, "unauthorized"), ({"noUser": True}, 401, "unauthorized"), ({"env": {"GOOGLE_CALENDAR_CLIENT_ID": ""}}, 503, "google_not_configured"), ({"env": {"CALENDAR_ENCRYPTION_KEY": "bad"}}, 503, "google_not_configured"), ({"steps": [{"action": "x", "padding": "x" * 9000}]}, 413, "body_too_large"), ({"storageError": True}, 500, "storage_error")])
def test_request_errors(run, options, status, code):
    result, = run({"steps": ["google_start"], **options})
    assert result["responses"][0]["status"] == status
    assert result["responses"][0]["data"] == {"error": code}
    assert result["logs"] == []
