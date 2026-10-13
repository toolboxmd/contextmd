"""Cue's scorer: Qwen3-Reranker with a LoRA adapter, scored the way the adapter was trained and measured.

Vendored from the intent pilot's ``loop/student/lib.py`` (``Qwen``: the packed serving layout) and
``loop/int8/bench.py`` (LoRA merged, TorchAO W8A8 on the decoder layers' Linear modules, ``torch.compile`` with
dynamic shapes), the variant measured in INT8-REPORT.md (2026-10-12). Every entry is scored exactly as if it were
alone after the window: one sequence holds the window prefix and several entry suffixes, and a 4D mask lets each
suffix see the prefix and itself only. score = logit("yes") - logit("no") at the suffix's last token; an entry
fires at or above the variant's own held-out val threshold. Needs torch, transformers, peft and (for w8a8) torchao.
"""

from __future__ import annotations

import torch

MODELS = {"qwen3-4b": ("Qwen/Qwen3-Reranker-4B", "22e683669bc0f0bd69640a1354a6d0aebcfeede5")}
MAX_WINDOW_TOKENS = 6144  # newest window tokens kept; the current message is last
MAX_TOKENS = 12288  # one packed sequence


class Scorer:
    def __init__(self, model: str, instruction: str, adapter: str | None, quant: str = "none",
                 compile: bool = False):
        from transformers import AutoModelForCausalLM, AutoTokenizer
        mid, rev = MODELS[model]
        self.tok = AutoTokenizer.from_pretrained(mid, revision=rev)
        m = AutoModelForCausalLM.from_pretrained(mid, revision=rev, dtype=torch.bfloat16,
                                                 attn_implementation="sdpa").to("cuda")
        m.config.use_cache = False
        if adapter:
            from peft import PeftModel
            m = PeftModel.from_pretrained(m, adapter).merge_and_unload()
        if quant == "w8a8":
            from torchao.quantization import Int8DynamicActivationInt8WeightConfig, quantize_
            quantize_(m, Int8DynamicActivationInt8WeightConfig(),
                      filter_fn=lambda mod, fqn: isinstance(mod, torch.nn.Linear) and fqn.startswith("model.layers"))
        elif quant != "none":
            raise ValueError(f"unknown quant {quant!r}")
        m.eval()
        if compile:
            m.model = torch.compile(m.model, dynamic=True)
        self.causal = m
        enc = lambda s: self.tok.encode(s, add_special_tokens=False)
        self.enc = enc
        self.pre = enc("<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and "
                       "the Instruct provided. Note that the answer can only be \"yes\" or \"no\".<|im_end|>\n"
                       "<|im_start|>user\n")
        self.head = enc(f"<Instruct>: {instruction}\n<Query>: ")
        self.suf = enc("<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n")
        self.yes, self.no = self.tok.convert_tokens_to_ids("yes"), self.tok.convert_tokens_to_ids("no")
        self._docs: dict[str, list[int]] = {}

    def doc(self, text):
        if text not in self._docs:
            self._docs[text] = self.enc(f"\n<Document>: {text}") + self.suf
        return self._docs[text]

    def prefix(self, window):
        """The prefix tokens and whether the window was cut from its oldest end."""
        w = self.enc(window)
        cut = len(w) > MAX_WINDOW_TOKENS
        if cut:
            w = w[-MAX_WINDOW_TOKENS:]
        return self.pre + self.head + w, cut

    def packed(self, pre, docs):
        sufs = [self.doc(d) for d in docs]
        P = len(pre)
        ids = torch.tensor(pre + [t for s in sufs for t in s], device="cuda").unsqueeze(0)
        T = ids.shape[1]
        pos = torch.cat([torch.arange(P)] + [torch.arange(P, P + len(s)) for s in sufs]).to("cuda").unsqueeze(0)
        blk = torch.cat([torch.full((P,), -1)] + [torch.full((len(s),), k) for k, s in enumerate(sufs)]).to("cuda")
        q = torch.arange(T, device="cuda")
        allow = (q.unsqueeze(0) <= q.unsqueeze(1)) & ((blk.unsqueeze(0) == -1) | (blk.unsqueeze(0) == blk.unsqueeze(1)))
        dt = torch.bfloat16
        mask = torch.zeros(1, 1, T, T, dtype=dt, device="cuda").masked_fill_(~allow, torch.finfo(dt).min)
        last = torch.tensor([P + sum(len(s) for s in sufs[:k + 1]) - 1 for k in range(len(sufs))], device="cuda")
        h = self.causal.model(input_ids=ids, position_ids=pos, attention_mask=mask, use_cache=False).last_hidden_state
        w = self.causal.lm_head.weight[[self.yes, self.no]]
        lg = torch.nn.functional.linear(h[0, last], w).float()
        return lg[:, 0] - lg[:, 1]

    @torch.inference_mode()
    def score(self, window: str, docs: list[str]) -> tuple[list[float], dict]:
        """One score per doc, plus the input sizes for the log."""
        pre, cut = self.prefix(window)
        room = MAX_TOKENS - len(pre)
        chunks, cur, n = [], [], 0
        for j, d in enumerate(docs):
            s = len(self.doc(d))
            if s > room:
                raise ValueError(f"entry {j} of {s} tokens does not fit after a {len(pre)}-token prefix")
            if cur and n + s > room:
                chunks.append(cur)
                cur, n = [], 0
            cur.append(j)
            n += s
        if cur:
            chunks.append(cur)
        out: list[float] = [0.0] * len(docs)
        for c in chunks:
            for j, s in zip(c, self.packed(pre, [docs[j] for j in c]).tolist()):
                out[j] = s
        torch.cuda.synchronize()
        return out, {"window_tokens": len(pre) - len(self.pre) - len(self.head), "window_cut": cut,
                     "sequences": len(chunks)}
