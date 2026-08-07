"""
WeChat 4.1.x 密钥自动管理脚本

流程:
1. 检查缓存密钥是否存在且有效
2. 无效则调用 chatlog-keeper 主动模式提取
3. PBKDF2 派生所有数据库的 per-DB 密钥
4. 写入 wechat-decrypt 的 all_keys.json

用法:
    source .venv/Scripts/activate
    python tools/update_keys.py
"""

import os, sys, json, hashlib, hmac, struct

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
RESERVE_SZ = 80
IV_SZ = 16
HMAC_SZ = 64

# --- 配置 ---
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WECHAT_DECRYPT_DIR = os.path.join(PROJECT_ROOT, "wechat-decrypt")
CHATLOG_KEEPER_DIR = os.path.join(PROJECT_ROOT, "chatlog-keeper")
CACHED_KEY_PATH = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "chatlog-keeper", "data", "secrets", "wechat_db.key"
)
DB_DIR = r"D:\WeChat\Record\xwechat_files\wxid_l07mzpzpbnv522_156b\db_storage"
ALL_KEYS_FILE = os.path.join(WECHAT_DECRYPT_DIR, "all_keys.json")


def load_master_key():
    """加载缓存的 master key，如无效则重新提取"""
    if os.path.exists(CACHED_KEY_PATH):
        with open(CACHED_KEY_PATH) as f:
            key_hex = f.read().strip()
        if len(key_hex) == 64:
            return bytes.fromhex(key_hex)

    print("[!] 密钥缓存不存在或无效，开始主动提取...")
    print("[!] 需要管理员权限，微信可能会重新登录")
    import subprocess
    result = subprocess.run(
        [
            sys.executable, "-m", "chatlog_keeper.cli", "extract-key",
            "--source", "wechat",
            "--method", "active",
            "--data-root", os.path.dirname(DB_DIR)
        ],
        cwd=CHATLOG_KEEPER_DIR,
        capture_output=True, text=True
    )
    print(result.stdout)
    if result.returncode != 0:
        print("[FATAL] 密钥提取失败:", result.stderr)
        sys.exit(1)

    if os.path.exists(CACHED_KEY_PATH):
        with open(CACHED_KEY_PATH) as f:
            key_hex = f.read().strip()
        return bytes.fromhex(key_hex)

    print("[FATAL] 提取后仍找不到密钥文件")
    sys.exit(1)


def derive_all_keys(master_key):
    """PBKDF2 派生所有数据库的密钥并写入 all_keys.json"""
    results = {}
    total_ok = 0
    total_fail = 0

    for root, dirs, files in os.walk(DB_DIR):
        for f in files:
            if not f.endswith('.db') or f.endswith('-wal') or f.endswith('-shm'):
                continue
            path = os.path.join(root, f)
            rel = os.path.relpath(path, DB_DIR)
            sz = os.path.getsize(path)
            if sz < PAGE_SZ:
                continue

            with open(path, 'rb') as fh:
                page1 = fh.read(PAGE_SZ)

            salt = page1[:SALT_SZ]
            enc_key = hashlib.pbkdf2_hmac('sha512', master_key, salt, 256000, dklen=KEY_SZ)

            # HMAC 验证
            mac_salt = bytes(b ^ 0x3a for b in salt)
            mac_key = hashlib.pbkdf2_hmac('sha512', enc_key, mac_salt, 2, dklen=KEY_SZ)
            p1_hmac_data = page1[SALT_SZ : PAGE_SZ - RESERVE_SZ + IV_SZ]
            p1_stored_hmac = page1[PAGE_SZ - HMAC_SZ : PAGE_SZ]
            hm = hmac.new(mac_key, p1_hmac_data, hashlib.sha512)
            hm.update(struct.pack('<I', 1))

            if hm.digest() == p1_stored_hmac:
                results[rel] = {
                    "enc_key": enc_key.hex(),
                    "salt": salt.hex(),
                    "size_mb": round(sz / 1024 / 1024, 1)
                }
                total_ok += 1
            else:
                print(f"  [FAIL] HMAC mismatch: {rel}")
                total_fail += 1

    os.makedirs(os.path.dirname(ALL_KEYS_FILE), exist_ok=True)
    with open(ALL_KEYS_FILE, 'w') as f:
        json.dump(results, f, indent=2)

    print(f"[OK] {total_ok} 个密钥通过, {total_fail} 失败")
    print(f"[OK] 保存到: {ALL_KEYS_FILE}")
    return results


def main():
    print("=" * 60)
    print("  WeChat 4.1.x 密钥管理器")
    print("=" * 60)

    # Step 1: 获取 master key
    master_key = load_master_key()
    print(f"[+] Master key: {master_key.hex()[:16]}... (32 bytes)")

    # Step 2: 派生所有 per-DB 密钥
    derive_all_keys(master_key)

    print("\n[OK] 密钥就绪，可以运行:")
    print(f"  cd {WECHAT_DECRYPT_DIR}")
    print("  python decrypt_db.py")


if __name__ == '__main__':
    main()
