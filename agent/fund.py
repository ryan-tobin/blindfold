"""Bring USDC to a NEAR mainnet account from Base or Arbitrum, through NEAR Intents 1Click.

    python agent/fund.py quote --amount 2 --to you.near --refund-to 0xYourBaseWallet
    python agent/fund.py quote --amount 2 --to you.near --refund-to 0xYourBaseWallet --go
    python agent/fund.py status 0xDepositAddress [--tx 0xYourBaseTxHash]

quote        Checks the NEAR account can receive USDC, then asks 1Click for a dry
             quote: what you send, what lands, the fees and the time. Nothing moves.
quote --go   Asks for a real quote, which comes with a one-time deposit address.
             You send the USDC to it from your own wallet (this script never holds
             your keys), then it watches until the USDC lands on NEAR.
status       Watches an earlier deposit address again. --tx tells 1Click your
             deposit transaction, which can make it go faster.

The quote is saved to data/intents/<deposit address>.json with 1Click's
signature, in case anything needs to be disputed. Set ONECLICK_API_KEY
(from partners.near-intents.org) to avoid the extra 0.25% fee.
Intents has no testnet: this always runs on mainnet with real money.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse

from common import ROOT, USDC, http_json, near_client, usdc_text, usdc_units

API = os.environ.get("ONECLICK_URL", "https://1click.chaindefuser.com")
ORIGIN = {  # USDC on the origin chains, as 1Click names them (GET /v0/tokens)
    "base": "nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near",
    "arb": "nep141:arb-0xaf88d065e77c8cc2239327c5edb3a432268e5831.omft.near",
}
DESTINATION = "nep141:" + USDC["mainnet"]
DONE = {"SUCCESS", "REFUNDED", "FAILED"}
SAVE_DIR = ROOT / "data" / "intents"
ADDRESS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def safe_address(address) -> str:
    """depositAddress comes from 1Click's reply and becomes a file name: letters, digits, . _ - only."""
    if not isinstance(address, str) or not ADDRESS.fullmatch(address) or ".." in address:
        raise SystemExit(f"1Click sent a deposit address that doesn't look like one: {address!r}. Not saving it.")
    return address


def headers() -> dict:
    key = os.environ.get("ONECLICK_API_KEY")
    return {"X-API-Key": key} if key else {}


def quote_request(amount: int, origin: str, recipient: str, refund_to: str, dry: bool,
                  slippage_bps: int = 100, minutes: int = 60, now: dt.datetime | None = None) -> dict:
    """The POST /v0/quote body: send exactly `amount` USDC units, deliver USDC on NEAR."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return {
        "dry": dry,
        "swapType": "EXACT_INPUT",
        "slippageTolerance": slippage_bps,
        "originAsset": ORIGIN[origin],
        "depositType": "ORIGIN_CHAIN",
        "destinationAsset": DESTINATION,
        "amount": str(amount),
        "refundTo": refund_to,
        "refundType": "ORIGIN_CHAIN",
        "recipient": recipient,
        "recipientType": "DESTINATION_CHAIN",
        "deadline": (now + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "referral": "blindfold",
    }


def describe(quote: dict, origin: str) -> str:
    q = quote["quote"]
    lines = [
        f"you send     {q['amountInFormatted']} USDC on {origin} (${q.get('amountInUsd', '?')})",
        f"you get      {q['amountOutFormatted']} USDC on NEAR (${q.get('amountOutUsd', '?')}), "
        f"at least {usdc_text(q['minAmountOut'])}",
        f"takes about  {q.get('timeEstimate', '?')} s after your deposit confirms",
    ]
    if q.get("depositAddress"):
        lines += [f"deposit to   {q['depositAddress']}  (USDC on {origin} only)",
                  f"before       {q.get('deadline', '?')}"]
    return "\n".join(lines)


def can_receive(account: str) -> str | None:
    """Why `account` can't receive mainnet USDC, or None if it can."""
    near = near_client("mainnet")
    try:
        near.rpc("query", {"request_type": "view_account", "finality": "final", "account_id": account})
    except Exception:
        return f"{account} doesn't exist on NEAR mainnet"
    if near.view(USDC["mainnet"], "storage_balance_of", {"account_id": account}) is None:
        bounds = near.view(USDC["mainnet"], "storage_balance_bounds")
        return (f"{account} isn't registered with the USDC contract, so a transfer to it would fail.\n"
                f"Register it once (costs {int(bounds['min']) / 10**24:g} NEAR, signed by {account}):\n"
                f"    python agent/wallet.py register --buyer {account} --network mainnet --no-contract")
    return None


def usdc_balance(account: str) -> int:
    return int(near_client("mainnet").view(USDC["mainnet"], "ft_balance_of", {"account_id": account}))


def watch(deposit_address: str, recipient: str | None, every: float = 10, sleep=time.sleep) -> str:
    """Poll /v0/status until it's done. Returns the final status."""
    last = None
    while True:
        s = http_json("GET", f"{API}/v0/status?depositAddress={urllib.parse.quote(deposit_address, safe='')}", headers=headers(), timeout=30)
        if s["status"] != last:
            print(f"{dt.datetime.now().strftime('%H:%M:%S')}  {s['status']}")
            last = s["status"]
        if s["status"] in DONE:
            details = s.get("swapDetails") or {}
            for tx in details.get("destinationChainTxHashes") or []:
                print(f"NEAR tx      https://nearblocks.io/txns/{tx['hash'] if isinstance(tx, dict) else tx}")
            if s["status"] == "SUCCESS" and recipient:
                print(f"{recipient} now holds {usdc_text(usdc_balance(recipient))} USDC on NEAR")
            return s["status"]
        sleep(every)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("quote", help="quote a transfer (dry unless --go)")
    q.add_argument("--amount", required=True, help="USDC to send, e.g. 2")
    q.add_argument("--to", required=True, help="the NEAR mainnet account that gets the USDC, e.g. you.near")
    q.add_argument("--refund-to", required=True, help="your wallet on the origin chain, for refunds")
    q.add_argument("--from", dest="origin", choices=sorted(ORIGIN), default="base")
    q.add_argument("--slippage", type=int, default=100, help="basis points (100 = 1%%)")
    q.add_argument("--go", action="store_true", help="get a real deposit address and watch for the deposit")
    s = sub.add_parser("status", help="watch a deposit address")
    s.add_argument("deposit_address")
    s.add_argument("--tx", help="your deposit transaction hash on the origin chain")
    s.add_argument("--to", help="the NEAR recipient, to print its balance at the end")
    a = p.parse_args()

    if a.cmd == "status":
        if a.tx:
            http_json("POST", f"{API}/v0/deposit/submit", {"txHash": a.tx, "depositAddress": a.deposit_address},
                      headers=headers(), timeout=30)
            print("told 1Click about the deposit tx")
        sys.exit(0 if watch(a.deposit_address, a.to) == "SUCCESS" else 1)

    problem = can_receive(a.to)
    if problem:
        raise SystemExit(problem)
    print(f"{a.to} holds {usdc_text(usdc_balance(a.to))} USDC on NEAR now")
    if not headers():
        print("no ONECLICK_API_KEY: 1Click adds a 0.25% fee")

    body = quote_request(usdc_units(a.amount), a.origin, a.to, a.refund_to, dry=not a.go, slippage_bps=a.slippage)
    quote = http_json("POST", f"{API}/v0/quote", body, headers=headers(), timeout=60)
    print(describe(quote, a.origin))
    if not a.go:
        print("\nthat was a dry quote: nothing moved. Add --go for a real deposit address.")
        return

    address = safe_address(quote["quote"]["depositAddress"])
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    (SAVE_DIR / f"{address}.json").write_text(json.dumps(quote, indent=2))
    print(f"\nsaved the signed quote to data/intents/{address}.json")
    print(f"now send exactly {quote['quote']['amountInFormatted']} USDC on {a.origin} to {address} "
          f"from your wallet. Watching (Ctrl-C is safe; resume with `python agent/fund.py status {address} --to {a.to}`)")
    sys.exit(0 if watch(address, a.to) == "SUCCESS" else 1)


if __name__ == "__main__":
    main()