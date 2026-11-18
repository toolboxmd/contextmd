// The OpenCode 2.x entry (`setup`) against a stand-in of the 2.3.2 plugin context.
// Hook names, inputs and the synthetic-message call follow what OpenCode 2.3.2 sent
// a probe plugin (toolboxmd/contextmd#88).
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { test } from "node:test";

const POINTER = "ContextMD, the user's own rule store, applies this rule from the user here:\n\nRead /library/README.md in full.";
const CUE = "Keep the project's command output concise.";
const RULE = "Read the deploy runbook before deploying.";
const source = readFileSync(process.env.CONTEXTMD_PLUGIN_SOURCE ||
  new URL("../../contextmd/serve/opencode/contextmd.js", import.meta.url), "utf8");
const json = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { "content-type": "application/json" },
});

async function fixture(t, { reply = () => ({}), synthetic } = {}) {
  const base = mkdtempSync(join(tmpdir(), "contextmd-opencode2-test-"));
  t.after(() => rmSync(base, { recursive: true, force: true }));
  mkdirSync(join(base, "userdata", "contextmd"), { recursive: true });
  writeFileSync(join(base, "userdata", "server-runtime.json"), JSON.stringify({ origin: "http://serving.invalid" }));
  writeFileSync(join(base, "userdata", "contextmd", "hook-credential"), JSON.stringify({ token: "t" }));
  const pluginFile = join(base, "plugin.mjs");
  writeFileSync(pluginFile, source.replace('"__BASE__"', JSON.stringify(base)));
  const calls = [];
  const oldFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = oldFetch; });
  globalThis.fetch = async (url, init) => {
    const path = new URL(url).pathname;
    const raw = init.body && JSON.parse(init.body);
    calls.push({ path, headers: init.headers, raw });
    return path.endsWith("/moment") ? json(reply(raw)) : new Response(null, { status: 204 });
  };
  const hooks = {};
  const saved = [];
  let emit;
  const events = { [Symbol.asyncIterator]: () => ({
    next: () => new Promise((resolve) => { emit = (value) => resolve({ value, done: false }); }),
    return: async () => ({ done: true }),
  }) };
  const ctx = {
    location: { directory: "/workspace" },
    session: {
      hook: async (name, fn) => { hooks[`session.${name}`] = fn; },
      synthetic: async (input) => {
        saved.push(input);
        return synthetic ? synthetic(input) : { id: input.id, sessionID: input.sessionID, type: "synthetic" };
      },
    },
    tool: { hook: async (name, fn) => { hooks[`tool.${name}`] = fn; } },
    permission: { hook: async (name, fn) => { hooks[`permission.${name}`] = fn; } },
    event: { subscribe: (options) => { assert.deepEqual(options.types, ["session.compaction.ended"]); return events; } },
  };
  const plugin = (await import(pathToFileURL(pluginFile).href)).default;
  const cleanup = await plugin.setup(ctx);
  const user = (id, text, metadata) => ({ id, role: "user", content: [{ type: "text", text }], ...(metadata ? { metadata } : {}) });
  const context = async (messages) => { await hooks["session.context"]({ sessionID: "ses_a", messages }); return messages; };
  const prompt = (messageID = "msg_user") => hooks["session.prompt"]({ sessionID: "ses_a", messageID, prompt: { text: "hi" }, delivery: "steer" });
  return { plugin, hooks, calls, saved, context, prompt, user, emit: (v) => emit(v), cleanup };
}

const paths = (calls) => calls.map((c) => c.path.replace(/[0-9a-f]{24}/g, "<id>"));

test("exports one definition both OpenCode loaders accept", async () => {
  const plugin = (await import(`data:text/javascript,${encodeURIComponent(source)}`)).default;
  assert.equal(typeof plugin, "object", "OpenCode 2.x rejects a function default export");
  assert.equal(plugin.id, "contextmd");
  assert.equal(typeof plugin.setup, "function");
  assert.equal(typeof plugin.server, "function");
});

test("a prompt saves the pointer as a marked synthetic message before the model call, once", async (t) => {
  let first = true;
  const f = await fixture(t, { reply: (raw) => raw.hook === "chat.message" && first ? (first = false, { context: CUE, pointer: POINTER }) : {} });
  await f.prompt();
  const moment = f.calls[0];
  assert.equal(moment.raw.hook, "chat.message");
  assert.equal(moment.raw.directory, "/workspace");
  const p = moment.headers["x-contextmd-pointer-delivery"];
  assert.match(p, /^[0-9a-f]{24}$/);
  assert.equal(f.saved.length, 1);
  const save = f.saved[0];
  assert.equal(save.id, `msg_contextmdgl${p}`);
  assert.equal(save.text, POINTER);
  assert.equal(save.resume, false);
  assert.equal(save.delivery, "steer");
  assert.equal(save.metadata.contextmd.delivery, p);
  assert.match(save.metadata.contextmd.version, /^[0-9a-f]{12}$/);
  assert.deepEqual(paths(f.calls), ["/contextmd/v1/moment", "/contextmd/v1/pointer/applying/<id>", "/contextmd/v1/pointer/settle/<id>"]);

  // The stored message replays natively; the served context joins the user's message.
  const native = f.user(save.id, POINTER, save.metadata);
  const messages = await f.context([native, f.user("msg_user", "hi")]);
  assert.deepEqual(messages[1].content.map((c) => c.text), ["hi", CUE]);
  assert.equal(messages.filter((m) => m.content.some((c) => c.text === POINTER)).length, 1);
  assert.ok(f.calls.some((c) => /^\/contextmd\/v1\/settle\//.test(c.path)));

  // Later turns save nothing new; a duplicated replay keeps one copy.
  await f.prompt("msg_later");
  const later = await f.context([native, f.user("msg_user", "hi"), { ...native, id: "msg_copy" }, f.user("msg_later", "again")]);
  assert.equal(f.saved.length, 1);
  assert.equal(later.filter((m) => m.content.some((c) => c.text === POINTER)).length, 1);
  assert.deepEqual(later.at(-1).content.map((c) => c.text), ["again"], "context from the previous turn is dropped");
});

test("an ambiguous save retries the same id; a pointer after compaction waits for the next user message", async (t) => {
  const f = await fixture(t, { reply: (raw) => raw.hook === "chat.message" ? { pointer: POINTER } : {}, synthetic: () => { throw Object.assign(new Error("down"), { status: 503 }); } });
  await f.prompt();
  assert.equal(f.saved.length, 1);
  // An ambiguous save keeps the charge: the request shows the text and retries the same id.
  const messages = await f.context([f.user("msg_user", "hi")]);
  assert.equal(f.saved.length, 2);
  assert.equal(f.saved[1].id, f.saved[0].id);
  assert.deepEqual(messages[0].content.map((c) => c.text), ["hi", POINTER]);

  // After a compaction the new epoch's pointer waits for the next user message.
  const g = await fixture(t, { reply: (raw) => raw.hook === "chat.message" ? {} : { pointer: POINTER } });
  g.emit({ type: "session.compaction.ended", data: { sessionID: "ses_a" } });
  await new Promise((resolve) => setTimeout(resolve, 10));
  const compacted = g.calls.find((c) => c.raw && c.raw.hook === "session.compacted");
  assert.ok(compacted, "compaction sends its moment");
  assert.equal(compacted.headers["x-contextmd-pointer-delivery"], undefined);
  await g.context([g.user("msg_user", "hi")]);
  assert.equal(g.saved.length, 0);
});

test("a deny stops the call at its permission check and shows the rule at the next model call", async (t) => {
  const f = await fixture(t, { reply: (raw) => raw.hook === "tool.execute.before" && raw.output.args.command === "deploy" ? { deny: RULE } : {} });
  await f.prompt();
  await f.hooks["tool.execute.before"]({ tool: "shell", sessionID: "ses_a", agent: "build", messageID: "msg_a", id: "call_1", input: { command: "deploy" } });
  const pre = f.calls.find((c) => c.raw && c.raw.hook === "tool.execute.before");
  assert.deepEqual(pre.raw.input, { tool: "bash", sessionID: "ses_a", callID: "call_1" });
  assert.deepEqual(pre.raw.output, { args: { command: "deploy" } });

  const other = { sessionID: "ses_a", action: "shell", resources: ["ls"], source: { type: "tool", messageID: "msg_a", id: "call_0" }, effect: "allow" };
  await f.hooks["permission.evaluate"](other);
  assert.equal(other.effect, "allow");
  const check = { sessionID: "ses_a", action: "shell", resources: ["deploy"], source: { type: "tool", messageID: "msg_a", id: "call_1" }, effect: "allow" };
  await f.hooks["permission.evaluate"](check);
  assert.equal(check.effect, "deny");
  assert.match(check.message, /stopped this call/);
  assert.ok(!check.message.includes(RULE), "the rule goes to the user message, not the tool error");

  const messages = await f.context([f.user("msg_user", "deploy it")]);
  assert.deepEqual(messages[0].content.map((c) => c.text), ["deploy it", RULE]);

  // The retry is not denied again by the plugin.
  const retry = { ...check, effect: "allow", source: { ...check.source, id: "call_2" } };
  await f.hooks["tool.execute.before"]({ tool: "shell", sessionID: "ses_a", agent: "build", messageID: "msg_a", id: "call_2", input: { command: "ls" } });
  await f.hooks["permission.evaluate"](retry);
  assert.equal(retry.effect, "allow");
});

test("a new user message before the permission check releases the rule and lets the call run", async (t) => {
  const f = await fixture(t, { reply: (raw) => raw.hook === "tool.execute.before" ? { deny: RULE } : {} });
  await f.prompt();
  await f.hooks["tool.execute.before"]({ tool: "shell", sessionID: "ses_a", agent: "build", messageID: "msg_a", id: "call_1", input: { command: "deploy" } });
  await f.prompt("msg_new");
  assert.ok(f.calls.some((c) => /^\/contextmd\/v1\/release\//.test(c.path)), "the unseen rule is released");
  const check = { sessionID: "ses_a", action: "shell", resources: ["deploy"], source: { type: "tool", messageID: "msg_a", id: "call_1" }, effect: "allow" };
  await f.hooks["permission.evaluate"](check);
  assert.equal(check.effect, "allow");
  const messages = await f.context([f.user("msg_new", "new prompt")]);
  assert.deepEqual(messages[0].content.map((c) => c.text), ["new prompt"]);
});

test("a tool result is forwarded in the 1.x shape, cut reads stay partial", async (t) => {
  const f = await fixture(t);
  await f.prompt();
  const after = (input, result) => f.hooks["tool.execute.after"]({ sessionID: "ses_a", agent: "build", messageID: "msg_a", id: "call_r", status: "completed", ...input, result });
  await after({ tool: "read", input: { path: "notes.md", offset: 3 } },
    { output: { type: "text", content: "x" }, content: [{ type: "text", text: "Read file notes.md\n1: x" }], metadata: { truncated: false } });
  await after({ tool: "shell", input: { command: "make" } },
    { output: { exitCode: 2, output: "boom" }, content: [{ type: "text", text: "boom" }], metadata: { status: "completed", truncated: true, exit: 2 } });
  await f.hooks["tool.execute.after"]({ tool: "shell", sessionID: "ses_a", id: "call_e", input: { command: "x" }, status: "error", error: { _tag: "Tool.Error" } });
  const sent = f.calls.filter((c) => c.raw && c.raw.hook === "tool.execute.after").map((c) => c.raw);
  assert.equal(sent.length, 2, "only completed calls are tool results");
  assert.deepEqual(sent[0].input, { tool: "read", sessionID: "ses_a", callID: "call_r", args: { filePath: "notes.md", offset: 3 } });
  assert.deepEqual(sent[0].output, { metadata: { truncated: false }, output: "Read file notes.md\n1: x" });
  assert.equal(sent[1].input.tool, "bash");
  assert.deepEqual(sent[1].output.metadata, { exit: 2, truncated: true });
});
