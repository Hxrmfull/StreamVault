# -*- coding: utf-8 -*-
"""Проверка подписи OpenPGP: настоящий выпуск yt-dlp и одноразовый тестовый ключ."""
import base64
import unittest

from streamvault import openpgp
from streamvault.archive.ytdlp_update import YTDLP_KEY, YTDLP_KEY_FINGERPRINT
from tests import pgp_testkey

# Список сумм выпуска yt-dlp 2026.10.21 и его подпись — как они лежат на GitHub.
RELEASE_SUMS = b"""\
b7af77f686e95f7845ce8d09d21957718febb8b48b30555adb9dd2292ad7bf14  yt-dlp
33121d192d706e62f684a06f80918e3f3a8ba52ac3a65213b6951c39c7d9f555  yt-dlp.exe
5f1e419f76e49c996061511ef9eacce50d4aa4959c34480539562e9e1295708d  yt-dlp.tar.gz
1380a9f105302764e6a5d383c2f07238ae242f0f4fbf3414aea00e56619a79b3  yt-dlp_arm64.exe
d8bd33fca9d5b23bd5b9952b3fca36db5c46331933fda9f598a3da14ce2c243f  yt-dlp_linux
dcddc44b6151ccd855a9a3c8fcdb00e897bb872ec9f4908b1b708b16eaaaa114  yt-dlp_linux.zip
e137c2171b0b8ef648e6136e86aa70c65923e93e434f01c93901d64744b190aa  yt-dlp_linux_aarch64
d98a0aeb26b670aac94ee892a03a196f3f2cf0a81c3872186199c0c67ab36bde  yt-dlp_linux_aarch64.zip
cfff2f4e4a7dfc893fbca781195fabe1965e28151c6b08ad906662cb944c9bed  yt-dlp_linux_armv7l.zip
7a6fe71d7d680f1fb0ac18d4949873d77db2859e90017c0f44bd94b23be4ee40  yt-dlp_macos
2b5251788b36fcbc476568b4eea8cb239f0a775759456f2ff9344a437c52f000  yt-dlp_macos.zip
d39487ff5c8eb1254d6f142bea05f495440d87ff529b2df786dcd3b5f66b23a8  yt-dlp_musllinux
66358aac681cf9adc2bea09bdf200d45c6ff59b5084e655f9ea1eb53c9ea3140  yt-dlp_musllinux.zip
105ccea5684c00c71cb391e91342cf70c580aea7c57b03892dbadc4ee2e61dd8  yt-dlp_musllinux_aarch64
246b2f1285cca36c6d949cae4b162ec2bdde304fb100b6e926c298a3bbbd6c29  yt-dlp_musllinux_aarch64.zip
a981b50fff6e437576d1c3c8df0680af572028ca12dff5e49435d9d33c98294b  yt-dlp_win.zip
fa0c7962c94409d5204d501dec95029a32df98bf632b5098a0d6219ea0d822ef  yt-dlp_win_arm64.zip
f1c71e5b651b51161be000c640469d10ea2e0a6ecbb1782fb33a6945c9f145a4  yt-dlp_win_x86.zip
7ce38c77db13db50c64e656b1056a0448e6c38ec0f0ccf28e30dd5271c008850  yt-dlp_x86.exe
"""
RELEASE_SIGNATURE = base64.b64decode(
    "iQIzBAABCgAdFiEErAy75oSNaoc0ZK9OV89lkztadYEFAmrZNHQACgkQV89lkztadYEmHBAAoJRU2Qra6TqQG9Vg"
    "HDQnjMTL9UEXT61TOvOLwXMBq8R16PDSAxrpkGCtgBoa4zJ9JiWrmLx2ACNwyRRyTkqKEkVya4buBQTidnGftCGb"
    "/tK+amgqbtwnLQnz1eoWNJV8FFn+0WLflqV3sNvBCvbKUgMLLnRR6jWfgH1OPcsVltRoEX/5Pb9l/6A+ei6Evl4V"
    "Ypv8spE+RNHPOuHHhIrfInS3WEY9wFHaUIPEdGNn8ZW8J2XAW5D1Q9833b/3GBtllgCJrsTTSBWY9bHQNYhX0XjJ"
    "yUWKTNdLo/xw+zx36wzN2fNaVPmtsru3/i84Fm/PcQE7O1o8UtwMqElKvFByUVzG7I59Rh9wwViCz0I3YzjXAiCt"
    "ROaOBXuGvkUE1PYVdz2Zr7cbH8+ye/FePhqLmKrxYNIqBtI4iPsVit2ud5zjTaH0aL0lDw21uyypSoTttxVfvmjl"
    "uxMinvM7z4MxzQHD0MDyqYMH5M7U2vxZJhjJprNHHMf53aLknWnhiEmn8anCHU+/xbX9A+Qw7tUdFD9aF5ZHr+lv"
    "jF3zLsesTSr/WN5JsjC0LXjjWKCdzZ55YWfhl36amna5Aq9WjGRqTmmls71eoPrfjgVfanDzgvV7fgJSGzWN3KHr"
    "p2I0hAmFovywubBfVQbnV179QrJD2UIceE6An9rE2MsYNVCkfjE=")


class RealReleaseTest(unittest.TestCase):

    def test_built_in_key(self):
        self.assertEqual(openpgp.primary_key(YTDLP_KEY)[0], YTDLP_KEY_FINGERPRINT)

    def test_release_signature(self):
        openpgp.verify(RELEASE_SUMS, RELEASE_SIGNATURE, YTDLP_KEY, YTDLP_KEY_FINGERPRINT)

    def test_changed_sum_is_rejected(self):
        changed = RELEASE_SUMS.replace(b"b7af77f6", b"b7af77f7")
        with self.assertRaises(openpgp.SignatureError):
            openpgp.verify(changed, RELEASE_SIGNATURE, YTDLP_KEY, YTDLP_KEY_FINGERPRINT)

    def test_damaged_signature_is_rejected(self):
        damaged = bytearray(RELEASE_SIGNATURE)
        damaged[-10] ^= 0x01
        with self.assertRaises(openpgp.SignatureError):
            openpgp.verify(RELEASE_SUMS, bytes(damaged), YTDLP_KEY, YTDLP_KEY_FINGERPRINT)

    def test_other_fingerprint_is_rejected(self):
        with self.assertRaises(openpgp.SignatureError):
            openpgp.verify(RELEASE_SUMS, RELEASE_SIGNATURE, YTDLP_KEY, "0" * 40)

    def test_damaged_armor_is_rejected(self):
        broken = YTDLP_KEY.replace("mQINBGP78C4BEAD0", "mQINBGP78C4BEAD1")
        with self.assertRaises(openpgp.SignatureError):
            openpgp.primary_key(broken)


class TestKeyTest(unittest.TestCase):
    """Подписи, которые собираются здесь же, — чтобы проверить каждый отказ по отдельности."""

    def setUp(self):
        self.key = pgp_testkey.armored_key()
        self.fingerprint = pgp_testkey.fingerprint()
        self.data = b"0" * 64 + b"  yt-dlp\n"

    def verify(self, signature, data=None):
        openpgp.verify(self.data if data is None else data, signature, self.key, self.fingerprint)

    def test_valid(self):
        self.verify(pgp_testkey.sign(self.data))

    def test_other_data(self):
        with self.assertRaises(openpgp.SignatureError):
            self.verify(pgp_testkey.sign(self.data), data=self.data + b"x")

    def test_signed_by_other_key(self):
        with self.assertRaisesRegex(openpgp.SignatureError, "другим ключом"):
            self.verify(pgp_testkey.sign(self.data, issuer=YTDLP_KEY_FINGERPRINT))

    def test_test_key_cannot_pass_for_yt_dlp(self):
        # подпись тестовым ключом при встроенном ключе yt-dlp не проходит
        with self.assertRaises(openpgp.SignatureError):
            openpgp.verify(self.data, pgp_testkey.sign(self.data), YTDLP_KEY, YTDLP_KEY_FINGERPRINT)

    def test_expired(self):
        expires = bytes([5, 3]) + (60).to_bytes(4, "big")
        with self.assertRaisesRegex(openpgp.SignatureError, "истёк"):
            self.verify(pgp_testkey.sign(self.data, extra_hashed=expires))

    def test_unknown_critical_subpacket(self):
        critical = bytes([2, 0x80 | 100, 0])     # длина 2: тип и байт данных
        with self.assertRaisesRegex(openpgp.SignatureError, "обязательный"):
            self.verify(pgp_testkey.sign(self.data, extra_hashed=critical))

    def test_armored_signature(self):
        signature = base64.b64encode(pgp_testkey.sign(self.data)).decode("ascii")
        self.verify("-----BEGIN PGP SIGNATURE-----\n\n%s\n-----END PGP SIGNATURE-----\n" % signature)

    def test_garbage(self):
        for garbage in (b"", b"\x00\x01\x02", b"\xc2\x05abc"):
            with self.assertRaises(openpgp.SignatureError):
                self.verify(garbage)


if __name__ == "__main__":
    unittest.main()
