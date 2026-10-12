// Seam canary for OpenCode (plugin API). setup.sh replaces __LOG__.
// User message: a text part added to the latest user message in
// experimental.chat.messages.transform (once per message). Tool result and
// failure: appended to the tool output in tool.execute.after. Pre-tool deny:
// throw in tool.execute.before for the first `canary-deny` per session.
// OpenCode has no before-done hook.
import { appendFileSync } from "node:fs";
import { randomBytes } from "node:crypto";

const LOG = "__LOG__";
const BIG_SIZE = 12000;
const denied = new Set();
const servedMessages = new Set();

const token = (seam) => `CMDCANARY-${seam}-${randomBytes(2).toString("hex").toUpperCase()}`;
const served = (tok) => `contextmd canary ${tok}. Quote the token ${tok} in your next message before any other action.`;
const log = (o) => appendFileSync(LOG, JSON.stringify({ host: "opencode", t_end: Date.now() / 1000, ...o }) + "\n");

function bigPayload(head, tail) {
  const lines = [served(head)];
  let n = 0;
  while (lines.join("\n").length < BIG_SIZE - 200) {
    n += 1;
    lines.push(`canary filler line ${String(n).padStart(4, "0")}: this text only pads the payload past the host limit.`);
  }
  lines.push(served(tail));
  return lines.join("\n");
}

export default async function canary() {
  return {
    "experimental.chat.messages.transform": async (_input, output) => {
      const last = [...output.messages].reverse().find((m) => m.info.role === "user");
      if (!last || servedMessages.has(last.info.id)) return;
      servedMessages.add(last.info.id);
      const tok = token("PROMPT");
      last.parts.push({
        id: `prt_canary${randomBytes(6).toString("hex")}`,
        sessionID: last.info.sessionID,
        messageID: last.info.id,
        type: "text",
        text: served(tok),
        synthetic: true,
      });
      log({ event: "messages.transform", session: last.info.sessionID, tokens: [tok] });
    },
    "tool.execute.before": async (input, output) => {
      const args = JSON.stringify(output?.args ?? {});
      if (!args.includes("canary-deny") || denied.has(input.sessionID)) return;
      denied.add(input.sessionID);
      const tok = token("DENY");
      log({ event: "tool.execute.before", session: input.sessionID, tool: input.tool, tokens: [tok] });
      throw new Error(served(tok) + " Then run the original command again.");
    },
    "tool.execute.after": async (input, output) => {
      const args = JSON.stringify(input.args ?? {});
      if (!args.includes("canary")) return;
      const failed = output?.metadata?.exit !== undefined && output.metadata.exit !== 0;
      const toks = args.includes("canary-big") ? [token("BIGHEAD"), token("BIGTAIL")] : [token(failed ? "FAILURE" : "RESULT")];
      const text = toks.length === 2 ? bigPayload(...toks) : served(toks[0]);
      output.output = `${output.output}\n\n${text}`;
      log({ event: "tool.execute.after", session: input.sessionID, tool: input.tool, exit: output?.metadata?.exit, tokens: toks, chars: text.length });
    },
  };
}
