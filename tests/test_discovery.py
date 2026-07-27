"""Discovery parsing, exercised with real Tuya framing built by tinytuya itself.

Beta 1 speaks protocol 3.5, which uses the 6699/GCM framing; older devices use 55AA. Both
land on the same UDP ports, so both are covered here.
"""

from __future__ import annotations

import json
import struct

import tinytuya
from tinytuya.core.header import PREFIX_55AA_VALUE, PREFIX_6699_VALUE

from pot.skills.lights import discovery

BROADCAST = {
    "ip": "192.0.2.27",
    "gwId": "bf00000000000000beta1",
    "productKey": "keyexampleproduct",
    "version": "3.5",
}


def _packet_55aa(payload: dict) -> bytes:
    encrypted = tinytuya.AESCipher(tinytuya.udpkey).encrypt(json.dumps(payload).encode(), False)
    # Devices put a return code in front of the encrypted body; pack_message doesn't add one
    # for 55AA, so emulate it here or the unpacker eats the first four ciphertext bytes.
    body = struct.pack(">I", 0) + encrypted
    message = tinytuya.TuyaMessage(0, tinytuya.UDP_NEW, None, body, 0, True, PREFIX_55AA_VALUE, False)
    return tinytuya.pack_message(message)


def _packet_6699(payload: dict) -> bytes:
    body = json.dumps(payload).encode()
    message = tinytuya.TuyaMessage(0, tinytuya.UDP_NEW, 0, body, 0, True, PREFIX_6699_VALUE, False)
    return tinytuya.pack_message(message, hmac_key=tinytuya.udpkey)


def test_parses_a_55aa_broadcast():
    device = discovery._parse(_packet_55aa(BROADCAST), "192.0.2.27")
    assert device is not None
    assert device.gwid == BROADCAST["gwId"]
    assert device.ip == "192.0.2.27"
    assert device.version == 3.5
    assert device.product_key == BROADCAST["productKey"]


def test_parses_a_6699_broadcast_from_a_protocol_35_device():
    device = discovery._parse(_packet_6699(BROADCAST), "192.0.2.27")
    assert device is not None
    assert device.gwid == BROADCAST["gwId"]
    assert device.version == 3.5


def test_ignores_foreign_traffic():
    assert discovery._parse(b"not a tuya packet at all", "10.0.0.5") is None


def test_falls_back_to_the_source_ip():
    payload = {key: value for key, value in BROADCAST.items() if key != "ip"}
    device = discovery._parse(_packet_6699(payload), "192.0.2.99")
    assert device is not None and device.ip == "192.0.2.99"


def test_defaults_the_version_when_missing_or_junk():
    device = discovery._parse(_packet_6699(dict(BROADCAST, version="banana")), "192.0.2.27")
    assert device is not None and device.version == 3.3


def test_forget_clears_a_cached_address():
    device = discovery._parse(_packet_6699(BROADCAST), "192.0.2.27")
    assert device is not None
    discovery._cache[device.gwid] = (0.0, device)
    discovery.forget(device.gwid)
    assert device.gwid not in discovery._cache
