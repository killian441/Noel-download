# -*- coding: utf-8 -*-
"""
Configuration Management Module - Includes version information and global configuration
(Original) API document: http://49.232.137.12/docs
"""

__version__ = "1.1.0"
__author__ = "Tomato Novel Downloader"
__description__ = "A modern novel downloader with GitHub auto-update support"
__github_repo__ = "POf-L/Fanqie-novel-Downloader"
__build_time__ = "2025-12-13 21:00:00"
__build_channel__ = "custom"

try:
    import version as _ver
except Exception:
    _ver = None
else:
    __version__ = getattr(_ver, "__version__", __version__)
    __author__ = getattr(_ver, "__author__", __author__)
    __description__ = getattr(_ver, "__description__", __description__)
    __github_repo__ = getattr(_ver, "__github_repo__", __github_repo__)
    __build_time__ = getattr(_ver, "__build_time__", __build_time__)
    __build_channel__ = getattr(_ver, "__build_channel__", __build_channel__)

import random
import threading
import os
import json
import tempfile
from typing import Dict
from fake_useragent import UserAgent
from locales import t

_LOCAL_CONFIG_FILE = os.path.join(tempfile.gettempdir(), 'tomato_novel_downloader_config.json')

# Hard-coded API source configuration
HARDCODED_API_SOURCES = [
    {"name": "中国|浙江省|宁波市|电信", "base_url": "http://qkfqapi.vv9v.cn"},
    {"name": "中国|北京市|腾讯云", "base_url": "http://49.232.137.12"},
    {"name": "中国|江苏省|常州市|电信", "base_url": "http://43.248.77.205:22222"},
    {"name": "日本|东京", "base_url": "https://fq.shusan.cn"}
]

# Hard-coded configuration parameters
HARDCODED_CONFIG = {
    "max_workers": 2,
    "max_retries": 3,
    "request_timeout": 30,
    "request_rate_limit": 0.5,
    "connection_pool_size": 100,
    "api_rate_limit": 5,
    "rate_limit_window": 1.0,
    "async_batch_size": 30,
    "download_enabled": True
}

def _normalize_base_url(url: str) -> str:
    url = (url or "").strip()
    return url.rstrip('/')

def _load_local_pref() -> Dict:
    try:
        if os.path.exists(_LOCAL_CONFIG_FILE):
            with open(_LOCAL_CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}

# API Endpoint Configuration - Interconnection http://49.232.137.12/docs
LOCAL_ENDPOINTS = {
    "search": "/api/search",       # Search books (key, tab_type, offset)
    "detail": "/api/detail",       # Get book details (book_id)
    "book": "/api/book",           # Get Book Catalog (book_id)
    "directory": "/api/directory", # Get a simplified directory (fq_id)
    "content": "/api/content",     # Content Interface (tab=novel/batch/download, item_id/book_id)
}


def load_config() -> Dict:
    """Load hard-coded configuration"""
    print(t("config_loading_local"))
    
    config = {
        "api_base_url": "",
        "api_sources": HARDCODED_API_SOURCES.copy(),
        "request_timeout": HARDCODED_CONFIG["request_timeout"],
        "max_retries": HARDCODED_CONFIG["max_retries"],
        "connection_pool_size": HARDCODED_CONFIG["connection_pool_size"],
        "max_workers": HARDCODED_CONFIG["max_workers"],
        "download_delay": HARDCODED_CONFIG["request_rate_limit"],
        "retry_delay": 2,
        "status_file": ".download_status.json",
        "download_enabled": HARDCODED_CONFIG["download_enabled"],
        "verbose_logging": False,
        "request_rate_limit": HARDCODED_CONFIG["request_rate_limit"],
        "api_rate_limit": HARDCODED_CONFIG["api_rate_limit"],
        "rate_limit_window": HARDCODED_CONFIG["rate_limit_window"],
        "async_batch_size": HARDCODED_CONFIG["async_batch_size"],
        "endpoints": LOCAL_ENDPOINTS
    }

    # Read local preferences (manual/automatic selection can be reused).
    local_pref = _load_local_pref()
    mode = str(local_pref.get("api_base_url_mode", "auto") or "auto").lower()
    pref_url = _normalize_base_url(str(local_pref.get("api_base_url", "") or ""))
    if mode in ("manual", "auto") and pref_url:
        config["api_base_url"] = pref_url

    print(t("config_success", config['api_base_url'] or "auto"))
    return config

CONFIG = load_config()

print_lock = threading.Lock()

_UA_SINGLETON = None
_UA_LOCK = threading.Lock()

def _get_ua() -> UserAgent:
    global _UA_SINGLETON
    if _UA_SINGLETON is None:
        with _UA_LOCK:
            if _UA_SINGLETON is None:
                try:
                    _UA_SINGLETON = UserAgent()
                except Exception:
                    _UA_SINGLETON = None
    return _UA_SINGLETON

def get_headers() -> Dict[str, str]:
    user_agent = None
    try:
        ua = _get_ua()
        if ua is not None:
            user_agent = ua.chrome if random.choice(["chrome", "edge"]) == "chrome" else ua.edge
    except Exception:
        user_agent = None

    if not user_agent:
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

    return {
        "User-Agent": user_agent,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://fanqienovel.com/",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json"
    }

__all__ = [
    "CONFIG",
    "print_lock",
    "get_headers",
    "__version__",
    "__author__",
    "__description__",
    "__github_repo__",
    "__build_time__",
    "__build_channel__"
]
