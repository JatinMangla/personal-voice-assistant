"""Reranker for the coder helper: Qwen3-Reranker-0.6B judges "does this code piece answer
the question?" (yes/no) and the probability of "yes" is the score. The laptop's search
finds ~30 candidate chunks; this keeps the best few, so the coder reads less and answers
better.

Loaded only if it works: any failure (download, GPU memory) leaves `load()` returning None
and the helper simply runs without reranking.
"""
import time

import config

INSTRUCTION = "Given a question about a code repository, judge whether the code piece helps answer it"
PREFIX = ("<|im_start|>system\nJudge whether the Document meets the requirements based on the Query "
          "and the Instruct provided. Note that the answer can only be \"yes\" or \"no\"."
          "<|im_end|>\n<|im_start|>user\n")
SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
MAX_TOKENS = 1536     # per code piece (longer pieces are cut at the end)
BATCH = 4


def format_pair(query: str, document: str) -> str:
    return f"<Instruct>: {INSTRUCTION}\n<Query>: {query}\n<Document>: {document}"


class Reranker:
    def __init__(self, tokenizer, model, device: str):
        import torch
        self.torch = torch
        self.tokenizer, self.model, self.device = tokenizer, model, device
        self.yes = tokenizer.convert_tokens_to_ids("yes")
        self.no = tokenizer.convert_tokens_to_ids("no")
        self.prefix = tokenizer.encode(PREFIX, add_special_tokens=False)
        self.suffix = tokenizer.encode(SUFFIX, add_special_tokens=False)

    def __call__(self, query: str, documents: list[str]) -> list[float]:
        scores: list[float] = []
        for i in range(0, len(documents), BATCH):
            pairs = [format_pair(query, d) for d in documents[i:i + BATCH]]
            room = MAX_TOKENS - len(self.prefix) - len(self.suffix)
            ids = [self.prefix + self.tokenizer.encode(p, add_special_tokens=False)[:room] + self.suffix
                   for p in pairs]
            batch = self.tokenizer.pad({"input_ids": ids}, padding=True, return_tensors="pt")
            batch = {k: v.to(self.device) for k, v in batch.items()}
            with self.torch.no_grad():
                logits = self.model(**batch).logits[:, -1, :]
            pair = self.torch.stack([logits[:, self.no], logits[:, self.yes]], dim=1)
            scores += self.torch.nn.functional.softmax(pair.float(), dim=1)[:, 1].tolist()
        return scores


def load(device: str = "cuda:1"):
    """The reranker, or None if it can't be loaded (never raises)."""
    t = time.time()
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(config.CODER_RERANK_REPO, padding_side="left")
        model = AutoModelForCausalLM.from_pretrained(
            config.CODER_RERANK_REPO, torch_dtype=torch.float16).to(device).eval()
        reranker = Reranker(tokenizer, model, device)
        reranker("test question", ["def f(): pass"])   # first call is slow: do it now
        print(f"[coder] reranker ready on {device} in {time.time() - t:.0f}s")
        return reranker
    except Exception as e:
        print(f"[coder] no reranker ({type(e).__name__}: {str(e)[:120]}); search is not reranked")
        return None
