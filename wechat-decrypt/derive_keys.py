"""
Derive per-DB encryption keys from WeChat 4.1.x master password
PBKDF2-HMAC-SHA512(password, salt, 256000 iterations, 32 bytes)
"""
import hashlib, hmac, struct, os, json, sys
from Crypto.Cipher import AES

from config import load_config

PAGE_SZ = 4096
SALT_SZ = 16
IV_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = 80
KEY_SZ = 32

_cfg = load_config()
DB_DIR = _cfg["db_dir"]
OUT_FILE = _cfg["keys_file"]
master_key_hex = _cfg.get("master_key", "")
if not master_key_hex:
    print("[ERROR] 缺少 master_key，请在 ~/.learnlove_data/.wechat-decrypt/config.json 中配置")
    sys.exit(1)
master_key = bytes.fromhex(master_key_hex)

results = {}

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

        # PBKDF2 derive enc_key (WeChat 4.1.x password mode)
        enc_key = hashlib.pbkdf2_hmac('sha512', master_key, salt, 256000, dklen=KEY_SZ)

        # Verify via HMAC
        mac_salt = bytes(b ^ 0x3a for b in salt)
        mac_key = hashlib.pbkdf2_hmac('sha512', enc_key, mac_salt, 2, dklen=KEY_SZ)
        p1_hmac_data = page1[SALT_SZ : PAGE_SZ - RESERVE_SZ + IV_SZ]
        p1_stored_hmac = page1[PAGE_SZ - HMAC_SZ : PAGE_SZ]
        hm = hmac.new(mac_key, p1_hmac_data, hashlib.sha512)
        hm.update(struct.pack('<I', 1))

        if hm.digest() == p1_stored_hmac:
            print(f'HMAC OK: {rel}')
            results[rel] = {
                'enc_key': enc_key.hex(),
                'salt': salt.hex(),
                'size_mb': round(sz / 1024 / 1024, 1)
            }
        else:
            print(f'HMAC FAIL: {rel}')

with open(OUT_FILE, 'w') as f:
    json.dump(results, f, indent=2)

print(f'\nSaved {len(results)} keys to {OUT_FILE}')
