from __future__ import annotations

import importlib.util
import json
import struct
from hashlib import sha256
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "package_firmware_update.py"
KEY_VALIDATOR_PATH = ROOT / "tools" / "validate_ota_public_key.py"
MAX_IMAGE_SIZE = 0x280000


def load_packager():
    assert TOOL_PATH.exists(), "固件更新打包工具尚未实现"
    spec = importlib.util.spec_from_file_location("package_firmware_update", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_key_validator():
    assert KEY_VALIDATOR_PATH.exists(), "OTA 构建公钥校验器尚未实现"
    spec = importlib.util.spec_from_file_location("validate_ota_public_key", KEY_VALIDATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def minimal_esp_image(
    payload: bytes = b"\x00",
    *,
    chip_id: int = 0x0009,
    hash_appended: bool = False,
) -> bytes:
    header = bytearray(24)
    header[0] = 0xE9
    header[1] = 1
    struct.pack_into("<H", header, 12, chip_id)
    header[23] = int(hash_appended)
    segment = struct.pack("<II", 0x3F400020, len(payload)) + payload
    checksum = 0xEF
    for value in payload:
        checksum ^= value
    image_without_checksum = bytes(header) + segment
    padding = b"\x00" * (-(len(image_without_checksum) + 1) % 16)
    image = image_without_checksum + padding + bytes([checksum])
    if hash_appended:
        image += sha256(image).digest()
    return image


def write_private_key(path: Path):
    key = ec.generate_private_key(ec.SECP256R1())
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return key.public_key()


def test_package_is_canonical_signed_and_self_verifiable(tmp_path: Path) -> None:
    packager = load_packager()
    image = tmp_path / "input.bin"
    private_key = tmp_path / "signing-key.pem"
    output = tmp_path / "package"
    image.write_bytes(minimal_esp_image(b"signed firmware image"))
    public_key = write_private_key(private_key)

    result = packager.create_update_package(
        image_path=image,
        private_key_path=private_key,
        output_dir=output,
        sequence=42,
        version="1.2.3",
        key_id="factory-2026",
    )

    manifest_bytes = (output / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert set(manifest) == {
        "schema",
        "board_id",
        "key_id",
        "sequence",
        "version",
        "image_name",
        "image_size",
        "sha256",
        "min_runtime_api",
    }
    assert manifest_bytes == json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    assert manifest == result
    assert manifest["schema"] == 1
    assert manifest["board_id"] == "XIAO_ESP32S3_SENSE"
    assert manifest["image_name"] == "firmware.bin"
    assert manifest["min_runtime_api"] == 1
    assert (output / "firmware.bin").read_bytes() == image.read_bytes()
    public_key.verify(
        (output / "signature.der").read_bytes(),
        manifest_bytes,
        ec.ECDSA(hashes.SHA256()),
    )


def test_package_rejects_wrong_key_curve_empty_and_oversized_images(tmp_path: Path) -> None:
    packager = load_packager()
    private_key = tmp_path / "signing-key.pem"
    write_private_key(private_key)
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    oversized = tmp_path / "oversized.bin"
    with oversized.open("wb") as stream:
        stream.seek(MAX_IMAGE_SIZE)
        stream.write(b"x")
    valid = tmp_path / "valid.bin"
    valid.write_bytes(minimal_esp_image())

    with pytest.raises(ValueError, match="image size"):
        packager.create_update_package(empty, private_key, tmp_path / "empty-out", 1, "1", "dev")
    with pytest.raises(ValueError, match="image size"):
        packager.create_update_package(
            oversized,
            private_key,
            tmp_path / "large-out",
            1,
            "1",
            "dev",
        )

    wrong_curve = ec.generate_private_key(ec.SECP384R1())
    wrong_path = tmp_path / "wrong-curve.pem"
    wrong_path.write_bytes(
        wrong_curve.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(ValueError, match="P-256"):
        packager.create_update_package(
            valid,
            wrong_path,
            tmp_path / "curve-out",
            1,
            "1",
            "dev",
        )


def test_package_rejects_invalid_manifest_inputs_and_bad_private_key(tmp_path: Path) -> None:
    packager = load_packager()
    image = tmp_path / "valid.bin"
    image.write_bytes(minimal_esp_image())
    private_key = tmp_path / "signing-key.pem"
    write_private_key(private_key)

    invalid_values = (
        {"sequence": -1},
        {"sequence": True},
        {"sequence": 0x1_0000_0000},
        {"version": ""},
        {"version": "v" * 65},
        {"key_id": ""},
        {"key_id": "k" * 65},
    )
    for overrides in invalid_values:
        values = {"sequence": 1, "version": "1.0", "key_id": "dev"}
        values.update(overrides)
        with pytest.raises(ValueError):
            packager.create_update_package(
                image,
                private_key,
                tmp_path / ("bad-" + str(len(str(overrides)))),
                **values,
            )

    bad_key = tmp_path / "not-a-key.pem"
    bad_key.write_text("not a private key", encoding="ascii")
    with pytest.raises(ValueError, match="private key"):
        packager.create_update_package(image, bad_key, tmp_path / "bad-key", 1, "1", "dev")


def test_package_rejects_manifest_that_exceeds_device_limit_after_ascii_escaping(
    tmp_path: Path,
) -> None:
    packager = load_packager()
    image = tmp_path / "valid.bin"
    image.write_bytes(minimal_esp_image())
    private_key = tmp_path / "signing-key.pem"
    write_private_key(private_key)

    with pytest.raises(ValueError, match="manifest size"):
        packager.create_update_package(
            image,
            private_key,
            tmp_path / "oversized-manifest",
            1,
            "\U0001f600" * 64,
            "\U0001f680" * 64,
        )


def test_mutated_package_signature_is_rejected(tmp_path: Path) -> None:
    packager = load_packager()
    image = tmp_path / "valid.bin"
    image.write_bytes(minimal_esp_image())
    private_key = tmp_path / "signing-key.pem"
    public_key = write_private_key(private_key)
    output = tmp_path / "package"
    packager.create_update_package(image, private_key, output, 1, "1.0", "dev")

    with pytest.raises(InvalidSignature):
        public_key.verify(
            (output / "signature.der").read_bytes(),
            (output / "manifest.json").read_bytes() + b" ",
            ec.ECDSA(hashes.SHA256()),
        )


@pytest.mark.parametrize(
    "image_bytes",
    [
        b"not an esp image",
        b"\xE9\x01" + b"\x00" * 21,
        b"\x00\x01" + b"\x00" * 31,
        b"\xE9\x00" + b"\x00" * 31,
        b"\xE9\x11" + b"\x00" * 31,
        bytes(bytearray([0xE9, 1]) + bytearray(22)) + struct.pack("<II", 0, 8) + b"\x00",
    ],
)
def test_package_rejects_non_esp_images(tmp_path: Path, image_bytes: bytes) -> None:
    packager = load_packager()
    image = tmp_path / "invalid.bin"
    image.write_bytes(image_bytes)
    private_key = tmp_path / "signing-key.pem"
    write_private_key(private_key)

    with pytest.raises(ValueError, match="ESP image"):
        packager.create_update_package(image, private_key, tmp_path / "out", 1, "1", "dev")


@pytest.mark.parametrize(
    ("image_bytes", "error"),
    [
        (minimal_esp_image(chip_id=0x0000), "ESP32-S3"),
        (
            minimal_esp_image()[:-1] + bytes([minimal_esp_image()[-1] ^ 0x01]),
            "checksum",
        ),
        (
            minimal_esp_image(hash_appended=True)[:-1]
            + bytes([minimal_esp_image(hash_appended=True)[-1] ^ 0x01]),
            "SHA-256",
        ),
    ],
)
def test_package_rejects_wrong_chip_checksum_and_appended_hash(
    tmp_path: Path,
    image_bytes: bytes,
    error: str,
) -> None:
    packager = load_packager()
    image = tmp_path / "invalid.bin"
    image.write_bytes(image_bytes)
    private_key = tmp_path / "signing-key.pem"
    write_private_key(private_key)

    with pytest.raises(ValueError, match=error):
        packager.create_update_package(image, private_key, tmp_path / "out", 1, "1", "dev")


def test_package_accepts_valid_s3_image_with_appended_hash(tmp_path: Path) -> None:
    packager = load_packager()
    image = tmp_path / "valid-hash.bin"
    image.write_bytes(minimal_esp_image(b"hashed", hash_appended=True))
    private_key = tmp_path / "signing-key.pem"
    write_private_key(private_key)

    manifest = packager.create_update_package(
        image,
        private_key,
        tmp_path / "out",
        1,
        "1",
        "dev",
    )

    assert manifest["image_size"] == len(image.read_bytes())


def _write_public_key_header(path: Path, public_key) -> None:
    pem = public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    lines = "".join(f'"{line}\\n" \\\n' for line in pem.splitlines())
    path.write_text(
        "#define NAOBOT_OTA_PUBLIC_KEY_PEM \\\n" + lines.rstrip(" \\\n") + "\n",
        encoding="ascii",
    )


def test_build_public_key_validator_accepts_only_p256(tmp_path: Path) -> None:
    validator = load_key_validator()
    p256 = tmp_path / "p256.h"
    p384 = tmp_path / "p384.h"
    rsa_header = tmp_path / "rsa.h"
    malformed = tmp_path / "malformed.h"
    _write_public_key_header(p256, ec.generate_private_key(ec.SECP256R1()).public_key())
    _write_public_key_header(p384, ec.generate_private_key(ec.SECP384R1()).public_key())
    _write_public_key_header(
        rsa_header,
        rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key(),
    )
    malformed.write_text(
        '#define NAOBOT_OTA_PUBLIC_KEY_PEM "-----BEGIN PUBLIC KEY-----\\ninvalid\\n"\n',
        encoding="ascii",
    )

    validator.validate_public_key_header(p256)
    for rejected in (p384, rsa_header, malformed):
        with pytest.raises(ValueError, match="P-256"):
            validator.validate_public_key_header(rejected)


def test_build_public_key_validator_reads_only_one_named_macro(tmp_path: Path) -> None:
    validator = load_key_validator()
    valid_public_key = ec.generate_private_key(ec.SECP256R1()).public_key()
    valid = tmp_path / "valid.h"
    _write_public_key_header(valid, valid_public_key)
    valid_macro = valid.read_text(encoding="ascii")

    unrelated = tmp_path / "unrelated-strings.h"
    unrelated.write_text(
        '#define DECOY "-----BEGIN PUBLIC KEY-----\\n"\n'
        + valid_macro.replace("#define NAOBOT_OTA_PUBLIC_KEY_PEM", "#define OTHER_PART")
        + '#define NAOBOT_OTA_PUBLIC_KEY_PEM "invalid"\n',
        encoding="ascii",
    )
    duplicate = tmp_path / "duplicate.h"
    duplicate.write_text(valid_macro + valid_macro, encoding="ascii")

    with pytest.raises(ValueError, match="P-256"):
        validator.validate_public_key_header(unrelated)
    with pytest.raises(ValueError, match="exactly one"):
        validator.validate_public_key_header(duplicate)


def test_cli_identifies_the_ota_application_image() -> None:
    source = TOOL_PATH.read_text(encoding="utf-8")

    assert 'help="待打包 OTA 应用镜像 micropython.bin 路径"' in source
