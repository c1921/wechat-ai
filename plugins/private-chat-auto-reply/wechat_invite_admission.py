"""Short-lived, request-bound admission on the existing SSH transport."""

import hashlib
import hmac
import json
import re
import time


CREDENTIAL_NAME = "invite_admission_hmac"
PROBE_OPERATION = "verify_invite_admission"
PROOF_LIFETIME = 120
CLOCK_SKEW = 5


def read_key(path):
    value = path.read_text(encoding="ascii").strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise ValueError("invalid_invite_admission_key")
    return bytes.fromhex(value)


def _digest(key, payload, expires):
    body = json.dumps([
        "wechat-private-chat-invite-v1", payload["op"], payload["conversation"],
        payload["message_id"], expires, hashlib.sha256(payload["text"].encode("utf-8")).hexdigest(),
    ], separators=(",", ":")).encode("utf-8")
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def sign(key, payload):
    expires = int(time.time()) + PROOF_LIFETIME
    return {"expires_at": expires, "signature": _digest(key, payload, expires)}


def verify(key, payload):
    proof = payload.get("invite_admission")
    if key is None or not isinstance(proof, dict):
        return False
    expires = proof.get("expires_at")
    signature = proof.get("signature")
    now = int(time.time())
    return (
        type(expires) is int and now < expires <= now + PROOF_LIFETIME + CLOCK_SKEW
        and isinstance(signature, str) and re.fullmatch(r"[0-9a-f]{64}", signature) is not None
        and hmac.compare_digest(_digest(key, payload, expires), signature)
    )
