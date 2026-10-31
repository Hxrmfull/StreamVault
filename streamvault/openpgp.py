# -*- coding: utf-8 -*-
"""
Проверка подписи OpenPGP — ровно столько, сколько нужно для обновления
yt-dlp: отделённая подпись версии 4 ключом RSA над двоичным файлом
(так подписан список сумм SHA2-256SUMS в каждом выпуске yt-dlp).

Своя небольшая реализация вместо gpg: в Windows его обычно нет, а тащить
в программу целый gpg ради одной проверки незачем. Принимается подпись
только того основного ключа, отпечаток которого передан явно; подключи,
отзыв ключа и прочие возможности OpenPGP не поддерживаются — для них
подпись просто не пройдёт.
"""

import base64
import hashlib
import hmac
import re
import time

from .i18n import tr


class SignatureError(Exception):
    """Подпись не прошла проверку. Текст — для журнала."""


# алгоритм хеша OpenPGP → (имя в hashlib, префикс DigestInfo для PKCS#1 v1.5)
_HASHES = {
    8: ("sha256", bytes.fromhex("3031300d060960864801650304020105000420")),
    9: ("sha384", bytes.fromhex("3041300d060960864801650304020205000430")),
    10: ("sha512", bytes.fromhex("3051300d060960864801650304020305000440")),
    11: ("sha224", bytes.fromhex("302d300d06096086480165030402040500041c")),
}
_RSA = (1, 3)                   # RSA и RSA «только подпись»
_BINARY_DOCUMENT = 0x00
_TAG_SIGNATURE, _TAG_PUBLIC_KEY = 2, 6
# подпакеты подписи, которые здесь понимают
_SUB_CREATED, _SUB_EXPIRES, _SUB_ISSUER, _SUB_ISSUER_FPR = 2, 3, 16, 33


def _crc24(data):
    crc = 0xB704CE
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def dearmor(text):
    """Текстовую «броню» (-----BEGIN PGP …-----) — в байты, с проверкой CRC24."""
    lines = text.replace("\r\n", "\n").strip().split("\n")
    if not lines or not re.fullmatch(r"-----BEGIN PGP [A-Z ]+-----", lines[0].strip()):
        raise SignatureError(tr("нет заголовка брони OpenPGP"))
    body, checksum, index = [], None, 1
    while index < len(lines) and lines[index].strip():   # заголовки до пустой строки
        index += 1
    for line in lines[index + 1:]:
        line = line.strip()
        if line.startswith("-----END PGP"):
            break
        if line.startswith("="):
            checksum = line[1:]
        elif line:
            body.append(line)
    try:
        data = base64.b64decode("".join(body), validate=True)
    except ValueError:
        raise SignatureError(tr("броня OpenPGP испорчена"))
    if checksum is not None and int.from_bytes(base64.b64decode(checksum), "big") != _crc24(data):
        raise SignatureError(tr("контрольная сумма брони OpenPGP не сходится"))
    return data


def _packets(data):
    """Пакеты OpenPGP: (тег, тело)."""
    position = 0
    while position < len(data):
        header = data[position]
        if not header & 0x80:
            raise SignatureError(tr("не пакет OpenPGP"))
        if header & 0x40:                       # новый формат
            tag = header & 0x3F
            first = data[position + 1]
            if first < 192:
                length, position = first, position + 2
            elif first < 224:
                length = ((first - 192) << 8) + data[position + 2] + 192
                position += 3
            elif first == 255:
                length = int.from_bytes(data[position + 2:position + 6], "big")
                position += 6
            else:
                raise SignatureError(tr("частичная длина пакета не поддерживается"))
        else:                                   # старый формат
            tag = (header >> 2) & 0x0F
            size = {0: 1, 1: 2, 2: 4}.get(header & 0x03)
            if size is None:
                raise SignatureError(tr("пакет неопределённой длины не поддерживается"))
            length = int.from_bytes(data[position + 1:position + 1 + size], "big")
            position += 1 + size
        body = data[position:position + length]
        if len(body) != length:
            raise SignatureError(tr("пакет OpenPGP обрезан"))
        position += length
        yield tag, body


def _mpi(data, position):
    bits = int.from_bytes(data[position:position + 2], "big")
    end = position + 2 + (bits + 7) // 8
    if end > len(data):
        raise SignatureError(tr("число в пакете OpenPGP обрезано"))
    return int.from_bytes(data[position + 2:end], "big"), end


def primary_key(armored):
    """Основной ключ RSA из брони: (отпечаток, n, e)."""
    for tag, body in _packets(dearmor(armored)):
        if tag != _TAG_PUBLIC_KEY:
            continue
        if body[0] != 4 or body[5] not in _RSA:
            raise SignatureError(tr("поддерживаются только ключи RSA версии 4"))
        n, position = _mpi(body, 6)
        e, _ = _mpi(body, position)
        fingerprint = hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body).hexdigest().upper()
        return fingerprint, n, e
    raise SignatureError(tr("в броне нет открытого ключа"))


def _subpackets(data):
    position = 0
    while position < len(data):
        first = data[position]
        if first < 192:
            length, position = first, position + 1
        elif first < 255:
            length = ((first - 192) << 8) + data[position + 1] + 192
            position += 2
        else:
            length = int.from_bytes(data[position + 1:position + 5], "big")
            position += 5
        if length < 1 or position + length > len(data):
            raise SignatureError(tr("подпакет подписи испорчен"))
        kind = data[position]
        yield kind & 0x7F, bool(kind & 0x80), data[position + 1:position + length]
        position += length


def verify(data, signature, armored_key, fingerprint, now=None):
    """
    Проверяет отделённую подпись signature (байты или броня) над data
    ключом armored_key, отпечаток основного ключа которого обязан быть
    fingerprint. Не сошлось — SignatureError.
    """
    key_fingerprint, n, e = primary_key(armored_key)
    wanted = re.sub(r"\s", "", fingerprint).upper()
    if key_fingerprint != wanted:
        raise SignatureError(tr("встроенный ключ не тот: {fingerprint}", fingerprint=key_fingerprint))
    if isinstance(signature, str):
        signature = dearmor(signature)
    packets = [body for tag, body in _packets(signature) if tag == _TAG_SIGNATURE]
    if len(packets) != 1:
        raise SignatureError(tr("ожидалась ровно одна подпись"))
    body = packets[0]
    if len(body) < 6 or body[0] != 4:
        raise SignatureError(tr("поддерживаются только подписи версии 4"))
    kind, algorithm, hash_id = body[1], body[2], body[3]
    if kind != _BINARY_DOCUMENT:
        raise SignatureError(tr("это не подпись двоичного файла"))
    if algorithm not in _RSA:
        raise SignatureError(tr("поддерживаются только подписи RSA"))
    if hash_id not in _HASHES:
        raise SignatureError(tr("неизвестный алгоритм хеша: {id}", id=hash_id))

    hashed_length = int.from_bytes(body[4:6], "big")
    hashed_end = 6 + hashed_length
    unhashed_length = int.from_bytes(body[hashed_end:hashed_end + 2], "big")
    unhashed_end = hashed_end + 2 + unhashed_length
    if unhashed_end + 2 > len(body):
        raise SignatureError(tr("подпись обрезана"))

    issuers, created, expires = set(), None, None
    for kind_, critical, value in _subpackets(body[6:hashed_end]):
        if kind_ == _SUB_CREATED:
            created = int.from_bytes(value, "big")
        elif kind_ == _SUB_EXPIRES:
            expires = int.from_bytes(value, "big")
        elif kind_ == _SUB_ISSUER:
            issuers.add(value.hex().upper())
        elif kind_ == _SUB_ISSUER_FPR:
            issuers.add(value[1:].hex().upper())
        elif critical:
            # обязательное условие, которое здесь не понимают, — подпись не принимается
            raise SignatureError(tr("неизвестный обязательный подпакет подписи: {id}", id=kind_))
    for kind_, _, value in _subpackets(body[hashed_end + 2:unhashed_end]):
        # нехешированная часть подписью не защищена — только подсказка, чей ключ
        if kind_ == _SUB_ISSUER:
            issuers.add(value.hex().upper())
        elif kind_ == _SUB_ISSUER_FPR:
            issuers.add(value[1:].hex().upper())
    if issuers and not any(wanted.endswith(issuer) for issuer in issuers):
        raise SignatureError(tr("подписано другим ключом: {keys}", keys=", ".join(sorted(issuers))))
    if created is None:
        raise SignatureError(tr("в подписи нет времени создания"))
    if expires and created + expires < (now if now is not None else time.time()):
        raise SignatureError(tr("срок действия подписи истёк"))

    name, digest_info = _HASHES[hash_id]
    hashed = body[:hashed_end]
    digest = hashlib.new(name, data + hashed + b"\x04\xff" + len(hashed).to_bytes(4, "big")).digest()
    if body[unhashed_end:unhashed_end + 2] != digest[:2]:
        raise SignatureError(tr("подпись не от этих данных"))
    value, _ = _mpi(body, unhashed_end + 2)
    size = (n.bit_length() + 7) // 8
    if value >= n:
        raise SignatureError(tr("подпись испорчена"))
    block = pow(value, e, n).to_bytes(size, "big")
    payload = digest_info + digest
    padding = size - len(payload) - 3
    if padding < 8:
        raise SignatureError(tr("ключ слишком короткий"))
    expected = b"\x00\x01" + b"\xff" * padding + b"\x00" + payload
    if not hmac.compare_digest(block, expected):
        raise SignatureError(tr("подпись не сходится"))
