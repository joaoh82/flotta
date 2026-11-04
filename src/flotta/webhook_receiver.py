"""A local endpoint for budget alerts: `just webhook-receiver <secret>` (FLOTTA-93).

Loopback only, for trying `flotta.alerts` on a laptop. It answers 200 to a good
signature and 401 to anything else, so a bad one shows up as a retry.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer

from flotta.alerts import DELIVERY_HEADER, SIGNATURE_HEADER, verify_signature


def handler_for(secret: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            good = verify_signature(secret, self.headers.get(SIGNATURE_HEADER) or "", body)
            print(
                f"{self.headers.get(DELIVERY_HEADER)} signature {'good' if good else 'BAD'}: "
                f"{body.decode(errors='replace')}",
                flush=True,
            )
            self.send_response(200 if good else 401)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secret", required=True)
    parser.add_argument("--port", type=int, default=8184)
    args = parser.parse_args()
    print(f"listening on http://127.0.0.1:{args.port}", flush=True)
    HTTPServer(("127.0.0.1", args.port), handler_for(args.secret)).serve_forever()


if __name__ == "__main__":
    main()
