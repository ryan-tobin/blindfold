"""The human side of paying: fund the contract, set the agent's limits, give it a key.

    python agent/wallet.py status   --buyer alice.ryan-tobin.testnet
    python agent/wallet.py register --buyer alice.ryan-tobin.testnet
    python agent/wallet.py deposit 2 --buyer alice.ryan-tobin.testnet
    python agent/wallet.py limits 0.25 1 --buyer alice.ryan-tobin.testnet
    python agent/wallet.py agent-key --buyer alice.ryan-tobin.testnet
    python agent/wallet.py revoke-agent --buyer alice.ryan-tobin.testnet
    python agent/wallet.py withdraw 0.25 --buyer ryan-tobin.testnet

status        USDC in the wallet and on the contract, limits, the agent key. Reads only.
register      Registers the contract and the buyer with the USDC contract (NEP-145
              storage), so USDC can be sent to them. Once each, about 0.00125 NEAR.
deposit       Moves USDC from the wallet into the contract (ft_transfer_call).
limits        The most the agent may spend per question and per UTC day, in USDC.
agent-key     Makes the agent's own key and adds it to the account as a
              function-call key that can only call request_query on the contract.
              The agent can spend within the limits, but can't withdraw, deposit,
              change limits or touch anything else.
revoke-agent  Deletes that key from the account.
withdraw      Takes USDC back out of the contract (buyers' change, owners' earnings).

Everything that signs uses near-cli-rs and your full key from the legacy
keychain (~/.near-credentials). It prints each command first and asks before
sending; --yes skips the question, --dry-run only prints.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess

from common import USDC, agent_key_problem, key_path, key_permission, load_agent_key, near_client, \
    near_text, new_agent_key, usdc_text, usdc_units
from blindfold.near import public_key

DEFAULT_CONTRACT = {"testnet": "blindfold.ryan-tobin.testnet"}  # mainnet: pass --contract


class Wallet:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.near = near_client(a.network)
        self.usdc = a.usdc or USDC[a.network]
        self.contract = a.contract or DEFAULT_CONTRACT.get(a.network)
        if not self.contract and not (a.command in ("status", "register") and a.no_contract):
            raise SystemExit("set --contract or CONTRACT_ID (or --no-contract for status and register)")

    def send(self, cmd: list[str], why: str) -> None:
        cmd = ["near"] + cmd + ["network-config", self.a.network, "sign-with-legacy-keychain", "send"]
        print(f"\n{why}\n  {shlex.join(cmd)}")
        if self.a.dry_run:
            return
        if not self.a.yes and input("send it? [y/N] ").strip().lower() != "y":
            raise SystemExit("not sent")
        if subprocess.run(cmd).returncode != 0:
            raise SystemExit("near-cli failed")

    def call(self, contract: str, method: str, args: dict, deposit: str, why: str, gas: str = "30 Tgas") -> None:
        self.send(["contract", "call-function", "as-transaction", contract, method, "json-args", json.dumps(args),
                   "prepaid-gas", gas, "attached-deposit", deposit, "sign-as", self.a.buyer], why)

    def registered(self, account: str) -> bool:
        return self.near.view(self.usdc, "storage_balance_of", {"account_id": account}) is not None

    # --- commands ---

    def status(self) -> None:
        buyer = self.a.buyer
        wallet = int(self.near.view(self.usdc, "ft_balance_of", {"account_id": buyer}))
        print(f"network       {self.a.network}, contract {self.contract or '-'}, USDC {self.usdc[:12]}…")
        print(f"wallet        {usdc_text(wallet)} USDC in {buyer}, registered with USDC: {self.registered(buyer)}")
        if self.a.no_contract:
            return
        print(f"contract      registered with USDC: {self.registered(self.contract)}")
        b = self.near.view(self.contract, "get_balance", {"account_id": buyer})
        print(f"on contract   {usdc_text(b['amount'])} USDC; limits {usdc_text(b['per_query_max'])} per question, "
              f"{usdc_text(b['daily_max'])} a day, {usdc_text(b['spent_today'])} spent today")
        path = key_path(self.a.network, buyer)
        if not path.exists():
            print("agent key     none yet (agent-key makes one)")
            return
        key = load_agent_key(self.a.network, buyer)
        problem = agent_key_problem(key_permission(self.near, buyer, key), self.contract)
        print(f"agent key     {public_key(key)} in {path}: {problem or 'ok, request_query only'}")

    def register(self) -> None:
        bounds = self.near.view(self.usdc, "storage_balance_bounds")
        cost = near_text(bounds["min"])
        accounts = [self.a.buyer] + ([] if self.a.no_contract else [self.contract])
        todo = [acc for acc in accounts if not self.registered(acc)]
        if not todo:
            print("already registered: " + ", ".join(accounts))
        for acc in todo:
            self.call(self.usdc, "storage_deposit", {"account_id": acc, "registration_only": True}, cost,
                      f"register {acc} with USDC so it can hold USDC ({cost}, paid by {self.a.buyer})")

    def deposit(self) -> None:
        units = usdc_units(self.a.amount)
        have = int(self.near.view(self.usdc, "ft_balance_of", {"account_id": self.a.buyer}))
        if units > have:
            raise SystemExit(f"{self.a.buyer} holds only {usdc_text(have)} USDC")
        if not self.registered(self.contract):
            raise SystemExit(f"{self.contract} isn't registered with USDC yet: run register first")
        self.call(self.usdc, "ft_transfer_call",
                  {"receiver_id": self.contract, "amount": str(units), "msg": json.dumps({"action": "deposit"})},
                  "1 yoctoNEAR", f"deposit {usdc_text(units)} USDC into {self.contract}", gas="100 Tgas")

    def limits(self) -> None:
        args = {"per_query_max": str(usdc_units(self.a.per_query)), "daily_max": str(usdc_units(self.a.daily))}
        self.call(self.contract, "set_limits", args, "1 yoctoNEAR",
                  f"let the agent spend at most {self.a.per_query} USDC a question and {self.a.daily} a day")

    def agent_key(self) -> None:
        key = new_agent_key(self.a.network, self.a.buyer)
        pk = public_key(key)
        print(f"agent key {pk}\n  saved to {key_path(self.a.network, self.a.buyer)} (chmod 600)")
        problem = agent_key_problem(key_permission(self.near, self.a.buyer, key), self.contract)
        if problem is None:
            print("  it's already on the account, limited to request_query")
            return
        self.send(["account", "add-key", self.a.buyer, "grant-function-call-access",
                   "--allowance", self.a.allowance, "--contract-account-id", self.contract,
                   "--function-names", "request_query", "use-manually-provided-public-key", pk],
                  f"add it to {self.a.buyer}: request_query on {self.contract} only, "
                  f"{self.a.allowance} of gas allowance, no deposits")

    def revoke_agent(self) -> None:
        key = load_agent_key(self.a.network, self.a.buyer)
        self.send(["account", "delete-keys", self.a.buyer, "public-keys", public_key(key)],
                  f"delete the agent's key from {self.a.buyer}")

    def withdraw(self) -> None:
        units = usdc_units(self.a.amount)
        if not self.registered(self.a.buyer):
            raise SystemExit(f"{self.a.buyer} isn't registered with USDC: run register --no-contract first")
        self.call(self.contract, "withdraw", {"amount": str(units)}, "1 yoctoNEAR",
                  f"withdraw {usdc_text(units)} USDC from {self.contract} to {self.a.buyer}", gas="50 Tgas")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["status", "register", "deposit", "limits", "agent-key", "revoke-agent",
                                       "withdraw"])
    p.add_argument("amounts", nargs="*", help="deposit/withdraw: USDC; limits: per-question and daily USDC")
    p.add_argument("--buyer", required=True, help="the account that signs (buyer, or owner for withdraw)")
    p.add_argument("--network", default=os.environ.get("NEAR_NETWORK", "testnet"), choices=["testnet", "mainnet"])
    p.add_argument("--contract", default=os.environ.get("CONTRACT_ID") or None,
                   help="the Blindfold contract (default on testnet: blindfold.ryan-tobin.testnet)")
    p.add_argument("--usdc", default=None, help="the USDC contract (default Circle's for the network)")
    p.add_argument("--allowance", default="0.25 NEAR", help="agent-key: NEAR the agent may burn on gas")
    p.add_argument("--no-contract", action="store_true", help="status, register: only the signing account")
    p.add_argument("--yes", action="store_true", help="send without asking")
    p.add_argument("--dry-run", action="store_true", help="print the commands, send nothing")
    a = p.parse_args()
    need = {"deposit": 1, "withdraw": 1, "limits": 2}.get(a.command, 0)
    if len(a.amounts) != need:
        raise SystemExit(f"{a.command} takes {need} amount(s)")
    if a.command in ("deposit", "withdraw"):
        a.amount = a.amounts[0]
    if a.command == "limits":
        a.per_query, a.daily = a.amounts
    w = Wallet(a)
    getattr(w, a.command.replace("-", "_"))()


if __name__ == "__main__":
    main()