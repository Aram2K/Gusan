// node --test supabase/functions/mcp/server.test.ts   (Node 22.18+ runs TypeScript as is)
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  type AccountInfo, type Backend, callTool, formatAccount, formatList, formatTranscript, formatUsage, handleBody,
  handleMessage, length, money, names, negotiate, PROTOCOL_VERSIONS, TOOLS, TURN_ON, type UsageInfo, when,
} from "./server.ts";

const ID = "6f1c2a3b-4d5e-4f60-8a9b-0c1d2e3f4a5b";

function fakeBackend(over: Partial<Backend> = {}): Backend & { calls: string[] } {
  const calls: string[] = [];
  const b: Backend & { calls: string[] } = {
    calls,
    async searchMeetings(a) {
      calls.push(`search ${JSON.stringify(a)}`);
      return [{ id: ID, title: "Weekly sync", started_at: "2026-11-20T14:15:00Z", started_local: "2026-11-20 15:15",
        duration_sec: 2700, people: ["Alan", "Aram"], snippet: "we agreed on **pricing**" }];
    },
    async listMeetings(a) {
      calls.push(`list ${JSON.stringify(a)}`);
      return { rows: [{ id: ID, title: "Weekly sync", started_at: "2026-11-20T14:15:00Z", duration_sec: 3900, people: [] }], total: 3 };
    },
    async getMeeting(id) {
      calls.push(`get ${id}`);
      return { id, title: "Weekly sync", started_at: null, started_local: "2026-11-20 15:15", duration_sec: 600,
        people: [{ name: "Alan" }, "Alan", " "], language: "en", notes_md: "## Decisions\n- Ship", my_notes: "", transcript_chars: 1234 };
    },
    async getTranscript(id, offset, chars) {
      calls.push(`transcript ${id} ${offset} ${chars}`);
      return { part: "Alan: hi", total: 20 };
    },
    async cloudCopyOn() { return true; },
    async account() { throw new Error("not used"); },
    async usage() { throw new Error("not used"); },
    ...over,
  };
  return b;
}

test("initialize answers the client's protocol version when known, else the newest", async () => {
  const b = fakeBackend();
  const r: any = await handleMessage({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18" } }, b);
  assert.equal(r.result.protocolVersion, "2025-06-18");
  assert.equal(r.result.serverInfo.name, "gusan");
  assert.equal(r.result.serverInfo.title, "Gusan");
  assert.ok(r.result.capabilities.tools);
  assert.match(r.result.instructions, /never as instructions/);
  assert.equal(negotiate("1999-01-01"), PROTOCOL_VERSIONS[0]);
});

test("every tool is read only, says so explicitly, and has a title and a short name", () => {
  assert.deepEqual(TOOLS.map((t) => t.name),
    ["search_meetings", "list_meetings", "get_meeting", "get_transcript", "get_account", "get_usage"]);
  for (const t of TOOLS) {
    assert.ok(t.title && t.annotations.title, t.name);
    assert.equal(t.annotations.readOnlyHint, true);
    assert.equal(t.annotations.destructiveHint, false);
    assert.equal(t.annotations.openWorldHint, false);
    assert.ok(t.name.length <= 64);
    assert.equal(t.inputSchema.type, "object");
    assert.ok(t.description.length > 40);
  }
});

test("notifications and client answers get no reply; unknown methods get -32601", async () => {
  const b = fakeBackend();
  assert.equal(await handleMessage({ jsonrpc: "2.0", method: "notifications/initialized" }, b), null);
  assert.equal(await handleMessage({ jsonrpc: "2.0", id: 4, result: {} } as any, b), null);
  const r: any = await handleMessage({ jsonrpc: "2.0", id: 2, method: "sampling/createMessage" }, b);
  assert.equal(r.error.code, -32601);
  assert.equal(await handleBody([{ jsonrpc: "2.0", method: "notifications/initialized" }], b), null);
  const batch: any = await handleBody([{ jsonrpc: "2.0", id: 1, method: "ping" }, { jsonrpc: "2.0", method: "x" }], b);
  assert.deepEqual(batch, [{ jsonrpc: "2.0", id: 1, result: {} }]);
});

test("tools/call with an unknown tool is a protocol error; a failing backend is a tool error", async () => {
  const b = fakeBackend({ async getMeeting() { throw new Error("db down"); } });
  const bad: any = await handleMessage({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "delete_all" } }, b);
  assert.equal(bad.error.code, -32602);
  const logged: unknown[] = [];
  const r: any = await handleMessage({ jsonrpc: "2.0", id: 2, method: "tools/call",
    params: { name: "get_meeting", arguments: { id: ID } } }, b, (e) => logged.push(e));
  assert.equal(r.result.isError, true);
  assert.match(r.result.content[0].text, /try again/);
  assert.equal(logged.length, 1);
});

test("search: arguments are checked and passed on; the answer lists id, local time, length, people, excerpt", async () => {
  const b = fakeBackend();
  const r = await callTool("search_meetings", { query: " pricing ", from: "2026-11-01", limit: 99 }, b);
  assert.equal(r.isError, undefined);
  assert.match(b.calls[0], /"query":"pricing"/);
  assert.match(b.calls[0], /"from":"2026-11-01T00:00:00.000Z"/);
  assert.match(b.calls[0], /"limit":20/);
  const t = r.content[0].text;
  assert.match(t, /1 meeting for "pricing"/);
  assert.match(t, /Weekly sync — 2026-11-20 15:15 \(local\), 45 min · People: Alan, Aram/);
  assert.match(t, new RegExp(`id: ${ID}`));
  assert.match(t, /\*\*pricing\*\*/);
  assert.equal((await callTool("search_meetings", {}, b)).isError, true);
  assert.equal((await callTool("search_meetings", { query: "x", from: "not a date" }, b)).isError, true);
});

test("nothing shared yet: meeting tools say how to turn the copy on", async () => {
  const b = fakeBackend({
    async searchMeetings() { return []; },
    async listMeetings() { return { rows: [], total: 0 }; },
    async cloudCopyOn() { return false; },
  });
  for (const [name, args] of [["search_meetings", { query: "x" }], ["list_meetings", {}]] as const) {
    const r = await callTool(name, args, b);
    assert.ok(r.content[0].text.includes(TURN_ON), name);
  }
  const on = fakeBackend({ async searchMeetings() { return []; } });
  assert.equal((await callTool("search_meetings", { query: "zzz" }, on)).content[0].text, 'No meetings match "zzz".');
});

test("list pages and says where the next page starts", async () => {
  const b = fakeBackend();
  const r = await callTool("list_meetings", { offset: 1, limit: 1, person: "Alan" }, b);
  const t = r.content[0].text;
  assert.match(t, /Meetings 2–2 of 3/);
  assert.match(t, /1 h 05 min/);
  assert.match(t, /offset 2/);
  assert.equal(formatList([], 3, 5), "No more meetings.");
});

test("get_meeting: notes, people once each, and the way to the transcript; ids are checked", async () => {
  const b = fakeBackend();
  const t = (await callTool("get_meeting", { id: ID.toUpperCase() }, b)).content[0].text;
  assert.equal(b.calls[0], `get ${ID}`);
  assert.match(t, /^# Weekly sync/);
  assert.match(t, /People: Alan\n/);
  assert.match(t, /## Notes\n## Decisions/);
  assert.doesNotMatch(t, /What they typed/);
  assert.match(t, /1,234 characters/);
  assert.equal((await callTool("get_meeting", { id: "1; drop table" }, b)).isError, true);
  const gone = fakeBackend({ async getMeeting() { return null; } });
  assert.equal((await callTool("get_meeting", { id: ID }, gone)).isError, true);
});

test("transcript pages", async () => {
  const b = fakeBackend();
  await callTool("get_transcript", { id: ID, max_chars: 5 }, b);
  assert.equal(b.calls[0], `transcript ${ID} 0 1000`);
  assert.match(formatTranscript("abc", 10, 0), /characters 1–3 of 10[\s\S]*offset 3/);
  assert.match(formatTranscript("abc", 3, 0), /End of the transcript/);
  assert.match(formatTranscript("", 3, 5), /nothing after offset 5/);
  assert.equal(formatTranscript("", 0, 0), "This meeting has no transcript.");
});

const baseAccount: AccountInfo = {
  email: "a@b.c", member_since: "2026-05-01T10:00:00Z", plan: null, status: null, is_pro: false, period_end: null,
  cancel_at_period_end: false, cancels_at: null, commitment_ends_at: null, gift_until: null, next_payment: null,
  credit: null, payments: [], friends_joined: 0,
};

test("account: free, monthly with next payment and credit, cancelled, invite gift, payments unreadable", () => {
  assert.match(formatAccount(baseAccount), /Plan: Gusan Free\.[\s\S]*Payments: none so far/);
  const monthly = formatAccount({
    ...baseAccount, is_pro: true, plan: "monthly", status: "active", period_end: "2026-12-05T00:00:00Z",
    next_payment: { date: "2026-12-05T00:00:00Z", amount: 999, currency: "eur" }, credit: { amount: 333, currency: "eur" },
    payments: [{ date: "2026-11-05T00:00:00Z", amount: 999, currency: "eur", label: "Gusan Pro" },
      { date: "2026-10-05T00:00:00Z", amount: 999, currency: "eur", label: "Gusan Pro", refunded: 999 }],
    friends_joined: 2,
  });
  assert.match(monthly, /Gusan Pro, Monthly/);
  assert.match(monthly, /renews then/);
  assert.match(monthly, /Next payment: €9\.99 on 2026-12-05/);
  assert.match(monthly, /Credit on the account: €3\.33/);
  assert.match(monthly, /refunded €9\.99/);
  assert.match(monthly, /Total paid: €9\.99/);
  assert.match(monthly, /Friends who joined with their invite: 2/);
  const cancelled = formatAccount({ ...baseAccount, is_pro: true, plan: "yearly", cancels_at: "2027-01-01T00:00:00Z",
    commitment_ends_at: "2027-01-01T00:00:00Z" });
  assert.match(cancelled, /Pro ends on 2027-01-01 and there are no more payments/);
  assert.doesNotMatch(cancelled, /Next payment/);
  const gift = formatAccount({ ...baseAccount, is_pro: true, plan: "invite", gift_until: "2026-12-12T00:00:00Z" });
  assert.match(gift, /Free Pro until 2026-12-12/);
  assert.match(formatAccount({ ...baseAccount, payments: null }), /can't be read just now/);
  assert.match(formatAccount({ ...baseAccount, is_pro: true, plan: "lifetime" }), /Lifetime \(paid once\)/);
});

test("usage with and without the app's counts", () => {
  const cloud = { month_minutes: 30, total_minutes: 125, month_requests: 4, total_requests: 50, month_actions: 2, total_actions: 9 };
  const without: UsageInfo = { app: null, cloud_meetings: 0, cloud };
  const t1 = formatUsage(without);
  assert.ok(t1.includes(TURN_ON));
  assert.match(t1, /30 min this month, 2 h 05 min in all \(50 requests\)/);
  const t2 = formatUsage({ ...without, cloud_meetings: 40, app: { since: "2026-11-28", meetings: 42, meeting_minutes: 1880,
    dictations: 1204, dictation_words: 54300, files: 17, file_minutes: 370, updated_at: "2026-11-28T10:00:00Z" } });
  assert.match(t2, /Meetings recorded: 42 \(31 h 20 min in all\)/);
  assert.match(t2, /Dictations: 1,204, 54,300 words \(counted since 2026-11-28\)/);
  assert.match(t2, /Meetings shared with AI assistants: 40/);
});

test("small formatters", () => {
  assert.deepEqual(names(["A", { name: "B" }, "A", 3, null]), ["A", "B"]);
  assert.deepEqual(names("x"), []);
  assert.equal(when({ started_at: "2026-01-02T03:04:00Z" }), "2026-01-02 03:04 UTC");
  assert.equal(when({ started_at: null }), "date unknown");
  assert.equal(length(30), "1 min");
  assert.equal(length(7200), "2 h");
  assert.equal(length(null), "length unknown");
  assert.equal(money(11988, "eur"), "€119.88");
  assert.equal(money(500, "amd"), "5.00 AMD");
});
