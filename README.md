# shunt-ollama

**English** · [Português (Brasil)](README.pt-BR.md)

A [Claude Code](https://claude.com/claude-code) plugin that **keeps large reads out of the
context window** by delegating them to a model running locally on [Ollama](https://ollama.com).

The local model reads the whole file and returns bullets anchored to line numbers. Only that
answer reaches Claude, which then opens just the slice it needs to edit.

```text
Claude wants to read a 4141-line file
        │
        ├─ hook denies ──► bulk-read ──► Ollama (gemma4:e4b) reads all 4141 lines
        │                                        │
        └────────── 40 lines of bullets ◄────────┘
                    "- refresh_if_needed (launcher.sh:L1195-L1260): renews the token when..."
                              │
                              └─► Read with offset=1195 limit=65 to edit
```

## Where it came from

Spotify published a plugin called `shunt` in
[`spotify/portal-ai-plugins`](https://github.com/spotify/portal-ai-plugins). It intercepts
Claude Code's file reads and routes them to Portal/AiKA, their internal platform. The idea is
good and it does not depend on Spotify's infrastructure: any helper model can be the reader.

This repository ports that idea to Ollama, covering only the read-and-summarize path (the
original's `bulk-reader` mode).

**What came from the original:** the design of `PreToolUse` hooks that intercept large reads,
the notion of a "mode" as a system-prompt file, and the message format built from
`<file path="...">` tags.

**What changed:**

| | Original (Spotify) | Here |
|---|---|---|
| Transport | Portal CLI (`aika:invoke-chat`) | Ollama's local HTTP API |
| Bash detection | regex `^(cat\|head\|tail\|less\|more) ` | lexical parser (`shlex`), segment by segment |
| Threshold | file size | **effective lines** plus a per-session running total |
| Payload | command-line argument, capped by `ARG_MAX` | `curl` stdin |
| Line numbering | no | `cat -n` before sending |
| Sources | files | files, directories, globs, command output, stdin |
| Hook output format | `{"decision": "allow"}` | `hookSpecificOutput.permissionDecision` |

That last row matters. Recent Claude Code versions reject the old format with
`Hook JSON output validation failed`, and a hook that fails validation **blocks nothing**.

## Why version 0.2.0 exists

Version 0.1.0 was a faithful port, with the original hooks nearly untouched. In real usage logs
it fired **exactly once across two sessions**, and that single block was worked around: the
model reread the same file in four slices with `sed -n '30,120p'`, `'121,200p'`, `'201,276p'`.
It read everything, spent the same tokens, and the local model was never called.

The investigation showed that almost no read passed through where the hooks were watching:

- `cd project && cat AGENTS.md` did not match the `^cat ` regex
- `cat file 2>/dev/null` was discarded by the redirection filter
- `sed -n`, `awk`, and `head -150` inside a loop went unrecognized
- `Read` with `limit: 620` on a 1200-line file passed, because the rule was "it has an `offset`
  or a `limit`, so it must be a targeted read"
- reads issued by other tools, such as third-party MCP servers, fell outside the matcher

Version 0.2.0 rewrites the hooks in Python to close those gaps.

## Statistics

**Replaying the 0.2.0 hooks against real history.** Tool calls from 105 recorded Claude Code
sessions were reprocessed through the parser and the decision logic, with the threshold set to
100 lines:

| Metric | Value |
|---|---|
| Tool calls analyzed | 12,468 |
| With a file read detected | 1,354 |
| Denied | 364 |
| Allowed within the edit window (≤ 80 lines) | 1,027 |
| Allowed and counted toward the running total | 66 |
| Denied by the accumulated slices | 4 |
| Lines that would not have entered the context | 165,341 |

Those 165 thousand lines amount to somewhere between 1.6 and 2 million tokens, at 10-12 tokens
per line of code. The figure is conservative: the replay resolves absolute paths, so commands
written relative to a stale working directory counted as undetected.

For comparison, version 0.1.0 recorded **1** interception across the same history.

**Real delegation to the local model.** Measured with `gemma4:e4b` on Apple Silicon:

| Input | Tokens to Ollama | Tokens to Claude | Time |
|---|---|---|---|
| One 276-line file | 3,318 | 2,252 | 53 s |
| One 575-line file, 2 chunks | 13,383 | 378 | 68 s |

The second row is the typical case: the file cost 13 thousand tokens on the local model and 378
on Claude. The first shows the risk of broad questions, which make the local model enumerate
everything and hand back nearly the original volume. **Specific questions compress, vague ones
do not.**

Latency is the real cost: tens of seconds per delegation. Smaller models answer faster and lose
precision on line numbers.

**Test suite.** 27 cases, built from the exact commands that 0.1.0 let through.

## How it works

Three hooks, one delegation script, one metrics script.

| Piece | Role |
|---|---|
| `hooks/check-file-size` | `PreToolUse` on `Read`. Computes effective lines: `min(limit, total - offset)`. |
| `hooks/check-bash-read` | `PreToolUse` on `Bash` and on MCP tools that execute shell. Parses `cat`, `head`, `tail`, `sed -n`, `awk`, `nl`, `bat`, `rtk read`. |
| `hooks/session-start` | `SessionStart`. Injects the routing rule and records whether Ollama is up. |
| `scripts/bulk-read` | Builds the message, calls Ollama, prints the answer. |
| `scripts/shunt-stats` | Summarizes the log: decisions, most-blocked files, delegated tokens. |

`session-start` is what makes the plugin get used rather than discovered by accident. Without
it, the model only learns the shunt exists when a block happens, and the natural reaction to a
block is to try to route around it.

### Decision rules

In `hooks/lib/shunt_common.py`:

1. A read of up to `SHUNT_EDIT_WINDOW` (80) lines always passes and **is not counted**. That is
   the window Claude needs to edit after consulting the local model. Without this exception the
   editing flow breaks.
2. A read above `SHUNT_MIN_LINES` (350) is denied, and the denial message carries a
   ready-to-paste `bulk-read` command.
3. **Anti-slicing.** The ranges read from each file are stored per session as a union of
   intervals. Once the accumulated coverage passes the threshold, the next slice is denied.
   Rereading the same range does not grow the total.
4. Several files in a single command above `SHUNT_MAX_TOTAL_LINES` (3× the threshold) are denied
   as well.
5. If Ollama does not answer, **nothing is blocked**. The probe is cached for two minutes.
   Blocking with nowhere to delegate would only stall Claude.

### What the parser recognizes

It covers `cd dir && cat file`, `2>/dev/null` and `2>&1`, `&&`/`;`/`||`/newline separators,
pipes (`cat f | head -40` counts 40 lines, while `cat f | grep x` is a search and passes),
globs, `head -n150` in every flag spelling, `tail -n +5`, `sed -n '30,120p'` and range lists,
`awk 'NR>=10 && NR<=50'`, `sed -i` as an edit rather than a read, and the `rtk read` prefix left
by CLI proxies that rewrite `cat` before this hook sees it.

It does not cover heredocs (`<<EOF`) or paths held in shell variables (`cat "$f"`). Both are
logged as `skip` with the reason, instead of passing silently.

## Dependencies

| Requirement | What for | Note |
|---|---|---|
| [Ollama](https://ollama.com) running | the reader model | `ollama serve` |
| A pulled model | same | `ollama pull gemma4:e4b`, or another via `SHUNT_MODEL` |
| `python3` ≥ 3.9 | the three hooks | **standard library only**, no `pip install` |
| `bash` | `bulk-read` | 3.2+, the one shipped with macOS works |
| `curl` and `jq` | talking to Ollama's API | `brew install jq` |
| Claude Code | the host | a version that accepts `hookSpecificOutput` |

No external Python dependencies, by choice: a hook that fails because a package is missing from
the environment is a hook that protects nothing.

## Installation

```bash
claude plugin marketplace add tarcisiojr/shunt-ollama
claude plugin install shunt-ollama@shunt-ollama
```

Restart Claude Code. Before testing, make sure the local model is ready:

```bash
ollama serve &            # if it is not running yet
ollama pull gemma4:e4b
```

Ask Claude to read a file longer than 350 lines. It should report that the read was denied and
call `bulk-read` instead.

### Without a marketplace

Copy `hooks/hooks.json` into the `hooks` section of your `settings.json`, replacing
`${CLAUDE_PLUGIN_ROOT}` with the absolute path of your clone, and place
`skills/bulk-reader/SKILL.md` at `.claude/skills/bulk-reader/SKILL.md`.

### Tuning the threshold

The 350-line default comes from the original plugin. For aggressive use, something between 100
and 150 catches far more reads. In `~/.claude/settings.json`:

```json
{
  "env": {
    "SHUNT_MIN_LINES": "150",
    "SHUNT_MODEL": "gemma4:e4b",
    "SHUNT_NUM_CTX": "32768"
  }
}
```

## Using bulk-read

```bash
scripts/bulk-read --question "Which public methods exist and what does each one do?" --paths src/Big.java
scripts/bulk-read --question "How is the token renewed?" --paths launcher.sh lib/
scripts/bulk-read --question "Where are the handlers?" --glob 'src/**/*.rs'
scripts/bulk-read --question "What changed and where?" --cmd "git diff main"
git diff | scripts/bulk-read --question "Summarize per file" --stdin
```

Directories in `--paths` are expanded, skipping `.git` and `node_modules`. With no `--question`,
the default asks for public symbols, responsibilities, dependencies, and entry points.

The answer goes to stdout as `- Name (path:Lstart-Lend): description`. stderr carries
`[shunt: N input tokens | M output | Xs | model]`. Files larger than `SHUNT_NUM_CTX` are chunked
automatically, preserving original line numbers, and the script warns when the prompt gets close
to truncation.

Two phases, and the second is mandatory before editing: **ask** the local model, then **read
surgically** with `offset`/`limit` on the slice it pointed at. The local model can be off by a
few lines, so verify exact values before an `Edit`.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SHUNT_MODEL` | `gemma4:e4b` | Ollama model |
| `SHUNT_TEMPERATURE` | `0.2` | same as the original |
| `SHUNT_NUM_CTX` | `32768` | context window; Ollama starts at 4096 if unset, and then truncates silently |
| `SHUNT_KEEP_ALIVE` | `30m` | keeps the model loaded between calls |
| `SHUNT_MIN_LINES` | `350` | blocking threshold |
| `SHUNT_EDIT_WINDOW` | `80` | reads up to this size always pass and are not counted |
| `SHUNT_MAX_TOTAL_LINES` | `3 × MIN_LINES` | sum across several files in one command |
| `SHUNT_TIMEOUT_SECONDS` | `180` | `curl` timeout |
| `SHUNT_HOOK_LOG` | `~/.claude/shunt.log` | TSV decision log |
| `SHUNT_ASSUME_OLLAMA` | empty | `1` skips the probe and always blocks (tests/CI) |
| `OLLAMA_HOST` | `http://localhost:11434` | endpoint |

## Metrics and debugging

```bash
scripts/shunt-stats
scripts/shunt-stats --since 2026-09-01
```

Shows decisions per tool, most-blocked files, tokens delegated to Ollama against tokens returned
to Claude, and how often a block converted into a delegation.

The log is TSV with eight columns:

| # | Column | Content |
|---|---|---|
| 1 | timestamp | ISO 8601, local time |
| 2 | session | Claude Code session id, or `-` outside a session |
| 3 | tool | `Read`, `Bash`, the MCP tool name, `bulk-read`, `SessionStart` |
| 4 | decision | `allow`, `deny`, `skip`, `ok`, `error`, `info` |
| 5 | reason | see below |
| 6 | file | absolute path, or `-` |
| 7 | total | lines in the file |
| 8 | effective | lines that would enter the context |

Reasons: `edit-window` (≤ 80 lines, allowed), `counted` (allowed and added to the total),
`single-read` (denied on size), `cumulative` (denied on accumulated slices), `multi-file`
(denied on the sum), `ollama-off` (allowed because no model is available), plus `heredoc` and
`unresolved:$VAR` (not analyzable).

Per-session state lives in `$TMPDIR/shunt-state-<session_id>.json`. Deleting it resets the
running totals.

## Tests

```bash
python3 -m unittest discover -s tests
flake8 --max-line-length=100 hooks/lib tests scripts/shunt-stats
shellcheck scripts/bulk-read scripts/lib/ollama.sh
```

The cases come from real commands in sessions where 0.1.0 intercepted nothing. When adding
support for a new command, write the escaping case first.

Code comments and inline documentation are in Brazilian Portuguese.

## Known limitations

- **Latency.** Tens of seconds per delegation. That is the price of not spending context.
- **Line-number precision.** A 4B model is off by a few lines. Hence the rule to reread the
  slice before editing.
- **Heredocs and shell variables** are not analyzed.
- **A vague question compresses poorly.** The local model enumerates everything and hands back
  nearly the original volume.
- **Read-only.** The original's `code-writer` mode was not ported. The script would be
  analogous: a message shaped as `Spec: ...\n\nReference:\n<file>` and a system prompt ending in
  "Output only the code. No markdown fences, no explanation."

## Changelog

- **0.2.0** — Python hooks, effective lines, per-session anti-slicing, coverage of MCP tools
  that execute shell, a `SessionStart` hook, `--cmd`/`--stdin`/`--glob`, automatic chunking,
  `keep_alive`, TSV logging, and `shunt-stats`.
- **0.1.0** — initial port: the original hooks with the transport swapped for Ollama.

## License

Apache 2.0, the same as Spotify's original repository. See [LICENSE](LICENSE) and
[NOTICE](NOTICE).
