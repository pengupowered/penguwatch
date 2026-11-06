#!/usr/bin/env python3
"""A minimal PenguWatch webhook receiver that checks the signature. Standard library only.

    PENGUWATCH_WEBHOOK_SECRET=<the same secret as in config.yaml> python receive_webhook.py

Then point a webhook at http://<this machine>:9000/ (see docs/extending.md). Each delivery carries
  X-PenguWatch-Timestamp  Unix seconds when it was sent
  X-PenguWatch-Signature  sha256=<HMAC-SHA256 of "<timestamp>.<body>" keyed with the secret>
The receiver recomputes the signature over the exact bytes it received, compares in constant time, and refuses
a timestamp that is more than five minutes old (so a captured request cannot be replayed later).
X-PenguWatch-Delivery is NOT signed: to ignore a repeated delivery, remember the "id" inside the signed body.
"""

import hashlib
import hmac
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Mapping, Optional

TOLERANCE_SECONDS = 300
MAX_BODY = 64 * 1024


def verify(secret: bytes, headers: Mapping[str, str], body: bytes, now: Optional[float] = None) -> bool:
    """True when the body was signed with `secret` and is recent."""
    timestamp = headers.get("X-PenguWatch-Timestamp", "")
    signature = headers.get("X-PenguWatch-Signature", "")
    if not re.fullmatch(r"[0-9]{1,12}", timestamp) or abs((now if now is not None else time.time()) - int(timestamp)) > TOLERANCE_SECONDS:
        return False
    expected = "sha256=" + hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.encode("utf-8"))


def make_handler(secret: bytes):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if 0 < length <= MAX_BODY else b""
            if not body or not verify(secret, self.headers, body):
                self.send_response(401)
                self.end_headers()
                return
            event = json.loads(body)
            print(f"{event['type']}: {json.dumps(event['data'])}", flush=True)
            self.send_response(204)  # any 2xx tells PenguWatch the event arrived; it retries on 5xx and timeouts
            self.end_headers()

        def log_message(self, *args):  # no access log
            pass

    return Handler


if __name__ == "__main__":
    secret = os.environ.get("PENGUWATCH_WEBHOOK_SECRET", "")
    if len(secret) < 16:
        sys.exit("Set PENGUWATCH_WEBHOOK_SECRET to the secret in config.yaml (at least 16 characters).")
    print("Listening on :9000", flush=True)
    HTTPServer(("", 9000), make_handler(secret.encode())).serve_forever()
