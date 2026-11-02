"""Check an enclave's /attestation: is it real Intel TDX, running exactly our code?

Used by verify/verify.py (anyone checking a receipt), owner/encrypt.py (the
owner, before encrypting data to the enclave) and owner/register_enclave.py.

The chain of evidence, from Intel's root certificate down to our files:
1. The quote is signed by an Intel TDX CPU: dcap-qvl checks its certificate
   chain up to Intel's root CA, plus the revocation lists and TCB status
   fetched from a PCCS (Phala's by default, or Intel's own PCS).
2. The quote's report_data is sha256(signer key + encryption key + schema
   hash), so those keys were made inside this VM.
3. The quote's RTMR3 register equals a replay of the event log, so the log
   is genuine. Its compose-hash event is sha256 of app_compose, which holds
   docker-compose.tee.yml verbatim, which pins the engine image by digest.
4. The OS image (MRTD, RTMR0-2) is dstack's; Phala's key service checks it
   before it releases keys. We report its hash but don't rebuild it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time

from .near import b58decode

PHALA_PCCS = "https://pccs.phala.network"
BODY = 48  # TDX quote v4: 48-byte header, then the TD report
OFFSETS = {"mr_td": 136, "rt_mr0": 328, "rt_mr1": 376, "rt_mr2": 424, "rt_mr3": 472, "report_data": 520}
GOOD = {"UpToDate"}
WARN = {"SWHardeningNeeded", "ConfigurationNeeded", "ConfigurationAndSWHardeningNeeded", "OutOfDate",
        "OutOfDateConfigurationNeeded"}


def td_report(quote: bytes) -> dict[str, bytes]:
    """The measurement registers and report_data, read straight from the quote bytes."""
    if int.from_bytes(quote[:2], "little") != 4:
        raise ValueError("not a TDX v4 quote")
    return {k: quote[BODY + o:BODY + o + (64 if k == "report_data" else 48)] for k, o in OFFSETS.items()}


def event_digest(e: dict) -> str:
    """dstack's runtime event digest: sha384(event_type as u32 LE + ':' + name + ':' + payload)."""
    data = e["event_type"].to_bytes(4, "little") + b":" + e["event"].encode() + b":" + bytes.fromhex(e["event_payload"])
    return hashlib.sha384(data).hexdigest()


def replay(digests: list[str]) -> bytes:
    mr = bytes(48)
    for d in digests:
        mr = hashlib.sha384(mr + bytes.fromhex(d).ljust(48, b"\0")).digest()
    return mr


def as_text(payload: str) -> str:
    """An event payload as dstack shows it: raw bytes in hex, or hex text sent as bytes."""
    try:
        text = bytes.fromhex(payload).decode()
        bytes.fromhex(text)
        return text.lower()
    except ValueError:
        return payload.lower()


def expected_report_data(att: dict) -> bytes:
    signer = b58decode(att["signer_pubkey"].removeprefix("ed25519:"))
    return hashlib.sha256(signer + bytes.fromhex(att["encryption_pubkey"]) + bytes.fromhex(att["schema_hash"])).digest()


def intel_check(quote: bytes, pccs: str = PHALA_PCCS) -> tuple[bool | None, str]:
    """Verify the quote's signature chain up to Intel's root CA. Needs dcap-qvl and the network."""
    try:
        import dcap_qvl
    except ImportError:
        return False, "Intel signature: dcap-qvl not installed (pip install -e './engine[dev]')"
    try:
        async def fetch():  # dcap-qvl's fetch must start inside a running event loop
            return await dcap_qvl.get_collateral(pccs, quote)

        collateral = asyncio.run(fetch())
        report = dcap_qvl.verify(quote, collateral, int(time.time()))
    except Exception as e:
        return False, f"Intel signature: verification failed ({e})"
    advisories = f", advisories {', '.join(report.advisory_ids)}" if report.advisory_ids else ""
    ok = True if report.status in GOOD else None if report.status in WARN else False
    return ok, f"quote signed by a genuine Intel TDX CPU (TCB {report.status}{advisories})"


def check(att: dict, compose_file: str | None = None, pccs: str = PHALA_PCCS) -> list[tuple[bool | None, str]]:
    """All the checks on one /attestation reply, as (ok, text): True pass, None warn, False fail."""
    out: list[tuple[bool | None, str]] = []
    rd = expected_report_data(att)
    out.append((att["report_data"] == rd.hex(), "report_data = sha256(signer key + encryption key + schema hash)"))
    if not att.get("quote"):
        out.append((None, f"enclave mode: {att['mode']} (no quote: not a TEE)"))
        return out
    quote = bytes.fromhex(att["quote"])
    report = td_report(quote)
    out.append((report["report_data"] == rd.ljust(64, b"\0"), "the quote carries that report_data"))
    if att["mode"] != "tdx":
        out.append((None, f"enclave mode: {att['mode']} (not a real TEE, the quote proves nothing)"))
        return out

    out.append(intel_check(quote, pccs))
    events = json.loads(att["event_log"]) if isinstance(att["event_log"], str) else att["event_log"]
    rtmr3 = [e for e in events if e["imr"] == 3]
    out.append((all(event_digest(e) == e["digest"] for e in rtmr3), f"all {len(rtmr3)} RTMR3 events match their digests"))
    out.append((replay([e["digest"] for e in rtmr3]) == report["rt_mr3"], "replaying the event log gives the quote's RTMR3"))

    logged = {e["event"]: as_text(e["event_payload"]) for e in rtmr3}
    app_compose = att.get("app_compose") or ""
    composed = hashlib.sha256(app_compose.encode()).hexdigest()
    out.append((logged.get("compose-hash") == composed == att["compose_hash"],
                f"compose hash {composed[:16]}… is in the quote and matches app_compose"))
    if "app-id" in logged:
        out.append((logged["app-id"] == att["app_id"], f"app id {att['app_id']} is in the quote"))
    try:
        manifest = json.loads(app_compose)
    except ValueError:
        manifest = {}
    if compose_file is not None:
        out.append((manifest.get("docker_compose_file") == compose_file,
                    "the deployed compose file is docker-compose.tee.yml from this repo"))
    envs = manifest.get("allowed_envs") or []
    out.append((True if not envs else None, "no env vars can be injected by the operator" if not envs
                else f"the operator can set env vars: {', '.join(envs)}"))
    if manifest.get("pre_launch_script"):
        script = hashlib.sha256(manifest["pre_launch_script"].encode()).hexdigest()
        out.append((True, f"a pre-launch script runs too (sha256 {script[:16]}…, in app_compose)"))
    os_hash = att.get("os_image_hash") or "unknown"
    out.append((True, f"OS image {os_hash[:16]}…, MRTD {report['mr_td'].hex()[:16]}… (checked by Phala's KMS, not rebuilt here)"))
    return out