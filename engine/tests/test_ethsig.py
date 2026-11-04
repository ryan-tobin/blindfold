"""keccak-256 and secp256k1 recovery, against known values.

The signatures were made with eth-account (Account.sign_message with
encode_defunct) from the private key 0x1111...11.
"""

from __future__ import annotations

import pytest

from blindfold import ethsig

ADDRESS = "0x19e7e376e7c213b7e7e7e46cc70a5dd086daff2a"
HELLO_SIG = ("9208d5f86a1f5d9c1908dbb42969925675ceb388616fc5b186c248e4967a03e0"
             "2e9d28a1adcb8cce60ebcef9756dee20522a2987afea3055f4edd8380d425f891b")
TEXT = "zai-org/GLM-5.1-FP8:" + "ab" * 32 + ":" + "cd" * 32
TEXT_SIG = ("cd2201ec41c1da2df744e669fee2e49bd34d461d29a0d5cff55b7e8ef87476e8"
            "144753e2a3305b66ee4b6a75e4893a1aae4fc25ea7684032b4b5b366d3dc59561b")


def test_keccak256_known_values():
    assert ethsig.keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert ethsig.keccak256(b"a" * 200).hex() == "96ea54061def936c4be90b518992fdc6f12f535068a256229aca54267b4d084d"


def test_recovers_the_signing_address():
    assert ethsig.recover_personal("hello", HELLO_SIG) == ADDRESS
    assert ethsig.recover_personal(TEXT, "0x" + TEXT_SIG) == ADDRESS


def test_other_text_recovers_another_address():
    assert ethsig.recover_personal("hello!", HELLO_SIG) != ADDRESS
    assert ethsig.recover_personal(TEXT.replace("ab", "ac", 1), TEXT_SIG) != ADDRESS


def test_bad_signatures_raise():
    with pytest.raises(ValueError):
        ethsig.recover_personal("hello", HELLO_SIG[:-2])
    with pytest.raises(ValueError):
        ethsig.recover_personal("hello", "00" * 65)