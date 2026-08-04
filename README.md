# sol

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![npm](https://img.shields.io/npm/v/%40bytonylee%2Fsol?logo=npm&logoColor=white)
![Runtime](https://img.shields.io/badge/runtime-standard%20library-2e7d6d)
![Status](https://img.shields.io/badge/status-experimental-c27d35)

![sol execution workflow](docs/assets/readme-thumbnail.png)

A coding agent harness for the supported Solar models `solar-open2` and
`solar-pro4`.

[한국어](README.ko.md)

One design premise: **measure the model's behavior instead of guessing,
then pin it down as constants.** All measured values live in one place,
[src/sol/model.py](src/sol/model.py), with the evidence recorded next to
each constant. No other module second-guesses the model.

## What makes this model different

Three properties shaped the harness.

**Reasoning eats the output budget.** `reasoning` arrives as a separate
field from `content`, but they share the same `max_tokens`. Skimp on the
budget and you get an empty response. On practical tasks, 512 and 1024
returned `content=""`; answers only appeared at 2048. So the default is
the model's ceiling, 131072. Unused budget is not billed.

**Tool specs are part of the cache prefix.** Changing a single tool drops
`cached_tokens` from 13056 to 0. So the prefix is frozen, changes are
allowed only through `invalidate()`, and when the cache breaks the ledger
points at the segment responsible.

**Broken tool pairs mean HTTP 400.** A `tool_calls` message without its
responses, or an orphaned tool message, gets the request rejected. So
compaction only ever cuts at pair boundaries.

## Measured contract

| Item | Value |
| --- | --- |
| Max output | 131,072 tokens |
| Context | 1,000,000-token `solar-open2` baseline |
| `reasoning_effort` | CLI `off`/`on` mapped to API `none`/`high` |
| Cache chunk and minimum prefix | 1,088 tokens |
| Cache warmup | 3 requests |
| Korean token density | 1.85 chars/token (English 6.48) |
| Vision | unsupported (`Image input is not allowed`) |
| `logprobs` | accepted, but always returns `null` |

The CLI exposes only `off`/`on` for reasoning and maps them to the API's
`none`/`high` values. `solar-open2` is the measured model; `solar-pro4` is
supported through the same request path but remains a separate baseline.

Sampling parameters are fixed at `temperature=0`, `top_p=1`,
`presence_penalty=0`, and `frequency_penalty=0`.

## Installation

`sol` is a Python runtime. npm and Bun expose it through a small Node launcher;
curl, Homebrew, and Git use the Python launcher directly. The runtime has no
third-party Python or npm dependencies.

### Required environment

- **Python 3.10+** is required for every installation method.
- **Node.js 18+** is additionally required for npm and Bun installations.
- **Internet access and `UPSTAGE_API_KEY`** are required for model calls.
- **An ANSI-compatible interactive terminal** is required for the full-screen
  TUI. Non-interactive CLI execution does not require it.

`git`, `gh`, `rg`, LSP servers, and MCP servers are optional integrations, not
base installation requirements. Choose one installation method below; every
method provides the same `sol` command.

### npm

After the package is published:

```bash
npm install --global @bytonylee/sol
sol --help
```

### Bun

```bash
bun add --global @bytonylee/sol
sol --help
```

### curl

This installs the current `main` branch under `~/.local/share/solar-code` and
links `sol` into `~/.local/bin`:

```bash
curl -fsSL https://raw.githubusercontent.com/bytonylee/solar-code/main/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
sol --help
```

### Homebrew

The repository includes a formula that tracks the current `main` branch:

```bash
brew install --HEAD https://raw.githubusercontent.com/bytonylee/solar-code/main/Formula/sol.rb
sol --help
```

### Git

```bash
git clone https://github.com/bytonylee/solar-code.git \
  "$HOME/.local/share/solar-code"
mkdir -p "$HOME/.local/bin"
ln -sfn "$HOME/.local/share/solar-code/bin/sol" "$HOME/.local/bin/sol"
export PATH="$HOME/.local/bin:$PATH"
sol --help
```

### Agent installation prompt

Ask an agent to use this prompt:

> Install the `sol` CLI/TUI for this repository. Use the first available
> method in this order: npm, Bun, curl, Homebrew, then git. Confirm Python
> 3.10+ is available. For npm or Bun, also confirm Node.js 18+. Install only
> the runtime and its launcher, and do not install tests, caches, experiments,
> or `diagnose_solar.sh`. On macOS, run `sol --set-api-key` so I can enter the
> required `$UPSTAGE_API_KEY` through the hidden Keychain prompt. Offer
> `sol --set-tinyfish-api-key` only as an option. Never ask me to send, print,
> log, or commit an API key. Refer to keys only as `$UPSTAGE_API_KEY` and
> `$TINYFISH_API_KEY`. Verify `sol --help`, then report the installation
> method, executable path, and version. `sol` without arguments opens the TUI;
> `sol -p "..."`
> runs the CLI.

### Configure

`sol` resolves each key in this order: process environment, project `.env`,
macOS Keychain, then the installation directory. Key values are never printed;
messages and records use only `$UPSTAGE_API_KEY` or `$TINYFISH_API_KEY`.

#### Global secure input on macOS

Use the CLI prompts. Input is hidden and saved in the login Keychain, not in
shell history or a plaintext Solar configuration file:

```bash
sol --set-api-key
sol --set-tinyfish-api-key   # optional
```

The same prompts are available inside the TUI:

```text
/api-key
/tinyfish-key
```

To import values that are already exported, pipe the variable expansion rather
than writing the literal key in the command. Neither command prints the value:

```bash
echo -n "$UPSTAGE_API_KEY" | sol --set-api-key
echo -n "$TINYFISH_API_KEY" | sol --set-tinyfish-api-key
```

`sol` reads the Keychain directly. To expose the same global values to other
shell programs, add Keychain lookups to `~/.zshrc`; the recorded lines contain
only command substitutions, not literal keys:

```bash
echo 'export UPSTAGE_API_KEY="$(security find-generic-password -a "$USER" -s solar-code:UPSTAGE_API_KEY -w)"' >> ~/.zshrc
echo 'export TINYFISH_API_KEY="$(security find-generic-password -a "$USER" -s solar-code:TINYFISH_API_KEY -w)"' >> ~/.zshrc
source ~/.zshrc
```

On systems without macOS Keychain, provide environment variables through the
OS secret manager or use a project-local `.env` protected with mode `600`.

#### Project-local `.env`

For persistent project configuration, create `.env` beside the directory where
you run `sol`. When using a Git checkout, start from the included template:

```bash
cp .env.example .env
chmod 600 .env
```

For npm, Bun, curl, or Homebrew installations, create the same `.env` file
manually. Enter each value after `=` without quotes:

```dotenv
# Required for Solar model calls
UPSTAGE_API_KEY=

# Optional; leave empty when TinyFish is not used
TINYFISH_API_KEY=
```

Never commit `.env`, paste keys into prompts, or include them in logs. You can
instead provide the same names through your shell or a secret manager. An
environment variable takes precedence over `.env`.

When `TINYFISH_API_KEY` is empty or absent, the default search chain tries the
keyless Naver and Mojeek providers. Use
`SOL_SEARCH_PROVIDER=auto|tinyfish|naver|mojeek` to force a provider.

The default runtime model is `solar-pro4`. Set `SOL_MODEL=solar-open2` to use
the measured baseline explicitly, or set it to another supported model for an
experiment. The TUI marks `solar-pro4` as unverified because the output,
context, and cache constants are measured for `solar-open2`.

Maintainers can inspect the npm payload before publishing with:

```bash
npm pack --dry-run
npm publish --access public
```

## Usage

The command line exposes the prompt, automatic approval, thinking, model
selection, session continuation, and Git branch selection. `--help` is the single
entry point for the complete command reference. In the full-screen TUI,
`/model` opens the read-only model contract and current runtime settings.

```bash
sol                                      # full-screen TUI
sol -p "summarize this repository"       # CLI, approve each action
sol -p "fix and test the bug" --yolo     # CLI, automatic approval
sol -p "review this design" --think on
sol -p "quick review"
sol --continue 20260803-120000-a1b2c3d4
sol --branch feature/simple-command
```

| Option | Meaning |
| --- | --- |
| `-p PROMPT` | Request to run in the CLI. Omit it to open the TUI |
| `--yolo` | Approve file edits and shell commands automatically |
| `--think on|off` | Enable or disable reasoning. Defaults to `off` |
| `--model solar-open2|solar-pro4` | Select a supported model. Defaults to `solar-pro4` |
| `--set-api-key` | Securely prompt for and globally store `$UPSTAGE_API_KEY` |
| `--set-tinyfish-api-key` | Securely prompt for and globally store optional `$TINYFISH_API_KEY` |
| `--continue SESSION_ID` | Continue the exact saved session |
| `--branch BRANCH_NAME` | Switch to the branch, creating it when absent |
| `--help` | Show the complete reference and examples |

Without `--yolo`, file edits and shell commands are available but require
approval each time. `--yolo` does not remove workspace containment,
read-before-write, checkpoints, or the dangerous-command blocklist. The
CLI summary and the TUI's exit message or `/status` command show the session ID.

A session is written only once it records something. A run that ends without
any input leaves no file and therefore shows no session ID. Every other session
is saved and can be reopened with `--continue SESSION_ID`.

Solar keeps user-scoped state and reusable configuration under `~/.solar`,
following the same home-directory pattern as Codex. Set `SOLAR_HOME` to use a
different location. The layout is:

```text
~/.solar/
├── AGENTS.md       # user-wide instructions, loaded before project rules
├── skills/         # reusable skills, one <name>/SKILL.md per skill
└── sessions/       # append-only session JSONL files
```

Project `AGENTS.md` files and project skill directories remain supported and
override user defaults where names overlap.

The TUI has twelve commands: `/help`, `/think on|off`, `/yolo on|off`,
`/plan on|off`, `/goal`, `/model`, `/api-key`, `/tinyfish-key`, `/status`,
`/undo`, `/clear`, and `/quit`.
`/model` separates the values that
can change during a session from the measured values pinned for `solar-open2`.
Use `/think on|off` to change reasoning; the model ID, output ceiling, and
sampling parameters stay fixed. API keys are never shown. Calling `/yolo`
without an argument toggles the current mode, and `Shift+Tab` toggles the
same mode from the keyboard. Complete commands with `Tab` or `Enter`. `Ctrl+Z` undoes
the latest input edit and `Ctrl+R` restores the latest workspace checkpoint.
Terminals supporting the kitty keyboard protocol on macOS also accept
`Command+Z` and `Command+R`.

`solar-pro4` is accepted by the same OpenAI-compatible request path, but its
output, context, and cache behavior has not been measured in this repository.
The `/model` panel labels those values as a `solar-open2` baseline rather than
presenting them as verified `solar-pro4` limits.

The regular CLI streams progress to stderr and answers to stdout. Tool
results appear as soon as they finish; shell output streams line by line.

### Completion contract

Multi-step work is tracked outside the conversation. A final answer cannot
pass while a task is `open` or `doing`; the retry forces a real tool call so
the agent must continue the work or mark a concrete blocker. After a file
write, the final report must name a changed path and state what validation was
run or why it was not run. A reported `blocked` task may stop execution, but
the episode remains incomplete rather than becoming a successful run.

The normal loop has no fixed round limit, so long work is not cut off by a
round count. Instead a stall detector ends runs that stop making progress:
identical repeated tool calls, a run of consecutive tool failures, or rounds
without any new successful work. An absolute cap remains as a safety net.
Stalling, exhausting a completion gate, receiving an empty answer, or losing
the model request never produces a silent `answer=""`. The harness makes one
tool-free wrap-up request and falls back to a deterministic status report if
that request also fails. Stalled and gate-exhausted runs remain incomplete,
are visible in CLI/TUI status with the detected reason, and return a
non-zero CLI exit code even when a wrap-up message exists. Candidate answer
text is buffered until output guards accept it, so a rejected completion is
not leaked to stdout before its correction.

## Structure

Dependencies flow one way with no cycles. `model.py` imports nothing; the
loop only delegates to the layers below.

```
model.py       measured constants. If a value is wrong, fix this file,
               not the harness
client.py      API boundary. Preserves the whole response envelope
credentials.py global macOS Keychain storage and $API_KEY-only display markers
prefix.py      cache stability. StablePrefix, AppendOnlyLog, CacheLedger
compaction.py  3-stage compression that honors tool pairs
               (snip -> prune -> summary)
tools.py       registry. Always returns a tool message, even on failure
loop.py        turn lifecycle. Includes starved retry, tool-call nudge,
               incomplete-state preservation, and forced final wrap-up
guards.py      measurement-based detectors: degenerate repetition cutoff,
               unfinished-task gate, unbacked verification claims, and
               final-report completeness
spec.py        input specification: turns vague instructions into a
               scoped plan with done-criteria before the loop runs
goal.py        completion criteria (Goal), the GoalGate processor, and the
               outer Ralph loop that re-issues unmet criteria
interact.py    ask_user tool. Auto-selection in non-interactive runs is
               always recorded, never a silent assumption
websearch.py   TinyFish -> Naver -> Mojeek search chain.
               Failure is an explicit error, never fabricated results
cacheprobe.py  cache hit-rate probe. Measures only the API's
               usage.prompt_tokens_details.cached_tokens, excluding warmup
cacheprofile.py model-specific cache chunk and promotion profiles
codesearch.py  AST/declaration-based outline and symbol search
lsp.py         optional LSP definitions/references when a server is configured
mcpclient.py   optional stdio MCP servers with fixed tool schemas

workspace.py   file read/write/search/exec. Enforces read-before-write
checkpoint.py  undo. The core is external-change conflict detection
worktree.py    isolates parallel implementations via git worktree
session.py     JSONL tree. Rewind and fork never destroy the past
tui.py         full-screen TUI. cfonts title, SSE token streaming,
               4-dot-height 8-frame spinner in Upstage lavender. View logic
               is terminal-free and tested
progress.py    live CLI spinner, mode footer, and tool-result display
context.py     AGENTS.md / SKILL.md assembly. Deterministic, or no cache
processors.py  hooks around the loop. Guardrails and secret redaction
hooks.py       external command hooks from .sol/hooks.json
subagent.py    4-role delegation (explorer/implementer/reviewer/tester)
tracker.py     plans and progress. Session-scoped to prevent context bleed
git.py         learns repo conventions to write commit messages
github.py      PRs/issues via gh CLI. Tokens stay in gh
cassette.py    traffic record and replay
determinism.py Korean number/unit/null normalization
recall.py      BM25 search over past sessions. Hangul bigram tokenizer
status.py      cache and cost visibility
permissions.py permission presets and legacy flag normalization
```

## Validation

The distribution intentionally excludes test sources, cache files, and
experiment records. Maintainers validate the source tree before publishing;
the runtime itself uses only the Python standard library. `package.json` and
`package-lock.json` contain npm packaging metadata only; the Node launcher has
no runtime dependencies. `.env.example` contains empty placeholders only.

## Known limitations

With tools registered, the model tends to call them even when unneeded.
For pure computation, pass an empty `Registry()`.

Tool-call omission at `effort=off` was originally observed but has not
reproduced in 60 follow-up runs (single-turn and multi-turn). The
`tool_choice=required` nudge remains as zero-cost insurance; its evidence
status is "awaiting reproduction".

No image input, so screenshot-based work is impossible. Verification must
go through text observations (test output, hashes, diffs) instead.

Web search tries TinyFish first when `TINYFISH_API_KEY` is configured, then
keyless Naver and Mojeek providers. `SOL_SEARCH_PROVIDER` can force one
provider. Provider errors, bot challenges, parse failures, and responses with
no valid HTTP(S) sources are not treated as successful searches. When a task
explicitly requires external references, mutating tools remain blocked until a
source is observed; without one, the run ends incomplete. Image search is not
registered yet; direct page-body fetching is available through `web_fetch`.

The TinyFish integration follows the official [TinyFish Search API](https://docs.tinyfish.ai/search-api/reference)
contract. Keyless HTML providers are guarded by rate limits and explicit
blocked/invalid-response envelopes rather than fabricated results.

LSP tools are optional and require an available server configured in
`.sol/lsp.json`. MCP tools are also optional, use stdio servers configured in
`.sol/mcp.json`, and pass only the environment keys declared by each server.
Neither integration is required for basic CLI or TUI startup.

## Security

Credentials are read from environment variables or a local `.env` file and
are not part of the repository. Keep `.env`, generated session records,
dependency directories, and build output untracked. Do not paste credentials
into prompts, issues, pull requests, or diagnostic logs.
