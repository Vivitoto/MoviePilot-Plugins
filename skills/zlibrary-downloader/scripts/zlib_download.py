#!/usr/bin/env python3
"""
Z-Library 下载工具（支持配置文件自动登录）
优先使用 token，token 失效时自动用邮箱密码重新登录并更新 token
"""
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from Zlibrary import Zlibrary
from zlib_config import get_credentials, update_token, has_credentials


def get_zlib_client():
    """
    获取已登录的 Z-Library 客户端
    优先级：token > 邮箱密码
    如果 token 失效，自动用邮箱密码登录并更新 token
    """
    creds = get_credentials()
    
    # 先尝试用 token 登录
    if creds.get("remix_userid") and creds.get("remix_userkey"):
        Z = Zlibrary(
            remix_userid=creds["remix_userid"],
            remix_userkey=creds["remix_userkey"]
        )
        if Z.isLoggedIn():
            return Z
        print("Token 已失效，尝试用邮箱密码重新登录...")
    
    # Token 无效或不存在，用邮箱密码登录
    if not (creds.get("email") and creds.get("password")):
        raise ValueError(
            "未配置凭证。请运行:\n"
            "  python3 zlib_config.py --set-email 'your@email.com' --set-password 'your_password'\n"
            "或在 ~/.config/zlib_downloader.json 中配置"
        )
    
    Z = Zlibrary(email=creds["email"], password=creds["password"])
    
    if not Z.isLoggedIn():
        raise ValueError("登录失败，请检查邮箱密码")
    
    # 登录成功，保存 token 供下次使用
    # 从 Z 对象获取 token
    if hasattr(Z, '_Zlibrary__remix_userid') and hasattr(Z, '_Zlibrary__remix_userkey'):
        update_token(
            remix_userid=Z._Zlibrary__remix_userid,
            remix_userkey=Z._Zlibrary__remix_userkey
        )
        print("Token 已自动保存")
    
    return Z


def download_book(query, output_dir=".", extensions=None, limit=5):
    """
    搜索并下载书籍
    
    Args:
        query: 搜索关键词（书名、作者等）
        output_dir: 保存目录
        extensions: 格式过滤，如 ["epub", "pdf"]
        limit: 显示结果数量
    
    Returns:
        dict: 包含 success, file_path, filename, book_info
    """
    Z = get_zlib_client()
    
    # 检查剩余下载次数
    downloads_left = Z.getDownloadsLeft()
    print(f"今日剩余下载次数: {downloads_left}")
    
    if downloads_left <= 0:
        return {"success": False, "error": "今日下载次数已用完"}
    
    # 搜索
    print(f"搜索: {query}")
    search_params = {"message": query, "limit": limit}
    if extensions:
        search_params["extensions"] = extensions
    
    results = Z.search(**search_params)
    
    if not results or "books" not in results or not results["books"]:
        return {"success": False, "error": "未找到书籍"}
    
    # 显示结果
    print(f"\n找到 {len(results['books'])} 个结果:")
    for i, book in enumerate(results["books"][:limit], 1):
        title = book.get("name", book.get("title", "未知"))
        author = book.get("author", "未知")
        ext = book.get("extension", "N/A")
        print(f"{i}. {title} - {author} [{ext}]")
    
    # 下载第一本
    book = results["books"][0]
    title = book.get("name", book.get("title", "未知"))
    print(f"\n下载: {title}...")
    
    filename, content = Z.downloadBook(book)
    
    # 保存
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    file_path = output_path / filename
    
    with open(file_path, "wb") as f:
        f.write(content)
    
    print(f"✓ 已保存: {file_path}")
    print(f"剩余下载次数: {Z.getDownloadsLeft()}")
    
    return {
        "success": True,
        "file_path": str(file_path),
        "filename": filename,
        "book_info": book
    }


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Z-Library 下载工具")
    parser.add_argument("query", help="搜索关键词（书名、作者）")
    parser.add_argument("--output-dir", "-o", default=".", help="保存目录")
    parser.add_argument("--ext", action="append", help="格式过滤（可多次使用，如 --ext epub --ext pdf）")
    parser.add_argument("--limit", "-l", type=int, default=5, help="显示结果数量")
    
    args = parser.parse_args()
    
    if not has_credentials():
        print("错误: 未配置 Z-Library 凭证")
        print("请先运行: python3 zlib_config.py --set-email 'xxx' --set-password 'xxx'")
        sys.exit(1)
    
    try:
        result = download_book(
            query=args.query,
            output_dir=args.output_dir,
            extensions=args.ext,
            limit=args.limit
        )
        if not result["success"]:
            print(f"错误: {result['error']}")
            sys.exit(1)
    except Exception as e:
        print(f"错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
