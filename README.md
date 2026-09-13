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
| Threshold | file size | **bytes** of what the read brings, plus a per-session budget |
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
| One 276-line file, broad question | 3,318 | 2,252 | 53 s |
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

**Test suite.** 84 cases, built from the exact commands that earlier versions let through.

## How it works

Three hooks, one delegation script, one metrics script.

| Piece | Role |
|---|---|
| `hooks/check-file-size` | `PreToolUse` on `Read`. Charges the bytes the requested lines actually carry. |
| `hooks/check-bash-read` | `PreToolUse` on `Bash` and on any `mcp__*` tool: the shell command is looked up in `tool_input` by field name (`command`, `commands`, `cmd`, `script`, `code` with a shell `language`). Parses `cat`, `head`, `tail`, `sed -n`, `awk`, `nl`, `bat`, `rtk read`. |
| `hooks/session-start` | `SessionStart`. Injects the routing rule and records whether Ollama is up. |
| `scripts/bulk-read` | Builds the message, calls Ollama, prints the answer. |
| `scripts/shunt-stats` | Summarizes the log: comparison by version and by model, coverage, slicing, delegated tokens. |
| `scripts/shunt-model` | Lists the models pulled into Ollama and writes the choice to `SHUNT_MODEL` in `settings.json`. |
| `scripts/release` | Validates, tags and publishes a release for the version in the manifest. |

`session-start` is what makes the plugin get used rather than discovered by accident. Without
it, the model only learns the shunt exists when a block happens, and the natural reaction to a
block is to try to route around it.

### Decision rules

In `hooks/lib/shunt_common.py`. **The unit is the byte, not the line.** The threshold answers
two questions: by total file size it decides whether the plugin applies at all, and when it
does, it becomes that file's reading budget for the session.

1. A file of up to `SHUNT_MIN_BYTES` (6,480, about 2,100 tokens) is **out of scope**. Delegating
   costs more than reading it, so it never enters a budget. Logged as `small-file`.
2. Above that, every read of the file consumes the budget, charged by the bytes those lines
   actually carry. There is no exempt size and no free first read. Ranges are stored per session
   as a union of intervals, so rereading the same range does not grow the total, and splitting a
   read into pieces buys nothing.
3. Once the budget is spent, `SHUNT_ESCAPE_BYTES` (2,880) remain available in reads of up to
   `SHUNT_EDIT_BYTES` (2,880) each, so editing a slice the local model pointed at stays
   possible. Measured in bytes rather than in number of reads: counting reads allowed three full
   ones, which handed small files back whole.
4. Several files in a single command above `SHUNT_MAX_TOTAL_BYTES` (3× the threshold) are denied
   as well.
5. A binary file is left alone, logged as `binary`. Counting newlines in binary data produces
   meaningless numbers, and there is no way to delegate an image to a text model anyway.
6. If Ollama does not answer, **nothing is blocked**. The probe is cached for two minutes.
   Blocking with nowhere to delegate would only stall Claude.

The threshold is floored at twice the edit window, logged as `threshold-floor` at session start.
`SHUNT_MIN_LINES` from earlier versions is still read and converted at 36 bytes per line, the
median of the measured corpus, so an existing configuration keeps working.

Protection is weaker in proportion for files just above the threshold. The real gain is on large
files, where the budget is a small fraction of the whole.

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

### Why bytes and not lines

A line looked like a good stand-in for cost, and it is not. Measured across 11,692 files in
three projects, bytes per line ranges from 10 to 990. Between the 10th and 90th percentiles it
only varies 2.6 times, which is why the line-based rule seemed to work, and the tail is where it
breaks.

Comparing the old 180-line threshold against an equivalent token threshold over those files:

| Result | Files | Share |
|---|---|---|
| Agree | 11,021 | 94.3% |
| Passed on lines, expensive in tokens | 427 | 3.7% |

That 94% hides what matters. The 427 files that escaped hold 1.2 million tokens, and the worst
of them is a single-line JSON of 131 thousand tokens, more than half a 200k context window. The
line rule never even looked at it.

The error is asymmetric. Blocking a cheap file wastes one round trip, roughly 1,500 tokens.
Letting a dense one through can waste the conversation. That is a ratio of nearly a hundred to
one, and it decides which way to err.

Switching the unit protected 438 files holding 1.67 million tokens, and released 219 files that
were being blocked for having many short lines.

The conversion also stopped being a guess. Measured on the Gemma tokenizer, bytes per token runs
from 2.09 in dense JSON to 3.66 in generated Python; the earlier estimate of 4.0 understated
cost by about 30%, and by nearly 90% exactly where the risk is highest. The ratio is now learned
from the same calibration that sizes the timeout.

### The silent truncation

Measured with `num_ctx=32768`: a prompt of up to about 27,900 tokens is processed whole, and from
about 30,000 on `prompt_eval_count` drops to exactly 16,387, which is `num_ctx/2`. When the
context overflows, llama.cpp discards half of it, and none of this raises an error. The model
answers about whatever survived, with the same confidence.

That is why chunking targets `SHUNT_PROMPT_FRACTION` (80%) of the window rather than all of it,
and why the byte budget assumes the worst bytes-per-token ratio rather than the average one. The
ratio varies almost 2.5 times between dense JSON and code; a chunk sized for code overflows on
JSON, while a chunk sized for JSON merely costs one extra part on code. Measured on a 190 KB
shell script, the difference was five parts instead of four.

Every call also compares the tokens it expected to send against what Ollama reports processing.
A shortfall means the content was cut, and the answer is **rejected** rather than returned, since
a partial answer that looks complete is worse than no answer.

### Adapting to the machine

Model speed is a property of the hardware, not of the plugin. The same `gemma4:e4b` runs above
1,000 tokens per second on a dedicated GPU and below 30 on a laptop CPU. A number hardcoded here
would be wrong for almost everyone, so nothing is hardcoded: each call records how many tokens
it processed and how long it took, per model, in `SHUNT_CALIBRATION`.

The last 20 samples give two figures. The 20th percentile is the pessimistic rate, used to size
the timeout, and the median is the typical rate, used for the time shown in a denial message. A
slow machine gets a longer timeout automatically, and switching `SHUNT_MODEL` starts a separate
history rather than reusing the old one.

Two filters keep the history honest. Calls under 500 tokens are ignored, because they measure
noise. Samples implying more than `SHUNT_MAX_PLAUSIBLE_RATE` (3,000 tokens/s) are discarded as
well: Ollama reuses a cached prompt when a call repeats the same prefix, and the measured time
collapses. Learning that would shrink the timeout right before a fresh, slow prompt.

Until there is history, the assumed rate is deliberately low, which buys a generous timeout on
the first call. The second call is already measured.

**Raising `SHUNT_NUM_CTX` is not the answer for a large file.** Measured here, a ~48,000-token
prompt at `num_ctx=65536` was processed whole but took 1,328 seconds, against 78 seconds for an
equivalent call at 32,768. Processing time grows far faster than linearly with the window, so
splitting into parts always beats widening it. Raise the window only if a single indivisible
unit does not fit.

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

The default is 6,480 bytes, roughly 2,100 tokens. Because of the floor at twice the edit
window, values below that have no effect unless you shrink `SHUNT_EDIT_BYTES` too. For
aggressive use, lower both. In `~/.claude/settings.json`:

```json
{
  "env": {
    "SHUNT_MIN_BYTES": "4000",
    "SHUNT_EDIT_BYTES": "1500",
    "SHUNT_MODEL": "gemma4:e4b",
    "SHUNT_NUM_CTX": "32768"
  }
}
```

A tighter threshold blocks more, and every block costs either a delegation of tens of seconds
or a piece of information Claude will do without. Use `scripts/shunt-stats` to see where your
own setting lands before tightening it.

## Using bulk-read

```bash
scripts/bulk-read --question "Which public methods exist and what does each one do?" --paths src/Big.java
scripts/bulk-read --question "How is the token renewed?" --paths launcher.sh lib/
scripts/bulk-read --question "Where are the handlers?" --glob 'src/**/*.rs'
scripts/bulk-read --question "What changed and where?" --cmd "git diff main"
git diff | scripts/bulk-read --question "Summarize per file" --stdin
scripts/bulk-read --questions "Which validations reject input?" "Where does it persist?" --paths import_service.py
scripts/bulk-read --dry-run --questions "..." "..." --paths a.py   # prints the prompt, no Ollama call
```

**A broad question goes in decomposed.** "What does this module do" makes the model enumerate
the whole file. `--questions` takes three to five specific subtasks, numbered in a single prompt
per part, so the time is that of one question. Measured with `qwen3.5:4b` over five vague-versus-
decomposed pairs, the median ratio went from 10% to 8% at no time cost; the large gain sits with
models that inflate more, and in what decomposition enables: abstention per subtask. The model abstains per subtask with
`not found: N`, and the script folds the abstentions into one count line per part. It is the
part of MinionS (arXiv:2502.15964) that applies here: decomposition and abstention, not the
remote cost, which the plugin already removes by construction. Every log line carries
`subtasks=`, and `shunt-stats` compares the median ratio with and without decomposition.

Directories in `--paths` are expanded, skipping `.git` and `node_modules`. With no `--question`,
the default asks for public symbols, responsibilities, dependencies, and entry points.

The answer goes to stdout grouped by file, with the path written once and the findings indented
under it:

```text
/path/to/install.sh
  186 conferir_checksum: aborts when the sha256 does not match
  220-245 acrescenta_ao_path: writes the managed block into the shell rc
```

Grouping exists because the path was the largest repeated string in the answer. At 63 characters
across 35 findings it cost more than the findings themselves; measured over three samples, the
grouped shape cut the answer by 26%.

**The question sets the price.** The answer enters your context, the file does not, so what
matters is the size of the answer, and that depends entirely on what you asked. "Which line
verifies the checksum?" comes back at 1% of the file. "Explain what the script does" comes back
at 10% or more, and on a small file it can exceed 100%, at which point reading it directly would
have cost the same. The script warns on stderr when the answer passes `SHUNT_WARN_RATIO` (50%)
of the content read, and `shunt-stats` reports the median and the worst case. Two narrow
questions are cheaper than one broad one, and a follow-up on the same paths is free.

stderr also carries `[shunt: N input tokens | M output | Xs | model]`. Files larger than
`SHUNT_NUM_CTX` are chunked automatically, preserving original line numbers, and the script
warns when the prompt gets close to truncation.

Two phases, and the second is mandatory before editing: **ask** the local model, then **read
surgically** with `offset`/`limit` on the slice it pointed at. The local model can be off by a
few lines, so verify exact values before an `Edit`.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SHUNT_MODEL` | `gemma4:e4b` | Ollama model; on macOS the `-mlx` variants (e.g. `qwen3.5:4b-mlx`) run on Ollama's own MLX backend with no plugin change |
| `SHUNT_TEMPERATURE` | `0.2` | same as the original |
| `SHUNT_NUM_CTX` | `32768` | context window; Ollama starts at 4096 if unset, and then truncates silently |
| `SHUNT_KEEP_ALIVE` | `30m` | keeps the model loaded between calls |
| `SHUNT_MIN_BYTES` | `6480` | out-of-scope size and per-file reading budget, floored at `2 × EDIT_BYTES` |
| `SHUNT_EDIT_BYTES` | `2880` | largest single editing read |
| `SHUNT_ESCAPE_BYTES` | `2880` | extra bytes for editing reads after the budget is spent |
| `SHUNT_MIN_LINES` | unset | legacy: converted at 36 bytes per line when `SHUNT_MIN_BYTES` is absent |
| `SHUNT_MAX_TOTAL_BYTES` | `3 × MIN_BYTES` | sum across several files in one command |
| `SHUNT_WARN_RATIO` | `50` | warns when the answer exceeds this share of the content read |
| `SHUNT_EXEMPT_TOOLS` | empty | name fragments of MCP tools whose output does not enter the context (a sandbox returning only a summary); the read is allowed and logged as `exempt-tool` |
| `SHUNT_SHELL_KEYS` | empty | extra `tool_input` field names to look for a shell command in, for MCP tools off the common shape |
| `SHUNT_TIMEOUT_SECONDS` | unset | fixed timeout in seconds; overrides the calculated one |
| `SHUNT_TIMEOUT_SLACK` | `300` | % of the predicted time allowed before giving up |
| `SHUNT_TIMEOUT_MIN` | `60` | floor of the calculated timeout |
| `SHUNT_TIMEOUT_MAX` | `600` | ceiling of the calculated timeout |
| `SHUNT_PROMPT_FRACTION` | `80` | % of `NUM_CTX` a prompt may use before Ollama starts discarding |
| `SHUNT_PROMPT_RESERVE` | `900` | tokens held back for the system prompt and chat template |
| `SHUNT_BYTES_PER_TOKEN_FLOOR` | `16` | tenths of a byte per token, worst case, used to size chunks |
| `SHUNT_BYTES_PER_TOKEN_CEIL` | `40` | the other end of the same ratio, used to detect truncation |
| `SHUNT_CALIBRATION_SAMPLES` | `20` | measurements kept per model |
| `SHUNT_CALIBRATION_MIN_TOKENS` | `500` | smaller calls measure noise and are ignored |
| `SHUNT_EDIT_WINDOW` | unset | legacy: converted at 36 bytes per line when `SHUNT_EDIT_BYTES` is absent |
| `SHUNT_CALIBRATION` | `~/.claude/shunt-calibration.json` | learned speed, per model |
| `SHUNT_FALLBACK_RATE` | `40` | tokens/s assumed before the first measurement |
| `SHUNT_HOOK_LOG` | `~/.claude/shunt.log` | TSV decision log |
| `SHUNT_ASSUME_OLLAMA` | empty | `1` skips the probe and always blocks (tests/CI) |
| `OLLAMA_HOST` | `http://localhost:11434` | endpoint |

## Metrics and debugging

Three skills expose this to Claude: `bulk-reader` delegates a read, `shunt-model` lists and
switches the local model, and `shunt-stats` reads and interprets these numbers, so asking "how is the plugin doing?" is enough.

```bash
scripts/shunt-stats
scripts/shunt-stats --since 2026-09-01
scripts/shunt-stats --version 0.5.0
scripts/shunt-stats --file install.sh --top 20
scripts/shunt-stats --follow          # live: denies, delegations and the pairing between them
```

`--follow` tails the log live, like a `tail -f` that only shows what counts: every deny with the
bytes kept out of the context, every delegation with `pin`, `pout` and ratio, and the pairing
between the two. A delegation within ten minutes of a deny comes out marked with its origin and
the wait; a deny that passes ten minutes without one comes out as "no delegation", the sign
that Claude gave up. `--all` also shows `allow` and `skip`, to hunt slicing and blind spots. It
is the screen to keep open while using Claude in another window.

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
share of denials served by a delegation: the one for the same file within ten minutes or, failing
that, the most recent denial in the window, and each delegation serves one only. It is the number
that says whether the plugin is being used as a detour or merely as a brake. A sample under 30 events
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

The **Comparison by model** block groups delegations by the `model=` that `bulk-read` writes
since 0.11.0: delegations, tokens sent and returned, median ratio, seconds per delegation and
tokens per second. It answers whether a `SHUNT_MODEL` switch paid off, and `--model NAME` isolates
one of them.

The rest shows decisions per tool, most-blocked files, and tokens delegated to Ollama against
tokens returned to Claude.

The log is TSV with thirteen columns:

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
| 11 | covered | bytes of the file already read in this session |
| 12 | total bytes | size of the whole file |
| 13 | read bytes | bytes this read would bring |

Columns 10 and 11 exist so that diagnosing slicing is a query over the log rather than a
reconstruction from session transcripts. Lines written before 0.4.0 have eight columns and ones
from 0.4.0 have nine; `shunt-stats` reads all three shapes and groups the oldest as `<=0.3.0`,
which keeps the baseline for comparison. During an upgrade both versions appear in the same log:
sessions already open keep running the old hooks until restarted.

Reasons: `small-file` (file under the threshold, out of scope), `counted` (charged to the
budget), `escape` (charged to the escape balance after the budget was spent), `binary` (not text,
left alone), `single-read` (denied on size), `cumulative` (denied because the budget is spent),
`escape-exhausted` (denied because the escape balance is spent too), `multi-file` (denied on the
sum), `ollama-off` (allowed because no model is available), `threshold-floor` (the configured
threshold was raised to the floor), plus `heredoc` and `unresolved:$VAR` (not analyzable).

Earlier versions also wrote `always-free`, `edit-window` and `window-counted`, from the exempt
bands that 0.5.0 removed.

On `bulk-read` lines the reason is a `key=value` list: `model` (the Ollama model, since 0.11.0),
`files`, `chunks`, `subtasks` (number of `--questions` subtasks, since 0.12.0), `pin` and `pout`
(tokens sent and returned), `dur` (seconds) and `ratio` (answer as % of the content).

Per-session state lives in `$TMPDIR/shunt-state-<session_id>.json`. Deleting it resets the
running totals.

## Tests

```bash
python3 -m unittest discover -s tests
flake8 --max-line-length=100 hooks/lib tests scripts/shunt-stats scripts/release
shellcheck scripts/bulk-read scripts/lib/ollama.sh
```

### Releasing a version

Bump `version` in both files under `.claude-plugin/`, commit, push, then:

```bash
scripts/release --dry-run   # shows the tag, title and notes it would publish
scripts/release
```

It refuses to run on a dirty tree, on an already existing tag, or with unpushed commits, and it
runs the tests, flake8 and shellcheck before tagging, because a tag points at a commit forever.
The release notes come from that version's changelog entry in this README. Publishing the
release needs `gh` authenticated; without it the tag is still created and pushed, and the
command for the release is printed.

The cases come from real commands in sessions where 0.1.0 intercepted nothing. When adding
support for a new command, write the escaping case first.

**Language convention:** identifiers are always in English, including test names. Comments,
docstrings, and the messages printed to the user are in Brazilian Portuguese, which is also the
language of the routing text the hooks inject.

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

- **0.14.0** — the shell hook stops naming context-mode and intercepts any `mcp__*` tool,
  looking up the command in `tool_input` by field name, because every user has their own tools.
  `SHUNT_EXEMPT_TOOLS` exempts the ones that return only a summary, logged as `exempt-tool`, and
  `SHUNT_SHELL_KEYS` adds fields. The report shows conversion per tool, which is the data for
  deciding an exemption: in the first real session, half of the denials came from inside a
  sandbox.
- **0.13.1** — conversion and `--follow` pair a delegation with a denial by file path, and each
  delegation serves one denial only. The previous time-only rule reported sixteen denials as
  converted by a single one-file delegation, hiding that Claude gave up on the other fourteen.
  A multi-file delegation records only its first file; the rest fall back to time pairing,
  marked as such on screen.
- **0.13.0** — `shunt-stats --follow` tails the log live and pairs a deny with its delegation,
  marking the wait between the two and the deny that went ten minutes without one. `--all`
  includes `allow` and `skip`. The pairing logic lives in a class separate from the read loop,
  tested without waiting out the window.
- **0.12.0** — MinionS-style decomposition for broad questions. `--questions` sends numbered
  subtasks in a single prompt per part, the model abstains per subtask and the abstentions fold
  into one count line; `--dry-run` prints the prompt without calling Ollama, and the log gains
  `subtasks=`, which `shunt-stats` uses to compare the ratio with and without decomposition. The
  skill now guides decomposition before the call. Measured over five pairs with `qwen3.5:4b`:
  median ratio from 10% to 8%, same time (the gap that showed up first was prompt cache). The
  gain is modest because this model already answers a vague question at 7 to 16%; the piece
  stays for its zero cost and for the abstention.
- **0.11.1** — path restoration now accepts a header wrapped in brackets, backticks or quotes,
  and the mode drops a placeholder that `qwen3.5:4b-mlx` copied verbatim. Measured against the
  GGUF on the same question, the quantized MLX tied on time and got five line anchors wrong; the
  README note says how to use it, not that you should.
- **0.11.0** — the model becomes comparable. Every `bulk-read` line records `model=`,
  `shunt-stats` gains the comparison by model and the `--model` filter, and the `shunt-model`
  skill lists the pulled models and writes the choice to `settings.json`. `bulk-read` restores
  the full path in the answer header when the model shortens it, because Claude opens the file
  by that string and an abbreviated path broke the next step; the mode now demands the tag's
  `path` attribute, but small models ignore that often enough.
- **0.10.1** — documents why widening `SHUNT_NUM_CTX` does not help (a 48k-token prompt at
  65536 took 1,328s against 78s for an equivalent call at 32768) and removes two functions left
  dead by the previous release.
- **0.10.0** — fixes a silent truncation. Ollama discards half the context when a prompt
  overflows, without an error, and chunks were being sized at 80% of `num_ctx` using a assumed
  4 bytes per token, which landed just above the real ceiling. Large delegations were answering
  about part of the file. Chunks are now sized against the measured ceiling and the worst-case
  byte ratio, and every call compares the tokens it sent against what Ollama reports processing,
  rejecting the answer when they disagree.
- **0.9.0** — the decision unit changed from lines to bytes. A line is not a proxy for cost: a
  single-line JSON of 131 thousand tokens used to pass untouched. The switch protected 438 files
  holding 1.67 million tokens and released 219 that were blocked for having short lines. Bytes
  per token is now measured on the tokenizer in use instead of assumed, binary files are left
  alone, and the log carries byte columns.
- **0.8.0** — the timeout is derived from speed measured on the machine itself instead of a
  constant: each call records tokens and wall time per model, the 20th percentile sizes the
  timeout and the median feeds the estimate in a denial. Samples from a cached prompt are
  discarded, and the nanosecond conversion moved from `awk` to `jq`, where a decimal-comma
  locale cannot corrupt it.
- **0.7.0** — a `shunt-stats` skill lets Claude report and interpret the metrics on request, and
  `scripts/release` validates, tags and publishes a release for the version in the manifest.
  Versions 0.2.0 through 0.6.0 were tagged retroactively.
- **0.6.0** — the answer is grouped by file, with the path written once instead of repeated in
  every finding, which cut it by 26% over three samples. `bulk-read` warns when the answer
  exceeds `SHUNT_WARN_RATIO` of the content read, the ratio goes into the log, and `shunt-stats`
  reports median and worst case, because a broad question can cost more than the file itself.
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
