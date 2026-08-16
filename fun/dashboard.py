from __future__ import annotations

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class DashboardData:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()

    def snapshot(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return {"sessions": 0, "tasks": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "tool_calls": 0, "completed": 0, "failed": 0, "stopped": 0, "session_usage": [], "recent": []}
        connection = sqlite3.connect(self.db_path)
        rows = connection.execute("SELECT type, session_id, task_id, timestamp, payload FROM events ORDER BY seq").fetchall()
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
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
            session = by_session[session_id]
            if task_id:
                session["tasks"].add(task_id)
            session["last_activity"] = timestamp
            if event_type == "model.completed":
                usage = payload.get("usage", {})
                aliases = {"input_tokens": "prompt_tokens", "output_tokens": "completion_tokens", "total_tokens": "total_tokens"}
                for key, alias in aliases.items():
                    value = usage.get(key, usage.get(alias))
                    if isinstance(value, int):
                        totals[key] += value
                        session[key] += value
                if "total_tokens" not in usage and isinstance(usage.get("prompt_tokens"), int) and isinstance(usage.get("completion_tokens"), int):
                    derived = usage["prompt_tokens"] + usage["completion_tokens"]
                    totals["total_tokens"] += derived
                    session["total_tokens"] += derived
            elif event_type == "model.tool_call":
                counts["tool_calls"] += 1
                session["tool_calls"] += 1
            elif event_type == "task.completed":
                counts["completed"] += 1
            elif event_type == "task.failed":
                counts["failed"] += 1
            elif event_type == "task.stopped":
                counts["stopped"] += 1
            if event_type.startswith("background.task.") and task_id:
                item = background.setdefault(task_id, {"task_id": task_id, "status": "created"})
                item["status"] = event_type.rsplit(".", 1)[-1].replace("cancel_requested", "cancelling")
                for key in ("goal", "kind", "parent_task_id", "run_id", "result", "error"):
                    if key in payload:
                        item[key] = payload[key]
            if event_type in {"task.created", "task.completed", "task.failed", "recovery.required"}:
                recent.append({"type": event_type, "session_id": session_id, "task_id": task_id, "timestamp": timestamp, "goal": payload.get("goal"), "reason": payload.get("reason")})
        session_rows = []
        for item in by_session.values():
            item = dict(item)
            item["tasks"] = len(item["tasks"])
            session_rows.append(item)
        session_rows.sort(key=lambda item: item["last_activity"], reverse=True)
        return {"sessions": len(sessions), "tasks": len(tasks), **totals, **counts, "session_usage": session_rows[:20], "recent": recent[-20:][::-1], "background_tasks": list(background.values())}


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fun · Local Dashboard</title><style>
:root{color-scheme:dark;--bg:#0b1020;--card:#121a2d;--muted:#8d9ab5;--accent:#9ccaff;--line:#26334f}*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#0b1020,#111b32);color:#edf4ff;font:15px ui-sans-serif,system-ui,-apple-system,sans-serif}main{max-width:1100px;margin:0 auto;padding:42px 22px}.eyebrow{color:var(--accent);letter-spacing:.14em;text-transform:uppercase;font-size:12px}h1{font-size:42px;margin:8px 0}p{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin:28px 0}.card{background:rgba(18,26,45,.9);border:1px solid var(--line);border-radius:16px;padding:20px}.label{color:var(--muted);font-size:13px}.value{font-size:30px;margin-top:8px;color:#fff}.wide{grid-column:1/-1}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px 8px;border-bottom:1px solid var(--line);font-size:13px}th{color:var(--muted)}code{color:var(--accent)}footer{color:var(--muted);font-size:12px;margin-top:30px}@media(max-width:600px){h1{font-size:32px}}
</style></head><body><main><div class="eyebrow">Fun · local only</div><h1>Runtime overview</h1><p>Private usage metrics from your local Event Store. Nothing is uploaded.</p><section class="grid" id="cards"></section><section class="card wide"><h2>Session usage</h2><table><thead><tr><th>Session</th><th>Tasks</th><th>Input</th><th>Output</th><th>Total</th><th>Tools</th></tr></thead><tbody id="sessions"></tbody></table></section><section class="card wide"><h2>Background agents</h2><table><thead><tr><th>Task</th><th>Kind</th><th>Status</th><th>Parent</th><th>Goal</th></tr></thead><tbody id="background"></tbody></table></section><section class="card wide"><h2>Recent activity</h2><table><thead><tr><th>Time</th><th>Event</th><th>Task</th><th>Details</th></tr></thead><tbody id="recent"></tbody></table></section><footer>Bound to 127.0.0.1 · refreshes every 5 seconds</footer></main><script>
const labels={sessions:'Sessions',tasks:'Tasks',input_tokens:'Input tokens',output_tokens:'Output tokens',total_tokens:'Total tokens',tool_calls:'Tool calls',completed:'Completed',failed:'Failed',stopped:'Stopped'};
async function load(){const d=await fetch('/api/summary').then(r=>r.json());document.querySelector('#cards').innerHTML=Object.keys(labels).map(k=>`<div class="card"><div class="label">${labels[k]}</div><div class="value">${d[k]??0}</div></div>`).join('');document.querySelector('#sessions').innerHTML=d.session_usage.map(x=>`<tr><td><code>${x.session_id}</code></td><td>${x.tasks}</td><td>${x.input_tokens}</td><td>${x.output_tokens}</td><td>${x.total_tokens}</td><td>${x.tool_calls}</td></tr>`).join('')||'<tr><td colspan="6">No sessions yet.</td></tr>';document.querySelector('#background').innerHTML=(d.background_tasks||[]).map(x=>`<tr><td><code>${x.task_id}</code></td><td>${x.kind||'—'}</td><td>${x.status}</td><td>${x.parent_task_id||'—'}</td><td>${x.goal||'—'}</td></tr>`).join('')||'<tr><td colspan="5">No background agents yet.</td></tr>';document.querySelector('#recent').innerHTML=d.recent.map(x=>`<tr><td>${new Date(x.timestamp).toLocaleString()}</td><td><code>${x.type}</code></td><td>${x.task_id||'—'}</td><td>${x.goal||x.reason||'—'}</td></tr>`).join('')||'<tr><td colspan="4">No activity yet.</td></tr>'}load();setInterval(load,5000);
</script></body></html>"""


def serve(db_path: str | Path, port: int = 8765) -> None:
    data = DashboardData(db_path)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/summary":
                body = json.dumps(data.snapshot()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
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
