"""
配置加载器 - 从 ~/.learnlove_data/.wechat-decrypt/config.json 读取配置
首次运行时自动生成模板
"""
import json
import os
import sys

# 配置文件放在用户数据目录下，避免隐私数据进入 git 仓库
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".learnlove_data", ".wechat-decrypt")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

_DEFAULT = {
    "db_dir": r"D:\xwechat_files\your_wxid\db_storage",
    "keys_file": "all_keys.json",
    "decrypted_dir": "decrypted",
    "decoded_image_dir": "decoded_images",
    "wechat_process": "Weixin.exe",
    "master_key": "",
}


def load_config():
    if not os.path.exists(CONFIG_FILE):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(_DEFAULT, f, indent=4)
        print(f"[!] 已生成配置文件: {CONFIG_FILE}")
        print("    请修改其中的路径后重新运行")
        sys.exit(1)

    with open(CONFIG_FILE) as f:
        cfg = json.load(f)

    # 将相对路径转为绝对路径（相对于配置目录）
    for key in ("keys_file", "decrypted_dir", "decoded_image_dir"):
        if key in cfg and not os.path.isabs(cfg[key]):
            cfg[key] = os.path.join(CONFIG_DIR, cfg[key])

    # 自动推导微信数据根目录（db_dir 的上级目录）
    # db_dir 格式: D:\xwechat_files\<wxid>\db_storage
    # base_dir 格式: D:\xwechat_files\<wxid>
    db_dir = cfg.get("db_dir", "")
    if db_dir and os.path.basename(db_dir) == "db_storage":
        cfg["wechat_base_dir"] = os.path.dirname(db_dir)
    else:
        cfg["wechat_base_dir"] = db_dir

    return cfg


def save_config(cfg):
    """保存配置，自动去除运行时派生的字段"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    clean = {k: v for k, v in cfg.items() if not k.startswith("wechat_base_dir")}
    with open(CONFIG_FILE, "w") as f:
        json.dump(clean, f, indent=4, ensure_ascii=False)
    print(f"[+] 配置已保存到: {CONFIG_FILE}", flush=True)
