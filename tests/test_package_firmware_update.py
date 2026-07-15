from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "package_firmware_update.py"
MAX_IMAGE_SIZE = 0x280000


def load_packager():
    assert TOOL_PATH.exists(), "固件更新打包工具尚未实现"
    spec = importlib.util.spec_from_file_location("package_firmware_update", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    image.write_bytes(b"signed firmware image")
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
    valid.write_bytes(b"image")

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
    image.write_bytes(b"image")
    private_key = tmp_path / "signing-key.pem"
    write_private_key(private_key)

    invalid_values = (
        {"sequence": -1},
        {"sequence": True},
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
    image.write_bytes(b"image")
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
    image.write_bytes(b"image")
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
