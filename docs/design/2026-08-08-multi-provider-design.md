# cswap multi-provider design

Date: 2026-08-08
Fork of: `realiti4/claude-swap` @ `b872b73` (v0.24.1, MIT)
Status: approved design, not yet implemented

## Goal

One tool manages Anthropic and OpenAI subscription accounts together. It rotates within each
provider by remaining quota. When every Claude account is spent, it puts a running Claude Code
session onto a Codex backend without a restart.

## Non-goals

- Replacing CLIProxyAPI. The Anthropic-to-OpenAI request translation stays in that project.
- API-key or metered billing. Subscription accounts only.
- Gemini, Qwen, iFlow. The provider seam makes them possible later. This pass does not add them.
- Changing any Claude behaviour that works today.

## Evidence

Every number below was measured on this box on 2026-08-08 unless labelled otherwise.

| Fact | Value | How |
|---|---|---|
| Codex auth file | `~/.codex/auth.json`, mode 0600 | read |
| Codex identity source | `tokens.id_token` JWT claims | decoded |
| Claims present | `email`, `chatgpt_account_id`, `chatgpt_plan_type`, `organizations` | decoded |
| Access token life | `exp - iat = 864000 s` (10 days) | decoded |
| id_token life | 3600 s (1 hour) | decoded |
| Codex usage endpoint | `GET https://chatgpt.com/backend-api/wham/usage` → 200 | curl |
| Usage fields | `rate_limit.primary_window.{used_percent,limit_window_seconds,reset_at}`, `secondary_window`, `credits` | curl |
| Upstream activity | 30 commits in 30 days, 5 releases in July 2026 | GitHub API |
| Upstream size | 17585 lines, `switcher.py` alone 5610 | `wc -l` |
| Test suite | 35 files, 32778 lines, ~1929 tests | `wc -l`, pyproject comment |
| Claude Code here | 2.1.219 | `claude --version` |

Cited, from `code.claude.com/docs/en/llm-gateway`:

> Anthropic doesn't endorse, maintain, or audit third-party gateway products, and doesn't support
> routing Claude Code to non-Claude models through any gateway.

Cited, from `code.claude.com/docs/en/llm-gateway-connect`: setting `ANTHROPIC_BASE_URL` **without**
a credential variable keeps the saved claude.ai login active. Remote Control is disabled from
v2.1.196 whenever the base URL points at a non-Anthropic host. Voice dictation is disabled only by
a credential variable, so it survives a base-URL-only setup.

Cited, from the `DocksDocks/claudex` README: that project "fails closed if multiple account
credentials are present". Multi-account is the gap this fork fills.

## Decisions taken

1. Both activation targets, native first. One Codex account pool serves the native `codex` CLI and
   the Claude Code gateway path.
2. Additive fork. No renames, no restructuring of `switcher.py`. `git rebase upstream/main` must
   stay cheap.
3. Cross-provider autoswitch is last resort only, and must land mid-session.
4. A thin cswap router sits at 127.0.0.1 and hands off to CLIProxyAPI for Codex traffic.

## Architecture

### 1. Provider seam

New package `src/claude_swap/providers/`. One Protocol, two adapters.

```
providers/
  base.py     Provider Protocol + AccountIdentity + normalized usage shape
  claude.py   wraps today's functions, zero behaviour change
  codex.py    new
```

`Provider` members:

- `name`, `display_name`
- `active_path()` — where the vendor CLI reads credentials
- `read_active()`, `write_active(blob)`
- `identity(blob) -> AccountIdentity{email, account_uuid, org_uuid, org_name, plan}`
- `fingerprint(blob)` — lineage id, survives access-token rotation
- `is_expired(blob)`, `refresh(blob) -> RefreshOutcome`
- `fetch_usage(blob) -> dict` — normalized to today's `{five_hour, seven_day, scoped, spend}`
- `backup_namespace` — subdirectory under the backup root
- `session_env(profile_dir) -> dict` — `CLAUDE_CONFIG_DIR` or `CODEX_HOME`

The normalized usage dict is the existing one, so `oauth.account_headroom()`,
`poll_policy.binding_pct()`, the TUI and the autoswitch engine all work on Codex accounts with no
change to their logic.

`claude.py` is a pure move. Its functions keep their current bodies. This is what keeps upstream
rebases cheap: `switcher.py` changes from calling module-level functions to calling
`self._provider.<same function>`, and nothing else about it moves.

### 2. Codex adapter

- **Identity** parses the `id_token` payload with no signature check and no network call. An
  expired `id_token` still yields correct identity, because the claims are only being read.
- **Fingerprint** is `sha256(tokens.refresh_token)`, matching the Claude lineage rule.
- **Usage** calls `wham/usage` with `Authorization: Bearer <access_token>` and
  `chatgpt-account-id: <tokens.account_id>`. Windows are mapped by `limit_window_seconds`:
  18000 → `five_hour`, 604800 → `seven_day`, anything else → a `scoped` entry named by its length
  in hours. `credits` maps to the existing `spend` entry.
- **Polling cost is low.** Access tokens last 10 days, so a poll of an inactive account almost
  never needs a refresh first. Claude accounts need one nearly every time.
- **Refresh** posts to `https://auth.openai.com/oauth/token` with `grant_type=refresh_token` and
  `client_id=app_EMoamEEZ73f0CkXaXp7hrann`. **This is unverified.** See Risks.

### 3. Storage layout

Claude state does not move. Codex state goes in a new subtree.

```
~/.local/share/claude-swap/
  sequence.json                       unchanged, Claude accounts
  credentials/  configs/              unchanged, Claude
  settings.json                       gains provider keys
  providers/codex/
    sequence.json
    credentials/.creds-<slot>-<email>.enc
  router/
    mode.json
    cliproxy-auth/                    exactly one Codex credential at a time
```

Reasons: the 8 existing Claude accounts need no migration, and upstream never touches the new
tree, so it cannot conflict.

Slot numbers are per provider. Account identifiers become `[<provider>:]<slot|alias|email>`.
A bare identifier resolves against `settings.defaultProvider`, which is `claude`.

### 4. Router

New optional package `src/claude_swap/router/`, installed with the `cswap[router]` extra so the
core tool stays dependency-light. One extra dependency, `aiohttp`, which provides both the server
and a streaming client.

- Binds `127.0.0.1:8318` only. Never `0.0.0.0`. CLIProxyAPI keeps 8317.
- Reads `router/mode.json` per request, cached against mtime.
  `{"provider": "claude"}` or `{"provider": "codex", "slot": "2"}`.
- Mode `claude`: forward verbatim to `https://api.anthropic.com`. Every header except `Host` is
  preserved, both directions stream. Claude Code's own OAuth bearer passes straight through, so
  today's credential swapping keeps working untouched.
- Mode `codex`: rewrite `Authorization` to the local CLIProxyAPI client token and forward to
  `127.0.0.1:8317`.
- `GET /_cswap/health` returns the mode and upstream reachability.
- If CLIProxyAPI is unreachable in codex mode, return 503 with a plain body. Never fall back to
  Anthropic silently, because that would spend the Claude quota the fallback exists to protect.

Claude Code is configured with `ANTHROPIC_BASE_URL` only, and no credential variable. That keeps
the claude.ai subscription login active and keeps voice dictation working. Remote Control is lost
while the router is installed.

`cswap router install` writes the `env` block into `~/.claude/settings.json` and, on Linux, a
`systemd --user` unit. `cswap router uninstall` removes both, returning Claude Code to direct
Anthropic traffic.

### 5. CLIProxyAPI control

cswap owns the selection, CLIProxyAPI owns the translation.

cswap writes exactly one Codex credential into `router/cliproxy-auth/` and points CLIProxyAPI's
config at that directory. One credential in the directory means no round-robin, so the account is
whichever one cswap's quota-aware picker chose. Switching Codex accounts is a file write in that
directory.

### 6. Autoswitch

Within-provider rotation is unchanged.

New setting `autoswitch.fallbackProvider`, default off. When set to `codex` and every non-disabled
Claude account has headroom at or below zero, the engine picks the Codex account with the most
headroom, writes it into `router/cliproxy-auth/`, flips `router/mode.json`, and emits a new
`provider_switched` event. The mode file is read per request, so a running Claude Code session
lands on Codex at its next request.

The engine flips back to Claude as soon as any Claude account's window resets. Existing
`hysteresis_pct` and `cooldown_seconds` settings govern both directions.

**Known confusion, not solvable here.** Claude Code's own UI keeps naming a Claude model while a
GPT model answers. The mitigations are a loud CLI message on flip, a backend indicator in the TUI
header, and `cswap status` naming the live backend.

### 7. CLI surface

Additive. Every existing command keeps its current meaning.

```
cswap list [--provider codex]        both pools by default, provider column added
cswap add --provider codex          capture the live ~/.codex/auth.json
cswap switch codex:2                 make that account the active Codex account
cswap run codex:2 -- codex           per-terminal profile via CODEX_HOME
cswap backend claude|codex|auto      flip the router mode explicitly
cswap router install|start|stop|status|uninstall
```

`cswap switch codex:2` always writes `~/.codex/auth.json`, so the native CLI follows. When the
router is installed it also writes `router/cliproxy-auth/`, so both targets name the same account
at all times.

`cswap backend auto` hands the mode file back to the autoswitch engine. `claude` and `codex` pin
the mode and suppress phase 3 fallback until `auto` is set again.

TUI gains a provider column and a header showing the active backend.

### 8. Testing

- A shared provider contract suite, parametrized over `claude` and `codex`, so parity is enforced
  by construction. This follows the existing `test_macos_keychain_contract.py` pattern.
- Codex identity and usage parsing tested against fixtures recorded from the real responses
  captured on 2026-08-08, with tokens and ids redacted.
- Router tests: streaming passthrough against a local fake upstream, and a mode-flip test
  asserting request N reaches Anthropic while request N+1 reaches the proxy.
- The existing ~1929 tests staying green is the regression gate for "Claude behaviour unchanged".
  Any failure there means the seam extraction was not a pure move.

## Risks

1. **Terms of service.** Feeding a ChatGPT subscription to a non-Codex client uses an undocumented
   internal endpoint. The realistic failure is an OpenAI account ban, which matters more than
   usual for a tool whose purpose is holding many accounts. Anthropic separately states it does not
   support routing Claude Code to non-Claude models through any gateway. Phase 1 carries none of
   this exposure. Phase 2 and 3 carry all of it.
2. **The refresh flow is unverified. STILL OPEN after Phase 1.** The token endpoint rotates the
   refresh token, and OpenAI's tokens are single-use, so a failed write kills the login. Every test
   for `codex.try_refresh` is mocked. The request shape now matches OpenAI's own client, checked
   against `codex-rs/login/tests/suite/auth_refresh.rs`, but no live call has been made. Verify on
   a spare Codex account under a separate `CODEX_HOME` before this code runs against the primary
   one, and check specifically that the returned access token keeps `api.connectors.read` and
   `api.connectors.invoke`.
3. **Endpoint drift.** `wham/usage` and the Codex responses endpoint are undocumented. They can
   change without notice and break usage polling or the whole codex mode.
4. **The router is a new single point of failure** for every Claude Code session on this box.
   Mitigations: `claude` mode is a verbatim passthrough with no parsing, and
   `cswap router uninstall` restores direct traffic in one command.
5. **Merge cost.** Upstream ships about 30 commits a month, mostly into `switcher.py`. Guard with
   `git diff upstream/main --stat -- src/claude_swap/switcher.py` after each upstream release, and
   treat a growing number there as a defect.
6. **Remote Control is lost** while the router is installed. Voice dictation is not.
7. **Non-Claude upstreams need three known fix-ups**, cited from the gateway troubleshooting table:
   `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, and
   `CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK=1`. `cswap router install` sets all three.

## Phasing

**Phase 1 — provider seam and the Codex pool. Shipped 2026-08-08.** What landed:
`providers/base.py` (the `Provider` Protocol and `AccountIdentity`), `providers/codex.py`,
`codex_store.py`, `codex_cli.py`, and the per-provider storage subtree. The command surface is
`cswap codex list|status|add|switch|remove`, with live usage polling through the existing
`UsageStore`. 1912 tests pass; the branch adds 4900 lines and deletes none.

Two deliberate departures from this document:

- **No `providers/claude.py`.** Section 1 said the Claude paths would move behind the Protocol.
  They did not. `switcher.py` is 5610 lines and takes most of upstream's ~30 commits a month, so
  cutting a seam through it is the largest merge cost available, and nothing in Phase 1 consumes
  two providers polymorphically. `git diff upstream/main -- src/claude_swap/switcher.py` is empty.
  Write the Claude adapter when the TUI or the autoswitch engine first has to hold both at once.
- **Deferred out of Phase 1:** `codex run` profiles (`$CODEX_HOME` holds `config.toml`, session
  rollouts and sqlite state, so a profile has to mirror far more than `auth.json`), aliases,
  directory mappings, autoswitch, the TUI, and macOS Keychain storage. The `spend` usage entry is
  also absent: Codex `credits` carries a balance with no limit, so it cannot fill
  `{used, limit, pct}` honestly.

Two findings from Phase 1 that change how Phase 2 and 3 must be built:

- **Switching must recapture the live credential before reading the stored one.** The Codex CLI
  refreshes tokens in place, and OpenAI refresh tokens are single-use — reuse answers
  `refresh_token_reused`. An early version of `switch` rolled the live file back when the target
  slot was already live, and a later switch away then overwrote the good copy, leaving both dead.
  Fixed in `c0b8fbe`, with a test.
- **The refresh grant must not send `scope`.** OpenAI's own Codex client sends exactly
  `client_id`, `grant_type` and `refresh_token`
  (`codex-rs/login/tests/suite/auth_refresh.rs`). RFC 6749 §6 reads `scope` on a refresh as a
  narrowing request, and the scope granted at authorize time is wider than the one this design
  originally specified, so sending it risked a silent capability downgrade returned with no error.
  Fixed in `be53ebf`.

**Phase 1.5 — the unified account pool. Shipped 2026-08-09.** Phase 1's separate Codex pool
became one pool. `sequence.json` is the single registry; an account entry carries an optional
`provider` key, and an entry without one is a Claude account, so existing data reads unchanged.
`provider_ops.py` routes credential work to the owning backend. `pool_migration.py` folds a
standalone Phase 1 pool into the registry once, on the next command.

Every main command now takes a Codex slot: `list`, `status`, `switch`, `add --provider codex`,
`remove`, `swap`, `move`, `run`, `export`, `import`, and the TUI. `cswap codex <sub>` survives as
an alias that rewrites to the matching main command.

Three rules this phase locks in:

- **Active is per provider.** Claude Code and the Codex CLI read different files, so one global
  pointer would leave one of them with no credential. `sequence.json` keeps `activeAccountNumber`
  for Claude and gains `activeProviderAccounts` for the rest.
- **Auto-rotation never crosses providers.** `switchable_account_numbers()` excludes non-Claude
  slots, so every autoswitch candidate site is covered by one filter. Phase 2's router is what
  unblocks crossing.
- **Downgrading is unsafe once a Codex slot exists.** Upstream 0.24.1 reads one as a broken Claude
  account. Recorded in the README, not guarded in code.

Two departures from the plan for it:

- **`cswap run` on a Codex slot does not `exec`.** It stays resident and copies the credential back
  when Codex exits. Codex rotates its refresh token in place and an OpenAI refresh token is single
  use, so an `exec` would lose the only valid copy.
- **A Codex export entry carries no `config` key.** There is no Claude config to carry, and the
  absence doubles as the downgrade guard: an older cswap requires `config` to be an object, so it
  aborts the whole import before any write instead of restoring a Codex blob as a Claude
  credential.

**Phase 2 — router. Shipped 2026-08-09.** `cswap router install|uninstall|start|stop|status|serve`
and `cswap backend claude|codex [<slot>]|auto`. What landed: `router/mode.py` (the mode file and
its mtime-cached watcher), `router/server.py` (the aiohttp proxy), `router/install.py` (the
settings edit and the systemd unit), `router/cliproxy.py` (config, credential and process for the
Codex backend), `router/modelmap.py`, and `router_cli.py`. aiohttp is an extra, `cswap[router]`,
so the core tool stays dependency-light.

Verified against `api.anthropic.com` on 2026-08-09: a request through the router and the same
request sent directly both answer `401 invalid x-api-key` with the same body. 92 tests cover the
router; the suite is 2108 passing, 3 skipped.

Four rules this phase locks in:

- **Claude mode is a byte-for-byte passthrough.** The body streams, the client's own
  `Authorization` survives, and the client session runs with `auto_decompress` off. That last one
  makes `content-encoding` a header the proxy must forward, not strip — stripping it while
  forwarding a still-gzipped body was a real bug, caught by the live check above.
- **Codex mode never falls back to Anthropic.** An unreachable CLIProxyAPI is a 503. A fallback
  would spend exactly the quota the switch exists to protect.
- **The model name is rewritten in the router, not in CLIProxyAPI.** CLIProxyAPI can alias model
  names through `oauth-model-alias`, but the mapping would then be static config that goes stale
  as Codex model names change. The router holds two names in `router/models.json` and corrects
  them once per process against the backend's own `/v1/models`. Defaults `gpt-5.6-terra` and
  `gpt-5.6-sol` come from this box's Codex session history — 461 recorded turns.
- **Exactly one credential reaches CLIProxyAPI.** It load-balances across every file in its
  auth directory, so a second file there would spread traffic over accounts cswap did not pick.
  `write_credential` empties the directory on every write.

Two departures from sections 4 and 5:

- **`cswap backend claude` syncs the credential back.** Not in the plan. CLIProxyAPI refreshes the
  token while it serves, and an OpenAI refresh token is single-use, so without the sync cswap's
  stored copy is dead the first time the backend refreshes.
- **`cswap backend auto` only clears the pin.** Nothing moved the backend automatically in
  Phase 2. Phase 3 added the engine, and the command now reports whichever is true.

**Phase 3 — cross-provider fallback. Shipped 2026-08-09.** `autoswitch.fallbackProvider`
(choice `off` | `codex`, default `off`), `AutoSwitchEngine._maybe_flip_backend`, the
`backend-switched` event, and `router/switching.py` — the shared activate/deactivate steps that
`cswap backend`, `cswap switch` and the engine now all go through. 23 tests; the suite is 2132
passing, 3 skipped.

The hook runs once per tick, straight after the poll event, before any per-account logic. Which
backend answers is a different question from which Claude account is live, and the answer has to
be right even on a tick that returns early.

Four rules this phase locks in:

- **Exhausted means measured.** Every Claude account must have a known headroom at or below zero.
  One unreadable account holds the flip back. The failure mode being avoided is moving every
  running session to another provider on a guess.
- **The headroom map is not Claude-only.** `_collect_scheduled_usage` builds `usage` from every
  row the usage store holds, Codex slots included, so the fallback filters by
  `provider_of(num) == "claude"` and drops quarantined slots. Reading the map unfiltered made the
  engine see a healthy Codex account as a reason to stay on Claude — caught by a test.
- **A pinned mode outranks the engine.** `cswap backend claude|codex` sets `pinned`, and the
  engine then leaves the mode file alone in both directions until `cswap backend auto`.
- **The Codex ranking never polls.** It reads stored usage rows. A fallback that fetched OpenAI
  every tick would spend requests on a backend that is idle almost all the time, and the worst a
  stale ranking costs is one extra flip.

Two departures from section 6:

- **The event is `backend-switched`, not `provider_switched`.** Every other event kind in
  `autoswitch.py` is a hyphenated string; `provider_switched` would have been the only one that
  was not.
- **No TUI backend indicator.** `cswap status` grows a `Backend:` line, `cswap router status`
  reports the whole picture, and the flip is logged by `cswap auto`. The TUI header would need
  refresh plumbing for a fourth copy of the same fact.

**Installed and proved on this box, 2026-08-09.** CLIProxyAPI 7.2.125 (linux_amd64, sha256
verified against the release `checksums.txt`) at `~/.local/bin/cli-proxy-api`, then
`cswap router install`. Codex mode answered a real `/v1/messages` request end to end: a
`claude-sonnet-4-5` request came back as `gpt-5.6-terra`, a `claude-3-5-haiku` request came back
as `gpt-5.6-sol`. Claude mode still passes an invalid key straight through to Anthropic for its
401. Live use found two faults the tests did not:

- **CLIProxyAPI kept running after the flip back to Claude.** Idle, it still refreshes the Codex
  login every 15 minutes, which rotates a single-use token cswap is no longer watching. The next
  flip to Codex would publish a spent token. `activate_claude` now stops the process first, then
  reads the credential back.
- **The CLIProxyAPI unit was enabled at login.** Same fault at boot. The unit now has no
  `[Install]` section, and `install_unit` disables any copy an older install enabled.

Each phase is independently useful and independently revertible.
