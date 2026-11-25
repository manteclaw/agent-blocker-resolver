# agent-blocker-resolver

Auto-resolution framework for known agent blockers: read a JSONL blocker log,
match open blockers against known patterns, attempt the fix, log every
attempt, and **escalate after 3 failed tries** instead of retrying forever.

This is a framework plus starter patterns — not an AI that fixes anything.
It only resolves patterns someone has written (or configured) a resolver for.
Everything else is logged and skipped, and anything involving funding is
always left for a human.

## Why

Autonomous agents hit the same boring failures repeatedly: a process died, a
key got revoked, a wallet was never registered, a claim "failed" in the SDK
but actually landed on-chain. Retrying those blindly burns time and money.
Recording them, attempting the known fix a bounded number of times, and then
escalating is the whole idea: **stop retrying, start escalating.**

## Files

- `blocker-tracker.py` — the blocker-log writer. Log a blocker from anywhere:
  `python3 blocker-tracker.py log <service> <reason> <action> [severity]`,
  and list recent ones with `python3 blocker-tracker.py list [hours]`.
- `blocker-auto-resolve.py` — the resolver. `--check` is a dry run that
  reports what it *would* do; `--resolve` actually attempts it.
- `resolver-patterns.example.json` — copy to `resolver-patterns.json` and
  declare your own services' registration commands (see below).
- `examples/blockers.example.jsonl` — sample blockers in the resolver's format.

## Blocker log format

One JSON object per line in `memory/blockers.jsonl`:

```json
{"time": "2026-01-01T00:00:00+00:00", "service": "example-api", "issue": "api 401 unauthorized", "action": "check the key"}
```

`blocker-tracker.py` writes lines in this format (it also keeps its original
`timestamp`/`platform`/`reason`/`action_taken` fields on the same line).

## The attempt / escalate contract

- Every attempt is appended to `memory/blocker-resolution.jsonl` with the
  handler name, status (`resolved` / `failed` / `skipped` / `check`), and
  details.
- A blocker that already has a `resolved` or `escalated` entry is never
  attempted again.
- Retries back off exponentially: `2^attempt × 5s`.
- After 3 failed attempts the blocker is **escalated** (a final
  `escalated` entry is written) and left for a human.

## Built-in handlers (starter patterns)

| Pattern in issue/action text | Handler | What it does |
| --- | --- | --- |
| `process dead` | `process_dead` | Looks the service up in `memory/process-pids.json`; if the PID is gone, restarts it via its systemd unit under `projects/<service>/` or its `start.sh`. |
| `api 401` / `api 403` / `unauthorized` / `revoked` | `api_401` | Checks `.env` for the provider's key (name only, never the value) and runs your `scripts/api-health.py` if you have one. |
| `wallet not registered` / `not registered` | `wallet_not_registered` | Runs the registration command **you** declared for that service in `resolver-patterns.json`. None are built in. |
| `funding needed` | `funding_needed` | Always skipped — funding requires human approval. |
| `claim failed` | `claim_failed` | Extracts a tx hash from the blocker text and verifies it on-chain (`cast` if installed, else JSON-RPC) before believing the failure. Bring your own RPC: set `BLOCKER_RPC_URL` or `ALCHEMY_API_KEY`. |

Add your own: write a `handler_*(blocker, check_only) -> dict` function and
map a keyword to it in the `HANDLERS` table.

## Configuration

All paths root at `AGENT_WORKSPACE` (default: current directory) and can be
overridden individually:

| Env var | Default |
| --- | --- |
| `BLOCKERS_LOG` | `$AGENT_WORKSPACE/memory/blockers.jsonl` |
| `RESOLUTION_LOG` | `$AGENT_WORKSPACE/memory/blocker-resolution.jsonl` |
| `PROCESS_PIDS_FILE` | `$AGENT_WORKSPACE/memory/process-pids.json` |
| `ENV_FILE` | `$AGENT_WORKSPACE/.env` |
| `API_HEALTH_SCRIPT` | `$AGENT_WORKSPACE/scripts/api-health.py` |
| `RESOLVER_PATTERNS_FILE` | `$AGENT_WORKSPACE/resolver-patterns.json` |
| `BLOCKER_LOG` (tracker) | `$AGENT_WORKSPACE/memory/blockers.jsonl` |

## Quick start

```bash
cp examples/blockers.example.jsonl memory/blockers.jsonl   # or log real ones with blocker-tracker.py
python3 blocker-auto-resolve.py --check    # dry run: what would it do?
python3 blocker-auto-resolve.py --resolve  # attempt resolution
```

## Limits

- Keyword matching is substring-based and deliberately dumb — a blocker
  whose text doesn't contain a known pattern is skipped, not guessed at.
- The `process_dead` handler assumes systemd units or start scripts under
  `projects/<service>/`; adapt it to your supervisor.
- The `api_401` handler can only *detect* a dead key and point at rotation;
  rotating a key is your provider dashboard's job.
- Escalation means "a log entry a human will read" — wiring it to paging,
  chat, or email is up to you.

## License

MIT — see `LICENSE`.
