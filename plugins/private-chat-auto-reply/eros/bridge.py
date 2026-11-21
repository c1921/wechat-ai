"""Serve the private-chat reply protocol over a socket-activated SSH relay."""

import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path

from eros_provider import ErosGenerator
from llm_provider import ProviderError
from wechat_invite_admission import CREDENTIAL_NAME, PROBE_OPERATION, read_key, verify


REQUEST_LIMIT = 131072


class WeChatGenerator(ErosGenerator):
    def __init__(self, config: dict, credentials: Path):
        super().__init__(
            config["genome_id"],
            (credentials / "eros_jwt").read_text(encoding="utf-8").strip(),
            (credentials / "eros_caller").read_text(encoding="utf-8").strip(),
        )
        self.namespace = uuid.UUID(config["conversation_namespace"])

    def identity(self, conversation: str) -> uuid.UUID:
        # Keep the deployed identity prefix so existing Eros sessions and memories remain attached.
        return uuid.uuid5(self.namespace, "wechat-aimee:" + conversation)


def allowed_conversations(config: dict) -> set[str]:
    if "allowed_conversations" in config:
        values = config["allowed_conversations"]
    else:
        values = [config["allowed_conversation"]]
    if not isinstance(values, list) or any(
        not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
        for value in values
    ):
        raise ValueError("invalid_conversation_allowlist")
    return set(values)


def handle(request: dict, config: dict, generator: WeChatGenerator, admission_key=None) -> dict:
    operation = request.get("op")
    if operation == "health":
        with generator.http.open(generator.base_url + "/healthz", timeout=5) as response:
            if response.status != 200:
                raise ProviderError("engine_unhealthy")
        return {"ok": True, "role": config["role"]}
    if operation not in ("generate", PROBE_OPERATION):
        raise ValueError("invalid_operation")

    conversation = request.get("conversation")
    message_id = request.get("message_id")
    text = request.get("text")
    if (
        not isinstance(conversation, str) or re.fullmatch(r"[0-9a-f]{64}", conversation) is None
        or not isinstance(message_id, str)
        or not re.fullmatch(r"-?[1-9][0-9]{0,19}", message_id)
        or not isinstance(text, str)
    ):
        raise ValueError("invalid_turn")
    if operation == PROBE_OPERATION:
        if not verify(admission_key, request):
            raise ValueError("invalid_invite_admission")
        return {"ok": True}
    if not text.strip() or (
        conversation not in allowed_conversations(config) and not verify(admission_key, request)
    ):
        raise ValueError("invalid_turn")
    reply = asyncio.run(generator.generate(text, conversation=conversation, message_id=message_id))
    if not reply.strip() or len(reply.encode("utf-8")) > 16000:
        raise ProviderError("invalid_reply")
    return {"ok": True, "reply": reply}


def main():
    try:
        line = sys.stdin.buffer.readline(REQUEST_LIMIT + 1)
        if not line.endswith(b"\n") or len(line) > REQUEST_LIMIT:
            raise ValueError("invalid_request_size")
        request = json.loads(line)
        if not isinstance(request, dict):
            raise ValueError("invalid_request")
        credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
        config = json.loads((credentials / "bridge_config").read_text(encoding="utf-8"))
        generator = WeChatGenerator(config, credentials)
        admission_key = None
        try:
            admission_key = read_key(credentials / CREDENTIAL_NAME)
        except (OSError, ValueError):
            pass
        response = handle(request, config, generator, admission_key)
    except (ValueError, KeyError, OSError, ProviderError) as error:
        print(f"WeChat reply request stopped ({type(error).__name__})", file=sys.stderr)
        response = {"ok": False, "error": "request_failed"}
    print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
