"""The engine's side of the Blindfold contract: read the budget, settle requests.

On-chain, epsilon is a whole number of millionths (1.0 = 1000000), so the
engine converts its exact fractions both ways and refuses anything finer.
Only the enclave's own account (the implicit account of its signer key) can
call settle, and the contract checks that.
"""

from __future__ import annotations

import os
from fractions import Fraction

from .enclave import Enclave
from .near import RPC_URLS, Near

MILLION = 1_000_000


def to_millionths(eps: Fraction) -> int:
    x = eps * MILLION
    if x.denominator != 1 or x < 0:
        raise ValueError(f"epsilon {eps} is not a whole number of millionths")
    return int(x)


def from_millionths(n: int) -> Fraction:
    return Fraction(int(n), MILLION)


class Chain:
    def __init__(self, contract_id: str, near: Near, enclave: Enclave):
        self.contract_id = contract_id
        self.near = near
        self.enclave = enclave

    @classmethod
    def from_env(cls, enclave: Enclave) -> Chain | None:
        contract = os.environ.get("CONTRACT_ID", "").strip()
        if not contract:
            return None  # local mode: no chain
        network = os.environ.get("NEAR_NETWORK", "testnet")
        rpc = os.environ.get("NEAR_RPC_URL") or RPC_URLS[network]
        return cls(contract, Near(rpc), enclave)

    def dataset(self, dataset_id: str) -> dict:
        d = self.near.view(self.contract_id, "get_dataset", {"id": dataset_id})
        if d is None:
            raise SystemExit(f"dataset {dataset_id!r} is not registered on {self.contract_id}: run `make register`")
        return d

    def request(self, request_id: int) -> dict | None:
        return self.near.view(self.contract_id, "get_request", {"id": str(request_id)})

    def registered_enclave(self) -> dict | None:
        return self.near.view(self.contract_id, "get_enclave", {})

    def settle(self, request_id: int, status: str, receipt_hash: str, epsilon_charged: Fraction,
               refusal: str | None) -> str:
        """Settle one pending request, signed by the enclave's account. Returns the tx hash."""
        args = {"request_id": str(request_id), "status": status, "receipt_hash": receipt_hash,
                "epsilon_charged": to_millionths(epsilon_charged), "refusal": refusal}
        tx_hash, _ = self.near.call(self.enclave.signer, self.enclave.account_id, self.contract_id, "settle", args)
        return tx_hash