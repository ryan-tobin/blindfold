"""Ethereum-style signatures, from scratch: keccak-256 and secp256k1 recovery.

NEAR AI Cloud's private models sign each chat completion with an ECDSA key
that lives in their TEE. The signature is Ethereum's personal_sign (EIP-191)
over a short text, so checking it means: hash the text with the EIP-191
prefix using keccak-256, recover the public key from the signature, and
compare the address it gives with the attested signing address.

Python's hashlib has SHA3-256, which pads differently from keccak-256, so
both pieces are written here (about 80 lines, no dependencies). Speed is
fine for one signature per question.
"""

from __future__ import annotations

_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_MASK = (1 << 64) - 1

def _rol(x: int, n: int) -> int:
    return ((x << n) | (x >> (64 - n))) & _MASK if n else x 

def _keccak_f(a: list[list[int]]) -> None:
    for rc in _RC:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x][y] ^= d[x]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
        for x in range(5):
            for y in range(5):
                a[x][y] = b[x][y] ^ (~b[(x + 1) % 5][y] & b[(x + 2) % 5][y])
        a[0][0] ^= rc

def keccack256(data: bytes) -> bytes:
    rate = 136
    padded = bytearray(data) + b"\x01" + b"\x00" * ((-len(data) - 1) % rate)
    padded[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(block[8 * i:8 * u + 8], "little")
        _keccak_f(a)
    return b"".join(a[i % 5][i // 5].to_bytes(8, "little") for i in range(4))

P = 2**256 - 2**32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0] and (p[1] + q[1]) % P == 0:
        return None
    if p == q:
        lam = 3 * p[0] * p[0] * pow(2 * p[1], -1, P)
    else:
        lam = (q[1] - p[1]) * pow(q[0] - p[0], -1, P)
    x = (lam * lam - p[0] - q[0]) % P
    return x, (lam * (p[0] - x) - p[1]) % P

def _mul(k: int, p):
    out = None
    while k:
        if k & 1:
            out = _add(out,p)
        p = _add(p,p)
        k >>= 1
    return out 

def personal_sign_hash(text: str) -> bytes:
    """EIP-191: keccak256("\\x19Ethereum Signed Messaged:\\n" + len + message)."""
    msg = text.encode("utf-8")
    return keccack256(b"\x19Ethereum Signed Message:\n" + str(len(msg)).encode() + msg)

def recover_address(digest: bytes, signature: bytes) -> str:
    """The 0x address whose key made thus 65-byte (r,s,v) signature over digest."""
    if len(signature) != 65:
        raise ValueError("an ECDSA signature is 65 bytes")
    r, s, v = int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:64], "big"), signature[64]
    v = v - 27 if v >= 27 else v 
    if not (0 < r < N and 0 < s < N and v in (0,1)):
        raise ValueError("bad signature values")
    y2 = (pow(r,3,P) + 7) % P
    y = pow(y2, (P + 1) // 4, P)
    if y * y % P != y2:
        raise ValueError("r is not on the curve")
    if y % 2 != v:
        y = P - y
    z = int.from_bytes(digest, "big")
    r_inv = pow(r, -1, N)
    q =  _add(_mul(s * r_inv % N, (r, y)), _mul((-z * r_inv) % N, G))
    if q is None:
        raise ValueError("no public key")
    pub = q[0].to_bytes(32, "big") + q[1].to_bytes(32, "big")
    return "0x" + keccack256(pub)[-20:].hex()

def recover_personal(text: str, signature_hex: str) -> str:
    """Address that personal_sign-ed text, lower case with 0x."""
    return recover_address(personal_sign_hash(text), bytes.fromhex(signature_hex.removeprefix("0x")))