"""Re-run every reproduction from the audit against the current code."""
import io, os, json, subprocess, sys, tempfile, threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
sys.path.insert(0, os.getcwd())

ok = lambda label, cond, detail="": print(f"  {'PASS' if cond else 'FAIL':<4} {label}" + (f"  {detail}" if detail else ""))

print("P0-1 exec shell 绕过")
from fun.tools import Tools
from fun.policy import Policy, ApprovalMode, WorkspaceGuard, PolicyError
t = Tools(tempfile.mkdtemp(), Policy(mode=ApprovalMode.AUTO))
for cmd in ["bash -c 'echo P'", "bash -lc 'echo P'", "env bash -c 'echo P'", "sh -lc 'echo P'",
            "env A=1 bash -c 'echo P'", "timeout 5 bash -c 'echo P'", "xargs -a /dev/null rm -rf /tmp/x",
            "nohup nice env sudo rm -rf /", "python3 -c 'import os'", "cat ../../etc/passwd"]:
    r = t.exec(cmd); ok(f"拒绝 {cmd}", not r.ok, r.text[:28])
ok("echo hello 仍可运行", t.exec("echo hello").text == "hello")
ok("ls 仍可运行", t.exec("ls").ok)

print("\nP0-3 保护名大小写")
with TemporaryDirectory() as d:
    g = WorkspaceGuard(d)
    for name in (".ENV", "Server.PEM", "ID_RSA"):
        try: g.check_name(Path(d)/name); ok(f"{name} 被拦", False)
        except PolicyError: ok(f"{name} 被拦", True)

print("\nP1 必崩路径")
r = subprocess.run([sys.executable, "-m", "fun"], input="", capture_output=True, text=True, timeout=60)
ok("非 TTY 入口不再 NameError", "NameError" not in r.stderr and r.returncode == 0)
p = Policy(); p.set_mode("auto")
ok("/permissions 之后 .value 可读", p.mode.value == "auto")
from fun.commands import resolve_command_prefix, REGISTRY
ok("/cle 解析为 /clear", resolve_command_prefix("/cle", set(REGISTRY))[0] == "/clear")

from fun.ui.app import App
from fun.ui.stream import StreamSurface
from fun.ui.theme import Theme
PLAIN = Theme(mode="none")
app = App(StreamSurface(io.StringIO()), theme=PLAIN)
def submit(text):
    if text == "/config":
        app.open_form("Provider", ["base_url", ("api_key", True)], lambda v: None)
app._submit = submit
app._handle_key("palette", submit)
for ch in "config": app._handle_key(ch, submit)
app._handle_key("enter", submit)
ok("面板选 /config 打得开表单", app.modal is not None and app.modal.kind == "fields")

print("\nP2 Runtime")
from fun.runtime import Runtime, SMALL_TALK_PLAN
with TemporaryDirectory() as d:
    rt = Runtime(d, state_dir=d); rt.create_task("usage")
    for _ in range(6):
        rt.usage.merge_provider({"prompt_tokens": 100, "completion_tokens": 50})
        rt.emit("model.completed", rt.task.id, usage=rt.usage.as_dict())
    live = rt.usage.total_tokens; rt.close()
    rec = Runtime.recover(d, d, rt.session_id)
    ok("恢复后用量不翻倍", rec.usage.total_tokens == live, f"{live} -> {rec.usage.total_tokens}")
    rec.stop()
ok("'show' 不再被当成提问", tuple(Runtime._initial_plan("show")) == SMALL_TALK_PLAN)

from fun.ui.state import UiState
st = UiState(theme=PLAIN)
st.tool_status("tool.started", {"call_id": "c1", "name": "read"})
seen = []
with TemporaryDirectory() as d:
    rt = Runtime(d, "auto"); rt.create_task("t")
    rt.run_tool("read", on_status=lambda k, p: seen.append((k, p.get("call_id"))), call_id="c1", pathh="x")
ok("schema 失败也回调 on_status 且带 call_id", ("tool.failed", "c1") in seen, str(seen))

print("\nP2 凭据")
from fun.config import FunConfig, _keychain_set
with TemporaryDirectory() as d:
    path = Path(d)/"config.json"
    path.write_text('{"base_url":"https://e.test/v1","model":"m","api_key_store":"macos-keychain"}\n')
    with patch("fun.config._keychain_get", return_value=""):
        c = FunConfig.load(path); c.theme = "ember"; c.save(path)
    disk = json.loads(path.read_text())
    ok("钥匙串读不到时不毁掉 base_url", disk["base_url"] == "https://e.test/v1")
with patch("fun.config.shutil.which", return_value="/usr/bin/security"), patch("fun.config.subprocess.run") as run:
    from types import SimpleNamespace
    run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
    _keychain_set("sk-secret")
    ok("key 不经 argv", "sk-secret" not in run.call_args.args[0])

print("\nP2 Provider")
from fun.provider import OpenAICompatible, ModelConfig, ProviderError
class Resp:
    def __init__(self, body, ct="text/event-stream", n=3):
        self.body, self.headers, self.n = body, {"Content-Type": ct}, n
        self.status = 200
    def getcode(self): return 200
    def __iter__(self):
        for i in range(0, len(self.body) or 1, self.n):
            if self.body[i:i+self.n]: yield self.body[i:i+self.n]
    def __enter__(self): return self
    def __exit__(self, *a): return False
prov = OpenAICompatible(ModelConfig("https://x.test/v1", "sk-abcd1234", "m"))
def stream(body, ct="text/event-stream", n=3):
    with patch("fun.provider.urllib.request.urlopen", return_value=Resp(body, ct, n)):
        try: return list(prov.stream([{"role":"user","content":"hi"}]))
        except ProviderError as e: return f"ProviderError:{e.error_tag}"
ok("[DONE] 之后不再产出", stream(b'data: {"a":1}\n\ndata: [DONE]\n\ndata: {"evil":1}\n\n', n=4096) == [{"a":1,"_meta":{"ttft_ms":0}}])
ok("跨块 CJK 不损坏", stream('data: {"t":"你好"}\n\ndata: [DONE]\n\n'.encode(), n=1)[0]["t"] == "你好")
ok("200+JSON 错误体被识别", stream(b'{"error":{"message":"Invalid API key"}}', "") == "ProviderError:PROVIDER_AUTH_FAILED")

print("\nP3 渲染")
from fun.ui.text import truncate, display_width, strip_ansi
from fun.ui.screen import DockWriter
c = Theme(mode="truecolor")
ok("truncate 关掉 reverse", truncate(c.style(" Review ", "accent", reverse=True), 6).endswith("\x1b[0m"))
s2 = UiState(theme=PLAIN)
ok("标签行被裁宽", all(display_width(strip_ansi(l)) <= 20 for l in s2.dock_lines(20)))
s2.add_user("hi")
ok("每帧行数精确", all(len(s2.compose(60, h)) == h for h in range(1, 30)))
ok("矮终端仍有输入框", all(any("▌" in strip_ansi(l) for l in s2.compose(60, h)) for h in (2,4,6,8)))
from fun.ui.layout import frame_canvas
long_ws = "/Users/x/Development/clients/acme/services/api-gateway/worker"
ok("边框不超宽", all(display_width(strip_ansi(l)) == 80 for l in frame_canvas(PLAIN, ["b"], 80, 6, session="s", workspace=long_ws, mode="Build", approval="smart", version="v1")))
w = DockWriter(io.StringIO()); w.draw(["1","2","3","4"]); w.place_cursor(1, 0)
buf = w.output; buf.truncate(0); buf.seek(0); w.draw(["1","2","3","X"])
ok("重绘不吃 scrollback", buf.getvalue().count("\033[F") == 1, f"{buf.getvalue().count(chr(27)+'[F')} 次上移")

print("\nP4 死代码")
src = open("fun/ui/app.py").read()
ok("_dirty 现在会被读", "not self._dirty and not force" in src)
ok("renderer.py 已删除", not Path("fun/renderer.py").exists())
ok("_suggestion_index 已删除", "_suggestion_index" not in src)
from fun.ui.editor import Editor
e = Editor(); e.set("hello world"); e.cursor = 11; e.kill_word_left(); e.kill_to_end()
ok("空 kill 不清 kill ring", e.killed == "world")
st3 = UiState(theme=PLAIN); st3.add_command("/status")
ok("斜杠命令进历史", st3.composer_history == ["/status"])

print("\n=== 第二轮：新发现并已修的")
import time as _t
t2 = Tools(tempfile.mkdtemp(), Policy(mode=ApprovalMode.AUTO))
Path(t2.guard.root, "v").mkdir(exist_ok=True)
for cmd in ["awk 'BEGIN{system(\"id\")}'", "flock . -c 'id'", "env -C / cat etc/hostname",
            "env --chdir=/etc ls -d passwd", "rm -Rf v", "rm -fR v", "unshare rm -rf v",
            "vim -c '!id' /dev/null", "some-unknown-tool --go"]:
    ok(f"拒绝 {cmd[:38]}", not t2.exec(cmd).ok, t2.exec(cmd).text[:24])
ok("victim 目录还在", Path(t2.guard.root, "v").exists())
ok("echo/ls/pytest 仍可运行", t2.exec("echo hi").ok and t2.exec("ls").ok)

from fun.schema import validate_tool_arguments, SchemaError
def rejects(v):
    try: validate_tool_arguments("exec", {"command": "sleep 1", "timeout": v}); return False
    except SchemaError: return True
ok("timeout 拒绝 Infinity/NaN/bool", all(rejects(v) for v in (float("inf"), float("nan"), True, 1e300, 0, -1)))

from fun.ui.text import sanitize
ok("模型输出里的转义被剥掉", "\x1b" not in sanitize("a\x1b[2J\x1b]0;x\x07b") and sanitize("你好\t好") == "你好\t好")

from fun.ui import markdown as _md
from fun.ui.syntax import tokenize
s0 = _t.monotonic(); _md.wrap_segments([_md.Segment("a"*100000)], 80); wrap_ms = (_t.monotonic()-s0)*1000
s0 = _t.monotonic(); tokenize(" "*400000, "python"); tok_ms = (_t.monotonic()-s0)*1000
ok("长单词换行不再卡死", wrap_ms < 2000, f"{wrap_ms:.0f}ms（原 15300ms）")
ok("长同类 token 不再卡死", tok_ms < 2000, f"{tok_ms:.0f}ms（原 4250ms）")

from fun.ui.editor import Editor
e = Editor(); e.text = "alpha\nbeta \ngamma"; e.cursor = 11
ok("行尾空格不再把光标弹到左上角", e.visual_lines(40)[1:] == (1, 4), str(e.visual_lines(40)[1:]))

from fun.dashboard import DashboardData
from fun.persistence import SQLiteEventStore
from fun.events import Event
d3 = tempfile.mkdtemp(); dbp = Path(d3)/"events.db"
with SQLiteEventStore(dbp) as st:
    for turn in (1, 2, 3):
        st.append(Event("model.completed", "s", "t", {"usage": {"input_tokens": 100*turn, "output_tokens": 50*turn, "total_tokens": 150*turn}}))
snap = DashboardData(dbp).snapshot()
ok("dashboard 不再三角形累加", snap["total_tokens"] == 450, f"total={snap['total_tokens']}（原 900）")

st2 = UiState(theme=PLAIN); st2.add_user("x")
frame = st2.compose(40, 10, reserved=["超"*300]*200)
ok("overlay 也按宽度裁剪", max(display_width(strip_ansi(l)) for l in frame) <= 40 and len(frame) == 10)

import fun as _fun
from fun.telemetry import event_payload
ok("遥测版本号与包一致", event_payload(event="x", install="i")["fun_version"] == _fun.__version__)

from fun.ui.completion import detect, Completer
ok("光标在行首不再重复插入命令", detect("/hel", 0) is None)

print("\n=== 第三轮：新发现并已修的")
import threading as _th
from fun.ui.text import strip_ansi as _sa
st = UiState(theme=PLAIN)
for i in range(40): st.add_user(f"message {i}")
vis = lambda: [l for l in (_sa(r) for r in st.compose(70, 20)) if "message" in l]
ok("PgUp 能真的翻到历史", (st.scroll(-999), "message 0" in vis()[0])[1])
ok("PgDn 能回到最新", (st.scroll(999), "message 39" in vis()[-1])[1])

from fun.ui.app import App as _App, ApprovalRequest
from fun.policy import Risk as _Risk
a = _App(StreamSurface(io.StringIO()), theme=PLAIN)
a.state.tool_status("tool.started", {"call_id":"c1","name":"exec","arguments":{"command":"ls -la"}})
a.post("approval", ApprovalRequest("exec:ls", _Risk.MEDIUM, {"command":"ls -la"})); a._consume()
cards = [i.tool for i in a.state.transcript if i.tool]
ok("批准不再产生幽灵卡片", len(cards) == 1 and cards[0].arguments == {"command":"ls -la"})

import tempfile as _tf
from fun.runtime import Runtime as _RT
d = _tf.mkdtemp(); rt = _RT(d, "auto", state_dir=d); rt.create_task("t"); rt.complete("done")
try:
    rt.checkpoint("view"); ok("idle 时 /checkpoint 可用", True)
except Exception as e:
    ok("idle 时 /checkpoint 可用", False, str(e))
rt.shutdown()

d = _tf.mkdtemp(); rt = _RT(d, "auto", state_dir=d); rt.create_task("first"); rt.pause()
try:
    rt.create_task("second"); ok("暂停中新建任务被拒", False)
except RuntimeError as e:
    ok("暂停中新建任务被拒", str(e) == "TASK_PAUSED", str(e))
rt.stop()

from fun.provider import ProviderError as _PE
class _Fail:
    def stream(self, m, tools=None):
        raise _PE("PROVIDER_AUTH_FAILED")
        yield
d = _tf.mkdtemp(); rt = _RT(d, "auto", provider=_Fail(), state_dir=d); rt.create_task("t")
try: rt.run_model_turn()
except Exception: pass
ok("provider 错误按真因记录", "model.failed" in [e.type for e in rt.events.list()])
rt.stop()

d = _tf.mkdtemp(); rt = _RT(d, "auto", state_dir=d); rt.create_task("m")
def _stuck(g, c): _th.Event().wait(30)
for _ in range(4): rt.spawn_agent("s", _stuck)
try:
    rt.spawn_agent("5th", _stuck); ok("后台并发有上限", False)
except RuntimeError: ok("后台并发有上限", True)
import time as _t2
s0 = _t2.monotonic(); rt.shutdown(); took = _t2.monotonic() - s0
ok("退出等待有界", took < 4.0, f"{took:.1f}s（原 2s×N）")

from fun.tui import TerminalUI as _TUI
ok("TerminalUI(locale) 真的生效", _TUI(locale="zh-CN", output=io.StringIO()).state.theme.locale == "zh-CN")
en = Theme(mode="none", locale="en-US")
ok("命令摘要跟随语言", "后台" in Theme(mode="none", locale="zh-CN").text("ui_rail_background") and en.text("ui_rail_background") == "Background")

a2 = _App(StreamSurface(io.StringIO()), theme=en)
a2._handle_key("palette", lambda t: None)
widths = {display_width(strip_ansi(l)) for w in (28, 32, 40) for l in a2.modal.palette_lines(en, w)}
ok("窄终端下面板不超宽", max(widths) <= 40)

print("\n=== 第四轮：按旅程发现并已修的")
import contextlib as _cl, io as _io2, os as _os2
from unittest.mock import patch as _patch
from fun import cli as _cli
def _run(argv, state=None):
    o, e = _io2.StringIO(), _io2.StringIO()
    with _patch.dict(_os2.environ, {"FUN_STATE_DIR": state or tempfile.mkdtemp()}, clear=False), \
         _patch("sys.stdin", _io2.StringIO("")), _cl.redirect_stdout(o), _cl.redirect_stderr(e):
        return _cli.main(argv), e.getvalue()
c, e = _run(["--workspace", "/tmp/nope-abc-123"])
ok("工作区打错是错误不是 traceback", c == 2 and "does not exist" in e, e.strip()[:40])
c, e = _run(["--workspace", tempfile.mkdtemp(), "--resume-session", "ses_made_up"])
ok("恢复不存在的会话会说明", c == 2 and "no such session" in e, e.strip()[:40])

from fun.runtime import Runtime as _R
d = tempfile.mkdtemp()
rt = _R(d, "ask", state_dir=d, approve=lambda s, r: (_ for _ in ()).throw(RuntimeError("boom")))
rt.create_task("t")
calls = [{"id": "c1", "type": "function", "function": {"name": "exec", "arguments": json.dumps({"command": "rm -rf x"})}}]
rt.task.messages.append({"role": "assistant", "content": None, "tool_calls": calls})
try: rt.execute_tool_calls(calls)
except Exception: pass
answered = {m.get("tool_call_id") for m in rt.task.messages if m.get("role") == "tool"}
ok("每个工具调用都拿到回复", answered == {"c1"})
rt.task.status = "running"; rt.stop()

class _Mem:
    def __init__(self): self.seen = []
    def stream(self, m, tools=None):
        self.seen.append([x.get("content") for x in m if x.get("role") == "user"])
        yield {"choices": [{"delta": {"content": "ok"}}]}
d = tempfile.mkdtemp(); pv = _Mem(); rt = _R(d, "auto", provider=pv, state_dir=d)
rt.create_task("我叫小明"); rt.complete(rt.run_model_turn())
rt.create_task("我叫什么"); rt.complete(rt.run_model_turn())
ok("跨轮记得上一轮", pv.seen[1] == ["我叫小明", "我叫什么"], str(pv.seen[1]))
rt.shutdown()

import time as _t3
stt = UiState(theme=PLAIN)
for _ in range(700): stt.add_user("问题 细节 细节 细节"); stt.add_assistant("回答 " + "内容 " * 10)
stt.compose(100, 30)
s0 = _t3.monotonic(); stt.compose(100, 30); frame_ms = (_t3.monotonic() - s0) * 1000
ok("长会话每帧仍然很快", frame_ms < 30, f"{frame_ms:.1f}ms（1400 条，原 70ms+）")
stt.scroll(-99999)
ok("长会话仍能滚到最顶", "问题" in "\n".join(strip_ansi(l) for l in stt.compose(100, 30)[:6]))

print("\n=== 第四轮补完")
from fun.lock import WorkspaceLock as _WL, WorkspaceLockError as _WLE
_state = tempfile.mkdtemp(); _w1 = tempfile.mkdtemp(); _w2 = tempfile.mkdtemp()
_a = _WL(_w1, _state); _b = _WL(_w2, _state); _a.acquire(); _b.acquire()
ok("两个项目可以共用 state_dir", _a.held and _b.held); _a.release(); _b.release()
_c = _WL(_w1, _state); _c.acquire()
try:
    _WL(_w1, _state).acquire(); ok("同一工作区仍然互斥", False)
except _WLE: ok("同一工作区仍然互斥", True)
_c.release()

class _PlanP:
    def __init__(self): self.n = 0
    def stream(self, m, tools=None):
        self.n += 1
        if self.n == 1:
            yield {"choices": [{"delta": {"plan": ["读", "改", "测"]}}]}
            yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read", "arguments": json.dumps({"path": "a.py"})}}]}}]}
        else:
            yield {"choices": [{"delta": {"content": "好了"}}]}
_d = tempfile.mkdtemp(); Path(_d, "a.py").write_text("x = 1\n")
_rt = _R(_d, "auto", provider=_PlanP(), state_dir=_d)
_seen = []
_rt.on_plan = lambda s, st: _seen.append(list(st))
_rt.create_task("看"); _rt.run_model_turn()
ok("计划在轮次进行中就更新", len(_seen) >= 2 and "done" in _seen[-1], f"{len(_seen)} 次更新")
_rt.stop()

print("\n=== 评审提出的 15 条")
import threading as _th5, time as _t5
from fun.tools import classify_command as _cc, BENIGN as _BEN
from fun.policy import Policy as _Pol, PolicyError as _PE2, WorkspaceGuard as _WG2
_root = Path(tempfile.mkdtemp()).resolve()
ok("rm -rf 永远是 critical（不会被 always 记住）", _cc("rm -rf anything", _root).risk.value == "critical")
ok("pytest/pip/gcc 不再是免审批", all(p not in _BEN for p in ("pytest", "pip", "gcc", "java")))
ok("ls/cat/grep 仍然免审批", all(p in _BEN for p in ("ls", "cat", "grep")))
try: _WG2(tempfile.mkdtemp()).check_name(Path("/etc/passwd")); ok("check_name 越界抛 PolicyError", False)
except _PE2: ok("check_name 越界抛 PolicyError", True)
try: _Pol(agent_mode="Reveiw"); ok("拼错的 agent_mode 被拒", False)
except _PE2: ok("拼错的 agent_mode 被拒", True)

class _Slow2:
    def stream(self, m, tools=None):
        _t5.sleep(0.4); yield {"choices": [{"delta": {"content": "ok"}}]}
_d = tempfile.mkdtemp(); _rt = _R(_d, "auto", provider=_Slow2(), state_dir=_d); _rt.create_task("t")
_lp = _rt.lock.path
_th5.Thread(target=lambda: [None for _ in [0] if True] and None, daemon=True)
def _t():
    try: _rt.run_model_turn()
    except Exception: pass
_w = _th5.Thread(target=_t, daemon=True); _w.start(); _t5.sleep(0.15); _rt.stop()
ok("turn 进行中 stop() 不释放锁", _lp.exists())
_w.join(3); _t5.sleep(0.2)
ok("turn 结束后锁才释放", not _lp.exists())

_errs = []
_d = tempfile.mkdtemp(); _rt2 = _R(_d, "auto", provider=_Slow2(), state_dir=_d); _rt2.create_task("t")
def _t2():
    try: _rt2.run_model_turn()
    except Exception as ex: _errs.append(str(ex))
_w2 = _th5.Thread(target=_t2, daemon=True); _w2.start(); _t5.sleep(0.15)
_rt2.close(shutdown=True); _w2.join(3)
ok("shutdown 不在 turn 中关 store", _errs == [], str(_errs))

_d = tempfile.mkdtemp(); _rt3 = _R(_d, "auto", state_dir=_d); _rt3.create_task("目标")
_ev = [e for e in _rt3.events.list() if e.type == "task.created"][0]
_n = len(_ev.payload["messages"]); _rt3.task.messages.append({"role": "x", "content": "y"})
ok("已记录的事件不会被后续改动", len(_ev.payload["messages"]) == _n); _rt3.stop()

from fun.runtime import valid_tool_calls as _vtc
ok("tool-call 片段被校验", _vtc([{"id": "", "name": "read", "arguments": "{}"}]) == [] and len(_vtc([{"id": "c1", "name": "read", "arguments": "{}"}])) == 1)

from fun.ui.input import read_key as _rk
_r, _w3 = _os2.pipe(); _os2.write(_w3, b"\x1b[")
_s0 = _t5.monotonic(); _k = _rk(_r); _took = _t5.monotonic() - _s0
ok("截断的转义序列不挂住 UI", _k == "escape" and _took < 1.0, f"{_took*1000:.0f}ms")

print("\n=== exec 分级的自洽判据")
from fun.policy import ApprovalMode as _AM, Risk as _Rk
_r2 = Path(tempfile.mkdtemp()).resolve()
_low = _cc("ls -la", _r2).risk
_unknown = _cc("pytest -q", _r2).risk
_bad = _cc("rm -rf x", _r2).risk
ok("只读命令免审批", _low == _Rk.LOW and not _Pol(mode=_AM.AUTO).requires_approval(_low))
ok("陌生程序在 auto 下也问一次", _unknown == _Rk.HIGH and _Pol(mode=_AM.AUTO).requires_approval(_unknown))
ok("陌生程序可被本会话记住", _unknown != _Rk.CRITICAL)
ok("不可逆操作每次都问且不记忆", _bad == _Rk.CRITICAL and _Pol(mode=_AM.AUTO).requires_approval(_bad))
from fun.tools import BENIGN as _B2
ok("git/make/npm 不在免审批清单", not any(p in _B2 for p in ("git", "make", "npm", "pytest", "pip")))
