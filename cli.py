# -*- coding: utf-8 -*-
"""
Command-line interface entry point – supports Termux and non-GUI environments
"""

import argparse
import sys
import os
from typing import Optional

# Ensure that project modules can be imported.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from platform_utils import detect_platform, get_feature_status_report
from locales import t


def format_table(headers: list, rows: list, col_widths: Optional[list] = None) -> str:
    """
    Format a text-based table

    Args:
        headers: A list of table headers
        rows: A list of data rows
        col_widths: A list of column widths (optional)
    
    Returns:
        A formatted table string
    """
    if not rows:
        return "No data"
    
    # 计算列宽
    if col_widths is None:
        col_widths = []
        for i, header in enumerate(headers):
            max_width = len(str(header))
            for row in rows:
                if i < len(row):
                    max_width = max(max_width, len(str(row[i])))
            col_widths.append(min(max_width, 40))  # 最大宽度 40
    
    # 格式化表头
    header_line = " | ".join(
        str(h).ljust(col_widths[i])[:col_widths[i]] 
        for i, h in enumerate(headers)
    )
    separator = "-+-".join("-" * w for w in col_widths)
    
    # 格式化数据行
    data_lines = []
    for row in rows:
        line = " | ".join(
            str(row[i] if i < len(row) else "").ljust(col_widths[i])[:col_widths[i]]
            for i in range(len(headers))
        )
        data_lines.append(line)
    
    return "\n".join([header_line, separator] + data_lines)


def cmd_search(args):
    """Command to search for books"""
    from novel_downloader import get_api_manager
    from config import CONFIG
    
    keyword = args.keyword
    if not keyword:
        print("Error: Please provide search keywords")
        return 1
    
    print(f"正在搜索: {keyword}")
    
    api = get_api_manager()
    result = api.search_books(keyword, offset=0)
    
    if not result or not result.get('data'):
        print("No relevant books found")
        return 0
    
    # 解析搜索结果
    search_data = result.get('data', {})
    books = []
    
    search_tabs = search_data.get('search_tabs', [])
    for tab in search_tabs:
        if tab.get('tab_type') == 3:
            tab_data = tab.get('data', [])
            for item in tab_data:
                book_data_list = item.get('book_data', [])
                for book in book_data_list:
                    if isinstance(book, dict):
                        status_code = str(book.get('creation_status', ''))
                        if status_code == '0':
                            status = '已完结'
                        elif status_code == '1':
                            status = '连载中'
                        else:
                            status = ''
                        
                        books.append([
                            book.get('book_id', ''),
                            book.get('book_name', '未知')[:20],
                            book.get('author', '未知')[:10],
                            status
                        ])
            break
    
    if not books:
        print("No relevant books found")
        return 0
    
    # 显示结果表格
    headers = ['Book ID', 'Book Title', 'Author', 'Status']
    print(f"\nFound {len(books)} books:\n")
    print(format_table(headers, books))
    
    return 0


def cmd_info(args):
    """Command to display book information"""
    from novel_downloader import get_api_manager
    
    book_id = args.book_id
    if not book_id:
        print("Error: Please provide the book ID")
        return 1
    
    # 从 URL 提取 ID
    if 'fanqienovel.com' in book_id:
        import re
        match = re.search(r'/page/(\d+)', book_id)
        if match:
            book_id = match.group(1)
    
    if not book_id.isdigit():
        print("Error: Book ID must be a number")
        return 1
    
    print(f"Retrieving book information: {book_id}")
    
    api = get_api_manager()
    
    # 获取书籍详情
    book_detail = api.get_book_detail(book_id)
    if not book_detail:
        print("Error: Unable to retrieve book information")
        return 1
    
    # 获取章节列表
    chapters_data = api.get_chapter_list(book_id)
    chapter_count = 0
    if chapters_data:
        if isinstance(chapters_data, dict):
            all_ids = chapters_data.get("allItemIds", [])
            chapter_count = len(all_ids)
        elif isinstance(chapters_data, list):
            chapter_count = len(chapters_data)
    
    # 显示信息
    print("\n" + "=" * 50)
    print(f"Book Title: {book_detail.get('book_name', 'Unknown')}")
    print(f"Author: {book_detail.get('author', 'Unknown')}")
    print(f"Number of Chapters: {chapter_count}")
    print("-" * 50)
    
    abstract = book_detail.get('abstract', '')
    if abstract:
        print("Introduction:")
        # 限制简介长度
        if len(abstract) > 200:
            abstract = abstract[:200] + "..."
        print(abstract)
    
    print("=" * 50)
    
    return 0


def cmd_download(args):
    """Command to download books"""
    from novel_downloader import Run
    from platform_utils import detect_platform
    import os
    
    book_id = args.book_id
    if not book_id:
        print("Error: Please provide the book ID")
        return 1
    
    # 从 URL 提取 ID
    if 'fanqienovel.com' in book_id:
        import re
        match = re.search(r'/page/(\d+)', book_id)
        if match:
            book_id = match.group(1)
    
    if not book_id.isdigit():
        print("Error: Book ID must be a number")
        return 1
    
    # 确定保存路径
    save_path = args.path
    if not save_path:
        # 默认保存到用户下载目录
        home = os.path.expanduser('~')
        save_path = os.path.join(home, 'Downloads')
        if not os.path.exists(save_path):
            save_path = home
    
    # 确保目录存在
    os.makedirs(save_path, exist_ok=True)
    
    # 确定格式
    file_format = args.format or 'txt'
    if file_format not in ['txt', 'epub']:
        print(f"Warning: Unsupported format '{file_format}'; using default format 'txt'")
        file_format = 'txt'
    
    print(f"Starting download of book: {book_id}")
    print(f"Save path: {save_path}")
    print(f"File format: {file_format}")
    print("-" * 50)
    
    # 进度回调
    def progress_callback(progress, message):
        if progress >= 0:
            print(f"[{progress:3d}%] {message}")
        else:
            print(f"       {message}")
    
    # 执行下载
    success = Run(
        book_id=book_id,
        save_path=save_path,
        file_format=file_format,
        gui_callback=progress_callback
    )
    
    if success:
        print("\nDownload complete!")
        return 0
    else:
        print("\nDownload failed")
        return 1


def cmd_status(args):
    """Command to display platform status"""
    report = get_feature_status_report()
    print(report)
    return 0


def create_parser() -> argparse.ArgumentParser:
    """Create a command-line argument parser"""
    parser = argparse.ArgumentParser(
        prog='fanqie-cli',
        description='Tomato Novel Downloader - Command-Line Version',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    %(prog)s search "Battle Through the Heavens"    Search for books
    %(prog)s info 12345                             View book information
    %(prog)s download 12345                         Download a book
    %(prog)s download 12345 -f epub                 Download in EPUB format
    %(prog)s status                                 Show platform status
        """
    )
    
    parser.add_argument('-v', '--verbose', action='store_true',
                       help='Show detailed output')
    
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # 'search' command
    search_parser = subparsers.add_parser('search', help='Search for books')
    search_parser.add_argument('keyword', help='Search keyword')
    search_parser.set_defaults(func=cmd_search)
    
    # 'info' command
    info_parser = subparsers.add_parser('info', help='View book information')
    info_parser.add_argument('book_id', help='Book ID or URL')
    info_parser.set_defaults(func=cmd_info)
    
    # 'download' command
    download_parser = subparsers.add_parser('download', help='Download a book')
    download_parser.add_argument('book_id', help='Book ID or URL')
    download_parser.add_argument('-p', '--path', help='Save path')
    download_parser.add_argument('-f', '--format', choices=['txt', 'epub'],
    default='txt', help='Output format (default: txt)')
    download_parser.set_defaults(func=cmd_download)
    
    # 'status' command
    status_parser = subparsers.add_parser('status', help='Show platform status')
    status_parser.set_defaults(func=cmd_status)
        
    return parser


def check_termux_dependencies():
    '''Check dependencies in the Termux environment'''
    platform_info = detect_platform()
    
    if platform_info.is_termux:
        # 检查是否缺少依赖
        missing = []
        try:
            import requests
        except ImportError:
            missing.append('requests')
        
        try:
            import aiohttp
        except ImportError:
            missing.append('aiohttp')
        
        if missing:
            print("=" * 50)
            print("Termux 环境检测到缺少依赖:")
            print(f"  缺少: {', '.join(missing)}")
            print("")
            print("建议使用 Termux 专用依赖文件安装:")
            print("  pip install -r requirements-termux.txt")
            print("=" * 50)
            print("")


def main():
    """CLI main entry point"""
    # Detect platform
    platform_info = detect_platform()
    
    # Termux 环境检查依赖
    check_termux_dependencies()
    
    parser = create_parser()
    args = parser.parse_args()
    
    if args.command is None:
        parser.print_help()
        return 0
    
    # Execute command
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n操作已取消")
        return 130
    except Exception as e:
        print(f"错误: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
