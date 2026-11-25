#!/usr/bin/env python3
"""
Blocker Tracker
Self-Improvement #6: Logs dead-ends and blockers with timestamps.
Enables pattern recognition across sessions.
"""
import json, os
from datetime import datetime, timezone
from pathlib import Path

BLOCKER_LOG = os.environ.get(
    "BLOCKER_LOG",
    os.path.join(os.environ.get("AGENT_WORKSPACE", "."), "memory", "blockers.jsonl"),
)

def log_blocker(platform, reason, action_taken, severity="medium"):
    """Log a blocker encountered."""
    os.makedirs(os.path.dirname(os.path.abspath(BLOCKER_LOG)), exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    entry = {
        "timestamp": ts,
        "platform": platform,
        "reason": reason,
        "action_taken": action_taken,
        "severity": severity,
        # Fields blocker-auto-resolve.py reads (same line, both schemas):
        "time": ts,
        "service": platform,
        "issue": reason,
        "action": action_taken,
    }
    with open(BLOCKER_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"🚫 Blocker logged: {platform} — {reason[:60]}")

def list_blockers(hours=24):
    """List recent blockers."""
    if not os.path.exists(BLOCKER_LOG):
        print("No blockers logged yet")
        return []
    
    cutoff = datetime.now(timezone.utc).timestamp() - (hours * 3600)
    recent = []
    
    with open(BLOCKER_LOG) as f:
        for line in f:
            try:
                e = json.loads(line.strip())
                ts = datetime.fromisoformat(e["timestamp"])
                if ts.timestamp() > cutoff:
                    recent.append(e)
            except:
                pass
    
    return recent

def main():
    import sys
    if len(sys.argv) < 2:
        print("Usage: blocker-tracker.py [log <platform> <reason> <action> [severity] | list [hours]]")
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == "log":
        if len(sys.argv) < 5:
            print("Usage: blocker-tracker.py log <platform> <reason> <action> [severity]")
            sys.exit(1)
        platform = sys.argv[2]
        reason = sys.argv[3]
        action = sys.argv[4]
        severity = sys.argv[5] if len(sys.argv) > 5 else "medium"
        log_blocker(platform, reason, action, severity)
    
    elif cmd == "list":
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24
        blockers = list_blockers(hours)
        print(f"\n🚫 BLOCKERS LAST {hours}H")
        print("=" * 60)
        for b in blockers:
            print(f"  {b['timestamp'][:19]} | {b['platform']:15s} | {b['severity']:6s} | {b['reason'][:40]}")
        print(f"\nTotal: {len(blockers)} blockers")

if __name__ == "__main__":
    main()
