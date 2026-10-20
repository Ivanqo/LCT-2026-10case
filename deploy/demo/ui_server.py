"""Static server for the built inspector UI (frontend_react/dist) for the demo stand, plus `/go?project=ID&process=ID`:
a tiny page that logs in with the demo account, selects the project/process in the UI's own local storage and opens the
UI there -- "open the UI on the right object" without clicking through the project list."""
from __future__ import annotations

import argparse
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

GO_PAGE = """<!doctype html><meta charset="utf-8"><title>CASE10 demo</title>
<body style="font-family:sans-serif">Открываю интерфейс инспектора…
<script>
(async () => {
  const q = new URLSearchParams(location.search);
  const api = %(api)r, login = %(login)r, password = %(password)r;
  try {
    const r = await fetch(api + '/api/auth/login', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                  body: JSON.stringify({login, password})});
    const token = (await r.json()).access_token;
    localStorage.setItem('case10_react_token', token);
    sessionStorage.removeItem('case10_react_view');
    const project = q.get('project'), process = q.get('process');
    if (project) {
      localStorage.setItem('case10_react_project_id', project);
      const map = JSON.parse(localStorage.getItem('case10_react_process_map') || '{}');
      if (process) { map[project] = process; sessionStorage.setItem('case10_react_view', 'process'); }
      localStorage.setItem('case10_react_process_map', JSON.stringify(map));
    }
  } catch (e) { document.body.append(' не удалось войти автоматически: ' + e); return; }
  location.replace('/');
})();
</script></body>"""


def make_handler(dist: Path, api: str, login: str, password: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):  # quiet
            pass

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/go":
                body = (GO_PAGE % {"api": api.replace(":8000", ":8080"), "login": login, "password": password}).encode("utf-8")
                return self._send(200, "text/html; charset=utf-8", body)
            target = (dist / parsed.path.lstrip("/")).resolve()
            if not target.is_file() or not target.is_relative_to(dist):
                target = dist / "index.html"
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            self._send(200, ctype, target.read_bytes())

        def _send(self, code: int, ctype: str, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main() -> None:
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3100)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    handler = make_handler(args.dist.resolve(), args.api, os.environ.get("CASE10_DEMO_LOGIN", "admin"),
                           os.environ.get("CASE10_DEMO_PASSWORD", "Case10-Demo-2026"))
    ThreadingHTTPServer(("127.0.0.1", args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
