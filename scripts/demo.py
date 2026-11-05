"""make demo: the whole local flow in one command, no keys and no wallet.

Starts the engine in dev mode on a spare port with a fresh budget, then:
plans a question in plain English, answers it with noise and a signed receipt,
asks it again (free replay), tries to single out one patient (refused),
and checks the receipt with the verifier. Stops the engine at the end.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUESTION = "What is the 30-day readmission rate for diabetic patients aged 65 and older, by sex?"
ATTACK = "How many conditions does the 70-year-old woman in ZIP 606 who was admitted on March 3, 2026 have?"

def call(url, body=None):
    data = json.dumps(body).encode() if body is not None else None 
    req = urllib.request.Request(url, data=data, headers={"content-type":"application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"{url} answered {e.code}: {e.read().decode()}")

def step(n, title):
    print(f"\n[{n}/6] {title}")

def cells(answer):
    for c in answer["cells"]:
        group = ", ".join(str(v) for v in c["cell"].values()) or "all"
        print(f"    {group:<22} {c['display']}")

def main():
    if not (ROOT / "data/riverside.duckdb").exists():
        sys.exit("No data yet: run `make data` (or `make data-quick`) first.")
    with socket.socket() as s:
        s.bind(("127.0.0.1",0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    tmp = tempfile.mkdtemp(prefix="blindfold-demo-")
    env = {k: v for k, v in os.environ.items()
           if k not in ("CONTRACT_ID", "NEAR_AI_API_KEY", "DSTACK_SIMULATOR_ENDPOITN", "BUNDLE_URL")}
    env["BLINDFOLD_STATE"] = str(Path(tmp) / "state.sqlite")
    engine = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "blindfold.app:create_app", "--factory",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
         cwd=ROOT / "engine", env=env)
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(url + "/budget", timeout=1)
                break 
            except OSError:
                time.sleep(0.25)
        else:
            sys.exit("the engine didn't start")
        info = call(url + "/info")
        budget = call(url + "/budget")
        print(f"Engine on {url}: {info['mode']} enclave, planner {info['planner']}, "
              f"privacy budget {budget['epsilon_remaining']} of {budget['epsilon_total']}")

        step(1, "Plan a question in plain English (free)")
        print(f"    Q: {QUESTION}")
        plan = call(url + "/plan", {"question": QUESTION})
        print(f"    -> {plan['summary']}")
        print(f"      ε {plan['spec']['epsilon']}, spec hash {plan['spec_hash'][:16]}…, will refuse: {plan['will_refuse'] or 'no'}")

        step(2, "Ask it: a noisy answer with a 95% error bar and a signed receipt")
        first = call(url + "/query", {"buyer": "alice.testnet", "spec": plan["spec"]})
        cells(first["answer"])
        r = first["receipt"]
        print(f"      receipt #{r['request_id']}, ε charged {r['epsilon_charged']}, ε left {r['epsilon_remaining']}")

        step(3, "Ask the same question again: same noise, no ε spent (no averaging attack)")
        again = call(url + "/query", {"buyer": "bob.testnet", "spec": plan["spec"]})
        cells(again["answer"])
        r2 = again["receipt"]
        print(f"      receipt #{r2['request_id']}, replay of #{r2['replay_of']}, ε charged {r2['epsilon_charged']}")

        step(4, "Try to single out one patient")
        print(f"      Q: {ATTACK}")
        bad = call(url + "/plan", {"question": ATTACK})
        print(f"      the plan already says it will be refused: {bad['will_refuse']['rule']}. Send it anyway:")
        attack = call(url + "/query", {"buyer": "mallory.testnet", "spec": bad["spec"]})
        ref = attack["refusal"]
        print(f"      {attack['status']}: rule {ref['rule_number']} ({ref['rule']}), {ref['reason']}")
        print(f"      ε charged {attack['receipt']['epsilon_charged']}")

        step(5, "Verify receipt #1: hash, enclave signature, attestation")
        subprocess.run([sys.executable, str(ROOT / "verify/verify.py"), "--engine", url, "--request", "1"],
                       cwd=ROOT, env=env, check=True)

        step(6, "The public privacy budget")
        b = call(url + "/budget")
        print(f"      spent {b['epsilon_spent']} of {b['epsilon_total']}, {b['epsilon_remaining']} left")
        print("\nDone. The engine is stopped; this run's budget and receipts were in a temp folder.")
    finally:
        engine.terminate()
        engine.wait(timeout=10)


if __name__ == "__main__":
    main()