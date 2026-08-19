# Changelog

## Unreleased

### Added

- Zero-dependency `fun.ui` presentation layer: column-accurate text primitives, colour capability detection, an incremental frame writer, and reusable components.
- Fullscreen frontend, now the default: alternate screen, themed canvas, line-diffed repaints. `--stream` keeps the previous scrollback-preserving behaviour.
- An event spine for the session view — one status node per Runtime event, so the left column alone reports what ran, in what order, and how it ended.
- A real text buffer with a cursor: arrow motion, `Ctrl+A/E`, `Alt+F/B` word motion, `Ctrl+W/K/U` kills, `Ctrl+Y` yank, and multi-line editing, all measured in display columns.
- Terminal Markdown rendering and a dependency-free syntax highlighter (Python, JS/TS, Go, Rust, Bash, JSON, YAML, TOML, SQL); `read` output is coloured by file type and `edit` output as a diff.
- Fuzzy completion: `/` for commands and `@` for workspace files.
- A grouped command palette on `Ctrl+P` — search box, sections, right-aligned key hints, full-width selection bar — built from the same registry the `/` completion reads. A query that matches a command *name* hides the description matches, so "th" means `/theme` rather than every summary containing a t then an h. Commands that need an argument are typed into the composer instead of being run blind.
- Background sub-agents are reachable. `/agent <question>` starts a read-only
  sub-agent that answers from the workspace while the main task continues, and
  reports its answer into the transcript when it finishes. The machinery, the
  `/cancel` command, the rail's background card and the dashboard's background
  table had no way to be triggered at all — `spawn_agent` had no in-tree caller
  and no tool schema. Sub-agents are researchers on purpose: one that could edit
  or exec would need concurrent approvals arbitrated against the foreground
  task, concurrent writes to one workspace, and a second answer to who owns the
  plan. The restriction is enforced by `Policy` — the sub-agent's `Tools` is
  built in a read-only agent mode — not by its prompt or its tool list.
- The interface speaks the session's language. Every string in the chrome was
  hardcoded Chinese, so `fun --locale en-US` asked an English speaker to approve
  a critical `exec` in a language they may not read, while `fun/i18n.py` held a
  complete English table that the components never called.
- A right rail carrying the state the transcript scrolls away from: goal and task state, the plan, background sub-agents, and workspace/model/usage. It appears from 92 columns, takes the plan with it rather than duplicating it, drops whole cards rather than half a list when short of rows, and toggles with `Ctrl+T`.
- A per-turn receipt under each reply: agent mode, model, output tokens and wall time, so a scrolled-back answer still says what produced it.
- Agent modes via `Tab` — Build, Plan, Review — enforced in `Policy` rather than suggested to the model; read-only modes reject `edit` and `exec` with a `MODE_FORBIDS_TOOL` event.
- Four themes (`sky`, `dawn`, `ember`, `mono`), selectable with `--theme` or `/theme` and persisted in the config.
- `--no-color`, `NO_COLOR` / `FORCE_COLOR` support, and ASCII glyph fallback for non-UTF-8 locales.
- One slash-command registry shared by every frontend; `/logout`, `/diff`, `/checkpoint` and `/goal` now work in the interactive UI as well as the plain one.

### Security

- **"Always allow" never covers a critical operation.** Approving `rm -rf build`
  once remembered `exec:rm` for the whole session, so the next `rm -rf` — of
  anything — ran with no prompt. Session memory now applies only below critical.
- **`exec` decides only what it can decide.** The auto-run list contained
  `pytest`, `pip`, `gcc`, `java`, `cargo` — programs that execute code, possibly
  code the model just wrote with `edit` — and the replacement tier was another
  hand-written list with no rule behind it, which is a list that will be wrong
  again. Two questions are answerable from argv and are the only ones asked:
  does this command only read and report (a short, hand-checkable set), and is
  it one of a small number of provably irreversible operations (recursive
  delete, `git reset --hard`/`clean`/`push -f`, privilege escalation, fetch-and-
  run, `find -exec`, an argument outside the workspace). Everything else is
  admitted to be unknown: asked about once in *every* approval mode — including
  `auto`, so a gap in the tool's knowledge cannot fail open in the mode people
  leave it in — and then remembered for the session. Irreversible operations are
  asked about every time and never remembered. `git` is deliberately not benign:
  aliases and hooks make it a launcher.
- The workspace lock is not released, and the event store is not closed, while a
  turn is still running. `stop()` from a Ctrl-C handed the workspace to another
  process mid-tool-call, and `close(shutdown=True)` killed the store under the
  model worker.
- `WorkspaceGuard.check_name` raises `PolicyError` for a path outside the
  workspace instead of a bare `ValueError` that walked past every handler.
- `Policy` validates its own `agent_mode`. `read_only` tests membership in the
  read-only set, so `Policy(agent_mode="Reveiw")` silently granted `edit` and
  `exec`.
- A recorded event cannot change afterwards. `task.created` carried the task's
  *live* message list, so the event grew for the rest of the task and replaying
  it reproduced the end state rather than the beginning. `Event` now snapshots
  its payload at construction, covering every call site rather than only `emit`.
- Restoring a checkpoint verifies it and says what it will destroy. The snapshot
  was trusted as given — restoring runs `git apply` on whatever text it holds —
  and `git restore` discards *every* uncommitted change, not only the ones the
  checkpoint knew about. Snapshots now carry a digest bound to the session and
  task, and discarding unrelated work has to be asked for.
- Event sequence numbers are allocated authoritatively. `seq` is the table's
  primary key but was handed out by a process-global counter starting at 1, so
  two processes on one `events.db` collided and the loser's whole batch was
  rolled back. Allocation now happens inside `BEGIN IMMEDIATE` against the real
  maximum, with a retry: four concurrent writers lose nothing.
- A tool that raises leaves no pending call and no plan step stuck at "active",
  so recovery no longer offers to re-run something that already failed.
- Telemetry is reported per task, not once per process — the sent flag latched
  on the first task and every later one reported nothing.
- Streamed tool-call fragments are validated before dispatch: an empty name, a
  missing id, a duplicate index or non-string arguments no longer become a call
  the model never made.
- A truncated escape sequence or a paste with no terminator can no longer block
  the UI thread indefinitely; both reads are bounded.
- The background concurrency cap is checked and taken in one critical section,
  and the caret column is clamped to the screen in both frontends.
- **The exec check is no longer a denylist.** It was bypassed twice for the same
  reason — first by `bash -lc`, then by `awk 'BEGIN{system("id")}'` — because
  "programs that can run another program" is an unbounded set and every one
  forgotten fails open. The default is now inverted: a command runs without
  asking only if its resolved program is on a short explicit list of things that
  only read and report. Unknown programs are HIGH risk, ask once in every mode
  and may be remembered for the session; provably irreversible operations are
  CRITICAL, ask every time and are never remembered. Forgetting a program now
  costs a prompt, not a shell.
- The risk a command is assessed at is the risk the user is asked about, and
  approving it makes it run. Previously the Runtime asked about a flat "medium"
  for every `exec` — so `rm -rf` was presented as medium-risk — and the tool
  then refused it anyway *after* the user approved, leaving `APPROVAL_REQUIRED`
  as a tool result the model had to interpret.
- "Always allow" is scoped to the resolved program (`exec:awk`), not to the word
  `exec`, so approving one command does not silently approve every command.
- Closed in the rewritten resolver: `awk`/`gawk`/`vim`/`gdb`/`tar --checkpoint-action`
  and other interpreters with a shell escape; `flock`/`unshare`/`setarch`/`nsenter`,
  which both spawn shells and launder a critical command out of view; `env -C` /
  `env --chdir`, which relocated the child outside the workspace with the path
  consumed as an option value so it never reached the path check; and `rm -Rf` /
  `rm -fR`, which walked past a case-sensitive flag test and deleted the directory.
- Model output is stripped of escape sequences and control characters before it
  reaches the terminal. A reply containing `\x1b]0;…\x07` rewrote the window
  title, `\x1b[2J` cleared the screen, and on an OSC-52 terminal the clipboard
  could be written — all reachable through prompt injection in a file the `read`
  tool ingested.
- `explore` no longer names protected files. `read` refused `.env` and private
  keys while the listing printed them, and a filename is itself information.
- The `exec` timeout cannot be removed by a model argument. `json.loads` accepts
  `Infinity`, which passed the schema check and turned the poll loop into a spin
  that never returned — disabling the only bound on the highest-risk tool.

- `exec` resolves a command before judging it. The shell-wrapper block tested `argv[1] == "-c"` and unwrapped `env` *afterwards*, so `bash -lc` and `env bash -c` both ran a full shell — and once inside one, every downstream check (`sudo`, `curl`, `rm -rf`, `git reset --hard`) was reading `bash` as the program name. Wrappers are now peeled layer by layer with each layer checked as it is peeled, whole programs are refused rather than argument shapes (any shell, any spelling; `xargs`, `timeout` and friends whose grammar would have to be guessed), and a wrapper this code does not model is refused instead of parsed. `python -c` / `-m` is refused outright rather than having its code string scanned for keywords. An argument resolving outside the workspace raises the approval gate.
- The local dashboard no longer interpolates model-controlled strings into `innerHTML`. A goal or tool result carrying markup — reachable through prompt injection — executed as script in the dashboard's origin on the next page load. Cells are built as nodes and filled with `textContent`, with a CSP and a `Host` check to close DNS rebinding.
- `protected_names` matches case-insensitively. `fnmatch` is case-sensitive on POSIX but macOS volumes are not, so `read(".ENV")` matched no pattern and then opened the real `.env`. The default list also now covers SSH keys, `.aws`, `.npmrc`, `.netrc` and keystores.
- The API key is passed to `security` on stdin rather than in `argv`, where any process reading `ps` could see it.

### Fixed

- Two projects can be open at once. The workspace lock was one file per *state
  directory* — and the state directory defaults to `~/.fun` for everyone — so a
  second project in a second terminal was refused with a message naming the
  first project's path. The lock is now named after the workspace, which keeps
  the exclusion that matters and drops the one that never did.
- The plan and its progress reach the interface *during* the turn instead of
  after it. Plan updates were pushed once, when the turn was already over —
  exactly when a plan has stopped being useful — so the rail showed nothing and
  the step counter never moved through the minutes a long turn takes.
- **A follow-up question can refer to the one before it.** The Runtime models a
  *task* while the interface shows one continuous conversation, and nothing
  bridged them: every prompt started a task with an empty history, so "what did
  I just ask you?" was unanswerable and "now do the same for the other file" had
  no referent. A new task now carries the previous conversation, trimmed on a
  turn boundary so a `tool_calls` message is never separated from its replies,
  and bounded by the same per-request compaction as before.
- Every tool call the model declares gets a reply, even when the turn is cut
  short. An approval callback that raised — or a Ctrl-C between calls — left the
  assistant's `tool_calls` message with fewer `role: "tool"` replies than it
  declared, which every OpenAI-compatible endpoint rejects with a 400 from then
  on: one interrupted turn poisoned the rest of the task.
- Rendering cost no longer grows with the length of the conversation. The whole
  transcript was rendered and then mostly thrown away on every repaint — 70 ms a
  frame at 800 messages, felt as typing lag. Only enough items from the end to
  fill the viewport are rendered now: 2 ms a frame at 1 400.
- A mistyped `--workspace`, or a `FUN_STATE_DIR` pointing at a file, is an error
  message and exit code 2 rather than a raw `PolicyError` / `FileExistsError`
  traceback.
- `--resume-session` with an unknown id says so instead of handing back a blank
  session, which looked exactly like the user's previous task having vanished.
- **PgUp/PgDn scroll.** They did nothing at all: scrolling dropped transcript
  items from the *front* while overflow handling kept the *tail*, so the visible
  window never moved — and past a certain offset it started hiding the newest
  messages instead. The fullscreen frontend has no terminal scrollback, so the
  history above the viewport was unreachable by any key. The offset is now
  measured in rendered rows from the bottom and clamped to what exists.
- An approval no longer creates a second, permanently unsettled tool card
  carrying the Runtime's internal approval subject (`exec:ls`) and
  `str(Risk.MEDIUM)`. Because flushing stops at the first unsettled item, that
  phantom froze `--stream` scrollback for the rest of the session, and the real
  card's arguments never reached the prompt — so the approval for the
  highest-risk tool was the one that did not show its command.
- `/diff`, `/checkpoint` and `/agent` work between prompts. Completing a task
  closes the event store, and only `create_task`/`set_goal` reopened it, so
  every command that emits failed with `EVENT_STORE_CLOSED` for the whole idle
  time of a session — and `/agent` reported it as "configure a provider first"
  because it mapped every `RuntimeError` to that one message.
- A typed prompt can no longer discard a pending recovery or orphan a paused
  task. `run_goal` calls `create_task` directly, which guarded only "running",
  so a half-executed destructive call could be overwritten with no
  acknowledgement event and become permanently invisible to `--resume-session`.
  The refusals name the way out rather than printing a raw tag.
- A provider failure is recorded as itself. `provider.stream` is a generator
  function, so the `try` around the call could never fire and every auth,
  network and timeout failure was written to the durable log as
  `MALFORMED_RESPONSE`, blaming the parser; `model.failed` was unreachable.
- Ctrl-C during a tool call is a clean stop, not an internal error: the plan
  update after the tool finished raised `NO_ACTIVE_TASK` because the task had
  already moved to stopped, and the turn was then marked failed.
- `--resume-session` keeps the session's model, system-prompt preference and
  telemetry consent instead of silently dropping all three.
- Sub-agent lifecycle: a turn ending no longer cancels running sub-agents (only
  session shutdown does), a store closed mid-flight no longer turns a completed
  sub-agent into a failed one or escapes its thread as a traceback into the raw
  terminal, cancellation is heard during a stream rather than only between
  steps, live sub-agents are capped and finished ones pruned, exiting waits a
  bounded time in total rather than per task, and the answer reaches the
  transcript whole instead of cut to 120 characters mid-word.
- `App(locale=…)` and `TerminalUI(locale=…)` actually render in that language —
  the locale never reached the `Theme`, which is the only thing that reads it.
- Command summaries and `/help` are localised; the `cmd_*` keys existed in both
  tables and were referenced from nowhere. The command palette no longer renders
  wider than the terminal below 44 columns, and its search row is clipped.
- The local dashboard reports the truth and survives its own data: token totals
  are session snapshots rather than a running sum (ten turns of 100 tokens
  reported 5 500), and a malformed row — `"usage": null`, a payload that is a
  list, a NULL column — no longer takes the whole endpoint down with a 500.
- Two hangs on the UI thread, both quadratic and both reached from ordinary
  model output: wrapping one unbroken 100 KB token took 15 s (now 57 ms), and
  tokenizing 400 KB of one token kind took 4.3 s (now 70 ms). Each ran on every
  repaint.
- A tool card shows what the tool was called with. Only `approval.pending`
  carried arguments, so every call that did not need approval — `read` and
  `explore` always, everything in `auto` mode — rendered as a bare `read` with
  no path.
- The caret no longer jumps to the top-left when a middle line of a multi-line
  draft ends in a space: the visual mapping located each wrapped piece by
  guessing what `wrap()` had removed, and guessed wrong for trailing whitespace.
- Completing a command with the cursor at the start of the buffer inserts one
  command, not two.
- The stream deadline bounds silence rather than the whole response, so a long
  but healthy completion is no longer killed mid-flight with everything it had
  produced discarded. An error object framed as a normal SSE event is reported
  instead of being fed to the tool-call parser as model output.
- `fun` starts at all without a TTY. `_run_plain` — the fallback for pipes, dumb terminals and Windows consoles — called `banner()` without importing it, so its first line raised `NameError`.
- `/permissions` no longer kills the session. It stored a bare string where three call sites read `policy.mode.value`; the `AttributeError` surfaced on the UI thread and unwound the loop. `Policy` now normalises a mode wherever one is accepted.
- A command can open a dialog from a dialog's own callback. The key loop cleared the modal slot unconditionally after `handle()` returned, throwing away the form that `/config` had just installed — so `/config` was unreachable from the command palette.
- `/cle` clears. Dispatch resolved the prefix and reported "cleared" while the caller string-matched the raw input; the side effect now belongs to the handler rather than to `cli.py`.
- Replayed token usage no longer grows with the square of the turn count: `model.completed` records a cumulative snapshot, and recovery was merging every one of them through an accumulating merge.
- Every `run_tool` exit reports with a `call_id` and through `on_status`. Four early returns skipped one or both, and the UI drops an event with no `call_id` — so a rejected or malformed call left a card at "queued" (or, after a denied approval, at "running") for the rest of the session.
- Recovery is detected again after the first one. The guard asked whether *any* acknowledgement existed since task creation rather than since the current stall, so a second crash mid-tool was silently forgotten.
- `stop()` on a task awaiting recovery ends it instead of falling through every branch while still closing the store — which left the workspace lock on disk and made the next `/recover` raise `Cannot operate on a closed database`.
- Closing the store waits for a turn in flight. `stop()` from the UI thread could close the shared SQLite connection while the model worker was between two emits, killing that thread and losing the tool result it was recording.
- Context compaction cuts on turn boundaries, keeping an assistant `tool_calls` message with the `tool` messages that answer it. Splitting them produced an orphan tool reply, which every OpenAI-compatible endpoint rejects with a 400.
- "Allow for this session" works in the interactive UI. Only the plain `input()` fallback ever recorded it, so `a` allowed one call and then asked again.
- A stopped task is recorded as stopped, not as a malformed provider response.
- Provider streaming: events after `[DONE]` in the same read are no longer yielded into the tool-call parser; a multi-byte character split across reads is decoded incrementally instead of being replaced with `?`; a 200 with a JSON error body and no event-stream content type is reported instead of returning an empty reply; the stream has a wall-clock deadline and a buffer ceiling.
- Credentials: a Keychain that cannot be read is treated as "cannot read now", not "never configured" — it no longer blanks `base_url` and `model` and then persists the blanks on the next unrelated save. A key from `FUN_API_KEY` is never promoted into the Keychain behind the user's back. `/config` and `/logout` report what actually happened instead of always claiming durable storage and successful deletion.
- The dock writer tracks which row the cursor is on. It assumed the last dock row while `place_cursor` had deliberately parked it on the composer, so every repaint walked up too far and erased that many rows of scrollback.
- `truncate` closes a style it cuts through. A cut inside a `reverse` span left inverse video running to the right edge of the screen.
- The mode tab strip is clipped like every other dock row; the composer's width accounts for all five columns of panel chrome, so the character just typed is visible and the caret stays on screen; a frame is exactly as tall as the terminal, with the dock served first, so the input survives an 8-row pane; the canvas border clips both ends rather than only the right.
- The streaming frontend holds back the first unsettled item rather than only the last, so a tool card queued behind a running one is not frozen into scrollback in its transient state.
- `Ctrl+C` clears the completion popup with the draft, instead of leaving it live to paste itself back on the next Enter.
- A pending recovery blocks the composer rather than accepting every key except `r`/`d`/`f`/`s` — typing "restart from scratch" used to resume the task on its first character.
- Cancelling the command palette mid-turn no longer reports the turn as finished.
- A model list loaded in the background is applied only to the dialog that asked for it.
- `Ctrl+U` / `Ctrl+K` work inside dialogs; an empty kill no longer erases the kill ring; slash commands are recalled by the up arrow.
- The background-task poll converges instead of repainting on every pass for any task with a long goal.
- `SIGTERM` / `SIGHUP` restore the terminal instead of leaving the shell in cbreak mode inside the alternate screen.
- Pasting into the terminal no longer cancels the dialog it was pasted into. Bracketed paste (`ESC[200~`) was decoded as an unrecognised escape, which returned `escape` and left `00~` in the buffer to be typed as text — so a pasted API key both dismissed the form and arrived corrupted. Pastes are now one event, are always treated as content rather than control, and any unhandled escape sequence is consumed to its terminating byte instead of spilling into the text stream.
- An API-key rejection names the endpoint it was rejected by and identifies the key by its first and last four characters, instead of only saying it failed.
- Small talk no longer draws an "understand the request / respond" plan stuck at 0/2. The Runtime still records it — it is a real plan with real events — but a two-step generic plan reports nothing a reader did not already know.

- `EventStore` appends, loads and reads are serialised. The Runtime emits from model worker, background sub-agent and UI threads, and the duplicate check could previously interleave with the write.
- Layout no longer counts ANSI escapes or east-asian characters as one column each, so width guarantees hold with colour enabled and with CJK content.
- `Ctrl+C` has a path out at all. `tty.setcbreak` leaves `ISIG` on, so it arrived as `KeyboardInterrupt` and only cleared the draft; it now clears a draft, then interrupts a running task, then exits on a deliberate second press.
- The terminal's own cursor is placed in the input rather than a reverse-video block being drawn there, which also anchors the macOS IME candidate window to the text being typed.
- The start screen rendered a static placeholder instead of the editor, making typed input invisible; there is now a single layout, so the input, caret, completion popup and hints exist once.
- One model tool call renders as one card: `execute_tool_calls` passes the model's call id into `run_tool` instead of letting it mint a second one.
- `WorkspaceGuard.check_name` honours `Policy.protected_names` instead of ignoring its policy argument.
- The initial plan heuristic matches Latin verbs on word boundaries and measures goal size by display width, so "fix login" is no longer treated as small talk.
- `Usage.summary` reports nothing rather than `in ? out ? ttft ?` before anything has been measured.

### Changed

- Tests: several were green while guarding the wrong thing, and are rewritten to
  assert what the product should do rather than what it did. Two hand-wrote
  fixtures no code path can produce (per-turn token deltas; a background event
  keyed by the wrong id); two asserted a bug as the spec (a Keychain read failure
  destroying the saved endpoint; a critical command refused after approval);
  four could not fail (a race the test never triggered, a "retry" performed by
  the test body, assertions on an unrelated temp directory, a before/after state
  that was the same value); and two wrote to the developer's real macOS Keychain,
  passing on Linux only because the code they exercise is unreachable there.

- `cli.py` reduced from 792 to ~340 lines; interaction moved behind a `Frontend` protocol and drawing behind surfaces.
- Removed the block components superseded by the spine layout — they were reachable only from tests, which made unused code look covered.
- `fun.tui` and `fun.terminal_ui` are kept as thin compatibility shims. `fun.renderer` is removed: it was reachable only from tests and still rendered a first-run menu and a command list that no longer exist.
- Dead code removed rather than left looking live: `App._dirty` was assigned in thirteen places and read in none (it now actually gates repaints), an unreachable slash-command branch, `kill_line` handlers no key emits, and a background set written but never read.
- Test suite grown from 209 to 590 cases, including layout checks that run with colour on and off.

## 1.0.0a6 - Alpha

- Added durable store cleanup on every terminal Runtime path and expanded recovery/concurrency regression coverage.

## 1.0.0a5 - Alpha

- Hardened event replay against sequence races, conflicting recovery batches, and concurrent event creation.

## 1.0.0a4 - Alpha

- Hardened cross-process event persistence, CI packaging, checksum validation, and release diagnostics.

## 1.0.0a3 - Alpha

- Fixed release artifact version normalization checks and hardened package publishing validation.

## 1.0.0a2 - Alpha

- Improved provider streaming compatibility, release validation, CI packaging, and artifact auditing.

## 1.0.0a1 - Alpha

- Runtime-first terminal Coding Agent with SQLite Event Replay and recovery.
- Safe workspace tools, approval policies, dynamic plans, validation, and bounded repair.
- OpenAI-compatible streaming provider with privacy-safe failure classification.
- Initial CLI, local-only dashboard, telemetry opt-in controls, and package release automation.

- Added the V1 Core runtime foundation: bounded task planning, OpenAI-compatible streaming loop, SQLite event persistence, workspace tools, approval boundary, validation/checkpoint hooks, non-interactive CLI mode, and single-column terminal renderer.
- Added product, Runtime, protocol, flowchart, UI, and open-source design documentation.

## 1.0.0a1

Initial public development baseline. This alpha is not feature-complete and should not be used for unreviewed destructive automation.
