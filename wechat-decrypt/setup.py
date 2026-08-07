"""
一键初始化：检测状态 → 按需提密钥 → 按需解密

可以被 agent 直接调用，也可以手动运行。
只会执行缺失或失效的步骤，不会重复操作。

用法:
    python setup.py           # 交互式（手动）
    python setup.py --json    # JSON 输出（供 agent 调用）
"""

import os, sys, json, hashlib, hmac as hmac_mod, struct, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_SZ = 4096
SALT_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = 80
KEY_SZ = 32


def info(msg):
    print(f"  [*] {msg}", flush=True)


def ok(msg):
    print(f"  [OK] {msg}", flush=True)


def warn(msg):
    print(f"  [!] {msg}", flush=True)


def fail(msg):
    print(f"  [✗] {msg}", flush=True)


# ---- 步骤 0: 加载配置 ----

def ensure_config():
    """确保配置文件存在。如果不存在，生成模板并退出。"""
    from config import CONFIG_FILE, CONFIG_DIR, _DEFAULT

    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        ok(f"配置文件已存在: {CONFIG_FILE}")
        return cfg

    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(_DEFAULT, f, indent=4, ensure_ascii=False)
    warn(f"已生成配置模板: {CONFIG_FILE}")
    warn("请编辑此文件，填入 db_dir 和 master_key 后重新运行")
    return None


# ---- 步骤 1: 提取密钥 ----

def verify_key_for_db(enc_key_bytes, page1):
    """验证 enc_key 是否能解密 page 1（HMAC 校验）"""
    salt = page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3a for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key_bytes, mac_salt, 2, dklen=KEY_SZ)
    hmac_data = page1[SALT_SZ: PAGE_SZ - RESERVE_SZ + 16]
    stored_hmac = page1[PAGE_SZ - HMAC_SZ: PAGE_SZ]
    h = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    h.update(struct.pack('<I', 1))
    return h.digest() == stored_hmac


def keys_are_valid(cfg):
    """检查已有的 all_keys.json 是否仍然有效"""
    keys_file = _resolve_path(cfg.get("keys_file", ""))
    if not keys_file or not os.path.exists(keys_file):
        return False

    try:
        with open(keys_file) as f:
            keys = json.load(f)
    except Exception:
        return False

    if not keys:
        return False

    db_dir = cfg.get("db_dir", "")
    if not db_dir or not os.path.isdir(db_dir):
        return False

    # 随机抽查一个 DB 验证
    for rel_key, info in keys.items():
        db_path = os.path.join(db_dir, rel_key)
        if not os.path.exists(db_path):
            continue
        try:
            with open(db_path, "rb") as fh:
                page1 = fh.read(PAGE_SZ)
            enc_key = bytes.fromhex(info["enc_key"])
            if verify_key_for_db(enc_key, page1):
                return True
        except Exception:
            continue

    return False


def extract_keys(cfg):
    """尝试提取密钥：先内存提取，失败再密码派生"""
    keys_file = _resolve_path(cfg.get("keys_file", ""))

    # 检查已有密钥是否有效
    if keys_are_valid(cfg):
        ok("密钥文件有效，跳过提取")
        return keys_file

    warn("密钥缺失或失效，需要重新提取...")

    # 方式 1: 内存提取（微信需在运行）
    info("尝试从微信进程内存提取密钥...")
    find_keys = os.path.join(HERE, "find_all_keys.py")
    if os.path.exists(find_keys):
        try:
            r = subprocess.run(
                [sys.executable, find_keys],
                capture_output=True, text=True, timeout=120,
                cwd=HERE
            )
            if r.returncode == 0 and os.path.exists(keys_file):
                ok("内存提取成功")
                return keys_file
            warn(f"内存提取失败: {r.stderr.strip()[:200] if r.stderr else '未知错误'}")
        except subprocess.TimeoutExpired:
            warn("内存提取超时（微信是否在运行？）")
        except Exception as e:
            warn(f"内存提取异常: {e}")
    else:
        warn("find_all_keys.py 不存在")

    # 方式 2: 密码派生
    master_key_hex = cfg.get("master_key", "")
    if master_key_hex:
        info("尝试从 master_key 派生密钥...")
        derive_keys = os.path.join(HERE, "derive_keys.py")
        if os.path.exists(derive_keys):
            try:
                r = subprocess.run(
                    [sys.executable, derive_keys],
                    capture_output=True, text=True, timeout=300,
                    cwd=HERE
                )
                if r.returncode == 0 and os.path.exists(keys_file):
                    ok("密码派生成功")
                    return keys_file
                warn(f"密码派生失败: {r.stderr.strip()[:200] if r.stderr else '未知错误'}")
            except Exception as e:
                warn(f"密码派生异常: {e}")
    else:
        warn("未配置 master_key，跳过密码派生")

    fail("所有密钥提取方式均失败")
    return None


# ---- 步骤 2: 解密数据库 ----

def databases_decrypted(cfg):
    """检查是否已有解密后的数据库"""
    decrypted_dir = _resolve_path(cfg.get("decrypted_dir", ""))
    if not decrypted_dir or not os.path.isdir(decrypted_dir):
        return False
    # 至少有一个 .db 文件
    for root, dirs, files in os.walk(decrypted_dir):
        for f in files:
            if f.endswith('.db'):
                return True
    return False


def decrypt_databases(cfg):
    """解密所有数据库"""
    decrypted_dir = _resolve_path(cfg.get("decrypted_dir", ""))

    if databases_decrypted(cfg):
        ok(f"解密数据库已存在: {decrypted_dir}")
        return True

    warn("未找到解密数据库，开始解密...")

    decrypt_db = os.path.join(HERE, "decrypt_db.py")
    if not os.path.exists(decrypt_db):
        fail("decrypt_db.py 不存在")
        return False

    try:
        r = subprocess.run(
            [sys.executable, decrypt_db],
            capture_output=True, text=True, timeout=600,
            cwd=HERE
        )
        if r.returncode == 0:
            ok("数据库解密完成")
            return True
        fail(f"解密失败: {r.stderr.strip()[:300] if r.stderr else '未知错误'}")
    except subprocess.TimeoutExpired:
        fail("解密超时（数据库可能很大）")
    except Exception as e:
        fail(f"解密异常: {e}")

    return False


# ---- 辅助 ----

def _resolve_path(p):
    """展开 ~ 和相对路径"""
    if not p:
        return ""
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = os.path.join(HERE, p)
    return p


# ---- 主入口 ----

def setup(json_output=False):
    """执行完整初始化流程，返回状态码和结果字典"""
    result = {
        "success": False,
        "config": None,
        "keys_file": None,
        "decrypted": False,
        "errors": [],
    }

    # Step 0: Config
    cfg = ensure_config()
    if cfg is None:
        result["errors"].append("配置模板已生成，请编辑后重试")
        if json_output:
            return result
        sys.exit(1)
    result["config"] = cfg.get("db_dir", "")

    # 验证必填字段
    if not cfg.get("db_dir") or "your_wxid" in cfg.get("db_dir", ""):
        result["errors"].append("请先配置 db_dir（微信数据库路径）")
        if json_output:
            return result
        fail("请先编辑配置文件中的 db_dir 路径")
        sys.exit(1)

    # Step 1: Extract keys
    keys_file = extract_keys(cfg)
    if keys_file is None:
        result["errors"].append("密钥提取失败")
        if json_output:
            return result
        sys.exit(1)
    result["keys_file"] = keys_file

    # Step 2: Decrypt databases
    decrypted = decrypt_databases(cfg)
    result["decrypted"] = decrypted

    result["success"] = True
    return result


def main():
    json_mode = "--json" in sys.argv

    print("=" * 50, flush=True)
    print("  LearnLove 微信解密初始化", flush=True)
    print("=" * 50, flush=True)

    result = setup(json_output=json_mode)

    if json_mode:
        print("\n" + json.dumps(result, ensure_ascii=False, indent=2))
    elif result["success"]:
        print("", flush=True)
        ok("全部就绪！可以启动 agent 了。")
    else:
        print("", flush=True)
        for e in result["errors"]:
            fail(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
