#!/usr/bin/env zsh
set -euo pipefail

exec python3 -u -c '
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/health":
            payload = json.dumps({"status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, _format, *_args):
        pass

ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORTRAIT_LAB_PORT"])), Handler).serve_forever()
' "${PORTRAIT_LAB_TEST_SIGNATURE:-portrait-lab-test-api}"
