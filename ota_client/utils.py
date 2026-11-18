import hashlib
import hmac
import json


def calc_sha256(path):

    sha = hashlib.sha256()

    with open(path, "rb") as f:

        while True:

            data = f.read(
                1024 * 1024
            )

            if not data:
                break

            sha.update(data)

    return sha.hexdigest()


def verify_sha256(
    path,
    expected
):

    return (
        calc_sha256(path)
        ==
        expected.lower()
    )


# ==========================
# JSON 签名（HMAC-SHA256）
#
# 信任锚：设备出厂预置的密钥文件（config.SIGN_KEY_FILE）。
# 服务器被攻破时，攻击者拿不到密钥就无法伪造
# version.json / manifest.json，sha256/md5 字段才可信。
# 签名方式：对除 "signature" 字段外的整个 JSON 做
# 规范化序列化（sort_keys + 紧凑分隔符）后计算 HMAC。
# ==========================

def _canonical_json(payload):
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False
    ).encode("utf-8")


def sign_json(payload, key):
    """计算 payload（不含 signature 字段）的 HMAC-SHA256 签名"""
    msg = _canonical_json(
        {k: v for k, v in payload.items() if k != "signature"}
    )
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def verify_signed_json(data, key):
    """校验带 signature 字段的 JSON，返回 bool"""
    sig = data.get("signature")
    if not sig:
        return False

    expected = sign_json(data, key)

    return hmac.compare_digest(sig, expected)


def load_sign_key(key_file):
    """读取签名密钥，文件不存在返回 None（调用方据此决定是否强制验签）"""
    try:
        with open(key_file, "rb") as f:
            key = f.read().strip()
            return key or None
    except OSError:
        return None
