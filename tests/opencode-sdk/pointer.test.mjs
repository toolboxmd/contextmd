import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { test } from "node:test";
import { createOpencodeClient } from "@opencode-ai/sdk/client";

const HEAD = "ContextMD, the user's own rule store, applies this rule from the user here:";
const LINE = "The Great Library holds knowledge shared across projects and personal knowledge outside any project. Read /library/README.md in full.";
const POINTER = `${HEAD}\n\n${LINE}`;
const CUE = `${HEAD}\n\nKeep the project's command output concise.`;
const VERSION = createHash("sha256").update(LINE).digest("hex").slice(0, 12);
const source = readFileSync(process.env.CONTEXTMD_PLUGIN_SOURCE ||
  new URL("../../contextmd/serve/opencode/contextmd.js", import.meta.url), "utf8");
const json = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { "content-type": "application/json" },
});
const message = (id = "msg_first", parts = []) => ({
  info: { id, sessionID: "ses_native", role: "user" }, parts,
});
const owned = (output) => output.messages.flatMap((m) => m.parts)
  .filter((p) => p.metadata?.contextmd?.pointer === "great_library");

async function fixture(t, options = {}) {
  const base = mkdtempSync(join(tmpdir(), "contextmd-pointer-test-"));
  t.after(() => rmSync(base, { recursive: true, force: true }));
  const state = join(base, "userdata", "contextmd");
  mkdirSync(state, { recursive: true });
  writeFileSync(join(base, "userdata", "server-runtime.json"), JSON.stringify({ origin: "http://serving.invalid" }));
  writeFileSync(join(state, "hook-credential"), JSON.stringify({ token: "test-hook-token" }));
  const pluginFile = join(base, "plugin.mjs");
  writeFileSync(pluginFile, source.replace('"__BASE__"', JSON.stringify(base)));
  const calls = [];
  const patches = [];
  const stored = new Map();
  let provide = true;
  const oldFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = oldFetch; });
  globalThis.fetch = async (url, init) => {
    assert.equal(new URL(url).origin, "http://serving.invalid");
    assert.equal(init.headers.authorization, "Bearer test-hook-token");
    const path = new URL(url).pathname;
    const raw = init.body && JSON.parse(init.body);
    calls.push({ path, headers: init.headers, raw });
    if (path.endsWith("/moment")) {
      if (raw.hook === "session.compacted") provide = true;
      const reply = provide ? { context: CUE, pointer: POINTER } : {};
      provide = false;
      return options.moment ? options.moment(raw, reply) : json(reply);
    }
    if (path.includes("/pointer/applying/") && options.applying) return options.applying();
    return new Response(null, { status: 204 });
  };
  const client = createOpencodeClient({
    baseUrl: "http://native.invalid", directory: "/workspace/space and 知識",
    headers: { authorization: "Basic test-opencode-auth" },
    fetch: async (request) => {
      const raw = await request.text();
      const part = raw ? JSON.parse(raw) : null;
      patches.push({ request, raw, part });
      if (options.save) return options.save({ request, part, stored, attempt: patches.length });
      if (!part) return json({ error: "missing JSON text part" }, 400);
      stored.set(part.id, structuredClone(part));
      return json(part);
    },
  });
  const factory = (await import(pathToFileURL(pluginFile).href)).default;
  const plugin = await factory({ directory: "/workspace/space and 知識", client: options.noTransport ? {} : client });
  const transform = (output) => plugin["experimental.chat.messages.transform"]({}, output);
  const start = () => plugin["chat.message"]({ sessionID: "ses_native" });
  const recreate = async () => (await import(`${pathToFileURL(pluginFile).href}?recreated`)).default({
    directory: "/workspace/space and 知識", client,
  });
  return { plugin, transform, start, calls, patches, stored, state, client, recreate };
}

test("the real 1.19.0 root client saves the JSON part with its configured transport before pointer settlement", async (t) => {
  const sdk = JSON.parse(readFileSync(new URL("node_modules/@opencode-ai/sdk/package.json", import.meta.url)));
  assert.equal(sdk.version, "1.19.0");
  const f = await fixture(t);
  assert.equal(f.client.part?.update, undefined);
  await f.start();
  const output = { messages: [message("msg_first", [{ type: "text", text: "Hello" }])] };
  await f.transform(output);
  assert.equal(f.patches.length, 1);
  const patch = f.patches[0];
  assert.notEqual(patch.part, null, "raw root SDK PATCH must carry a JSON text-part body");
  assert.equal(patch.request.method, "PATCH");
  assert.equal(patch.request.url, `http://native.invalid/session/ses_native/message/msg_first/part/${patch.part.id}`);
  assert.equal(patch.request.headers.get("authorization"), "Basic test-opencode-auth");
  assert.equal(patch.request.headers.get("x-opencode-directory"), encodeURIComponent("/workspace/space and 知識"));
  assert.match(patch.request.headers.get("content-type"), /application\/json/);
  assert.equal(patch.part.text, POINTER);
  assert.equal(patch.part.synthetic, true);
  assert.equal(patch.part.metadata.contextmd.version, VERSION);
  assert.equal(f.stored.size, 1);
  assert.equal(owned(output).length, 1);
  const moment = f.calls.find((c) => c.path.endsWith("/moment"));
  const p = moment.headers["x-contextmd-pointer-delivery"];
  const d = moment.headers["x-contextmd-delivery"];
  assert.notEqual(p, d);
  assert.equal(patch.part.metadata.contextmd.delivery, p);
  assert.deepEqual(f.calls.slice(1).map((c) => c.path), [
    `/contextmd/v1/settle/${d}`, `/contextmd/v1/pointer/applying/${p}`, `/contextmd/v1/pointer/settle/${p}`,
  ]);
  const later = { messages: [message("msg_first", [...f.stored.values()]), message("msg_later")] };
  await f.start();
  await f.transform(later);
  assert.equal(f.patches.length, 1, "same epoch uses the stored pointer without another save");
});

test("a first native 4xx releases only the pointer while ordinary context remains", async (t) => {
  const f = await fixture(t, { save: () => json({ name: "InvalidPart", data: { message: "rejected" } }, 400) });
  await f.start();
  const output = { messages: [message()] };
  await f.transform(output);
  assert.equal(owned(output).length, 0);
  assert.equal(output.messages[0].parts[0].text, CUE);
  const moment = f.calls[0];
  const p = moment.headers["x-contextmd-pointer-delivery"];
  const d = moment.headers["x-contextmd-delivery"];
  assert.ok(f.calls.some((c) => c.path === `/contextmd/v1/settle/${d}`));
  assert.ok(f.calls.some((c) => c.path === `/contextmd/v1/pointer/release/${p}`));
  assert.ok(!f.calls.some((c) => c.path === `/contextmd/v1/release/${d}`));
});

test("missing native transport releases only the pointer and preserves the ordinary transform", async (t) => {
  const f = await fixture(t, { noTransport: true });
  await f.start();
  const output = { messages: [message()] };
  await f.transform(output);
  assert.equal(f.patches.length, 0);
  assert.equal(output.messages[0].parts[0].text, CUE);
  assert.ok(f.calls.some((c) => c.path.includes("/pointer/release/")));
  assert.ok(f.calls.some((c) => /\/v1\/settle\//.test(c.path)));
});

test("an ambiguous save stays charged across drop and retries the identical native id", async (t) => {
  const f = await fixture(t, { save: ({ part, stored, attempt }) => {
    assert.ok(part, "configured root transport sends the body");
    if (attempt === 1) {
      stored.set(part.id, structuredClone(part)); // native commit, response lost
      throw new TypeError("connection reset after commit");
    }
    if (attempt === 2) return json({ name: "NotFound" }, 404);
    stored.set(part.id, structuredClone(part));
    return json(part);
  } });
  await f.start();
  for (let i = 0; i < 3; i++) {
    const output = { messages: [message()] };
    if (i) await f.start(); // generic drop must not release a possibly saved pointer
    await f.transform(output);
    assert.equal(owned(output).length, 1);
    if (i < 2) assert.ok(!f.calls.some((c) => c.path.includes("/pointer/settle/")));
  }
  assert.equal(f.patches.length, 3);
  assert.equal(new Set(f.patches.map((p) => p.part.id)).size, 1);
  assert.equal(f.stored.size, 1);
  assert.ok(!f.calls.some((c) => c.path.includes("/pointer/release/")));
  assert.ok(f.calls.some((c) => c.path.includes("/pointer/settle/")));
});

test("a lost applying acknowledgement causes no native save or refund", async (t) => {
  const f = await fixture(t, { applying: () => { throw new TypeError("acknowledgement lost"); } });
  await f.start();
  const output = { messages: [message()] };
  await f.transform(output);
  assert.equal(f.patches.length, 0);
  assert.equal(owned(output).length, 0);
  assert.equal(output.messages[0].parts[0].text, CUE);
  assert.ok(!f.calls.some((c) => c.path.includes("/pointer/release/")));
});

test("native replay with new ids keeps one owned copy and preserves ordinary part order", async (t) => {
  const f = await fixture(t);
  await f.start();
  const output = { messages: [message()] };
  await f.transform(output);
  assert.equal(f.stored.size, 1);
  const part = [...f.stored.values()][0];
  const tail = structuredClone(part);
  const replay = { ...structuredClone(part), id: "prt_replay", messageID: "msg_replay" };
  const quoted = { type: "text", text: POINTER, synthetic: true }; // no ownership metadata
  const native = { messages: [
    message("msg_tail", [{ type: "text", text: "before" }, tail, { type: "text", text: "after" }]),
    message("msg_replay", [quoted, replay, { type: "text", text: "user continuation" }]),
  ] };
  await f.transform(native);
  assert.deepEqual(native.messages[0].parts.map((p) => p.text), ["before", "after"]);
  assert.equal(owned(native).length, 1);
  assert.equal(owned(native)[0].id, "prt_replay");
  assert.equal(native.messages[1].parts[0], quoted);
  assert.ok(f.stored.has(part.id), "request dedupe never edits stored history");
  assert.equal(f.patches.length, 1);
});

test("plugin recreation preserves native parts and does not save another copy", async (t) => {
  const f = await fixture(t);
  await f.start();
  await f.transform({ messages: [message()] });
  const plugin = await f.recreate();
  await plugin["chat.message"]({ sessionID: "ses_native" });
  const output = { messages: [message("msg_first", [...f.stored.values()]), message("msg_next")] };
  await plugin["experimental.chat.messages.transform"]({}, output);
  assert.equal(owned(output).length, 1);
  assert.equal(f.stored.size, 1);
  assert.equal(f.patches.length, 1);
});

test("summary quotes and unmarked user text do not satisfy an owed pointer", async (t) => {
  const f = await fixture(t);
  await f.start();
  const output = { messages: [
    { info: { id: "msg_summary", sessionID: "ses_native", role: "assistant" },
      parts: [{ type: "text", synthetic: true, text: POINTER }] },
    message("msg_first", [{ type: "text", synthetic: true, text: POINTER }]),
  ] };
  await f.transform(output);
  assert.equal(f.stored.size, 1);
  assert.equal(owned(output).length, 1);
  assert.equal(output.messages[0].parts.length, 1);
  assert.equal(output.messages[1].parts[0].text, POINTER);
});

test("the synchronous compaction barrier completes before either continuation path", async (t) => {
  let resolve;
  let arrived;
  const observed = new Promise((r) => { arrived = r; });
  const f = await fixture(t, { moment: (raw, reply) => {
    if (raw.hook !== "session.compacted") return json(reply);
    arrived();
    return new Promise((r) => { resolve = () => r(json({ pointer: POINTER })); });
  } });
  await f.start();
  await f.transform({ messages: [message()] });
  f.plugin.event({ event: { type: "session.compacted", properties: { sessionID: "ses_native" } } });
  await observed;
  const output = { messages: [message("msg_after_compaction")] };
  let continued = false;
  const request = f.transform(output).then(() => { continued = true; });
  await Promise.resolve();
  assert.equal(continued, false);
  assert.equal(f.patches.length, 1);
  resolve();
  await request;
  assert.equal(owned(output).length, 1);
  assert.equal(f.patches.length, 2);
  assert.equal(f.patches[1].part.messageID, "msg_after_compaction");
  await f.start();
  assert.equal(f.patches.length, 2);
});

test("turning serving off before native save releases only the unsaved pointer", async (t) => {
  const f = await fixture(t);
  await f.start();
  writeFileSync(join(f.state, "off"), "");
  const output = { messages: [message()] };
  await f.transform(output);
  assert.equal(output.messages[0].parts.length, 0);
  assert.equal(f.patches.length, 0);
  assert.ok(f.calls.some((c) => c.path.includes("/pointer/release/")));
});
