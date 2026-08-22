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
# 列是 5 不是 4：用户打出来的那个空格本身就占一格。报 4 等于"打了空格屏幕没反应"。
ok("行尾空格既不弹光标、也确实占一格", e.visual_lines(40)[1:] == (1, 5) and e.visual_lines(40)[0][1] == "beta ", str(e.visual_lines(40)[1:]))

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

print("\n=== 可用性：草稿、滚动、工具卡片")
_a = _App(StreamSurface(io.StringIO()), theme=PLAIN)
_sent2 = []
def _k(k): _a._handle_key(k, _sent2.append); _a._consume()
for ch in "以前说过的话": _k(ch)
_k("enter")
for ch in "我正在打的": _k(ch)
_k("up")
ok("打字时按 ↑ 不会顶掉草稿", _a.state.editor.text == "我正在打的", repr(_a.state.editor.text))
_k("enter")
ok("回车提交的是刚打的", _sent2[-1] == "我正在打的")
_k("up")
ok("空框时历史仍可用", _a.state.editor.text == "我正在打的")

from fun.ui.input import read_key as _rk2
for _seq, _want in ((b"\x1b[<64;10;5M", "wheel_up"), (b"\x1b[<0;10;5M", "mouse")):
    _r3, _w3b = _os2.pipe(); _os2.write(_w3b, _seq)
    ok(f"鼠标序列解析为 {_want}", _rk2(_r3) == _want)

_st = UiState(theme=PLAIN)
for i in range(40): _st.add_user(f"消息{i}")
_st.scroll(-5)
_rows = [strip_ansi(l) for l in _st.compose(70, 14)]
_top = [r for r in _rows[1:] if "消息" in r][0]
ok("滚动横幅独占一行不吃内容", "PgUp" in _rows[0] and "消息" not in _rows[0])
for i in range(3): _st.add_assistant(f"新回复{i}")
_rows2 = [strip_ansi(l) for l in _st.compose(70, 14)]
ok("往回读时不被新消息拽走", [r for r in _rows2[1:] if "消息" in r][0] == _top)
ok("新消息有计数提示", "↓" in _rows2[0])

from fun.ui import components as _comp
_v = _comp.ToolView("read", "completed", {"path": "a.py"}, 1, 0, "CONTENT\n" * 40)
_summary = "\n".join(_comp.tool_body(PLAIN, _v, 60))
ok("成功的 read 只给摘要", "Ctrl-O" in _summary and "CONTENT" not in _summary, _summary[:40])
_fail = _comp.ToolView("exec", "failed", {"command": "pytest"}, 1, 1, "\n".join(f"n{i}" for i in range(40)) + "\nFAILED test_x")
_r4 = "\n".join(_comp.tool_body(PLAIN, _fail, 60))
ok("失败从尾部截断，保住报错行", "FAILED test_x" in _r4 and "n0" not in _r4)
ok("表头只显示识别性参数", _comp._format_arguments({"path": "a.py", "expected_hash": "x", "patch": "@@"}, 60, "edit") == "a.py")

print("\n=== Key 落点：配置了就不会第二天消失")
import tempfile as _tf, json as _json2
from unittest.mock import patch as _patch
from fun.config import FunConfig as _FC
from fun.i18n import saved_message as _sm, key_location_message as _klm
with _tf.TemporaryDirectory() as _d:
    _p = _os2.path.join(_d, "config.json")
    _env_backup = _os2.environ.pop("FUN_API_KEY", None)
    with _patch("fun.config._keychain_set", return_value=False), _patch("fun.config._keychain_get", return_value=""):
        _written, _durable = _FC(base_url="https://x/v1", model="m", api_key="sk-real").save(_p)
        ok("钥匙串写失败时仍落盘", (_written, _durable) == (True, True), (_written, _durable))
        _re = _FC.load(_p)
    if _env_backup is not None: _os2.environ["FUN_API_KEY"] = _env_backup
    ok("第二天重开还能读到 key", _re.api_key == "sk-real" and _re.ready())
    ok("配置文件仅本人可读", _os2.stat(_p).st_mode & 0o777 == 0o600, oct(_os2.stat(_p).st_mode & 0o777))
    ok("storage() 说出真实落点", _FC().storage(_p) == "config-file")
    ok("提示语区分落点而不是一律说成功", _sm("zh-CN", "config-file", _p) != _sm("zh-CN", "keychain", _p) and _p in _sm("zh-CN", "config-file", _p))
    ok("未知落点降级为警告而不是安心话", _sm("zh-CN", "???", _p) == _sm("zh-CN", "none", _p) and _klm("en-US", "???", _p) == _klm("en-US", "none", _p))

print("\n=== 钥匙串写入不会打到屏幕上")
import subprocess as _sp
from fun.config import _keychain_set as _ks
with _patch("fun.config.shutil.which", return_value="/usr/bin/security"), _patch("fun.config.subprocess.run") as _run:
    _run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
    with _patch("fun.config._keychain_get", return_value="sk-x"):
        _ok = _ks("sk-x")
    _argv = _run.call_args.args[0]
    ok("不用会在终端提示的裸 -w", _argv[-2:] == ["-w", "sk-x"] and _run.call_args.kwargs.get("input") is None)
    ok("security 的 stdin 被关掉", _run.call_args.kwargs.get("stdin") == _sp.DEVNULL)
    ok("写完读回验证才算成功", _ok)
with _patch("fun.config.shutil.which", return_value="/usr/bin/security"), _patch("fun.config.subprocess.run") as _run:
    _run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
    with _patch("fun.config._keychain_get", return_value=""):
        ok("退出码 0 但没存下来 = 失败", not _ks("sk-x"))

print("\n=== 选模型：列表、筛选、多选")
from fun.ui.modal import select_modal as _sel
_picked = []
_m = _sel("Choose", ["gpt-4o-mini", "claude-opus", "claude-haiku"], _picked.append)
for _c in "haiku": _m.handle(_c)
ok("输入即筛选", _m.visible() == ["claude-haiku"])
_m.handle("enter")
ok("回车选中筛选后的那一个", _picked == ["claude-haiku"])
_m2 = _sel("Choose", ["a-one", "a-two"], lambda v: None)
for _c in "zzz": _m2.handle(_c)
ok("筛不到就明说没有匹配", _m2.visible() == [] and PLAIN.text("ui_select_empty") in "\n".join(_m2.lines(PLAIN, 60)))
_p2 = []
_m3 = _sel("Choose", ["a", "b", "c"], _p2.append, multi=True)
_m3.handle("down"); _m3.handle(" "); _m3.handle("down"); _m3.handle("space"); _m3.handle("enter")
ok("空格可以多选、按顺序返回", _p2 == [["b", "c"]])
_p3 = []
_sel("Choose", ["a", "b"], _p3.append, multi=True).handle("enter")
ok("不多选时仍是一键确认", _p3 == [["a"]])
for _th in (PLAIN, Theme(mode="truecolor")):
    _w = {display_width(_l) for _l in _sel("Choose", ["模型-大", "gpt-4o"], lambda v: None, multi=True, chosen=["gpt-4o"]).lines(_th, 60)}
    ok(f"多选框边框对齐({_th.mode})", len(_w) == 1, _w)

print("\n=== 报错要说人话、@ 文件要真的算数")
import re as _re3
from fun.frontends import friendly_error as _fe, attach_mentions as _am
from fun.provider import ProviderError as _PE
from fun.ui.completion import mentions as _mn, mention_token as _mt
_src = open("fun/provider.py", encoding="utf-8").read()
_tags = sorted(set(_re3.findall(r'ProviderError\("([A-Z_]+)"', _src)))
_raw = [tg for tg in _tags for lo in ("zh-CN", "en-US") if _fe(_PE(tg), lo) == tg]
ok("没有任何一个错误码是裸着显示的", not _raw, _raw)
_msgs = {_fe(_PE("PROVIDER_HTTP_FAILED", status=s), "zh-CN") for s in (404, 429, 500, 400)}
ok("404/429/5xx/400 各说各的", len(_msgs) == 4)
ok("404 不甩锅给 key", "sk" not in _fe(_PE("PROVIDER_HTTP_FAILED", status=404, endpoint="https://x/v1", key_hint="sk-a…1"), "zh-CN"))
ok("没见过的错误码也是一句话", "PROVIDER_BRAND_NEW" in _fe(_PE("PROVIDER_BRAND_NEW"), "zh-CN") and len(_fe(_PE("PROVIDER_BRAND_NEW"), "zh-CN")) > 20)
ok("@ 引用能读回来（含带空格的）", _mn('看 @a.py 和 @"src/my file.py"') == ["a.py", "src/my file.py"])
ok("邮箱不会被当成文件引用", _mn("a@b.com") == [])
ok("带空格的路径补全时加引号", _mt("my file.py") == '@"my file.py"')
with _tf.TemporaryDirectory() as _d2:
    open(_os2.path.join(_d2, "real.py"), "w").close()
    _sent, _miss = _am("看下 @real.py 和 @ghost.py", _d2)
    ok("存在的文件明确交给模型", "- real.py" in _sent)
    ok("不存在的文件当场告诉用户", _miss == ["ghost.py"])
    ok("逃逸路径不会被附上", _am("@../../etc/passwd", _d2)[1] == ["../../etc/passwd"])
    ok("普通消息原样不动", _am("普通消息", _d2) == ("普通消息", []))

print("\n=== 打空格必须有反应")
from fun.ui.editor import Editor as _Ed
_e1 = _Ed(); _e1.insert("帮我")
_e2 = _Ed(); _e2.insert("帮我 ")
ok("空格改变了渲染行", _e1.visual_lines(20)[0] != _e2.visual_lines(20)[0], (_e1.visual_lines(20)[0], _e2.visual_lines(20)[0]))
ok("空格推动了光标列", _e2.visual_lines(20)[2] == _e1.visual_lines(20)[2] + 1)
_full = _Ed(); _full.insert("a" * 10)
ok("整行填满时光标落到下一行", _full.visual_lines(10)[1:] == (1, 0))
def _snap(_t):
    _a = _App(StreamSurface(io.StringIO()), theme=PLAIN)
    for _c in _t: _a._handle_key(_c, lambda *_: None)
    _a._consume()
    return _a.state.compose(70, 14), _a.state.cursor_hint
_f1, _c1 = _snap("帮我")
_f2, _c2 = _snap("帮我 ")
ok("整帧确实变了（不是原地不动）", _f1 != _f2)
ok("终端真实光标也右移一格", _c2 == (_c1[0], _c1[1] + 1), (_c1, _c2))
_rand = __import__("random"); _rand.seed(11)
_bad = 0
for _ in range(3000):
    _t = "".join(_rand.choice("ab 中\n") for _ in range(_rand.randint(0, 20)))
    _w = _rand.randint(4, 12)
    _ed = _Ed(); _ed.text = _t; _ed.cursor = _rand.randint(0, len(_t))
    _ls, _r, _co = _ed.visual_lines(_w)
    if not (0 <= _r < len(_ls)) or _co > _w or any(display_width(_l) > _w for _l in _ls):
        _bad += 1
    if "\n" not in _t and "".join(_ls) != _t:
        _bad += 1
ok("3000 组随机输入不超宽、不丢字", _bad == 0, _bad)

print("\n=== 审批：拒绝必须是拒绝")
from fun.cli import approval_gate as _gate, ALLOWING_ANSWERS as _ALLOW
class _FakeApp:
    def __init__(self, answer): self.answer=answer; self.asked=[]
    def request_approval(self, name, risk, arguments=None):
        self.asked.append(name); return self.answer
def _run(answer, remembered=None, interactive=True):
    _a=_FakeApp(answer); _r=remembered if remembered is not None else set()
    with _patch("fun.cli.sys.stdin.isatty", return_value=True):
        return _gate(_r,{"app":_a},"zh-CN",interactive=interactive)("exec:rm","critical"), _a, _r
ok("按 n 拒绝 rm -rf 不会执行", _run("no")[0] is False)
ok("空答复也算拒绝", _run("")[0] is False and _run(None)[0] is False)
ok("只有 yes/always 放行", _ALLOW == frozenset({"yes","always"}) and _run("yes")[0] and _run("always")[0])
ok("critical 永远不记住", _run("always")[2] == set())
_rem=set()
with _patch("fun.cli.sys.stdin.isatty", return_value=True):
    _g=_gate(_rem,{"app":_FakeApp("always")},"zh-CN"); _g("exec:ls","high")
ok("非 critical 的 always 会被记住", "exec:ls" in _rem)
import inspect as _ins
from fun.ui.app import App as _App2
_ret=set(_re3.findall(r'return "([a-z]+)"', _ins.getsource(_App2.request_approval)))
ok("界面只会给出 gate 认识的答复", _ret <= {"yes","no","always"}, _ret)
from fun.ui.components import REFUSAL_MESSAGES as _RM, tool_body as _tb, ToolView as _TV
_tags = set(_re3.findall(r'ToolResult\(False, f?"([A-Z][A-Z_]{3,})(?=[":\\ ])', open("fun/tools.py",encoding="utf-8").read()+open("fun/runtime.py",encoding="utf-8").read()))
_tags |= set(_re3.findall(r'error_tag="([A-Z_]+)"', open("fun/runtime.py",encoding="utf-8").read()))
ok("每个拒绝码都有人话", not [t for t in _tags if t not in _RM], sorted(t for t in _tags if t not in _RM))
_denied = "\n".join(_tb(PLAIN, _TV("exec","failed",{"command":"rm -rf build"},1,1,"APPROVAL_REQUIRED"), 60))
ok("拒绝后卡片说人话不是打码", "APPROVAL_REQUIRED" not in _denied and _denied.strip() == PLAIN.text("refuse_approval_required"), _denied.strip())

print("\n=== 中断：屏幕和模型看到的必须是同一件事")
from fun.runtime import Runtime as _RT2
with _tf.TemporaryDirectory() as _d3:
    _r2 = _RT2(_d3, state_dir=_d3)
    _r2.create_task("写个长回答")
    _r2._partial_text = "第0段 第1段"
    _r2.stop()
    _roles = [m["role"] for m in _r2.task.messages if m["role"] != "system"]
    ok("中断后不会出现连续两条 user", _roles == ["user", "assistant"], _roles)
    ok("说到一半的内容被记下来了", "第0段 第1段" in _r2.task.messages[-1]["content"])
    ok("并且明确告诉模型是被打断的", "interrupted" in _r2.task.messages[-1]["content"].lower())
with _tf.TemporaryDirectory() as _d4:
    _r3 = _RT2(_d4, state_dir=_d4)
    _r3.create_task("一句没说就停")
    _r3.stop()
    ok("一个字没说也要收尾", _r3.task.messages[-1]["role"] == "assistant")
with _tf.TemporaryDirectory() as _d5:
    _r4 = _RT2(_d5, state_dir=_d5)
    _r4.create_task("正常一轮")
    _r4.task.messages.append({"role": "assistant", "content": "说完了"})
    _r4.stop()
    ok("说完的回答不会被改成中断", _r4.task.messages[-1]["content"] == "说完了")
from fun.i18n import t as _t2
ok("屏幕上也会留下中断的痕迹", _t2("zh-CN", "turn_interrupted") != "turn_interrupted" and _t2("en-US", "turn_interrupted") != "turn_interrupted")

print("\n=== 崩溃恢复：那一屏要自己讲清楚")
from fun.ui import components as _cp2
_st2 = UiState(theme=PLAIN)
_st2.set_recovery({"name": "exec", "call_id": "c9", "arguments": {"command": "git push origin main"}, "goal": "把改动推上去"})
_panel = "\n".join(_cp2.recovery_body(PLAIN, _st2.recovery, 70))
ok("先说发生了什么", PLAIN.text("ui_recovery_needed") in _panel)
ok("说出你当时要它做什么", "把改动推上去" in _panel)
ok("命令是给人看的形状", "git push origin main" in _panel and "{'command'" not in _panel and "command=" not in _panel)
for _k in ("resume", "discard", "mark_failed", "stop"):
    ok(f"选项 {_k} 说明了后果", PLAIN.text(f"ui_recovery_{_k}_why") in _panel)
ok("明确警告继续=再跑一遍", "twice" in PLAIN.text("ui_recovery_resume_why") and "跑两次" in _t2("zh-CN", "ui_recovery_resume_why"))
for _mode, _key in (("recovery", "ui_composer_recovery"), ("approval", "ui_composer_approval")):
    _s3 = UiState(theme=PLAIN); _s3.mode = _mode
    _fr = "\n".join(strip_ansi(_l) for _l in _s3.compose(70, 20))
    ok(f"{_mode} 阻塞时输入框不再假装能用", PLAIN.text(_key) in _fr and PLAIN.text("ui_composer_placeholder") not in _fr)

print("\n=== 发现性：找得到、看得懂、说人话")
from fun.commands import REGISTRY as _REG
_untr = [n for n, c in sorted(_REG.items()) if c.describe("zh-CN") == c.summary]
ok("每条命令都有中文说明", not _untr, _untr)
for _loc in ("zh-CN", "en-US"):
    _th = Theme(mode="none", locale=_loc)
    _a4 = _App(StreamSurface(io.StringIO()), theme=_th)
    _a4.state.provider_ready = False
    _fr4 = "\n".join(strip_ansi(_l) for _l in _a4.state.compose(76, 20))
    ok(f"没配 key 时首屏就说了({_loc})", _th.text("ui_needs_setup") in _fr4 and "/config" in _fr4)
    _a4.state.provider_ready = True
    ok(f"配好之后不再唠叨({_loc})", _th.text("ui_needs_setup") not in "\n".join(strip_ansi(_l) for _l in _a4.state.compose(76, 20)))
_hk = [k for k, _ in UiState(theme=PLAIN).dock_hints(80)]
ok("Ctrl-P 终于出现在提示栏里", "Ctrl-P" in _hk, _hk)
ok("窄屏时会让位而不是撑破", "Ctrl-P" not in [k for k, _ in UiState(theme=PLAIN).dock_hints(50)])
ok("发不出消息时告诉你去哪配", all("/config" in _t2(_l, "offline") for _l in ("zh-CN", "en-US")))
