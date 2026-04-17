#!/usr/bin/env python3
"""
Z-Library 配置文件管理
支持存储和读取邮箱密码、token 凭证
"""
import os
import json
from pathlib import Path

CONFIG_DIR = Path.home() / ".config"
CONFIG_FILE = CONFIG_DIR / "zlib_downloader.json"

def get_config():
    """读取配置文件"""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}

def save_config(config):
    """保存配置文件"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
    return True

def set_credentials(email=None, password=None, remix_userid=None, remix_userkey=None):
    """设置凭证（增量更新）"""
    config = get_config()
    
    if email:
        config["email"] = email
    if password:
        config["password"] = password
    if remix_userid:
        config["remix_userid"] = str(remix_userid)
    if remix_userkey:
        config["remix_userkey"] = remix_userkey
    
    save_config(config)
    return config

def get_credentials():
    """获取凭证，返回 dict 包含所有可用凭证"""
    config = get_config()
    return {
        "email": config.get("email"),
        "password": config.get("password"),
        "remix_userid": config.get("remix_userid"),
        "remix_userkey": config.get("remix_userkey"),
    }

def has_credentials():
    """检查是否有任何凭证"""
    creds = get_credentials()
    # 有邮箱密码 或 有 token 都算有凭证
    has_email_pass = bool(creds.get("email") and creds.get("password"))
    has_token = bool(creds.get("remix_userid") and creds.get("remix_userkey"))
    return has_email_pass or has_token

def update_token(remix_userid, remix_userkey):
    """更新 token（用于自动保存登录后的 token）"""
    return set_credentials(remix_userid=remix_userid, remix_userkey=remix_userkey)

def clear_credentials():
    """清除所有凭证"""
    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()
    return True


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Z-Library 配置管理")
    parser.add_argument("--set-email", help="设置邮箱")
    parser.add_argument("--set-password", help="设置密码")
    parser.add_argument("--set-userid", help="设置 remix_userid")
    parser.add_argument("--set-userkey", help="设置 remix_userkey")
    parser.add_argument("--show", action="store_true", help="显示当前配置（脱敏）")
    parser.add_argument("--clear", action="store_true", help="清除所有凭证")
    
    args = parser.parse_args()
    
    if args.clear:
        clear_credentials()
        print("已清除所有凭证")
    elif args.set_email or args.set_password or args.set_userid or args.set_userkey:
        cfg = set_credentials(
            email=args.set_email,
            password=args.set_password,
            remix_userid=args.set_userid,
            remix_userkey=args.set_userkey
        )
        print("凭证已保存")
    elif args.show:
        creds = get_credentials()
        print(f"邮箱: {creds.get('email', '未设置')}")
        print(f"密码: {'*' * 8 if creds.get('password') else '未设置'}")
        print(f"remix_userid: {creds.get('remix_userid', '未设置')}")
        print(f"remix_userkey: {'*' * 8 if creds.get('remix_userkey') else '未设置'}")
    else:
        parser.print_help()
