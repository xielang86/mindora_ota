#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import hashlib
import hmac
import json
from pathlib import Path
from datetime import datetime

# 输出文件名
OUTPUT = "manifest.json"


def calc_md5(filename):
    """计算文件MD5"""
    md5 = hashlib.md5()

    with open(filename, "rb") as f:
        while True:
            data = f.read(1024 * 1024)
            if not data:
                break
            md5.update(data)

    return md5.hexdigest()


# ==========================
# 签名（HMAC-SHA256）
#
# 与设备端 ota/utils.py 的算法保持一致：
# 对除 signature 字段外的 JSON 做规范化序列化后计算 HMAC。
# 密钥文件由发布机保管，设备出厂预置同一把密钥。
# ==========================

def sign_payload(payload, key):
    msg = json.dumps(
        {k: v for k, v in payload.items() if k != "signature"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False
    ).encode("utf-8")

    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def load_key(key_file):
    with open(key_file, "rb") as f:
        key = f.read().strip()

    if not key:
        raise ValueError(f"sign key file is empty: {key_file}")

    return key


def build_manifest(resource_dir, version, sign_key_file=None):
    resource_root = Path(resource_dir)

    if not resource_root.exists():
        raise FileNotFoundError(f"Resource directory not found: {resource_root}")

    key = load_key(sign_key_file) if sign_key_file else None

    files = []

    for file in sorted(resource_root.rglob("*")):
        if not file.is_file():
            continue

        # 不把 manifest 自己加入
        if file.name == OUTPUT:
            continue

        rel_path = file.relative_to(resource_root).as_posix()

        files.append({
            "path": rel_path,
            "md5": calc_md5(file),
            "size": file.stat().st_size
        })

    manifest = {
        "version": version,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "files": files
    }

    if key:
        manifest["signature"] = sign_payload(manifest, key)

    # 生成 manifest.json
    output_file = resource_root / OUTPUT
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=4, ensure_ascii=False)

    print(f"Generate : {output_file}")
    print(f"Version  : {version}")
    print(f"Files    : {len(files)}")
    print(f"Signed   : {bool(key)}")

def main():
    parser = argparse.ArgumentParser(description="Generate resource manifest.")

    parser.add_argument(
        "resource_dir",
        help="Resource directory, e.g. ./resources"
    )

    parser.add_argument(
        "version",
        help="Resource version, e.g. 1.0.3"
    )

    parser.add_argument(
        "--sign-key-file",
        help="HMAC key file for signing manifest.json only",
        default=None
    )

    args = parser.parse_args()

    build_manifest(args.resource_dir, args.version, args.sign_key_file)


if __name__ == "__main__":
    main()
