# -*- coding: utf-8 -*-
"""
Одноразовый ключ RSA для тестов и сборка из него ключа и подписи OpenPGP —
чтобы проверять обновление yt-dlp на подставном сервере. Ключ ничего
не защищает: его закрытая часть лежит прямо здесь.
"""
import base64
import hashlib

from streamvault import openpgp

N = int(
    "f2c03c383c9815518fa7190ab82492fa213c65a7fb29c8208fe655e0f34a3aaf608275577b8cda170d34111a"
    "98eebc93e67b396c588364fd383c24dc5179a671bf9d241bbe87b6a4da43a637318cd1a050467637877302b9"
    "32f78419dcf6830293b9349d9186890d30398356a2b4091cc12e54b7390a7fcebcd6b6214b975913", 16)
E = 65537
D = int(
    "b642099f5a34aaccf1ee4e5448ad82820a8783a0fbbd5864c4fb4b69f9eb0c3a0b5be7d37c6539d97f85a2d1"
    "e0165f152c9742d0a0cf064e989f8f8d5b74300d01051805d79999eac4d6bd222a8f30189c8748c8114c360b"
    "86914bad342aaa390f24f0e76fa7095c8c2168c2f0bc5569aabc67ef588afa194962c877e894c21", 16)
CREATED = 1700000000


def _packet(tag, body):
    if len(body) < 192:
        length = bytes([len(body)])
    elif len(body) < 8384:
        length = bytes([((len(body) - 192) >> 8) + 192, (len(body) - 192) & 0xFF])
    else:
        length = b"\xff" + len(body).to_bytes(4, "big")
    return bytes([0xC0 | tag]) + length + body


def _mpi(value):
    return value.bit_length().to_bytes(2, "big") + value.to_bytes((value.bit_length() + 7) // 8, "big")


def _key_body():
    return b"\x04" + CREATED.to_bytes(4, "big") + b"\x01" + _mpi(N) + _mpi(E)


def fingerprint():
    body = _key_body()
    return hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body).hexdigest().upper()


def armored_key():
    data = _packet(6, _key_body())
    text = base64.b64encode(data).decode("ascii")
    body = "\n".join(text[start:start + 64] for start in range(0, len(text), 64))
    crc = base64.b64encode(openpgp._crc24(data).to_bytes(3, "big")).decode("ascii")
    return ("-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n%s\n=%s\n"
            "-----END PGP PUBLIC KEY BLOCK-----\n" % (body, crc))


def sign(data, created=CREATED, issuer=None, extra_hashed=b""):
    """Отделённая подпись v4 (sha256) над data; issuer — чужой отпечаток для проверки отказа."""
    subpackets = (bytes([5, 2]) + created.to_bytes(4, "big")
                  + bytes([22, 33, 4]) + bytes.fromhex(issuer or fingerprint()) + extra_hashed)
    hashed = b"\x04\x00\x01\x08" + len(subpackets).to_bytes(2, "big") + subpackets
    digest = hashlib.sha256(data + hashed + b"\x04\xff" + len(hashed).to_bytes(4, "big")).digest()
    size = (N.bit_length() + 7) // 8
    payload = openpgp._HASHES[8][1] + digest
    block = b"\x00\x01" + b"\xff" * (size - len(payload) - 3) + b"\x00" + payload
    value = pow(int.from_bytes(block, "big"), D, N)
    body = hashed + b"\x00\x00" + digest[:2] + _mpi(value)
    return _packet(2, body)
