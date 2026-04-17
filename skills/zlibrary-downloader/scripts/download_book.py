#!/usr/bin/env python3
"""
Z-Library Book Downloader Script
Downloads books from Z-Library using the unofficial API.

Usage:
    python3 download_book.py --userid <remix_userid> --userkey <remix_userkey> --query "book title" [--output-dir <dir>]
"""

import sys
import os
import argparse
from pathlib import Path

# Import the Zlibrary class from the same directory
sys.path.insert(0, os.path.dirname(__file__))
from Zlibrary import Zlibrary


def download_book(remix_userid, remix_userkey, query, output_dir=".", limit=5):
    """
    Search and download a book from Z-Library.
    
    Args:
        remix_userid: Z-Library user ID
        remix_userkey: Z-Library user key
        query: Search query (book title, author, etc.)
        output_dir: Directory to save downloaded books
        limit: Maximum number of search results to display
    
    Returns:
        dict: Result information including success status and file path
    """
    try:
        # Login to Z-Library
        Z = Zlibrary(remix_userid=remix_userid, remix_userkey=remix_userkey)
        
        if not Z.isLoggedIn():
            return {"success": False, "error": "Failed to login to Z-Library"}
        
        # Check remaining downloads
        downloads_left = Z.getDownloadsLeft()
        print(f"Remaining downloads today: {downloads_left}")
        
        if downloads_left <= 0:
            return {"success": False, "error": "No downloads left for today"}
        
        # Search for books
        print(f"Searching for: {query}")
        results = Z.search(message=query, limit=limit)
        
        if not results or "books" not in results or len(results["books"]) == 0:
            return {"success": False, "error": "No books found"}
        
        # Display search results
        print(f"\nFound {len(results['books'])} results:")
        for i, book in enumerate(results["books"], 1):
            title = book.get("title", "Unknown")
            author = book.get("author", "Unknown")
            year = book.get("year", "N/A")
            extension = book.get("extension", "N/A")
            print(f"{i}. {title} - {author} ({year}) [{extension}]")
        
        # Download the first book
        print(f"\nDownloading: {results['books'][0].get('title', 'Unknown')}")
        filename, filecontent = Z.downloadBook(results["books"][0])
        
        # Create output directory if it doesn't exist
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        # Save the file
        file_path = output_path / filename
        with open(file_path, "wb") as f:
            f.write(filecontent)
        
        print(f"✓ Downloaded successfully: {file_path}")
        
        return {
            "success": True,
            "file_path": str(file_path),
            "filename": filename,
            "book_info": results["books"][0]
        }
        
    except Exception as e:
        return {"success": False, "error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="Download books from Z-Library")
    parser.add_argument("--userid", required=True, help="Z-Library remix_userid")
    parser.add_argument("--userkey", required=True, help="Z-Library remix_userkey")
    parser.add_argument("--query", required=True, help="Search query (book title, author, etc.)")
    parser.add_argument("--output-dir", default=".", help="Output directory for downloaded books")
    parser.add_argument("--limit", type=int, default=5, help="Maximum number of search results")
    
    args = parser.parse_args()
    
    result = download_book(
        remix_userid=args.userid,
        remix_userkey=args.userkey,
        query=args.query,
        output_dir=args.output_dir,
        limit=args.limit
    )
    
    if not result["success"]:
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
