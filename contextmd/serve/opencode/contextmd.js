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
 * a reply when it first adds its part, just before the model reads it.
 * OpenCode has no before-done hook. Every failure is silent: the call proceeds
 * unserved.
 */
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { randomBytes } from "node:crypto";

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

async function moment(raw) {
  const ep = endpoint();
  if (!ep) return null;
  const delivery = randomBytes(12).toString("hex");
  try {
    const res = await fetch(`${ep.url}/contextmd/v1/moment`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${ep.token}`,
        "content-type": "application/json",
        "x-contextmd-harness": "opencode",
        "x-contextmd-delivery": delivery,
      },
      body: JSON.stringify(raw),
      signal: AbortSignal.timeout(1000),
    });
    if (res.status !== 200) return null;
    const reply = await res.json();
    const settle = () =>
      fetch(`${ep.url}/contextmd/v1/settle/${delivery}`, {
        method: "POST",
        headers: { authorization: `Bearer ${ep.token}` },
        signal: AbortSignal.timeout(300),
      }).catch(() => {});
    return { reply, settle };
  } catch {
    return null;
  }
}

// Per session: served texts waiting for, or already in, the current user message.
const served = new Map();

function queue(sessionID, text, settle) {
  const list = served.get(sessionID) || [];
  list.push({ text, settle, settled: false });
  served.set(sessionID, list);
}

export default async function contextmd(ctx) {
  const directory = (ctx && (ctx.directory || ctx.worktree)) || process.cwd();
  return {
    "chat.message": async (input) => {
      // A new user message: earlier served text belonged to the previous turn.
      served.delete(input.sessionID);
      const got = await moment({ hook: "chat.message", directory, input: { sessionID: input.sessionID } });
      const text = got && got.reply && got.reply.context;
      if (text) queue(input.sessionID, text, got.settle);
    },
    "experimental.chat.messages.transform": async (_input, output) => {
      const last = [...output.messages].reverse().find((m) => m.info.role === "user");
      const list = last && served.get(last.info.sessionID);
      if (!list || list.length === 0) return;
      for (const item of list) {
        last.parts.push({
          id: `prt_contextmd${randomBytes(6).toString("hex")}`,
          sessionID: last.info.sessionID,
          messageID: last.info.id,
          type: "text",
          text: item.text,
          synthetic: true,
        });
        if (!item.settled) {
          item.settled = true;
          await item.settle();
        }
      }
    },
    "tool.execute.before": async (input, output) => {
      const got = await moment({
        hook: "tool.execute.before",
        directory,
        input: { tool: input.tool, sessionID: input.sessionID, callID: input.callID },
        output: { args: (output && output.args) || {} },
      });
      const text = got && got.reply && got.reply.deny;
      if (!text) return;
      queue(input.sessionID, text, got.settle);
      throw new Error(STOPPED);
    },
    "tool.execute.after": async (input, output) => {
      const out = output && typeof output.output === "string" ? output.output : "";
      const got = await moment({
        hook: "tool.execute.after",
        directory,
        input: { tool: input.tool, sessionID: input.sessionID, callID: input.callID, args: input.args || {} },
        output: { metadata: { exit: output && output.metadata ? output.metadata.exit : undefined }, output: out.slice(-MAX_OUTPUT) },
      });
      const text = got && got.reply && got.reply.context;
      if (text) queue(input.sessionID, text, got.settle);
    },
    event: async ({ event }) => {
      if (event && event.type === "session.compacted" && event.properties && event.properties.sessionID) {
        await moment({ hook: "session.compacted", directory, input: { sessionID: event.properties.sessionID } });
      }
    },
  };
}
