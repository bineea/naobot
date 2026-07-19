from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

_C_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
_PUBLIC_KEY_DEFINE = re.compile(
    r"^\s*#define\s+NAOBOT_OTA_PUBLIC_KEY_PEM\b(?P<body>.*)$"
)


def _extract_public_key_pem(path: Path) -> bytes:
    try:
        source = path.read_text(encoding="ascii")
        lines = source.splitlines()
    except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
        raise ValueError("OTA public key header must contain a valid P-256 public key") from exc

    definitions = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := _PUBLIC_KEY_DEFINE.match(line)) is not None
    ]
    if len(definitions) != 1:
        raise ValueError("OTA public key header must define exactly one NAOBOT_OTA_PUBLIC_KEY_PEM")

    index, match = definitions[0]
    fragments = []
    fragment = match.group("body")
    while True:
        continued = fragment.rstrip().endswith("\\")
        fragments.append(fragment.rstrip()[:-1] if continued else fragment)
        if not continued:
            break
        index += 1
        if index >= len(lines):
            raise ValueError("OTA public key header must contain a valid P-256 public key")
        fragment = lines[index]
    macro_body = "\n".join(fragments)
    tokens = _C_STRING.findall(macro_body)
    if not tokens or _C_STRING.sub("", macro_body).strip():
        raise ValueError("OTA public key header must contain a valid P-256 public key")
    try:
        decoded = "".join(ast.literal_eval(token) for token in tokens)
    except (SyntaxError, ValueError) as exc:
        raise ValueError("OTA public key header must contain a valid P-256 public key") from exc

    begin_marker = "-----BEGIN PUBLIC KEY-----"
    end_marker = "-----END PUBLIC KEY-----"
    if (
        decoded.count(begin_marker) != 1
        or decoded.count(end_marker) != 1
        or "PRIVATE KEY" in decoded
        or not decoded.startswith(begin_marker)
        or not decoded.rstrip().endswith(end_marker)
    ):
        raise ValueError("OTA public key header must contain only a P-256 public key")
    return (decoded.rstrip() + "\n").encode("ascii")


def validate_public_key_header(path: str | Path) -> None:
    pem = _extract_public_key_pem(Path(path))
    try:
        key = serialization.load_pem_public_key(pem)
    except Exception as exc:
        raise ValueError("OTA public key header must contain a valid P-256 public key") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
        key.curve, ec.SECP256R1
    ):
        raise ValueError("OTA public key must use ECDSA P-256")


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 OTA 构建公钥头文件")
    parser.add_argument("header", type=Path)
    args = parser.parse_args()
    validate_public_key_header(args.header)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
