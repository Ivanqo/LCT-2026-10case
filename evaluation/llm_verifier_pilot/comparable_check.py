import json, glob
pools=[json.loads(l) for l in open('candidate_pools.jsonl',encoding='utf-8')]
labels={}
for f in glob.glob('labels_*.json'): labels.update({k:v for k,v in json.load(open(f,encoding='utf-8')).items() if not k.startswith('_')})
fwd={json.loads(l)['pool_index']:json.loads(l)['forward'] for l in open('llm_fwd.jsonl',encoding='utf-8')}
summ=json.load(open('capture_summary.json',encoding='utf-8'))
comp={(s['object'],c) for s in summ for c in s['comparable_codes']}
def ok(c,s): return (c is None) if not s else (c in s)
from collections import Counter
mech=Counter(p['mechanism'] for p in pools); print('mechanisms',dict(mech))
rows=[]
for i,p in enumerate(pools):
    if (p['object'],p['code']) not in comp or p['stage'] not in ('PD','RD'): continue
    lab=labels[f"{p['object']}|{p['code']}|{p['stage']}|{p['mechanism']}"]
    lc=None if fwd[i]['parsed'] is None else fwd[i]['parsed']['choice']
    rows.append((p['object'],p['code'],p['stage'],ok(p['baseline_choice'],lab['strict']),ok(lc,lab['strict'])))
groups={}
for o,c,s,b,l in rows: groups.setdefault((o,c),{})[s]=(b,l)
bothok_b=sum(all(v[0] for v in g.values()) for g in groups.values()); bothok_l=sum(all(v[1] for v in g.values()) for g in groups.values())
print(f"comparable groups: {len(groups)}; groups with ALL stage values correct: baseline {bothok_b}, LLM {bothok_l}")
for k,g in sorted(groups.items()): print(k, {s:('B+' if b else 'B-')+('L+' if l else 'L-') for s,(b,l) in g.items()})
