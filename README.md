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

## Statistics

**Effect of each version, straight from the log.** This is what `shunt-stats` prints, and it is
the only table here you can reproduce on your own usage:

| Version | Events | Block rate | Denials converted into a delegation |
|---|---|---|---|
| ≤ 0.3.0 | 589 | 26% | 0% |
| 0.4.0 | 1,238 | 5% | 0% |
| 0.5.0 | 45 | 48% | 71% |

The 0.4.0 row looks like a plugin at rest, and for a while it was read that way. Measuring
coverage instead of denials showed otherwise: 29 files had 60% or more of their content in the
context, several at 100%, assembled from small reads. Nine were above the threshold, 2,150 lines
that should have been stopped, and 13,603 of the 17,281 lines that entered came through the
always-free band. That is what 0.5.0 repaired, and why conversion is the row that matters: it
counts denials that turned into a delegation rather than into a shrug.
**A one-off replay, kept for reference.** Tool calls from 105 recorded Claude Code
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

**How to check this yourself.** Every log line carries the plugin version, the ranges it asked
for and the coverage reached so far, and `shunt-stats` turns that into a version comparison and
a slicing report. The numbers above came from the log itself, not from reading transcripts.

**Test suite.** 47 cases, built from the exact commands that earlier versions let through.

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

In `hooks/lib/shunt_common.py`. The threshold answers two questions: by total file size it
decides whether the plugin applies at all, and when it does, it becomes that file's reading
budget for the session.

1. A file of up to `SHUNT_MIN_LINES` (180) lines is **out of scope**. Delegating costs more than
   reading it, so it never enters a budget. Logged as `small-file`.
2. Above that, every read of the file consumes the budget. There is no exempt size and no free
   first read. The ranges are stored per session as a union of intervals, so rereading the same
   range does not grow the total, and splitting a read into pieces does not buy more lines.
3. Once the budget is spent, `SHUNT_ESCAPE_BUDGET` (80) extra lines remain available in reads of
   up to `SHUNT_EDIT_WINDOW` (80) lines each, so editing a slice the local model pointed at
   stays possible. Measured in lines rather than in number of reads: counting reads allowed
   three of 80, which handed back 300-line files whole.
4. Several files in a single command above `SHUNT_MAX_TOTAL_LINES` (3× the threshold) are denied
   as well.
5. If Ollama does not answer, **nothing is blocked**. The probe is cached for two minutes.
   Blocking with nowhere to delegate would only stall Claude.

The threshold is floored at twice the edit window, logged as `threshold-floor` at session start.

Protection is weaker in proportion for files just above the threshold: a 300-line file with a
budget of 180 and an escape of 80 can still reach 87% coverage. The real gain is on large files,
where 260 lines out of 4,000 is 6%.

The session-start hook states the rule without publishing the numbers. The earlier version
listed the exempt sizes, and the log showed the result: 41% of reads landed exactly inside the
exempt band, and files above the threshold reached the context whole, assembled from slices. A
published limit is a map of the way around it.

### Why the rules look like this

The rules above are not precautions, they are repairs. The first version was a
faithful port, with a regex over the command line, and in real usage logs it fired
**exactly once across two sessions**. That single block was worked around: the model
reread the same file in four slices with `sed -n`. It read everything, spent the same
tokens, and the local model was never called.

Almost no read passed through where the hooks were watching:

- `cd project && cat AGENTS.md` did not match the `^cat ` regex
- `cat file 2>/dev/null` was discarded by the redirection filter
- `sed -n`, `awk`, and `head -150` inside a loop went unrecognized
- `Read` with `limit: 620` on a 1200-line file passed, because the rule was "it has an `offset`
  or a `limit`, so it must be a targeted read"
- reads issued by other tools, such as third-party MCP servers, fell outside the matcher

That is what the lexical parser is for. Each of those five shapes is a test case.

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

The default is 250 lines. Because of the floor at twice the edit window, values below 160 have
no effect unless you shrink `SHUNT_EDIT_WINDOW` too. For aggressive use, lower both. In
`~/.claude/settings.json`:

```json
{
  "env": {
    "SHUNT_MIN_LINES": "160",
    "SHUNT_EDIT_WINDOW": "60",
    "SHUNT_MODEL": "gemma4:e4b",
    "SHUNT_NUM_CTX": "32768"
  }
}
```

A tighter threshold blocks more, and every block costs either a delegation of tens of seconds
or a piece of information Claude will do without. Measured on real logs, dropping from 250 to
160 raised the block rate from 32% to 41% and the denial count from 78 to 97.

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
| `SHUNT_MIN_LINES` | `180` | out-of-scope size and per-file reading budget, floored at `2 × EDIT_WINDOW` |
| `SHUNT_EDIT_WINDOW` | `80` | largest single editing read |
| `SHUNT_ESCAPE_BUDGET` | `80` | extra lines for editing reads after the budget is spent |
| `SHUNT_MAX_TOTAL_LINES` | `3 × MIN_LINES` | sum across several files in one command |
| `SHUNT_TIMEOUT_SECONDS` | `180` | `curl` timeout |
| `SHUNT_HOOK_LOG` | `~/.claude/shunt.log` | TSV decision log |
| `SHUNT_ASSUME_OLLAMA` | empty | `1` skips the probe and always blocks (tests/CI) |
| `OLLAMA_HOST` | `http://localhost:11434` | endpoint |

## Metrics and debugging

```bash
scripts/shunt-stats
scripts/shunt-stats --since 2026-09-01
scripts/shunt-stats --version 0.5.0
scripts/shunt-stats --file install.sh --top 20
```

The first section compares plugin versions, so you can tell whether a change actually worked
instead of guessing from timestamps:

```text
Comparação por versão
  versão      eventos  negativas    entrou  bloqueado   taxa  delegações  conversão
  <=0.3.0         589         50     17949       6297    26%        0+9!         0%
  0.4.0          1238          5     23977       1324     5%           4         0%
  0.5.0            45          7      1457       1366    48%           1        71%

  0.4.0 -> 0.5.0: taxa de bloqueio 5% -> 48%, conversão 0% -> 71%
```

`delegações` counts successful `bulk-read` runs, with `+N!` marking failures. `conversão` is the
share of denials followed by a delegation within ten minutes, which is the number that says
whether the plugin is being used as a detour or merely as a brake. A sample under 30 events
gets an explicit warning, and conversion under 20% gets one too.

A second section reports how much of each file reached the context, and flags slicing:

```text
Cobertura por arquivo e sessão (top 10 por percentual)
   coberto   total     %  leituras  fatias  arquivo
       240     299   80%         4       3  mia-cli/install.sh
       266    4289    6%        11      11  claude-local/claude-local.sh

  Fatiamento: 1 arquivo(s) com 50%+ de cobertura montada em 3+ leituras de até 80 linhas
```

`fatias` counts reads of up to 80 lines. A file with high coverage assembled almost entirely
from slices is the signature of reading around the budget, and `--file` narrows either section
to one path.

The rest shows decisions per tool, most-blocked files, and tokens delegated to Ollama against
tokens returned to Claude.

The log is TSV with eleven columns:

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
| 9 | version | plugin version that made the decision |
| 10 | ranges | the line ranges this read asked for, e.g. `1-80` or `10-25,60-90` |
| 11 | covered | total lines of the file already read in this session |

Columns 10 and 11 exist so that diagnosing slicing is a query over the log rather than a
reconstruction from session transcripts. Lines written before 0.4.0 have eight columns and ones
from 0.4.0 have nine; `shunt-stats` reads all three shapes and groups the oldest as `<=0.3.0`,
which keeps the baseline for comparison. During an upgrade both versions appear in the same log:
sessions already open keep running the old hooks until restarted.

Reasons: `small-file` (file under the threshold, out of scope), `counted` (charged to the
budget), `escape` (charged to the escape balance after the budget was spent), `single-read`
(denied on size), `cumulative` (denied because the budget is spent), `escape-exhausted` (denied
because the escape balance is spent too), `multi-file` (denied on the sum), `ollama-off`
(allowed because no model is available), `threshold-floor` (the configured threshold was raised
to the floor), plus `heredoc` and `unresolved:$VAR` (not analyzable).

Earlier versions also wrote `always-free`, `edit-window` and `window-counted`, from the exempt
bands that 0.5.0 removed.

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

- **0.5.0** — the threshold became a per-file reading budget and every exempt band is gone,
  closing the slicing route that let whole files reach the context. The escape allowance after
  the budget is spent is measured in lines. The routing text stops publishing the limits. The
  log gains the requested ranges and the accumulated coverage, and `shunt-stats` reports
  coverage and flags slicing, so diagnosing this no longer requires reading transcripts.
  Default threshold drops from 250 to 180.
- **0.4.0** — every log line carries the plugin version as a ninth column, and `shunt-stats`
  compares versions side by side, so the effect of a change is measurable instead of inferred.
  Eight-column lines from earlier versions are still read and grouped as `<=0.3.0`.
- **0.3.0** — the editing window now counts from the second read of a file onward, closing the
  80-line slicing bypass; an always-free tier of 25 lines keeps surgical edits possible;
  denials compare the cost of reading against the cost of delegating; the threshold is floored
  at twice the window, and its default drops from 350 to 250.
- **0.2.0** — Python hooks, effective lines, per-session anti-slicing, coverage of MCP tools
  that execute shell, a `SessionStart` hook, `--cmd`/`--stdin`/`--glob`, automatic chunking,
  `keep_alive`, TSV logging, and `shunt-stats`.
- **0.1.0** — initial port: the original hooks with the transport swapped for Ollama.

Each entry above was written after measuring the previous one against real logs.
`scripts/shunt-stats` is what makes that possible.
## License

Apache 2.0, the same as Spotify's original repository. See [LICENSE](LICENSE) and
[NOTICE](NOTICE).
