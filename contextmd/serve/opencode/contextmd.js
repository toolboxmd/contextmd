/**
 * ContextMD serving for OpenCode: forwards each moment to the serving module of
 * the Chromeria server that owns BASE and applies its reply. Written by
 * `contextmd-serve install opencode --base <dir>`, which replaces __BASE__. It
 * authenticates like the command hooks: the server URL from Chromeria's
 * server-runtime.json and the Chromeria session stored in hook-credential.
 *
 * Served text reaches the model as part of the user's message, not as tool
 * output: OpenCode models doubted rules that arrived as tool output
 * (obedience test, 2026-10-12), and the canary (#5) proved a text part added in
 * `experimental.chat.messages.transform` is read before the next action. A deny
 * still throws in tool.execute.before to stop the call, with a short reason
 * that points at that part. The part is added again on every model call until
 * the next user message, because a transform is not saved. The plugin settles
 * a reply when it first adds its part, just before the model reads it, and
 * releases a reply it drops before that, so the reservation does not wait out
 * OpenCode's settle window (chromeria#221).
 * The Great Library pointer has a separate receipt and a saved, marked text
 * part. Native replay preserves its marker; each request keeps one owned copy.
 * Compaction processing is registered synchronously and awaited before the
 * continuation can save or reuse that part.
 * OpenCode has no before-done hook. Every failure is silent: the call proceeds
 * unserved.
 */
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { createHash, randomBytes } from "node:crypto";

const BASE = "__BASE__";
const MAX_OUTPUT = 4096;
const STOPPED =
  "ContextMD, the user's own rule store, stopped this call. The user's rule is in the latest user message; follow it, then run the same call again.";

function endpoint() {
  const dir = join(BASE, "userdata", "contextmd");
  if (existsSync(join(dir, "off"))) return null;
  try {
    const url = JSON.parse(readFileSync(join(BASE, "userdata", "server-runtime.json"), "utf8")).origin;
    const token = JSON.parse(readFileSync(join(dir, "hook-credential"), "utf8")).token;
    return url && token ? { url, token } : null;
  } catch {
    return null;
  }
}

async function moment(raw, { pointer: wantsPointer = false } = {}) {
  const ep = endpoint();
  if (!ep) return null;
  const delivery = randomBytes(12).toString("hex");
  const pointerDelivery = wantsPointer ? randomBytes(12).toString("hex") : null;
  try {
    const res = await fetch(`${ep.url}/contextmd/v1/moment`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${ep.token}`,
        "content-type": "application/json",
        "x-contextmd-harness": "opencode",
        "x-contextmd-delivery": delivery,
        ...(pointerDelivery ? { "x-contextmd-pointer-delivery": pointerDelivery } : {}),
      },
      body: JSON.stringify(raw),
      signal: AbortSignal.timeout(2000), // the serving cut-off while Cue is new (chromeria#248)
    });
    if (res.status !== 200) return null;
    const reply = await res.json();
    // "confirmed" (204), "refused" (the module released or never held it: stop showing it;
    // it serves the rule again if it still applies) or "retry" (network, timeout, 5xx).
    const post = (action) =>
      fetch(`${ep.url}/contextmd/v1/${action}/${delivery}`, {
        method: "POST",
        headers: { authorization: `Bearer ${ep.token}` },
        signal: AbortSignal.timeout(300),
      });
    const settle = () =>
      post("settle")
        .then((r) => (r.status === 204 ? "confirmed" : r.status >= 500 ? "retry" : "refused"))
        .catch(() => "retry");
    // A drop the server never hears about holds the reservation until the 120 s window.
    const release = () => post("release").then(() => undefined, () => undefined);
    const held = pointerDelivery && typeof reply.pointer === "string" && reply.pointer
      ? { ep, text: reply.pointer, delivery: pointerDelivery, state: "waiting",
          applyingSent: false, attempts: 0, part: null, next: null }
      : null;
    return { reply, settle, release, pointer: held };
  } catch {
    return null;
  }
}

// Per session: served texts waiting for, or already in, the current user message, and the
// turn they belong to. A reply that arrives after a new user message belongs to the old turn
// and is dropped.
const served = new Map();
const turns = new Map();
// Pointer receipts never enter `served`: a generic drop cannot refund a saved part.
const pointer = new Map();
const pending = new Map();

function ownedParts(messages) {
  return messages.flatMap((message) => message.info.role === "user"
    ? message.parts.filter((part) => {
      const mark = part.metadata && part.metadata.contextmd;
      return part.type === "text" && part.synthetic === true && part.ignored !== true
        && typeof part.text === "string" && part.text.length > 0
        && mark && mark.pointer === "great_library"
        && typeof mark.delivery === "string" && /^[0-9a-f]{24}$/.test(mark.delivery)
        && typeof mark.version === "string" && /^[0-9a-f]{12}$/.test(mark.version);
    }) : []);
}

function keepOneCopyPerText(messages) {
  const owned = new Set(ownedParts(messages));
  const seen = new Set();
  // Native replay follows the retained tail. Keep its copy in this request only.
  for (let i = messages.length - 1; i >= 0; i--) {
    const kept = [];
    for (let j = messages[i].parts.length - 1; j >= 0; j--) {
      const part = messages[i].parts[j];
      if (owned.has(part)) {
        if (seen.has(part.text)) continue;
        seen.add(part.text);
      }
      kept.push(part);
    }
    messages[i].parts = kept.reverse();
  }
}

const pointerCall = (held, action, reason) =>
  fetch(`${held.ep.url}/contextmd/v1/pointer/${action}/${held.delivery}` +
        (reason ? `?reason=${encodeURIComponent(reason)}` : ""), {
    method: "POST",
    headers: { authorization: `Bearer ${held.ep.token}` },
    signal: AbortSignal.timeout(300),
  }).then((res) => res.status, () => 0);

async function finishHeld(sessionID, held, action, reason) {
  const status = await pointerCall(held, action, reason);
  if (status === 204 || status === 409) {
    if (pointer.get(sessionID) === held) pointer.delete(sessionID);
  } else held.next = { action, reason };
}

const settleHeld = (id, held) => finishHeld(id, held, "settle");
const releaseHeld = (id, held, reason) => finishHeld(id, held, "release", reason);

async function retireHeld(id, held) {
  if (held.next) return finishHeld(id, held, held.next.action, held.next.reason);
  if (held.attempts === 0) return releaseHeld(id, held, "epoch_closed");
  if (pointer.get(id) === held) pointer.delete(id);
  // A save may still commit. Its old-epoch charge remains owned by that delivery.
}

function markedPart(held, message) {
  const headEnd = held.text.indexOf("\n\n");
  const line = headEnd < 0 ? held.text : held.text.slice(headEnd + 2);
  const version = createHash("sha256").update(line, "utf8").digest("hex").slice(0, 12);
  return { id: `prt_contextmdgl${held.delivery}`, sessionID: message.info.sessionID,
    messageID: message.info.id, type: "text", synthetic: true, text: held.text,
    metadata: { contextmd: { pointer: "great_library", delivery: held.delivery, version } } };
}

function pushIntoOwnMessage(messages, part) {
  const own = messages.find((message) => message.info.id === part.messageID);
  if (own && !own.parts.some((existing) => existing.id === part.id)) own.parts.push(part);
}

function queue(sessionID, turn, text, settle, release) {
  if ((turns.get(sessionID) || 0) !== turn) {
    void release();
    return;
  }
  const list = served.get(sessionID) || [];
  list.push({ text, settle, release, state: "pending" });
  served.set(sessionID, list);
}

/** Forget queued text for a session, and free any reply the model has not been shown. */
async function drop(sessionID) {
  const list = served.get(sessionID) || [];
  served.delete(sessionID);
  for (const item of list) {
    // A failed settle puts a shown reply back to pending so the next call can retry.
    // That reply already reached the model, so the reservation stays until it settles
    // or the window expires.
    if (item.shown) continue;
    if (item.state !== "pending" && item.state !== "retry") continue;
    item.state = "dropped";
    await item.release();
  }
}

export default async function contextmd(ctx) {
  const directory = (ctx && (ctx.directory || ctx.worktree)) || process.cwd();
  const client = ctx && ctx.client;

  async function save(part) {
    try {
      // OpenCode supplies its root SDK client, whose configured transport owns auth,
      // directory and in-process fetch. That SDK has no public part-update method.
      const result = await client._client.patch({
        url: "/session/{sessionID}/message/{messageID}/part/{partID}",
        path: { sessionID: part.sessionID, messageID: part.messageID, partID: part.id },
        body: part, throwOnError: true, signal: AbortSignal.timeout(2000),
      });
      return result && result.data && result.data.id === part.id ? "stored" : "unknown";
    } catch (error) {
      const status = error && error.cause && error.cause.status;
      return status >= 400 && status < 500 ? "rejected" : "unknown";
    }
  }

  async function ensurePointer(sessionID, output) {
    keepOneCopyPerText(output.messages);
    await pending.get(sessionID);
    const held = pointer.get(sessionID);
    if (!held) return;
    if (held.next) return finishHeld(sessionID, held, held.next.action, held.next.reason);
    if (endpoint() === null)
      return held.attempts === 0 ? releaseHeld(sessionID, held, "serving_off") : undefined;
    const owned = ownedParts(output.messages);
    if (owned.some((part) => part.metadata.contextmd.delivery === held.delivery)
        || owned.some((part) => part.text === held.text)) return settleHeld(sessionID, held);
    if (held.state === "waiting") {
      if (typeof client?._client?.patch !== "function")
        return releaseHeld(sessionID, held, "transport_unavailable");
      held.applyingSent = true;
      const status = await pointerCall(held, "applying");
      if (status === 409) return void pointer.delete(sessionID);
      if (status !== 204) return; // The acknowledgement may be lost after commit.
      held.state = "applying";
      const last = [...output.messages].reverse().find((message) => message.info.role === "user");
      if (!last) return releaseHeld(sessionID, held, "no_user_message");
      held.part = markedPart(held, last);
    }
    held.attempts += 1;
    const outcome = await save(held.part);
    if (outcome === "rejected" && held.attempts === 1)
      return releaseHeld(sessionID, held, "save_rejected");
    pushIntoOwnMessage(output.messages, held.part);
    if (outcome === "stored") return settleHeld(sessionID, held);
    // Abort, 5xx, a wrong response id or a retry's 4xx can follow a committed save.
    // Keep the charge and retry the identical part until the request shows it.
  }

  async function applyGenericItems(output) {
    const last = [...output.messages].reverse().find((m) => m.info.role === "user");
    const list = last && served.get(last.info.sessionID);
    if (!list || list.length === 0) return;
    if (endpoint() === null) {
      await drop(last.info.sessionID);
      return;
    }
    for (const item of list) {
      if (item.state === "refused") continue;
      item.shown = true;
      last.parts.push({
        id: `prt_contextmd${randomBytes(6).toString("hex")}`,
        sessionID: last.info.sessionID,
        messageID: last.info.id,
        type: "text",
        text: item.text,
        synthetic: true,
      });
      if (item.state === "pending") item.state = await item.settle();
      if (item.state === "retry") item.state = "pending";
    }
  }

  return {
    "chat.message": async (input) => {
      await pending.get(input.sessionID);
      // A new user message: earlier served text belonged to the previous turn.
      const turn = (turns.get(input.sessionID) || 0) + 1;
      turns.set(input.sessionID, turn);
      await drop(input.sessionID);
      const old = pointer.get(input.sessionID);
      if (old && old.next) await finishHeld(input.sessionID, old, old.next.action, old.next.reason);
      else if (old && old.attempts === 0) await releaseHeld(input.sessionID, old, "turn_ended");
      const got = await moment({ hook: "chat.message", directory, input: { sessionID: input.sessionID } },
                               { pointer: true });
      const text = got && got.reply && got.reply.context;
      if (text) queue(input.sessionID, turn, text, got.settle, got.release);
      if (got && got.pointer) pointer.set(input.sessionID, got.pointer);
    },
    "experimental.chat.messages.transform": async (_input, output) => {
      await applyGenericItems(output);
      const last = [...output.messages].reverse().find((m) => m.info.role === "user");
      if (last) await ensurePointer(last.info.sessionID, output);
    },
    "tool.execute.before": async (input, output) => {
      const turn = turns.get(input.sessionID) || 0;
      const got = await moment({
        hook: "tool.execute.before",
        directory,
        input: { tool: input.tool, sessionID: input.sessionID, callID: input.callID },
        output: { args: (output && output.args) || {} },
      });
      const text = got && got.reply && got.reply.deny;
      // A deny whose rule could not reach the model must not stop the call.
      if (!text || (turns.get(input.sessionID) || 0) !== turn) {
        if (got && got.reply) await got.release();
        return;
      }
      queue(input.sessionID, turn, text, got.settle, got.release);
      throw new Error(STOPPED);
    },
    "tool.execute.after": async (input, output) => {
      const turn = turns.get(input.sessionID) || 0;
      const out = output && typeof output.output === "string" ? output.output : "";
      const meta = (output && output.metadata) || {};
      // A cut read must stay partial (chromeria#225): OpenCode's own flag, or a line it clipped,
      // whose marker may fall outside the slice forwarded below.
      const truncated = meta.truncated === true || /\(line truncated to \d+ chars\)/.test(out);
      const got = await moment({
        hook: "tool.execute.after",
        directory,
        input: { tool: input.tool, sessionID: input.sessionID, callID: input.callID, args: input.args || {} },
        output: { metadata: { exit: meta.exit, truncated }, output: out.slice(-MAX_OUTPUT) },
      });
      const text = got && got.reply && got.reply.context;
      if (text) queue(input.sessionID, turn, text, got.settle, got.release);
      else if (got && got.reply) await got.release();
    },
    event: ({ event }) => {
      if (event && event.type === "session.compacted" && event.properties && event.properties.sessionID) {
        const id = event.properties.sessionID;
        // Registration is synchronous; the continuation waits for the whole reply processing.
        pending.set(id, (async () => {
          const got = await moment({ hook: "session.compacted", directory, input: { sessionID: id } },
                                   { pointer: true });
          const old = pointer.get(id);
          if (old) await retireHeld(id, old);
          if (got && got.pointer) pointer.set(id, got.pointer);
        })());
        return pending.get(id);
      }
    },
  };
}
