"""In-sandbox browser executor: serve the extracted tree, drive Chromium through validated steps, report evidence.

Runs inside the runner's container as the unprivileged user with no network, a read-only root and no
capabilities; copied beside the job inputs like harness.py. Everything it reports is evidence from inside the
sandbox, which the application under test can influence, and is validated by the host before use.

Isolation here is defence in depth, not the boundary. The boundary is the container (no network namespace
route, no credentials). On top of that every browser request is checked against the single local origin, DNS for
anything else is disabled, and service workers, downloads, popups and dialogs are refused.
"""
import base64
import collections
import functools
import http.server
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

try:  # inside the container the modules sit beside this file; on the host they are package members
    import browser_spec
    import harness
except ImportError:  # pragma: no cover - exercised by the host-side unit tests
    from odytor import browser_spec, harness

MARK = harness.MARK
WORK = harness.WORK
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 24 * 1024 * 1024
MAX_CONSOLE, MAX_PAGE_ERRORS, MAX_NETWORK, MAX_PATHS = 100, 50, 400, 100
MAX_SNAPSHOT_CHARS, MAX_TEXT_CHARS = 12000, 4000
SERVER_LOG_BYTES = 16384
LOCAL_SCHEMES = ("data", "blob", "about")


# --- pure helpers (unit-tested on the host without a browser) -----------------------------------------------------

def origin_allowed(url, port):
    """Only the one local application origin, plus inert in-page schemes. Everything else is blocked."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme in LOCAL_SCHEMES:
        return True
    if parts.scheme not in ("http", "ws"):
        return False
    try:
        return parts.hostname == "127.0.0.1" and (parts.port or 80) == port and parts.username is None and parts.password is None
    except ValueError:
        return False


def request_path(url, limit=200):
    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    return (path + ("?" + parts.query if parts.query else ""))[:limit]


def match_mock(mocks, method, url):
    """First mock whose method and path (query ignored) equal the request's."""
    path = urllib.parse.urlsplit(url).path or "/"
    for mock in mocks:
        if mock["method"] == method and mock["path"] == path:
            return mock
    return None


def compare(kind, actual, expected):
    return actual == expected if kind == "eq" else actual <= expected if kind == "le" else actual >= expected


def count_requests(entries, path, method=None, since=0):
    """Requests (including mocked and failed ones, excluding blocked ones) to an exact path after a baseline."""
    return sum(1 for entry in entries[since:] if not entry.get("blocked") and entry["path"].split("?", 1)[0] == path
               and (method is None or entry["method"] == method))


def summarize_network(entries):
    by_path = collections.Counter(f"{e['method']} {e['path'].split('?', 1)[0]}" for e in entries if not e.get("blocked"))
    return {"total": len(entries), "blocked": sum(1 for e in entries if e.get("blocked")),
            "mocked": sum(1 for e in entries if e.get("mocked")), "failed": sum(1 for e in entries if e.get("failed")),
            "by_path": dict(by_path.most_common(MAX_PATHS)), "by_path_truncated": len(by_path) > MAX_PATHS,
            "entries": entries[:MAX_NETWORK], "entries_truncated": len(entries) > MAX_NETWORK}


class StaticHandler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".mjs": "text/javascript", ".js": "text/javascript",
                      ".json": "application/json", ".wasm": "application/wasm", ".svg": "image/svg+xml"}

    def log_message(self, *args):  # the report carries the evidence; nothing may spam the log
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


# --- server -----------------------------------------------------------------------------------------------------

class Server:
    def __init__(self, serve):
        self.serve, self.proc, self.httpd, self.log = serve, None, None, bytearray()

    def start(self):
        port = self.serve["port"]
        if self.serve["kind"] == "static":
            root = os.path.realpath(os.path.join(WORK, self.serve["root"]))
            if not (root == os.path.realpath(WORK) or root.startswith(os.path.realpath(WORK) + os.sep)) or not os.path.isdir(root):
                raise RuntimeError("the static root does not exist in the tested tree")
            self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(StaticHandler, directory=root))
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        else:
            self.proc = subprocess.Popen(self.serve["argv"], cwd=WORK, env=_clean_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, start_new_session=True)
            threading.Thread(target=self._pump, daemon=True).start()
        self._wait_ready()

    def _pump(self):
        for chunk in iter(lambda: self.proc.stdout.read(4096), b""):
            if len(self.log) < SERVER_LOG_BYTES:
                self.log += chunk[:SERVER_LOG_BYTES - len(self.log)]

    def _wait_ready(self):
        deadline = time.monotonic() + self.serve["ready_timeout"]
        url_path = self.serve["ready_path"]
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(f"the application exited with status {self.proc.returncode} before it was ready")
            try:
                with socket.create_connection(("127.0.0.1", self.serve["port"]), timeout=1) as conn:
                    conn.sendall(f"GET {url_path} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n".encode())
                    head = conn.recv(64).decode("latin-1", "replace")
                    if head.startswith("HTTP/") and int(head.split()[1]) < 500:
                        return
            except (OSError, ValueError, IndexError):
                pass
            time.sleep(0.25)
        raise RuntimeError(f"the application did not answer {url_path} within {self.serve['ready_timeout']}s")

    def stop(self):
        if self.httpd is not None:
            self.httpd.shutdown()
        if self.proc is not None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                pass


def _clean_env():
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": "/tmp/home", "TMPDIR": "/tmp", "LANG": "C.UTF-8",
            "NODE_ENV": "test", "CI": "1", "NO_COLOR": "1"}


# --- browser run -------------------------------------------------------------------------------------------------

class Run:
    def __init__(self, spec, scenario, port):
        self.spec, self.scenario, self.port = spec, scenario, port
        self.origin = f"http://127.0.0.1:{port}"
        self.network, self.console, self.page_errors, self.step_results = [], [], [], []
        self.snapshots, self.artifacts, self.events = [], [], []
        self.artifact_bytes, self.baseline = 0, 0
        self.by_request = {}

    # evidence hooks ------------------------------------------------------------------------------------------
    def on_route(self, route, request):
        url, method = request.url, request.method
        entry = {"method": method, "path": request_path(url), "type": request.resource_type, "mocked": False, "blocked": False}
        if not origin_allowed(url, self.port):
            entry["blocked"] = True
            entry["path"] = "[blocked] " + (urllib.parse.urlsplit(url).netloc or url)[:120]
            self.network.append(entry)
            route.abort("blockedbyclient")
            return
        self.network.append(entry)
        self.by_request[id(request)] = entry
        mock = match_mock(self.scenario["mocks"], method, url)
        if mock is None:
            route.continue_()
            return
        entry["mocked"] = True
        entry["status"] = mock["status"]
        if mock["delay_ms"]:
            # Not time.sleep: that freezes Playwright's dispatcher, so the "slow" response would block every other step.
            request.frame.page.wait_for_timeout(mock["delay_ms"])
        body = json.dumps(mock["json"]) if "json" in mock else mock["body"]
        route.fulfill(status=mock["status"], body=body, content_type=mock["content_type"], headers={"Cache-Control": "no-store"})

    def on_response(self, response):
        entry = self.by_request.get(id(response.request))
        if entry is not None and "status" not in entry:
            entry["status"] = response.status

    def on_websocket(self, socket):
        local = origin_allowed(socket.url, self.port)
        entry = {"method": "WS", "path": (request_path(socket.url) if local else "[no route] " + (urllib.parse.urlsplit(socket.url).netloc or socket.url)[:120]),
                 "type": "websocket", "mocked": False, "blocked": not local}
        self.network.append(entry)
        socket.on("socketerror", lambda error: entry.__setitem__("failed", str(error)[:80]))

    def on_failed(self, request):
        entry = self.by_request.get(id(request))
        if entry is not None:
            entry["failed"] = (request.failure or "failed")[:80]

    def on_console(self, message):
        location = message.location or {}
        self.console.append({"type": message.type, "text": message.text[:300], "at": request_path(location.get("url", ""), 120) if location.get("url") else None,
                             "line": location.get("lineNumber")})

    def on_page_error(self, error):
        self.page_errors.append(str(error)[:300])

    # artifacts -----------------------------------------------------------------------------------------------
    def add_artifact(self, name, kind, data):
        if len(data) > MAX_ARTIFACT_BYTES or self.artifact_bytes + len(data) > MAX_TOTAL_ARTIFACT_BYTES:
            self.events.append({"artifact_omitted": name, "reason": "size", "bytes": len(data)})
            return False
        self.artifact_bytes += len(data)
        self.artifacts.append({"name": name, "kind": kind, "b64": base64.b64encode(data).decode(), "bytes": len(data)})
        return True

    # snapshots -----------------------------------------------------------------------------------------------
    def take_snapshot(self, page, selector=None, index=None):
        target = page.locator(selector or "body").first
        try:
            aria = target.aria_snapshot()
        except Exception as exc:
            aria = "(unavailable: " + str(exc)[:100] + ")"
        try:
            text = target.inner_text(timeout=2000)
        except Exception:
            text = ""
        snap = {"step": index, "url": request_path(page.url), "title": page.title()[:200], "selector": selector or "body",
                "aria": aria[:MAX_SNAPSHOT_CHARS], "aria_truncated": len(aria) > MAX_SNAPSHOT_CHARS,
                "text": text[:MAX_TEXT_CHARS], "text_truncated": len(text) > MAX_TEXT_CHARS}
        self.snapshots.append(snap)
        return snap


def execute_step(run, page, step, index, expect):
    op, timeout = step["op"], step.get("timeout_ms", 5000)
    page.set_default_timeout(timeout)
    if op == "goto":
        response = page.goto(run.origin + step["path"], wait_until=step.get("wait_until", "load"))
        return {"status": response.status if response else None, "url": request_path(page.url)}
    if op == "click":
        page.locator(step["selector"]).click()
    elif op == "hover":
        page.locator(step["selector"]).hover()
    elif op == "fill":
        page.locator(step["selector"]).fill(step["text"])
    elif op == "type":
        page.locator(step["selector"]).press_sequentially(step["text"], delay=step.get("delay_ms", 0))
    elif op == "press":
        (page.locator(step["selector"]).press(step["key"]) if "selector" in step else page.keyboard.press(step["key"]))
    elif op == "select":
        page.locator(step["selector"]).select_option(step["value"])
    elif op == "check":
        page.locator(step["selector"]).check()
    elif op == "uncheck":
        page.locator(step["selector"]).uncheck()
    elif op == "wait_for":
        (page.locator(step["selector"]).first if "selector" in step else page.get_by_text(step["text"]).first).wait_for(state="visible")
    elif op == "wait_ms":
        page.wait_for_timeout(step["ms"])
    elif op == "screenshot":
        kind = step.get("format", "png")
        options = {"type": kind}
        if kind == "jpeg":
            options["quality"] = 80
        data = (page.locator(step["selector"]).screenshot(**options) if "selector" in step
                else page.screenshot(full_page=step.get("full_page", False), **options))
        stored = run.add_artifact(step["name"] + (".jpg" if kind == "jpeg" else ".png"), "screenshot", data)
        return {"artifact": step["name"], "bytes": len(data), "stored": stored}
    elif op == "snapshot":
        snap = run.take_snapshot(page, step.get("selector"), index)
        return {"title": snap["title"], "url": snap["url"]}
    elif op == "assert_visible":
        page.locator(step["selector"]).first.wait_for(state="visible")
    elif op == "assert_hidden":
        page.locator(step["selector"]).first.wait_for(state="hidden")
    elif op == "assert_text":
        expect(page.locator(step["selector"]).first).to_contain_text(step["text"], timeout=timeout)
    elif op == "assert_count":
        locator, wanted, kind = page.locator(step["selector"]), step["count"], step.get("compare", "eq")
        deadline = time.monotonic() + timeout / 1000
        while True:
            actual = locator.count()
            if compare(kind, actual, wanted) or time.monotonic() > deadline:
                break
            time.sleep(0.1)
        if not compare(kind, actual, wanted):
            raise AssertionError(f"expected count {kind} {wanted}, found {actual}")
        return {"count": actual}
    elif op == "assert_requests":
        actual = count_requests(run.network, step["path"], step.get("method"), run.baseline)
        kind = step.get("compare", "eq")
        if not compare(kind, actual, step["count"]):
            raise AssertionError(f"expected {kind} {step['count']} request(s) to {step['path']} since the last reset, observed {actual}")
        return {"requests": actual}
    elif op == "wait_requests":
        # Race-free alternative to sleeping: wait until the application has *issued* at least `count` requests to the path.
        deadline = time.monotonic() + timeout / 1000
        while count_requests(run.network, step["path"], step.get("method"), run.baseline) < step["count"]:
            if time.monotonic() > deadline:
                raise AssertionError(f"fewer than {step['count']} request(s) to {step['path']} were issued within {timeout} ms")
            page.wait_for_timeout(50)
        return {"requests": count_requests(run.network, step["path"], step.get("method"), run.baseline)}
    elif op == "reset_network_counts":
        run.baseline = len(run.network)
    return {}


def _emergency(run, report, total_steps):
    """A hang inside the browser stack must still leave evidence: write what exists, mark the rest skipped, exit."""
    try:
        done = len(run.step_results)
        steps = list(run.step_results) + [{"index": i, "op": "?", "ok": None, "skipped": True, "reason": "the run hung and was stopped"}
                                          for i in range(done, total_steps)]
        counts = collections.Counter(item["type"] for item in list(run.console))
        report.update({"steps": steps, "steps_total": total_steps, "timed_out": True,
                       "console": {"messages": list(run.console)[:MAX_CONSOLE], "counts": dict(counts), "truncated": False},
                       "page_errors": list(run.page_errors)[:MAX_PAGE_ERRORS], "page_errors_total": len(run.page_errors),
                       "network": summarize_network(list(run.network)), "snapshots": list(run.snapshots)[-6:],
                       "events": list(run.events)[:20] + [{"watchdog": "the browser driver hung and was stopped"}],
                       "artifacts": list(run.artifacts)})
        sys.stdout.write("\n" + MARK + json.dumps(report, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    finally:
        os._exit(0)


def run_browser(spec, report):
    from playwright.sync_api import expect, sync_playwright

    serve = browser_spec.validate_serve(spec["serve"])
    scenario = browser_spec.validate_scenario(spec["scenario"], max_steps=browser_spec.MAX_SESSION_STEPS)
    run = Run(spec, scenario, serve["port"])
    watchdog = threading.Timer(max(spec["timeout"] - 3, 2), _emergency, args=(run, report, len(scenario["steps"])))
    watchdog.daemon = True
    watchdog.start()
    server = Server(serve)
    report["serve"] = {"kind": serve["kind"], "port": serve["port"], "ready_path": serve["ready_path"]}
    started = time.monotonic()
    deadline = started + spec["timeout"] - 5
    try:
        try:
            server.start()
        except Exception as exc:
            report["error"] = {"stage": "startup", "message": str(exc)[:300]}
            return
        report["serve"]["ready_seconds"] = round(time.monotonic() - started, 3)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--disable-dev-shm-usage", "--disable-gpu", "--no-first-run",
                                               "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"])
            report["browser"] = {"name": "chromium", "version": browser.version, "viewport": scenario["viewport"],
                                 "locale": scenario["locale"], "timezone": scenario["timezone"], "color_scheme": scenario["color_scheme"]}
            context = browser.new_context(viewport=scenario["viewport"], locale=scenario["locale"], timezone_id=scenario["timezone"],
                                          color_scheme=scenario["color_scheme"], service_workers="block", accept_downloads=False,
                                          device_scale_factor=1, ignore_https_errors=False, permissions=[])
            context.route("**/*", run.on_route)

            # WebSockets are not routed: closing one from inside Playwright's route handler deadlocks the sync API. The
            # container has no network route and Chromium resolves nothing but 127.0.0.1, so an external socket cannot
            # connect; the attempt is recorded for the evidence instead.
            context.on("websocket", run.on_websocket)
            context.on("response", run.on_response)
            context.on("requestfailed", run.on_failed)
            if scenario["trace"]:
                context.tracing.start(screenshots=True, snapshots=True, sources=False)
            page = context.new_page()
            page.on("console", run.on_console)
            page.on("pageerror", run.on_page_error)
            page.on("dialog", lambda dialog: (run.events.append({"dialog": dialog.type, "message": dialog.message[:120]}), dialog.dismiss()))
            context.on("page", lambda other: None if other == page else (run.events.append({"popup_closed": True}), other.close()))
            failed = False
            replay = scenario.get("replay", 0)
            for index, step in enumerate(scenario["steps"]):
                if failed or time.monotonic() > deadline:
                    run.step_results.append({"index": index, "op": step["op"], "ok": None, "skipped": True,
                                             "reason": "an earlier step failed" if failed else "the run deadline was reached"})
                    report["timed_out"] = report.get("timed_out", False) or (not failed)
                    continue
                began = time.monotonic()
                result = {"index": index, "op": step["op"], "ok": True}
                replaying = index < replay
                if replaying:
                    result["replayed"] = True
                try:
                    if replaying and step["op"] in ("screenshot", "snapshot"):
                        detail = {}  # captured when the step first ran; replay only rebuilds the state
                    else:
                        detail = execute_step(run, page, step, index, expect)
                    result.update(detail)
                except Exception as exc:
                    result["ok"] = False
                    result["error"] = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"[:300]
                    result["assertion"] = step["op"].startswith("assert_")
                    failed = True
                result["ms"] = round((time.monotonic() - began) * 1000)
                run.step_results.append(result)
                if replay and index == replay - 1 and not failed:
                    # Evidence must describe only the new steps: drop what the replay produced.
                    replay_noise = {"network_requests": len(run.network), "console_messages": len(run.console), "page_errors": len(run.page_errors)}
                    report["replay"] = replay_noise
                    run.network.clear()
                    run.console.clear()
                    run.page_errors.clear()
                    run.by_request.clear()
                    run.baseline = 0
            try:
                page.set_default_timeout(3000)
                if failed:
                    data = page.screenshot(type="png")
                    run.add_artifact("failure.png", "screenshot", data)
                run.take_snapshot(page, None, "final")
            except Exception as exc:
                run.events.append({"final_capture_failed": str(exc)[:120]})
            if scenario["trace"]:
                trace_path = "/tmp/gp/trace.zip"
                try:
                    context.tracing.stop(path=trace_path)
                    with open(trace_path, "rb") as handle:
                        blob = handle.read(MAX_ARTIFACT_BYTES + 1)
                    run.add_artifact("trace.zip", "trace", blob) if len(blob) <= MAX_ARTIFACT_BYTES else run.events.append({"artifact_omitted": "trace.zip", "reason": "size"})
                except Exception as exc:
                    run.events.append({"trace_failed": str(exc)[:120]})
            context.close()
            browser.close()
    finally:
        watchdog.cancel()
        server.stop()
        if server.log:
            run.add_artifact("server.log", "log", bytes(server.log))
        counts = collections.Counter(item["type"] for item in run.console)
        report.update({
            "steps": run.step_results, "steps_total": len(scenario["steps"]),
            "console": {"messages": run.console[:MAX_CONSOLE], "counts": dict(counts), "truncated": len(run.console) > MAX_CONSOLE},
            "page_errors": run.page_errors[:MAX_PAGE_ERRORS], "page_errors_total": len(run.page_errors),
            "network": summarize_network(run.network), "snapshots": run.snapshots[-6:], "events": run.events[:20],
            "artifacts": run.artifacts, "duration_seconds": round(time.monotonic() - started, 3)})


def main():
    with open(os.path.join(harness.INPUT, "job.json"), "r", encoding="utf-8") as handle:
        spec = json.load(handle)
    os.makedirs("/tmp/gp", exist_ok=True)
    os.makedirs("/tmp/home", exist_ok=True)
    os.environ.update(HOME="/tmp/home", TMPDIR="/tmp", LANG="C.UTF-8")
    report = {"harness": 1, "kind": "browser", "extract": {"ok": False}}
    try:
        files, size, skipped = harness._extract(spec["limits"]["files"], spec["limits"]["bytes"])
        report["extract"] = {"ok": True, "members": files, "bytes": size, "skipped_unsafe_links": skipped[:20],
                             "skipped_unsafe_link_count": len(skipped)}
    except Exception as exc:
        report["error"] = {"stage": "extract", "message": f"{type(exc).__name__}: {exc}"[:400]}
    else:
        try:
            run_browser(spec, report)
        except Exception as exc:  # a driver defect must still produce a report, never a silent exit
            report["error"] = report.get("error") or {"stage": "driver", "message": f"{type(exc).__name__}: {str(exc)[:300]}"}
    sys.stdout.write("\n" + MARK + json.dumps(report, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
