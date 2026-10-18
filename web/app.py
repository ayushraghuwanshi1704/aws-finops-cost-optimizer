"""
Dashboard HTTP layer.

Standard library only, so the demo launches with the repo's existing install and
keeps working with no network at all. Routing lives in `dispatch()`, a plain
function the tests call directly; `DashboardHandler` is a thin adapter over it.

The server binds to 127.0.0.1 by default and serves no credentials, no profile
names and no file paths: /api/* returns the dashboard payload and nothing else.
"""
import argparse
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from web import data, ebs, reports

WEB_ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(WEB_ROOT, "templates", "index.html")

# Only these filenames are ever read from disk, so a crafted /static/..%2F path
# cannot reach anything outside web/.
STATIC_FILES: Dict[str, Tuple[str, str]] = {
    "styles.css": ("text/css; charset=utf-8", os.path.join(WEB_ROOT, "static", "styles.css")),
    "app.js": ("application/javascript; charset=utf-8", os.path.join(WEB_ROOT, "static", "app.js")),
}

REGION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,24}$")
DEFAULT_MODE = "demo"
DEFAULT_DAYS = 30
DEFAULT_REGION = "us-east-1"

Response = Tuple[int, Dict[str, str], bytes]

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'"
)


def _json(status: int, body: Dict[str, Any], extra: Optional[Dict[str, str]] = None) -> Response:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    headers.update(extra or {})
    return status, headers, json.dumps(body, indent=2).encode("utf-8")


def _text(status: int, content_type: str, body: str, extra: Optional[Dict[str, str]] = None) -> Response:
    headers = {"Content-Type": content_type}
    headers.update(extra or {})
    return status, headers, body.encode("utf-8")


def _error(status: int, message: str) -> Response:
    return _json(status, {"error": message})


def _read_file(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def parse_params(query: Dict[str, List[str]], allow_live: bool = True) -> Dict[str, Any]:
    """
    Validate the few query parameters the dashboard accepts.

    Anything unrecognised, malformed or out of the offered set falls back to the
    demo-safe default rather than reaching AWS with a guessed argument.
    """
    raw_mode = (query.get("mode") or [DEFAULT_MODE])[0].strip().lower()
    raw_days = (query.get("days") or [str(DEFAULT_DAYS)])[0]
    raw_region = (query.get("region") or [DEFAULT_REGION])[0].strip().lower()

    mode = raw_mode if raw_mode in data.MODES else DEFAULT_MODE
    if mode == "live" and not allow_live:
        mode = DEFAULT_MODE
    days = data.normalize_days(raw_days) or DEFAULT_DAYS
    region = raw_region if REGION_PATTERN.match(raw_region) else DEFAULT_REGION

    return {"mode": mode, "days": days, "region": region, "liveRequested": raw_mode == "live"}


def parse_regions(query: Dict[str, List[str]], fallback: str) -> List[str]:
    """
    Validate the region list an EBS request may query.

    Volumes are regional, so the panel accepts more than one region, but every
    token is checked against the same pattern as the single `region` parameter
    before it reaches boto3 and the list is capped at `MAX_REGIONS`: a typed-in
    region must not turn one poll into a fan-out across the partition. Repeated
    `regions` parameters are joined, malformed tokens are dropped rather than
    guessed at, and an empty result falls back to the dashboard's own region.
    """
    raw = ",".join(query.get("regions") or [])
    regions: List[str] = []
    for token in raw.replace(";", ",").split(","):
        code = token.strip().lower()
        if code and REGION_PATTERN.match(code) and code not in regions:
            regions.append(code)
    return regions[:ebs.MAX_REGIONS] or [fallback]


def health(allow_live: bool = True) -> Dict[str, Any]:
    """
    Status block for the dashboard's health badge.

    Reports what can be known locally. It never claims Cost Explorer works,
    because confirming that would need an API call this endpoint does not make.
    """
    status = data.aws_status()
    return {
        "service": "aws-finops-dashboard",
        "status": "ok",
        "defaultMode": DEFAULT_MODE,
        "liveModeEnabled": allow_live,
        "supportedDayFilters": list(data.SUPPORTED_DAYS),
        "aws": status,
        "note": (
            "Demo mode serves labelled sample data and makes no AWS call. "
            "Live mode reads Cost Explorer and the account's read-only describe "
            "APIs; it never writes to AWS."
        ),
    }


def build_payload(params: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve one dashboard payload for validated parameters."""
    return data.build_payload(
        days=params["days"],
        mode=params["mode"],
        region=params["region"],
    )


def dispatch(method: str, path: str, query: Optional[Dict[str, List[str]]] = None,
             allow_live: bool = True) -> Response:
    """Route a request to its handler. The only entry point the server uses."""
    if method != "GET":
        return _error(405, "only GET is supported")

    params = parse_params(query or {}, allow_live=allow_live)
    clean = path.rstrip("/") or "/"

    if clean == "/":
        html = _read_file(INDEX_PATH)
        if html is None:
            return _error(500, "dashboard template is missing from web/templates/index.html")
        return _text(200, "text/html; charset=utf-8", html, {"Content-Security-Policy": CSP})

    if clean.startswith("/static/"):
        name = clean[len("/static/"):]
        if name not in STATIC_FILES:
            return _error(404, "unknown static asset")
        content_type, absolute = STATIC_FILES[name]
        body = _read_file(absolute)
        if body is None:
            return _error(500, f"static asset is missing: {name}")
        return _text(200, content_type, body, {"X-Content-Type-Options": "nosniff"})

    if clean == "/api/health":
        return _json(200, health(allow_live=allow_live))

    if clean == "/api/dashboard":
        payload = build_payload(params)
        status = 200 if params["mode"] == "demo" or not payload["errors"] else 207
        return _json(status, payload)

    if clean == "/api/ebs":
        # Volume status refreshes on its own timer, so it must not drag a Cost
        # Explorer query along with it; the same 207 rule applies, because a
        # partly-answered region list is not a clean report.
        regions = parse_regions(query or {}, params["region"])
        payload = data.ebs_payload(params["mode"], regions)
        status = 200 if params["mode"] == "demo" or not payload["errors"] else 207
        return _json(status, payload)

    if clean in ("/api/report/csv", "/api/report/json"):
        payload = build_payload(params)
        stamp = f"{params['mode']}-{params['days']}d"
        if clean.endswith("csv"):
            body = reports.to_csv(data.csv_rows(payload))
            return _text(
                200, "text/csv; charset=utf-8", body,
                {"Content-Disposition": f'attachment; filename="finops-{stamp}.csv"'},
            )
        body = reports.to_json(payload)
        return _text(
            200, "application/json; charset=utf-8", body,
            {"Content-Disposition": f'attachment; filename="finops-{stamp}.json"'},
        )

    return _error(404, "unknown path")


class DashboardHandler(BaseHTTPRequestHandler):
    """Adapter from http.server to dispatch()."""

    allow_live = True

    def do_GET(self) -> None:  # noqa: N802 - name required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            status, headers, body = dispatch(
                "GET", parsed.path, query, allow_live=self.allow_live)
        except Exception as exc:  # a broken payload must not kill the server
            status, headers, body = _error(
                500, f"dashboard error: {type(exc).__name__}: {exc}")

        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("dashboard %s - %s\n" % (self.address_string(), format % args))


def create_server(host: str = "127.0.0.1", port: int = 8080,
                  allow_live: bool = True) -> ThreadingHTTPServer:
    class BoundHandler(DashboardHandler):
        pass

    BoundHandler.allow_live = allow_live
    return ThreadingHTTPServer((host, port), BoundHandler)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Serve the CloudLedger-style FinOps dashboard locally.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m web.app                        # demo dashboard on http://127.0.0.1:8080
  python -m web.app --port 9000            # different port, still demo data
  python -m web.app --demo-only            # refuse live mode even if requested
  python -m web.app --live                 # print the live-mode notice, start on demo

Demo mode needs no AWS credentials and issues no AWS calls. Live mode uses the
same read-only scripts/ modules against your own account.
        """
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (default: 127.0.0.1, loopback only)")
    parser.add_argument("--port", type=int, default=8080,
                        help="Port to listen on (default: 8080)")
    parser.add_argument("--demo-only", action="store_true",
                        help="Refuse mode=live requests; sample data only")
    parser.add_argument("--live", action="store_true",
                        help="Print the live-mode notice and keep serving demo by default")
    args = parser.parse_args(argv)

    allow_live = not args.demo_only
    server = create_server(host=args.host, port=args.port, allow_live=allow_live)

    print("CloudLedger-style FinOps dashboard")
    print(f"  URL      : http://{args.host}:{args.port}")
    print(f"  Data mode: {data.DEMO_WARNING} (sample figures, no AWS calls)")
    print(f"  Live mode: {'enabled (choose it in the UI)' if allow_live else 'disabled (--demo-only)'}")
    if args.live:
        print("  Note     : live spend comes from Cost Explorer via scripts/cost_analysis.py")
    print("  Stop     : Ctrl+C")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
