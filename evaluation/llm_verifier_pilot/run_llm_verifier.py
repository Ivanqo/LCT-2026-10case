"""LLM-verifier pilot, phase 2 (isolated GPU venv on E:). Loads the
PRODUCTION module api_service/app/domain/llm_candidate_verifier.py by path
(same prompt/parse/backend code the app would run), feeds it every captured
candidate pool, and records verdicts + latency.

usage: run_llm_verifier.py POOLS OUT (forward|reversed)
  forward : candidates in the deterministic ranking's order (the real use)
  reversed: same pools, candidate order reversed (position-bias check);
            the recorded choice is mapped back to forward indexing.
Resumable: pools already present in OUT are skipped."""
import importlib.util
import json
import sys
import time
from pathlib import Path

SCRATCH = Path(__file__).parent
MODULE = Path(r"D:\Proga\LCT-hack-2026\api_service\app\domain\llm_candidate_verifier.py")
spec = importlib.util.spec_from_file_location("llm_candidate_verifier", MODULE)
verifier = importlib.util.module_from_spec(spec)
sys.modules["llm_candidate_verifier"] = verifier
spec.loader.exec_module(verifier)

pools_path = SCRATCH / sys.argv[1]
out_path = SCRATCH / sys.argv[2]
direction = sys.argv[3]
pools = [json.loads(line) for line in open(pools_path, encoding="utf-8")]
done = set()
if out_path.exists():
    done = {json.loads(line)["pool_index"] for line in open(out_path, encoding="utf-8")}

import torch
t0 = time.perf_counter()
assert verifier._load(), "model failed to load"
print(f"model loaded in {time.perf_counter() - t0:.1f}s, vram={torch.cuda.memory_allocated()/1024**3:.2f} GB", flush=True)


def call(definition, cands):
    messages = verifier.build_messages(definition, cands)
    text = verifier._tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    n_tokens = len(verifier._tokenizer(text)["input_ids"])
    torch.cuda.synchronize()
    started = time.perf_counter()
    raw = verifier.generate(messages)
    torch.cuda.synchronize()
    latency = time.perf_counter() - started
    parsed = verifier.parse_verdict(raw, len(cands))
    return {"raw": raw, "parsed": None if parsed is None else {"choice": parsed[0], "reason": parsed[1]},
            "latency": round(latency, 3), "prompt_tokens": n_tokens}


with open(out_path, "a", encoding="utf-8") as out:
    for i, pool in enumerate(pools):
        if i in done:
            continue
        cands = pool["candidates"][: verifier.MAX_CANDIDATES]
        if direction == "reversed":
            if len(cands) < 2:
                continue
            res = call(pool["definition"], list(reversed(cands)))
            if res["parsed"] and res["parsed"]["choice"] is not None:
                res["parsed"]["choice"] = len(cands) - 1 - res["parsed"]["choice"]
        else:
            res = call(pool["definition"], cands)
        record = {"pool_index": i, "object": pool["object"], "code": pool["code"], "stage": pool["stage"],
                  "mechanism": pool["mechanism"], direction: res}
        out.write(json.dumps(record, ensure_ascii=False) + "\n")
        out.flush()
        print(f"{i+1}/{len(pools)} {pool['object']} {pool['code']} {pool['stage']} n={len(cands)} tok={res['prompt_tokens']} "
              f"{res['latency']}s choice={res['parsed'] and res['parsed']['choice']}", flush=True)
print(f"peak vram {torch.cuda.max_memory_allocated()/1024**3:.2f} GB", flush=True)
