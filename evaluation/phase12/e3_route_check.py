"""E3: every /api/... route that frontend_react/src calls must be allowed by node_gateway/server.js allowedRoutes.

Static check: pulls the `allowedRoutes` array out of server.js (regex literals), pulls every /api/... string or template
literal out of the frontend sources, replaces ${...} by a dummy id, strips the query and tests method+path.
Usage: python e3_route_check.py <repo_root>
"""
import re
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
server = (root / "node_gateway" / "server.js").read_text(encoding="utf-8")
block = server[server.index("const allowedRoutes = ["):]
block = block[: block.index("\n];")]
routes = []
for m in re.finditer(r'\["(GET|POST|PATCH|DELETE|PUT)",\s*/(.*?)/\],?\s*$', block, re.M):
    routes.append((m.group(1), re.compile(m.group(2).replace("\\/", "/"))))
print("allowed routes parsed:", len(routes))

def strip_templates(s):
    # replace ${...} (possibly nested with a template literal inside) with X1
    out, i = "", 0
    while i < len(s):
        if s.startswith("${", i):
            depth, j = 1, i + 2
            while j < len(s) and depth:
                depth += (s[j] == "{") - (s[j] == "}")
                j += 1
            out += "X1"
            i = j
        else:
            out += s[i]
            i += 1
    return out

calls = []
for f in sorted((root / "frontend_react" / "src").rglob("*")):
    if f.suffix not in {".js", ".jsx", ".ts", ".tsx"}:
        continue
    text = f.read_text(encoding="utf-8")
    starts = [m.start() for m in re.finditer(r"/api/", text)]
    for n, st in enumerate(starts):
        # opening quote = nearest quote/backtick before the match on the same line
        line_start = text.rfind(chr(10), 0, st) + 1
        qpos = max(text.rfind(c, line_start, st) for c in ("'", '"', "`"))
        if qpos < 0:
            continue
        q = text[qpos]
        j, depth = st, 0
        while j < len(text):
            ch = text[j]
            if q == "`" and text.startswith("${", j):
                depth += 1
                j += 2
                continue
            if depth and ch == "}":
                depth -= 1
            elif not depth and ch == q:
                break
            j += 1
        lit = text[st:j]
        path = strip_templates(lit).split("?")[0].replace("X1", "x1")
        end = starts[n + 1] if n + 1 < len(starts) else len(text)
        window = text[j: min(end, j + 200)]
        before = text[max(0, qpos - 60): qpos]
        mm = re.search(r"method:\s*'(GET|POST|PATCH|DELETE|PUT)'", window)
        method = mm.group(1) if mm else ("POST" if "xhr.open('POST'" in before else "GET")
        calls.append((f.name, method, path))

seen, bad = set(), []
for fname, method, path in calls:
    key = (method, path)
    if key in seen:
        continue
    seen.add(key)
    ok = any(rm == method and rp.search(path) for rm, rp in routes)
    print(("OK   " if ok else "BLOCK"), method.ljust(6), path, f"({fname})")
    if not ok:
        bad.append(key)
print(f"calls: {len(seen)} unique; unresolved/blocked: {len(bad)}")
for b in bad:
    print("  NOT ALLOWED:", *b)
