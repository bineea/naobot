from __future__ import annotations

import argparse
import json
import shutil
import struct
from hashlib import sha256
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

BOARD_ID = "XIAO_ESP32S3_SENSE"
IMAGE_NAME = "firmware.bin"
MAX_IMAGE_SIZE = 0x280000
MAX_MANIFEST_SIZE = 1024
MAX_SIGNATURE_SIZE = 128
MAX_TEXT_LENGTH = 64
ESP_IMAGE_MAGIC = 0xE9
ESP_IMAGE_HEADER_SIZE = 24
ESP_SEGMENT_HEADER_SIZE = 8
ESP_MAX_SEGMENTS = 16
ESP32_S3_CHIP_ID = 0x0009
ESP_CHECKSUM_INITIAL = 0xEF
ESP_HASH_SIZE = 32
UINT32_MAX = 0xFFFFFFFF


def _bounded_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT_LENGTH:
        raise ValueError(f"{name} must be a non-empty string of at most {MAX_TEXT_LENGTH} characters")
    return value


def _load_private_key(path: Path):
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except Exception as exc:
        raise ValueError("invalid private key") from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
        key.curve, ec.SECP256R1
    ):
        raise ValueError("private key must use ECDSA P-256")
    return key


def _canonical_manifest(image: bytes, sequence: int, version: str, key_id: str) -> bytes:
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or not 0 <= sequence <= UINT32_MAX
    ):
        raise ValueError("sequence must be a uint32 integer")
    version = _bounded_text("version", version)
    key_id = _bounded_text("key_id", key_id)
    manifest = {
        "schema": 1,
        "board_id": BOARD_ID,
        "key_id": key_id,
        "sequence": sequence,
        "version": version,
        "image_name": IMAGE_NAME,
        "image_size": len(image),
        "sha256": sha256(image).hexdigest(),
        "min_runtime_api": 1,
    }
    return json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _validate_esp_image(image: bytes) -> None:
    if len(image) < ESP_IMAGE_HEADER_SIZE:
        raise ValueError("firmware must be a valid ESP image with a complete header")
    if image[0] != ESP_IMAGE_MAGIC:
        raise ValueError("firmware must be an ESP image with magic 0xE9")
    segment_count = image[1]
    if not 1 <= segment_count <= ESP_MAX_SEGMENTS:
        raise ValueError("firmware ESP image has an invalid segment count")
    chip_id = struct.unpack_from("<H", image, 12)[0]
    if chip_id != ESP32_S3_CHIP_ID:
        raise ValueError("firmware ESP image must target ESP32-S3")
    hash_appended = image[23]
    if hash_appended not in (0, 1):
        raise ValueError("firmware ESP image has an invalid hash_appended flag")

    offset = ESP_IMAGE_HEADER_SIZE
    checksum = ESP_CHECKSUM_INITIAL
    for _ in range(segment_count):
        if offset + ESP_SEGMENT_HEADER_SIZE > len(image):
            raise ValueError("firmware ESP image has a truncated segment header")
        _load_address, data_size = struct.unpack_from("<II", image, offset)
        offset += ESP_SEGMENT_HEADER_SIZE
        if data_size > MAX_IMAGE_SIZE or offset + data_size >= len(image):
            raise ValueError("firmware ESP image has a truncated segment")
        for value in image[offset : offset + data_size]:
            checksum ^= value
        offset += data_size

    checksum_position = offset + (-(offset + 1) % 16)
    if checksum_position >= len(image):
        raise ValueError("firmware ESP image has no aligned checksum")
    if any(image[offset:checksum_position]):
        raise ValueError("firmware ESP image has nonzero checksum padding")
    if image[checksum_position] != checksum:
        raise ValueError("firmware ESP image checksum mismatch")

    image_end = checksum_position + 1
    expected_size = image_end + (ESP_HASH_SIZE if hash_appended else 0)
    if len(image) != expected_size:
        raise ValueError("firmware ESP image has invalid aligned length")
    if hash_appended:
        expected_hash = sha256(image[:image_end]).digest()
        if image[image_end:] != expected_hash:
            raise ValueError("firmware ESP image appended SHA-256 mismatch")


def create_update_package(
    image_path: str | Path,
    private_key_path: str | Path,
    output_dir: str | Path,
    sequence: int,
    version: str,
    key_id: str,
) -> dict[str, object]:
    image_path = Path(image_path)
    private_key_path = Path(private_key_path)
    output_dir = Path(output_dir)
    try:
        image_size = image_path.stat().st_size
    except OSError as exc:
        raise ValueError("firmware image is unavailable") from exc
    if image_size <= 0 or image_size > MAX_IMAGE_SIZE:
        raise ValueError(f"image size must be between 1 and {MAX_IMAGE_SIZE} bytes")

    image = image_path.read_bytes()
    if len(image) != image_size:
        raise ValueError("firmware image changed while being read")
    _validate_esp_image(image)
    private_key = _load_private_key(private_key_path)
    manifest_bytes = _canonical_manifest(image, sequence, version, key_id)
    if len(manifest_bytes) > MAX_MANIFEST_SIZE:
        raise ValueError(f"manifest size exceeds {MAX_MANIFEST_SIZE} bytes")
    signature = private_key.sign(manifest_bytes, ec.ECDSA(hashes.SHA256()))
    if len(signature) > MAX_SIGNATURE_SIZE:
        raise RuntimeError("generated signature exceeds device package limit")

    try:
        private_key.public_key().verify(signature, manifest_bytes, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise RuntimeError("generated signature failed self-verification") from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "manifest.json").write_bytes(manifest_bytes)
    (output_dir / "signature.der").write_bytes(signature)
    with image_path.open("rb") as source, (output_dir / IMAGE_NAME).open("wb") as target:
        shutil.copyfileobj(source, target, length=64 * 1024)

    written_image = (output_dir / IMAGE_NAME).read_bytes()
    if len(written_image) != image_size or sha256(written_image).hexdigest() != sha256(image).hexdigest():
        raise RuntimeError("packaged firmware failed self-verification")
    written_manifest = (output_dir / "manifest.json").read_bytes()
    private_key.public_key().verify(
        (output_dir / "signature.der").read_bytes(),
        written_manifest,
        ec.ECDSA(hashes.SHA256()),
    )
    return json.loads(written_manifest)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="创建 XIAO ESP32S3 Sense 签名 OTA 包")
    parser.add_argument("image", type=Path, help="待打包 OTA 应用镜像 micropython.bin 路径")
    parser.add_argument("--private-key", required=True, type=Path, help="显式 P-256 私钥路径")
    parser.add_argument("--output", required=True, type=Path, help="输出包目录")
    parser.add_argument("--sequence", required=True, type=int)
    parser.add_argument("--version", required=True)
    parser.add_argument("--key-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    manifest = create_update_package(
        args.image,
        args.private_key,
        args.output,
        args.sequence,
        args.version,
        args.key_id,
    )
    print(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
