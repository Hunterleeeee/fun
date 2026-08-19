from __future__ import annotations

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import FunConfig


class DashboardData:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()

    def snapshot(self) -> dict[str, Any]:
        config_path = self.db_path.parent / "config.json"
        config = {}
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                config = {}
        # Read credentials through the same loader as the CLI.  A marker saying
        # "macos-keychain" is not proof the Keychain is currently readable, and
        # the dashboard must not claim ready while Runtime starts offline.
        try:
            loaded = FunConfig.load(config_path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            loaded = FunConfig()
        setup = {
            "configured": loaded.ready(),
            "needs_env": bool((config.get("api_key_env") or loaded.keychain_unreadable) and not loaded.api_key),
            "keychain_unreadable": loaded.keychain_unreadable,
            "config_path": str(config_path),
        }
        if not self.db_path.exists():
            return {"sessions": 0, "tasks": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "tool_calls": 0, "completed": 0, "failed": 0, "stopped": 0, "session_usage": [], "recent": [], "background_tasks": [], "setup": setup}
        connection = sqlite3.connect(self.db_path)
        try:
            rows = connection.execute("SELECT type, session_id, task_id, timestamp, payload FROM events ORDER BY seq").fetchall()
        except sqlite3.Error:
            # A zero-byte or half-written events.db is a normal state on a first
            # run; it is not a 500.
            rows = []
        finally:
            connection.close()
        sessions = {row[1] for row in rows}
        tasks = {row[2] for row in rows if row[2]}
        totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        counts = {"tool_calls": 0, "completed": 0, "failed": 0, "stopped": 0}
        by_session: dict[str, dict[str, Any]] = {
            session_id: {"session_id": session_id, "tasks": set(), "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "tool_calls": 0, "last_activity": ""}
            for session_id in sessions
        }
        recent: list[dict[str, Any]] = []
        background: dict[str, dict[str, Any]] = {}
        for event_type, session_id, task_id, timestamp, raw in rows:
            # Every shape below has been seen in a real events.db: a NULL
            # column, a payload that is a list, and `"usage": null`, which many
            # OpenAI-compatible providers stream.  One of them used to take the
            # whole endpoint down with a 500 until the row was deleted by hand.
            try:
                payload = json.loads(raw) if isinstance(raw, str) else {}
            except (json.JSONDecodeError, TypeError, ValueError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            timestamp = timestamp if isinstance(timestamp, str) else ""
            session = by_session[session_id]
            if task_id:
                session["tasks"].add(task_id)
            session["last_activity"] = timestamp
            if event_type == "model.completed":
                # ``model.completed`` carries the session's *cumulative* usage,
                # not that turn's delta, so the latest one per session is the
                # answer.  Adding them up made the numbers grow triangularly:
                # ten turns of 100 tokens reported 5 500.
                usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
                for key, alias in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"), ("total_tokens", "total_tokens")):
                    value = usage.get(key, usage.get(alias))
                    if isinstance(value, int) and not isinstance(value, bool):
                        session[key] = value
                if not isinstance(usage.get("total_tokens"), int) and isinstance(session["input_tokens"], int) and isinstance(session["output_tokens"], int):
                    session["total_tokens"] = session["input_tokens"] + session["output_tokens"]
            elif event_type == "model.tool_call":
                counts["tool_calls"] += 1
                session["tool_calls"] += 1
            elif event_type == "task.completed":
                counts["completed"] += 1
            elif event_type == "task.failed":
                counts["failed"] += 1
            elif event_type == "task.stopped":
                counts["stopped"] += 1
            if event_type.startswith("background.task."):
                background_id = payload.get("background_task_id") or task_id
                if not isinstance(background_id, str) or not background_id:
                    continue
                item = background.setdefault(background_id, {"task_id": background_id, "status": "created"})
                item["status"] = event_type.rsplit(".", 1)[-1].replace("cancel_requested", "cancelling")
                for key in ("goal", "kind", "parent_task_id", "run_id", "result", "error"):
                    if key in payload:
                        item[key] = payload[key]
            if event_type in {"task.created", "task.completed", "task.failed", "recovery.required"}:
                recent.append({"type": event_type, "session_id": session_id, "task_id": task_id, "timestamp": timestamp, "goal": payload.get("goal"), "reason": payload.get("reason")})
        # Session totals are snapshots, so the grand total is their sum — taken
        # once here rather than accumulated per event.
        for item in by_session.values():
            for key in totals:
                totals[key] += item[key]
        session_rows = []
        for item in by_session.values():
            item = dict(item)
            item["tasks"] = len(item["tasks"])
            session_rows.append(item)
        session_rows.sort(key=lambda item: item["last_activity"] or "", reverse=True)
        return {"sessions": len(sessions), "tasks": len(tasks), **totals, **counts, "session_usage": session_rows[:20], "recent": recent[-20:][::-1], "background_tasks": list(background.values()), "setup": setup}


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Fun · Local Dashboard</title><style>
:root{color-scheme:dark;--bg:#0b1020;--card:#121a2d;--muted:#8d9ab5;--accent:#9ccaff;--line:#26334f}*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#0b1020,#111b32);color:#edf4ff;font:15px ui-sans-serif,system-ui,-apple-system,sans-serif}main{max-width:1100px;margin:0 auto;padding:42px 22px}.eyebrow{color:var(--accent);letter-spacing:.14em;text-transform:uppercase;font-size:12px}h1{font-size:42px;margin:8px 0}p{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin:28px 0}.card{background:rgba(18,26,45,.9);border:1px solid var(--line);border-radius:16px;padding:20px}.label{color:var(--muted);font-size:13px}.value{font-size:30px;margin-top:8px;color:#fff}.wide{grid-column:1/-1}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px 8px;border-bottom:1px solid var(--line);font-size:13px}th{color:var(--muted)}code{color:var(--accent)}footer{color:var(--muted);font-size:12px;margin-top:30px}@media(max-width:600px){h1{font-size:32px}}
</style></head><body><main><div class="eyebrow">Fun · local only</div><h1>Runtime overview</h1><p>Private usage metrics from your local Event Store. Nothing is uploaded.</p><section class="card wide" id="setup"></section><section class="grid" id="cards"></section><section class="card wide"><h2>Session usage</h2><table><thead><tr><th>Session</th><th>Tasks</th><th>Input</th><th>Output</th><th>Total</th><th>Tools</th></tr></thead><tbody id="sessions"></tbody></table></section><section class="card wide"><h2>Background agents</h2><table><thead><tr><th>Task</th><th>Kind</th><th>Status</th><th>Parent</th><th>Goal</th></tr></thead><tbody id="background"></tbody></table></section><section class="card wide"><h2>Recent activity</h2><table><thead><tr><th>Time</th><th>Event</th><th>Task</th><th>Details</th></tr></thead><tbody id="recent"></tbody></table></section><footer>Bound to 127.0.0.1 · refreshes every 5 seconds</footer></main><script>
// Every value below reaches this page from the event store, which means it can
// contain whatever the model wrote or whatever a file the model read told it to
// write.  So nothing here is ever interpolated into innerHTML: cells are built
// as nodes and filled with textContent, which cannot execute.
const labels={sessions:'Sessions',tasks:'Tasks',input_tokens:'Input tokens',output_tokens:'Output tokens',total_tokens:'Total tokens',tool_calls:'Tool calls',completed:'Completed',failed:'Failed',stopped:'Stopped'};
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined&&text!==null)n.textContent=String(text);if(cls)n.className=cls;return n}
function cell(value,mono){const td=document.createElement('td');if(mono){const c=document.createElement('code');c.textContent=String(value);td.appendChild(c)}else{td.textContent=String(value)}return td}
function row(cells){const tr=document.createElement('tr');for(const c of cells)tr.appendChild(c);return tr}
function empty(span,text){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=span;td.textContent=text;tr.appendChild(td);return tr}
function fill(id,nodes,span,text){const host=document.querySelector(id);host.replaceChildren();if(!nodes.length){host.appendChild(empty(span,text));return}for(const n of nodes)host.appendChild(n)}
function setup(d){const host=document.querySelector('#setup');host.replaceChildren();
 if(d.setup.configured){host.appendChild(el('strong','\u2713 Provider configured'));host.appendChild(el('p','Fun is ready to run model tasks.'))}
 else if(d.setup.needs_env){host.appendChild(el('strong',d.setup.keychain_unreadable?'API key unavailable':'API key not stored'));host.appendChild(el('p',d.setup.keychain_unreadable?'The saved Keychain credential could not be read. Unlock or allow Keychain access, export FUN_API_KEY, or run fun --configure again.':'The key could not be saved to the Keychain. Export FUN_API_KEY before each run, or run fun --configure again.'))}
 else{host.appendChild(el('strong','First run setup'));host.appendChild(el('p','Provider is not configured. Run fun --configure, or export FUN_API_URL, FUN_API_KEY and FUN_MODEL.'))}}
async function load(){const d=await fetch('/api/summary').then(r=>r.json());setup(d);
 const cards=document.querySelector('#cards');cards.replaceChildren();
 for(const k of Object.keys(labels)){const card=el('div',null,'card');card.appendChild(el('div',labels[k],'label'));card.appendChild(el('div',d[k]??0,'value'));cards.appendChild(card)}
 fill('#sessions',(d.session_usage||[]).map(x=>row([cell(x.session_id,true),cell(x.tasks),cell(x.input_tokens),cell(x.output_tokens),cell(x.total_tokens),cell(x.tool_calls)])),6,'No sessions yet.');
 fill('#background',(d.background_tasks||[]).map(x=>row([cell(x.task_id,true),cell(x.kind||'\u2014'),cell(x.status),cell(x.parent_task_id||'\u2014'),cell(x.goal||'\u2014')])),5,'No background agents yet.');
 fill('#recent',(d.recent||[]).map(x=>row([cell(new Date(x.timestamp).toLocaleString()),cell(x.type,true),cell(x.task_id||'\u2014'),cell(x.goal||x.reason||'\u2014')])),4,'No activity yet.')}
load();setInterval(load,5000);
</script></body></html>"""


def serve(db_path: str | Path, port: int = 8765) -> None:
    data = DashboardData(db_path)

    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}", "127.0.0.1", "localhost", "[::1]"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            # Binding to 127.0.0.1 stops other machines connecting, but not a
            # web page the user is visiting from resolving its own hostname to
            # 127.0.0.1 and reading this origin.  Checking Host is what closes
            # that, and it costs nothing for a real local request.
            host = (self.headers.get("Host") or "").strip().lower()
            if host not in allowed_hosts:
                self.send_error(403, "FORBIDDEN_HOST")
                return
            if self.path == "/api/summary":
                body = json.dumps(data.snapshot()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Content-Type-Options", "nosniff")
            elif self.path in {"/", "/index.html"}:
                body = HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            else:
                self.send_error(404)
                return
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Fun dashboard: http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
