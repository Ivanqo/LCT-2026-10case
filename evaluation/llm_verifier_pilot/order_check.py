import json, glob, math
pools=[json.loads(l) for l in open('candidate_pools.jsonl',encoding='utf-8')]
labels={}
for f in glob.glob('labels_*.json'): labels.update({k:v for k,v in json.load(open(f,encoding='utf-8')).items() if not k.startswith('_')})
fwd={json.loads(l)['pool_index']:json.loads(l)['forward'] for l in open('llm_fwd.jsonl',encoding='utf-8')}
rev={json.loads(l)['pool_index']:json.loads(l)['reversed'] for l in open('llm_rev.jsonl',encoding='utf-8')}
def ch(r): return None if r['parsed'] is None else r['parsed']['choice']
def ok(c,s): return (c is None) if not s else (c in s)
def norm(v): return None if v is None else v.replace(' ','').replace(',','.').rstrip('.;,')
n=same_val=fo=ro=both=0; firsts=[]
for i,r in rev.items():
    p=pools[i]; lab=labels[f"{p['object']}|{p['code']}|{p['stage']}|{p['mechanism']}"]
    vals=[c['value'] for c in p['candidates']]
    cf,cr=ch(fwd[i]),ch(r)
    vf=None if cf is None else norm(vals[cf]); vr=None if cr is None else norm(vals[cr])
    n+=1; same_val+= vf==vr; fo+=ok(cf,lab['strict']); ro+=ok(cr,lab['strict']); both+=ok(cf,lab['strict'])==ok(cr,lab['strict'])
    firsts.append((cf==0, cr==len(p['candidates'][:10])-1))
print(f"pools={n} same chosen VALUE (or both none)={same_val}/{n}; correctness forward={fo}/{n} reversed={ro}/{n}; same correctness verdict={both}/{n}")
print(f"forward picked shown-first={sum(a for a,_ in firsts)}; reversed picked shown-first (=last in fwd order)={sum(b for _,b in firsts)}")
