#!/usr/bin/env python3
"""
blocker-auto-resolve.py — Auto-resolution framework for known blockers.

Usage:
    python3 scripts/blocker-auto-resolve.py --check    # Dry-run: list what it would do
    python3 scripts/blocker-auto-resolve.py --resolve  # Attempt resolution

Reads memory/blockers.jsonl for open blockers, attempts resolution for known
patterns, logs attempts to memory/blocker-resolution.jsonl.
Escalates after 3 failed attempts.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────
# Everything is rooted at AGENT_WORKSPACE (default: current directory) and
# each path can be overridden individually with an environment variable.
_WORKDIR = Path(os.environ.get("AGENT_WORKSPACE", "."))
BLOCKERS_LOG = Path(os.environ.get("BLOCKERS_LOG", _WORKDIR / "memory" / "blockers.jsonl"))
RESOLUTION_LOG = Path(os.environ.get("RESOLUTION_LOG", _WORKDIR / "memory" / "blocker-resolution.jsonl"))
PIDS_FILE = Path(os.environ.get("PROCESS_PIDS_FILE", _WORKDIR / "memory" / "process-pids.json"))
ENV_FILE = Path(os.environ.get("ENV_FILE", _WORKDIR / ".env"))
API_HEALTH_SCRIPT = Path(os.environ.get("API_HEALTH_SCRIPT", _WORKDIR / "scripts" / "api-health.py"))
PATTERNS_FILE = Path(os.environ.get("RESOLVER_PATTERNS_FILE", _WORKDIR / "resolver-patterns.json"))

# ── Logging ───────────────────────────────────────────────────────
def log_resolution(entry: dict):
    RESOLUTION_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(RESOLUTION_LOG, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ── Blocker loader ─────────────────────────────────────────────────
def load_blockers() -> list[dict]:
    if not BLOCKERS_LOG.exists():
        return []
    blockers = []
    with open(BLOCKERS_LOG, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                blockers.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return blockers

# ── Resolution history ──────────────────────────────────────────────
def count_prior_attempts(service: str, issue: str) -> int:
    if not RESOLUTION_LOG.exists():
        return 0
    count = 0
    with open(RESOLUTION_LOG, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("service") == service and entry.get("issue") == issue:
                    count += 1
            except json.JSONDecodeError:
                continue
    return count

def already_resolved(service: str, issue: str) -> bool:
    if not RESOLUTION_LOG.exists():
        return False
    with open(RESOLUTION_LOG, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("service") == service and entry.get("issue") == issue:
                    if entry.get("status") in ("resolved", "escalated"):
                        return True
            except json.JSONDecodeError:
                continue
    return False

# ── Pattern config ────────────────────────────────────────────────
def load_registration_patterns() -> dict:
    """Load {service: [command, args...]} from resolver-patterns.json.

    Returns an empty dict when no config file exists — the framework ships
    with starter patterns only; your services' CLIs are yours to declare.
    """
    if not PATTERNS_FILE.exists():
        return {}
    try:
        with open(PATTERNS_FILE, "r") as f:
            data = json.load(f)
        patterns = data.get("registration_commands", {})
        return {str(k).lower(): list(v) for k, v in patterns.items()}
    except (json.JSONDecodeError, AttributeError, TypeError):
        return {}

# ── Resolution handlers ────────────────────────────────────────────
def handler_process_dead(blocker: dict, check_only: bool) -> dict:
    """Restart dead process from process-pids.json."""
    service = blocker["service"]
    result = {
        "time": now_iso(),
        "service": service,
        "issue": blocker["issue"],
        "handler": "process_dead",
        "status": "pending",
        "details": "",
    }

    if not PIDS_FILE.exists():
        result["status"] = "failed"
        result["details"] = f"No process-pids.json found for {service}"
        return result

    with open(PIDS_FILE, "r") as f:
        pids = json.load(f)

    # Find the service in pids — match by key or partial name
    pid = pids.get(service)
    if pid is None:
        # Try partial match
        for key, val in pids.items():
            if service.lower() in key.lower() or key.lower() in service.lower():
                pid = val
                break

    if pid is None:
        result["status"] = "failed"
        result["details"] = f"No PID entry found for {service}"
        return result

    # Check if process is alive
    alive = os.path.exists(f"/proc/{pid}") if isinstance(pid, int) else False

    if alive:
        result["status"] = "skipped"
        result["details"] = f"Process {pid} for {service} is still alive"
        return result

    if check_only:
        result["status"] = "check"
        result["details"] = f"Would restart {service} (pid {pid} dead)"
        return result

    # Attempt restart — look for systemd service or script
    restarted = False
    projects_dir = _WORKDIR / "projects"
    service_file = projects_dir / service / f"{service}.service"
    if not service_file.exists():
        # Try variations
        candidates = list(projects_dir.rglob(f"*{service}*.service")) if projects_dir.exists() else []
        if candidates:
            service_file = candidates[0]

    if service_file.exists():
        try:
            svc_name = service_file.stem
            subprocess.run(
                ["systemctl", "--user", "restart", svc_name],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            restarted = True
            result["details"] = f"Restarted systemd service {svc_name}"
        except Exception as e:
            result["details"] = f"systemctl restart failed: {e}"
    else:
        # Try to find a start script
        start_script = projects_dir / service / "start.sh"
        if not start_script.exists():
            candidates = list(projects_dir.rglob(f"{service}*start*")) if projects_dir.exists() else []
            if candidates:
                start_script = candidates[0]
        if start_script.exists():
            try:
                subprocess.run(
                    ["bash", str(start_script)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                restarted = True
                result["details"] = f"Ran start script {start_script}"
            except Exception as e:
                result["details"] = f"Start script failed: {e}"
        else:
            result["details"] = f"No systemd service or start script found for {service}"

    result["status"] = "resolved" if restarted else "failed"
    return result

def handler_api_401(blocker: dict, check_only: bool) -> dict:
    """Check .env key, run api-health.py, suggest rotation."""
    service = blocker["service"]
    result = {
        "time": now_iso(),
        "service": service,
        "issue": blocker["issue"],
        "handler": "api_401",
        "status": "pending",
        "details": "",
    }

    # Extract provider name from service or issue
    provider = service
    issue_lower = blocker.get("issue", "").lower()
    if "api" in issue_lower or "401" in issue_lower or "403" in issue_lower:
        # Try to find a provider name in the issue text
        words = re.findall(r"[a-zA-Z0-9_]+", issue_lower)
        for w in words:
            if w not in ("api", "http", "unauthorized", "revoked", "dead", "key", "status"):
                if len(w) > 2:
                    provider = w
                    break

    # Check .env for key
    env_key = None
    if ENV_FILE.exists():
        with open(ENV_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or not line:
                    continue
                if provider.upper().replace(" ", "_") in line.upper() or provider.upper() in line.upper():
                    env_key = line
                    break

    if check_only:
        result["status"] = "check"
        details = f"Would check API key for {provider}"
        if env_key:
            details += f" — key found in .env: {env_key.split('=')[0]}=***"
        else:
            details += " — no key found in .env"
        if API_HEALTH_SCRIPT.exists():
            details += f", would run {API_HEALTH_SCRIPT}"
        result["details"] = details
        return result

    # Run api-health.py
    if API_HEALTH_SCRIPT.exists():
        try:
            proc = subprocess.run(
                [sys.executable, str(API_HEALTH_SCRIPT)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            health_out = proc.stdout + proc.stderr
            # Check if the provider is mentioned in the output
            provider_healthy = provider.lower() in health_out.lower() and "200" in health_out
            if provider_healthy:
                result["status"] = "resolved"
                result["details"] = f"api-health reports {provider} is healthy (200)"
            else:
                result["status"] = "failed"
                result["details"] = f"api-health shows {provider} is unhealthy. Suggest rotating key. .env entry: {'found' if env_key else 'missing'}"
        except Exception as e:
            result["status"] = "failed"
            result["details"] = f"api-health.py failed to run: {e}"
    else:
        result["status"] = "failed"
        result["details"] = f"api-health.py not found at {API_HEALTH_SCRIPT}"

    return result

def handler_wallet_not_registered(blocker: dict, check_only: bool) -> dict:
    """Attempt re-registration via known CLI pattern."""
    service = blocker["service"]
    result = {
        "time": now_iso(),
        "service": service,
        "issue": blocker["issue"],
        "handler": "wallet_not_registered",
        "status": "pending",
        "details": "",
    }

    # Registration commands are NOT built in — they come from your own
    # resolver-patterns.json (see resolver-patterns.example.json):
    #   {"registration_commands": {"<service>": ["cli", "register", "..."]}}
    cli_patterns = load_registration_patterns()

    cmd = cli_patterns.get(service.lower())
    if not cmd:
        # Try to infer from blocker action text
        action = blocker.get("action", "").lower()
        for name, candidate in cli_patterns.items():
            if name in action:
                cmd = candidate
                break

    if check_only:
        result["status"] = "check"
        if cmd:
            result["details"] = f"Would run re-registration: {' '.join(cmd)}"
        else:
            result["details"] = f"No known CLI pattern for {service}. Manual re-registration required."
        return result

    if cmd:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                result["status"] = "resolved"
                result["details"] = f"Re-registered successfully: {proc.stdout[:500]}"
            else:
                result["status"] = "failed"
                result["details"] = f"Re-registration failed: {proc.stderr[:500]}"
        except Exception as e:
            result["status"] = "failed"
            result["details"] = f"Re-registration command failed: {e}"
    else:
        result["status"] = "failed"
        result["details"] = f"No known CLI pattern for {service}"

    return result

def handler_funding_needed(blocker: dict, check_only: bool) -> dict:
    """Skip — requires human approval."""
    result = {
        "time": now_iso(),
        "service": blocker["service"],
        "issue": blocker["issue"],
        "handler": "funding_needed",
        "status": "skipped",
        "details": "Funding requires human approval — skipping auto-resolution",
    }
    return result

def handler_claim_failed(blocker: dict, check_only: bool) -> dict:
    """Verify on-chain before escalating."""
    service = blocker["service"]
    result = {
        "time": now_iso(),
        "service": service,
        "issue": blocker["issue"],
        "handler": "claim_failed",
        "status": "pending",
        "details": "",
    }

    # Try to find tx hash in issue or action
    text = f"{blocker.get('issue', '')} {blocker.get('action', '')}"
    tx_hashes = re.findall(r"0x[a-fA-F0-9]{64}", text)

    if check_only:
        result["status"] = "check"
        if tx_hashes:
            result["details"] = f"Would verify on-chain: {tx_hashes[0][:20]}... via Alchemy or BaseScan"
        else:
            result["details"] = "Would search for claim tx on-chain — no tx hash found in blocker text"
        return result

    # Verify on-chain via Alchemy or cast
    verified = False
    if tx_hashes:
        tx = tx_hashes[0]
        # Bring your own RPC endpoint: BLOCKER_RPC_URL (any EVM JSON-RPC URL)
        # or ALCHEMY_RPC_URL. No endpoint is hardcoded.
        rpc_url = os.environ.get("BLOCKER_RPC_URL") or os.environ.get("ALCHEMY_RPC_URL", "")
        if not rpc_url and not os.environ.get("ALCHEMY_API_KEY", ""):
            result["status"] = "failed"
            result["details"] = "No RPC configured — set BLOCKER_RPC_URL or ALCHEMY_API_KEY to verify on-chain"
            return result
        try:
            # Try cast (Foundry)
            proc = subprocess.run(
                ["cast", "receipt", tx, "--rpc-url", rpc_url],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if proc.returncode == 0:
                verified = True
                result["details"] = f"Tx {tx[:20]}... verified on-chain: {proc.stdout[:500]}"
            else:
                result["details"] = f"On-chain check returned error: {proc.stderr[:500]}"
        except FileNotFoundError:
            # cast not available — try curl with Alchemy
            alchemy_key = os.environ.get("ALCHEMY_API_KEY", "")
            if alchemy_key:
                rpc = f"https://base-mainnet.g.alchemy.com/v2/{alchemy_key}"
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_getTransactionReceipt",
                    "params": [tx],
                }
                try:
                    import urllib.request
                    req = urllib.request.Request(
                        rpc,
                        data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = json.loads(resp.read())
                        if data.get("result"):
                            verified = True
                            result["details"] = f"Tx {tx[:20]}... found on-chain via Alchemy"
                        else:
                            result["details"] = f"Tx {tx[:20]}... not found on-chain — may be pending or failed"
                except Exception as e:
                    result["details"] = f"Alchemy check failed: {e}"
            else:
                result["details"] = "No ALCHEMY_API_KEY — cannot verify on-chain"
        except Exception as e:
            result["details"] = f"On-chain verification error: {e}"
    else:
        result["details"] = "No tx hash found in blocker text — cannot verify on-chain"

    result["status"] = "resolved" if verified else "failed"
    return result

# ── Router ─────────────────────────────────────────────────────────
HANDLERS = {
    "process dead": handler_process_dead,
    "api 401": handler_api_401,
    "api 403": handler_api_401,
    "unauthorized": handler_api_401,
    "revoked": handler_api_401,
    "wallet not registered": handler_wallet_not_registered,
    "not registered": handler_wallet_not_registered,
    "funding needed": handler_funding_needed,
    "claim failed": handler_claim_failed,
}

def pick_handler(blocker: dict):
    issue = blocker.get("issue", "").lower()
    action = blocker.get("action", "").lower()
    combined = f"{issue} {action}"

    for keyword, handler in HANDLERS.items():
        if keyword in combined:
            return handler
    return None

# ── Main ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Auto-resolve known blockers")
    parser.add_argument("--check", action="store_true", help="Dry-run: list what it would do")
    parser.add_argument("--resolve", action="store_true", help="Attempt resolution")
    args = parser.parse_args()

    if not args.check and not args.resolve:
        print("Usage: blocker-auto-resolve.py --check | --resolve")
        sys.exit(1)

    blockers = load_blockers()
    if not blockers:
        print("No blockers found in memory/blockers.jsonl")
        return

    check_only = args.check
    mode_str = "CHECK (dry-run)" if check_only else "RESOLVE"
    print(f"═ blocker-auto-resolve.py ═ {mode_str} ═")
    print(f"Loaded {len(blockers)} blocker(s)\n")

    resolved_count = 0
    failed_count = 0
    skipped_count = 0
    escalated_count = 0

    for blocker in blockers:
        service = blocker.get("service", "unknown")
        issue = blocker.get("issue", "unknown")

        print(f"▸ {service}: {issue[:80]}")

        if already_resolved(service, issue):
            print("  ↪ Already resolved/escalated — skipping\n")
            continue

        prior = count_prior_attempts(service, issue)
        if prior >= 3:
            print(f"  ↪ Already attempted {prior} times — ESCALATING")
            entry = {
                "time": now_iso(),
                "service": service,
                "issue": issue,
                "handler": "escalation",
                "status": "escalated",
                "details": f"Auto-escalated after {prior} failed attempts",
                "attempt": prior + 1,
            }
            log_resolution(entry)
            escalated_count += 1
            print()
            continue

        handler = pick_handler(blocker)
        if handler is None:
            print(f"  ↪ No handler matched — skipping")
            skipped_count += 1
            print()
            continue

        # Exponential backoff: 2^attempt * 5 seconds (but only on actual resolve)
        if not check_only and prior > 0:
            backoff = (2 ** prior) * 5
            print(f"  ↪ Backing off {backoff}s (attempt {prior + 1}/3)")
            time.sleep(backoff)

        result = handler(blocker, check_only=check_only)
        result["attempt"] = prior + 1
        log_resolution(result)

        status = result["status"]
        print(f"  ↪ [{status.upper()}] {result['details'][:200]}")

        if status == "resolved":
            resolved_count += 1
        elif status in ("failed", "failed"):
            failed_count += 1
        elif status == "skipped":
            skipped_count += 1

        # Mark escalated after 3 total attempts (this was the 3rd)
        if not check_only and result["attempt"] >= 3 and status != "resolved":
            print(f"  ↪ Max attempts reached — ESCALATING")
            entry = {
                "time": now_iso(),
                "service": service,
                "issue": issue,
                "handler": "escalation",
                "status": "escalated",
                "details": f"Escalated after {result['attempt']} failed attempts",
                "attempt": result["attempt"],
            }
            log_resolution(entry)
            escalated_count += 1

        print()

    print(f"═ Summary ═")
    print(f"  Resolved:  {resolved_count}")
    print(f"  Failed:    {failed_count}")
    print(f"  Skipped:   {skipped_count}")
    print(f"  Escalated: {escalated_count}")
    print(f"  Log:       {RESOLUTION_LOG}")

if __name__ == "__main__":
    main()
