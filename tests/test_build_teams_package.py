"""Offline package safety and exact requested identity/permission wiring."""
import importlib.util
import json
from pathlib import Path
import struct
import zipfile
import zlib

import pytest

_spec = importlib.util.spec_from_file_location(
    "teams_package_builder", Path(__file__).resolve().parents[1] / "scripts" / "build_teams_package.py"
)
builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builder)


@pytest.fixture
def inputs():
    return dict(bot_id="11111111-2222-4333-8444-555555555555",
                app_id="66666666-7777-4888-8999-aaaaaaaaaaaa",
                developer_name="Test publisher", website_url="https://example.com/support",
                privacy_url="https://example.com/privacy", terms_url="https://example.com/terms")


def _png(size):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    pixels = (b"\x00" + b"\xff\xff\xff\x00" * size) * size
    return (builder.PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


@pytest.fixture
def assets(tmp_path):
    color, outline = tmp_path / "supplied-color.png", tmp_path / "supplied-outline.png"
    color.write_bytes(_png(192))
    outline.write_bytes(_png(32))
    return dict(color_icon=color, outline_icon=outline, output=tmp_path / "hermes-teams.zip")


def test_package_preserves_inputs_and_only_requested_capabilities(inputs, assets):
    output = builder.build_package(**inputs, **assets)
    with zipfile.ZipFile(output) as package:
        assert package.namelist() == ["manifest.json", "color.png", "outline.png"]
        manifest = json.loads(package.read("manifest.json"))
        assert package.read("color.png") == assets["color_icon"].read_bytes()
        assert package.read("outline.png") == assets["outline_icon"].read_bytes()
    assert manifest["id"] == inputs["app_id"]
    assert manifest["bots"] == [{"botId": inputs["bot_id"], "scopes": ["personal", "team"],
                                 "supportsFiles": True, "isNotificationOnly": False}]
    assert manifest["webApplicationInfo"] == {"id": inputs["bot_id"], "resource": f"api://{inputs['bot_id']}"}
    assert manifest["authorization"]["permissions"]["resourceSpecific"] == [
        {"name": "ChannelMessage.Read.Group", "type": "Application"}]
    assert manifest["manifestVersion"] == "1.25"
    assert manifest["supportsChannelFeatures"] == "tier1"
    assert "supportedChannelTypes" not in manifest
    assert manifest["name"] == {"short": "Hermes Teams", "full": "Hermes Teams"}
    assert manifest["developer"]["privacyUrl"] == inputs["privacy_url"]
    assert "Sites.ReadWrite.All" not in json.dumps(manifest)  # Granted in Entra, not RSC.


@pytest.mark.parametrize("key,value", [
    ("bot_id", "not-a-guid"), ("app_id", "00000000-0000-0000-0000-000000000000"),
    ("bot_id", "{11111111-2222-4333-8444-555555555555}"),
    ("developer_name", ""), ("developer_name", "x" * 33), ("developer_name", "A\nB"),
    ("display_name", ""), ("display_name", "x" * 31), ("display_name", " App"),
    ("full_name", "x" * 101), ("full_name", "A\nB"),
    ("short_description", ""), ("short_description", "x" * 81),
    ("full_description", ""), ("full_description", "x" * 4001),
    ("version", "1.0"), ("version", "01.0.0"),
    ("website_url", "http://example.com"), ("privacy_url", "https://user:secret@example.com"),
    ("terms_url", "https://localhost"), ("website_url", "https://127.0.0.1"),
    ("website_url", "https://example.invalid/privacy"), ("website_url", "https://example.com/?token=secret"),
    ("privacy_url", "https://example.com\\@evil.com"), ("terms_url", "https://example.com:9443"),
])
def test_invalid_manifest_inputs_fail_before_writing(inputs, assets, key, value):
    inputs[key] = value
    with pytest.raises(ValueError):
        builder.build_package(**inputs, **assets)
    assert not assets["output"].exists()


@pytest.mark.parametrize("invalid", [b"not png", _png(32), _png(192)[:-4], _png(192) + b"extra",
                                    _png(192)[:-1] + b"X", b"x" * (builder.MAX_ICON_BYTES + 1)],
                         ids=["not_png", "wrong_dimensions", "truncated", "trailing_data", "crc", "oversize"])
def test_invalid_color_icon_rejected(inputs, assets, invalid):
    assets["color_icon"].write_bytes(invalid)
    with pytest.raises(ValueError):
        builder.build_package(**inputs, **assets)
    assert not assets["output"].exists()


def test_outline_dimensions_checked(inputs, assets):
    assets["outline_icon"].write_bytes(_png(192))
    with pytest.raises(ValueError, match="32x32"):
        builder.build_package(**inputs, **assets)


def test_existing_output_never_overwritten(inputs, assets):
    assets["output"].write_bytes(b"existing package")
    with pytest.raises(FileExistsError):
        builder.build_package(**inputs, **assets)
    assert assets["output"].read_bytes() == b"existing package"


def test_cli_build_and_input_failure(inputs, assets, capsys):
    inputs.update(display_name="Example Assistant", full_name="Example Company's Assistant",
                  short_description="An assistant for our team.",
                  full_description="Work with our team's documents and share progress in Teams.")
    args = [part for key, value in {**inputs, **assets}.items() for part in ("--" + key.replace("_", "-"), str(value))]
    assert builder.main(args) == 0
    assert "Created" in capsys.readouterr().out
    with zipfile.ZipFile(assets["output"]) as package:
        manifest = json.loads(package.read("manifest.json"))
    assert manifest["name"] == {"short": inputs["display_name"], "full": inputs["full_name"]}
    assert manifest["description"] == {"short": inputs["short_description"], "full": inputs["full_description"]}
    with pytest.raises(SystemExit) as exc:
        builder.main(args)
    assert exc.value.code == 2


def test_custom_display_name_is_full_name_fallback(inputs):
    manifest = builder.build_manifest(**inputs, display_name="Example Assistant")
    assert manifest["name"] == {"short": "Example Assistant", "full": "Example Assistant"}


def test_output_must_be_zip(inputs, assets):
    assets["output"] = assets["output"].with_suffix(".png")
    with pytest.raises(ValueError, match=".zip"):
        builder.build_package(**inputs, **assets)
