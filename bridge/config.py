"""Validated, atomic local settings. No credentials are stored here."""

import ipaddress
import json
import math
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

DEFAULTS = {"upstream_url": "", "port": 8787, "timeout_seconds": 600,
            "allow_response_link_removal": False}


def validate(values, *, require_upstream=False):
    if not isinstance(values, dict) or set(values) - DEFAULTS.keys():
        raise ValueError("配置字段无效。API Key 应填写在客户端中。")
    merged = DEFAULTS | values
    if type(merged["port"]) is not int or not 1 <= merged["port"] <= 65535:
        raise ValueError("端口必须是 1 到 65535 的整数。")
    timeout = merged["timeout_seconds"]
    if type(timeout) not in {int, float} or not 1 <= timeout <= 3600 or not math.isfinite(timeout):
        raise ValueError("超时必须在 1 到 3600 秒之间。")
    if type(merged["allow_response_link_removal"]) is not bool:
        raise ValueError("完整历史选项必须是布尔值。")
    upstream = merged["upstream_url"]
    if not isinstance(upstream, str):
        raise ValueError("上游地址必须是字符串。")
    if not upstream and not require_upstream:
        return merged
    if not upstream or any(character.isspace() or ord(character) < 32 for character in upstream):
        raise ValueError("请填写完整的上游 API Base URL。")
    try:
        parsed = urlsplit(upstream)
        upstream_port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise ValueError("上游地址或端口无效。") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or not 1 <= upstream_port <= 65535):
        raise ValueError("上游地址必须是 HTTP(S) 地址，不能含凭据、查询参数或片段。")
    host = parsed.hostname.rstrip(".").lower()
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost"
    if local and upstream_port == merged["port"]:
        raise ValueError("上游不能指向代理自己的端口。")
    merged["upstream_url"] = upstream.rstrip("/")
    return merged


def read(path):
    if not Path(path).exists():
        return DEFAULTS.copy()
    return validate(json.loads(Path(path).read_text(encoding="utf-8-sig")))


def write(path, values):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent,
                                         delete=False) as output:
            temporary = Path(output.name)
            json.dump(values, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
