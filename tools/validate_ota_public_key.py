from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

_C_STRING = re.compile(r'"(?:\\.|[^"\\])*"')


def _extract_public_key_pem(path: Path) -> bytes:
    try:
        source = path.read_text(encoding="ascii")
        decoded = "".join(ast.literal_eval(token) for token in _C_STRING.findall(source))
    except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
        raise ValueError("OTA public key header must contain a valid P-256 public key") from exc
    begin = decoded.find("-----BEGIN PUBLIC KEY-----")
    end_marker = "-----END PUBLIC KEY-----"
    end = decoded.find(end_marker, begin)
    if begin < 0 or end < 0 or "PRIVATE KEY" in decoded:
        raise ValueError("OTA public key header must contain only a P-256 public key")
    return (decoded[begin : end + len(end_marker)] + "\n").encode("ascii")


def validate_public_key_header(path: str | Path) -> None:
    try:
        key = serialization.load_pem_public_key(_extract_public_key_pem(Path(path)))
    except Exception as exc:
        if isinstance(exc, ValueError) and "P-256" in str(exc):
            raise
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
