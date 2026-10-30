#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Python Build Script
For compiling executables within GitHub Actions
Supports different variants (release/debug) and platform-specific executable naming
Incorporates the functionality of the original `build_config.py`
"""

import subprocess
import sys
import os
import argparse
import re
from pathlib import Path

# Import the encoding tool (if it exists).
try:
    from encoding_utils import safe_print, setup_utf8_encoding
    # Ensure UTF-8 encoding settings
    setup_utf8_encoding()
    # Use the safe print function
    print = safe_print
except ImportError:
    # If the encoding tool is not available, use the basic encoding settings.
    if sys.platform.startswith('win'):
        import locale
        try:
            locale.setlocale(locale.LC_ALL, 'C.UTF-8')
        except locale.Error:
            try:
                locale.setlocale(locale.LC_ALL, 'en_US.UTF-8')
            except locale.Error:
                pass  # Use default encoding


def parse_requirements(requirements_file='requirements.txt'):
    """
    Parse the requirements.txt file and extract all package names.

    Args:
        requirements_file: Path to the requirements.txt file.

    Returns:
        list: A list of package names.
    """
    packages = []
    req_path = Path(__file__).parent / requirements_file
    
    if not req_path.exists():
        return packages
    
    with open(req_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            # Skip comments and blank lines
            if not line or line.startswith('#'):
                continue
            
            # Extract package name (remove version constraints)
            # Supported formats：package, package==1.0, package>=1.0,<2.0
            match = re.match(r'^([a-zA-Z0-9_-]+)', line)
            if match:
                packages.append(match.group(1))
    
    return packages


# Some packages require explicit import of submodules.
PACKAGE_SUBMODULES = {
    'requests': [
        'requests.adapters',
        'requests.auth',
        'requests.cookies',
        'requests.exceptions',
        'requests.models',
        'requests.sessions',
        'requests.structures',
        'requests.utils',
        'requests.api',
        'requests.compat',
        'requests.help',
        'requests.hooks',
        'requests.packages',
        'requests.status_codes',
    ],
    'urllib3': [
        'urllib3.util',
        'urllib3.util.retry',
        'urllib3.util.ssl_',
        'urllib3.util.timeout',
        'urllib3.util.url',
        'urllib3.connection',
        'urllib3.connectionpool',
        'urllib3.poolmanager',
        'urllib3.response',
        'urllib3.exceptions',
        'urllib3._collections',
    ],
    'packaging': [
        'packaging.version',
        'packaging.specifiers',
        'packaging.requirements',
        'packaging.markers',
        'packaging.utils',
        'packaging.tags',
    ],
    'PIL': [
        'PIL.Image',
        'PIL.ImageTk',
        'PIL.ImageDraw',
        'PIL.ImageFile',
        'PIL.ImageFont',
        'PIL.ImageOps',
        'PIL.JpegImagePlugin',
        'PIL.PngImagePlugin',
        'PIL.GifImagePlugin',
        'PIL.BmpImagePlugin',
        'PIL.WebPImagePlugin',
        'PIL._imaging',
    ],
    'beautifulsoup4': [
        'bs4',
    ],
    'fake_useragent': [
        'fake_useragent.data',
    ],
    'pillow_heif': [
        'pillow_heif.heif',
        'pillow_heif.misc',
        'pillow_heif.options',
    ],
}

# Standard library modules (require explicit import)
# Note: tkinter-related modules have been removed; folder selection has been shifted to a front-end implementation.
STDLIB_MODULES = [
    'threading',
    'json',
    'os',
    'sys',
    'time',
    're',
    'base64',
    'gzip',
    'urllib.parse',
    'concurrent.futures',
    'collections',
    'typing',
    'signal',
    'random',
    'io',
    'tempfile',
    'zipfile',
    'shutil',
    'subprocess',
    'datetime',
]

# Package name mapping (handling special cases)
PACKAGE_NAME_MAPPING = {
    'pillow': 'PIL',
    'fake-useragent': 'fake_useragent',
    'beautifulsoup4': 'bs4',
}

# Implicit dependencies (runtime dependencies of certain packages)
IMPLICIT_DEPENDENCIES = [
    'charset_normalizer',
    'idna',
    'certifi',
]


def get_hidden_imports():
    """
    Get all the necessary hiddenimports
    
    Returns:
        list: Complete list of hiddenimports
    """
    packages = parse_requirements()
    hidden_imports = []
    
    # Add base package
    for pkg in packages:
        pkg_lower = pkg.lower()
        
        # Handling special package name mappings
        if pkg_lower in PACKAGE_NAME_MAPPING:
            mapped_pkg = PACKAGE_NAME_MAPPING[pkg_lower]
            hidden_imports.append(mapped_pkg)
            pkg = mapped_pkg
        else:
            # Convert hyphens to underscores
            pkg = pkg.replace('-', '_')
            hidden_imports.append(pkg)
        
        # Add known submodules
        if pkg in PACKAGE_SUBMODULES:
            hidden_imports.extend(PACKAGE_SUBMODULES[pkg])
    
    # Add standard library module
    hidden_imports.extend(STDLIB_MODULES)
    
    # Add implicit dependencies
    hidden_imports.extend(IMPLICIT_DEPENDENCIES)
    
    # Include local modules (PyInstaller sometimes fails to detect imports located within functions/methods)
    # Ensure configuration modules are correctly bundled
    hidden_imports.extend([
        'config',
        'web_app',
        'novel_downloader',
    ])

    # Deduplication and sorting
    hidden_imports = sorted(set(hidden_imports))
    
    return hidden_imports


def get_platform_config(target_platform: str = None) -> dict:
    """
    Obtain the build configuration of the target platform
    
    Args:
        target_platform: 'windows', 'linux', 'darwin'，默认为当前平台
    
    Returns:
        包含 hidden_imports, data_files, options 的配置字典
    """
    if target_platform is None:
        if sys.platform == 'win32':
            target_platform = 'windows'
        elif sys.platform == 'darwin':
            target_platform = 'darwin'
        else:
            target_platform = 'linux'
    
    config = {
        'platform': target_platform,
        'hidden_imports': get_hidden_imports(),
        'data_files': [
            ('static', 'static'),
            ('templates', 'templates'),
        ],
        'options': [],
    }
    
    # 平台特定配置
    if target_platform == 'windows':
        config['hidden_imports'].extend([
            'win32api',
            'win32con',
            'pywintypes',
        ])
        # Windows 使用分号作为路径分隔符
        config['path_separator'] = ';'
    elif target_platform == 'linux':
        config['hidden_imports'].extend([
            'gi',
            'gi.repository.Gtk',
        ])
        config['path_separator'] = ':'
        # Linux 不需要 tkinter 相关的 Windows 模块
        config['exclude_modules'] = ['win32api', 'win32con', 'pywintypes']
    elif target_platform == 'darwin':
        config['hidden_imports'].extend([
            'AppKit',
            'Foundation',
        ])
        config['path_separator'] = ':'
    
    # 添加新的平台检测模块
    config['hidden_imports'].extend([
        'platform_utils',
        'cli',
    ])
    
    # 去重
    config['hidden_imports'] = sorted(set(config['hidden_imports']))
    
    return config


def get_executable_name(base_name: str, platform: str, variant: str) -> str:
    """
    Generate a platform-appropriate executable filename

    Args:
        base_name: Base name
        platform: Target platform
        variant: Build variant
    
    Returns:
        The complete executable filename (excluding the file extension)
    """
    name = base_name
    
    # 添加变体后缀
    if variant == 'debug':
        name = f"{name}-debug"
    
    # 添加平台后缀
    if platform == 'linux':
        name = f"{name}-linux"
    elif platform == 'darwin':
        name = f"{name}-macos"
    # Windows 不添加后缀，保持兼容性
    
    return name


def build_executable(variant="release", executable_name=None, target_platform=None):
    """Compile an executable file
    
    Args:
        variant: Build variant ('release' or 'debug')
        executable_name: Custom name for the executable (excluding the file extension)
        target_platform: Target platform ('windows', 'linux', 'darwin'); defaults to the current platform
    
    Returns:
        tuple: (success, built_name, target_name)
            - success: Whether the build was successful
            - built_name: The original name as defined in the spec file
            - target_name: The desired final name
    """
    # 获取平台配置
    platform_config = get_platform_config(target_platform)
    current_platform = platform_config['platform']
    
    print(f"Starting build process for {variant} variant on {current_platform}...")
    
    # 确定目标可执行文件名称
    if executable_name:
        target_name = executable_name
    else:
        target_name = get_executable_name("TomatoNovelDownloader", current_platform, variant)
    
    # Web版本使用 main.py 作为入口
    print("Building Web version with main.py as entry point")
    
    # 构建基础命令
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        f"--name={target_name}",
    ]
    
    # 根据变体选择窗口模式或控制台模式
    if variant == "debug":
        cmd.append("--console")
    else:
        cmd.append("--windowed")
    
    # 添加隐藏导入（使用平台配置）
    for import_name in platform_config['hidden_imports']:
        cmd.extend(["--hidden-import", import_name])
    
    # 添加数据收集
    cmd.extend(["--collect-data", "fake_useragent"])
    cmd.extend(["--collect-submodules", "PIL"])
    
    # 添加静态文件和模板（使用平台适配的路径分隔符）
    sep = platform_config['path_separator']
    for src, dst in platform_config['data_files']:
        cmd.extend(["--add-data", f"{src}{sep}{dst}"])
    
    # 添加入口文件（Web版）
    cmd.append("main.py")
    
    # built_name和target_name相同
    built_name = target_name
    
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, encoding='utf-8')
        print("Build successful")
        print(result.stdout)
        return True, target_name, target_name
    except subprocess.CalledProcessError as e:
        print("Build failed")
        print(f"Error output: {e.stderr}")
        return False, target_name, target_name

def check_output(expected_name):
    """Check the compilation output

    Args:
        expected_name: The expected name of the executable file (excluding the extension)
    """
    print("Checking build output...")
    if os.path.exists("dist"):
        files = os.listdir("dist")
        print(f"dist directory contents: {files}")
        
        # 检查可执行文件
        exe_name = f"{expected_name}.exe" if os.name == "nt" else expected_name
        exe_path = os.path.join("dist", exe_name)
        
        if os.path.exists(exe_path):
            size = os.path.getsize(exe_path)
            print(f"Executable created successfully: {exe_name} ({size} bytes)")
            return True
        else:
            print(f"Executable not found: {exe_path}")
            return False
    else:
        print("dist directory does not exist")
        return False

def rename_executable(current_name, target_name):
    """Rename an executable file

    Args:
        current_name: The current filename (excluding the extension)
        target_name: The target filename (excluding the extension)
    """
    if current_name == target_name:
        return True
        
    ext = ".exe" if os.name == "nt" else ""
    current_path = os.path.join("dist", f"{current_name}{ext}")
    target_path = os.path.join("dist", f"{target_name}{ext}")
    
    if os.path.exists(current_path):
        try:
            os.rename(current_path, target_path)
            print(f"Renamed {current_name}{ext} to {target_name}{ext}")
            return True
        except OSError as e:
            print(f"Failed to rename executable: {e}")
            return False
    else:
        print(f"Source file not found: {current_path}")
        return False

def main():
    """Main function"""
    parser = argparse.ArgumentParser(description="Build Tomato Novel Downloader")
    parser.add_argument("--variant", choices=["release", "debug"], default="release",
                       help="Build variant (release or debug)")
    parser.add_argument("--name", type=str, help="Custom executable name (without extension)")
    
    args = parser.parse_args()
    
    # 构建可执行文件
    success, built_name, target_name = build_executable(args.variant, args.name)
    
    if success:
        # 先检查构建输出
        if check_output(built_name):
            # If `built_name` and `target_name` are different, a rename is required.
            if built_name != target_name:
                if rename_executable(built_name, target_name):
                    print(f"Build completed successfully! Final executable: {target_name}")
                    return True
                else:
                    print("Build successful but renaming failed")
                    return False
            else:
                print(f"Build completed successfully! Executable: {built_name}")
                return True
        else:
            print("Build output check failed")
            return False
    else:
        print("Build failed")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1) 
