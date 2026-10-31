# -*- coding: utf-8 -*-
"""
Web Application - Flask backend for an HTML GUI
"""

import os
import json
import time
import threading
import queue
import tempfile
import subprocess
import re
import requests
from locales import t
from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_cors import CORS
import logging

# Pre-import version information (ensuring the correct version is retrieved as soon as the module is loaded)
from config import __version__ as APP_VERSION

# Disable Flask's default logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__, template_folder='templates', static_folder='static')
CORS(app)

# Access token (set by main.py upon startup)
ACCESS_TOKEN = None

def set_access_token(token):
    """Set access token"""
    global ACCESS_TOKEN
    ACCESS_TOKEN = token

# Configuration file path – saved to the system's temporary directory (cross-platform compatible)
TEMP_DIR = tempfile.gettempdir()
CONFIG_FILE = os.path.join(TEMP_DIR, 'fanqie_novel_downloader_config.json')

def _read_local_config() -> dict:
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}

def _write_local_config(updates: dict) -> bool:
    try:
        cfg = _read_local_config()
        cfg.update(updates or {})
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False

def _normalize_base_url(url: str) -> str:
    return (url or '').strip().rstrip('/')

def get_default_download_path():
    """Get the default download path (cross-platform compatible)"""
    import sys
    # Prioritize the user's download directory
    home = os.path.expanduser('~')
    if sys.platform == 'win32':
        # Windows: Try using the Downloads folder
        downloads = os.path.join(home, 'Downloads')
    elif sys.platform == 'darwin':
        # macOS
        downloads = os.path.join(home, 'Downloads')
    else:
        # Linux / Termux / 其他 Unix
        downloads = os.path.join(home, 'Downloads')
        # If the Downloads directory does not exist, try using the XDG user directories.
        if not os.path.exists(downloads):
            xdg_download = os.environ.get('XDG_DOWNLOAD_DIR')
            if xdg_download and os.path.exists(xdg_download):
                downloads = xdg_download
            else:
                # Return to the user's home directory
                downloads = home
    
    # Ensure the directory exists
    if not os.path.exists(downloads):
        try:
            os.makedirs(downloads, exist_ok=True)
        except:
            downloads = home
    
    return downloads

# Global variables
download_queue = queue.Queue()
current_download_status = {
    'is_downloading': False,
    'progress': 0,
    'message': '',
    'book_name': '',
    'total_chapters': 0,
    'downloaded_chapters': 0,
    'queue_total': 0,
    'queue_done': 0,
    'queue_current': 0,
    'messages': []  # Message queue, which stores all messages awaiting delivery
}
status_lock = threading.Lock()


# ===================== 任务管理器 =====================

class TaskManager:
    """Download Queue Task Manager

    Manages the list of download tasks, tracks their status, and handles operations such as skipping, retrying, or forcing a save.
    """
    
    # Task Status Constants
    STATUS_PENDING = 'pending'
    STATUS_DOWNLOADING = 'downloading'
    STATUS_COMPLETED = 'completed'
    STATUS_FAILED = 'failed'
    STATUS_SKIPPED = 'skipped'
    
    def __init__(self):
        self.tasks = []  # List of tasks
        self.current_index = 0  # Index of the current task
        self.is_running = False  # Whether the queue is currently running
        self.skip_requested = False  # Whether a skip for the current task has been requested
        self.force_save_requested = False  # Whether a forced save has been requested
        self.current_download_mode = None  # Current download mode (fast/slow)
        self.downloaded_chapters = {}  # Downloaded chapters {book_id: {index: content}}
        self._lock = threading.Lock()
    
    def start_queue(self, tasks: list) -> bool:
        """Initiate queued downloads

        Args:
            tasks: A list of tasks, where each task contains information such as book_id, book_name, and author.
        
        Returns:
            bool: Whether the initiation was successful.
        """
        with self._lock:
            if self.is_running:
                return False
            
            self.tasks = []
            for i, task in enumerate(tasks):
                self.tasks.append({
                    'id': task.get('id', f"task_{i}_{int(time.time())}"),
                    'book_id': task.get('book_id'),
                    'book_name': task.get('book_name', ''),
                    'author': task.get('author', ''),
                    'status': self.STATUS_PENDING,
                    'progress': 0,
                    'error_message': None,
                    'file_format': task.get('file_format', 'txt'),
                    'save_path': task.get('save_path', ''),
                    'start_chapter': task.get('start_chapter'),
                    'end_chapter': task.get('end_chapter'),
                    'selected_chapters': task.get('selected_chapters'),
                    'created_at': time.time(),
                    'started_at': None,
                    'completed_at': None
                })
            
            self.current_index = 0
            self.is_running = True
            self.skip_requested = False
            self.force_save_requested = False
            
            return True
    
    def get_current_task(self) -> dict:
        """Retrieve the currently executing task"""
        with self._lock:
            if 0 <= self.current_index < len(self.tasks):
                return self.tasks[self.current_index].copy()
            return None
    
    def update_task_status(self, task_id: str, status: str, progress: int = None, 
                          error_message: str = None) -> bool:
        """Update task status

        Args:
            task_id: Task ID
            status: New status
            progress: Progress (0-100)
            error_message: Error message
        
        Returns:
            bool: Whether the update was successful
        """
        with self._lock:
            for task in self.tasks:
                if task['id'] == task_id:
                    task['status'] = status
                    if progress is not None:
                        task['progress'] = progress
                    if error_message is not None:
                        task['error_message'] = error_message
                    
                    if status == self.STATUS_DOWNLOADING and task['started_at'] is None:
                        task['started_at'] = time.time()
                    elif status in [self.STATUS_COMPLETED, self.STATUS_FAILED, self.STATUS_SKIPPED]:
                        task['completed_at'] = time.time()
                    
                    return True
            return False
    
    def skip_current(self) -> bool:
        """Skip the current task

        Returns:
            bool: Whether the skip flag was successfully set
        """
        with self._lock:
            if not self.is_running:
                return False
            
            if 0 <= self.current_index < len(self.tasks):
                current_task = self.tasks[self.current_index]
                if current_task['status'] == self.STATUS_DOWNLOADING:
                    self.skip_requested = True
                    current_task['status'] = self.STATUS_SKIPPED
                    current_task['completed_at'] = time.time()
                    return True
            return False
    
    def force_save(self) -> dict:
        """Force-save the content downloaded so far.

        Returns:
            dict: The save result, containing 'success', 'saved_chapters', and 'book_id'.
        """
        with self._lock:
            if not self.is_running:
                return {'success': False, 'message': 'Queue is not running'}
            
            current_task = self.get_current_task_unsafe()
            if not current_task:
                return {'success': False, 'message': 'No tasks currently in progress'}
            
            book_id = current_task['book_id']
            
            # Set the forced save flag
            self.force_save_requested = True
            
            # Retrieve downloaded chapters
            downloaded = self.downloaded_chapters.get(book_id, {})
            
            return {
                'success': True,
                'book_id': book_id,
                'saved_chapters': len(downloaded),
                'message': f'已保存 {len(downloaded)} 个章节'
            }
    
    def get_current_task_unsafe(self) -> dict:
        """Get the current task (no locking; for internal use)"""
        if 0 <= self.current_index < len(self.tasks):
            return self.tasks[self.current_index]
        return None
    
    def move_to_next_task(self) -> bool:
        """Move to the next task

        Returns:
            bool: Whether there is another task
        """
        with self._lock:
            self.current_index += 1
            self.skip_requested = False
            self.force_save_requested = False
            
            if self.current_index >= len(self.tasks):
                self.is_running = False
                return False
            return True
    
    def retry_task(self, task_id: str) -> bool:
        """Retry a specified task

        Args:
            task_id: Task ID
        
        Returns:
            bool: Whether the retry was successfully set
        """
        with self._lock:
            for task in self.tasks:
                if task['id'] == task_id and task['status'] == self.STATUS_FAILED:
                    task['status'] = self.STATUS_PENDING
                    task['progress'] = 0
                    task['error_message'] = None
                    task['started_at'] = None
                    task['completed_at'] = None
                    return True
            return False
    
    def retry_all_failed(self) -> int:
        """Retry all failed tasks

        Returns:
            int: The number of tasks retried
        """
        with self._lock:
            count = 0
            for task in self.tasks:
                if task['status'] == self.STATUS_FAILED:
                    task['status'] = self.STATUS_PENDING
                    task['progress'] = 0
                    task['error_message'] = None
                    task['started_at'] = None
                    task['completed_at'] = None
                    count += 1
            return count
    
    def get_queue_status(self) -> dict:
        """Get queue status

        Returns:
            dict: Queue status information
        """
        with self._lock:
            completed_count = sum(1 for t in self.tasks if t['status'] == self.STATUS_COMPLETED)
            failed_count = sum(1 for t in self.tasks if t['status'] == self.STATUS_FAILED)
            skipped_count = sum(1 for t in self.tasks if t['status'] == self.STATUS_SKIPPED)
            
            current_task = self.get_current_task_unsafe()
            current_task_id = current_task['id'] if current_task else None
            current_task_progress = current_task['progress'] if current_task else 0
            
            return {
                'is_running': self.is_running,
                'total_tasks': len(self.tasks),
                'completed_count': completed_count,
                'failed_count': failed_count,
                'skipped_count': skipped_count,
                'current_task_id': current_task_id,
                'current_task_progress': current_task_progress,
                'current_download_mode': self.current_download_mode,
                'tasks': [t.copy() for t in self.tasks]
            }
    
    def set_download_mode(self, mode: str):
        """Sets the current download mode

        Args:
            mode: 'fast' or 'slow'
        """
        with self._lock:
            self.current_download_mode = mode
    
    def store_chapter(self, book_id: str, chapter_index: int, content: dict):
        """Store the content of a downloaded chapter

        Args:
            book_id: Book ID
            chapter_index: Chapter index
            content: Chapter content
        """
        with self._lock:
            if book_id not in self.downloaded_chapters:
                self.downloaded_chapters[book_id] = {}
            self.downloaded_chapters[book_id][chapter_index] = content
    
    def get_downloaded_chapters(self, book_id: str) -> dict:
        """Retrieve downloaded chapters

        Args:
            book_id: Book ID
        
        Returns:
            dict: Downloaded chapters {index: content}
        """
        with self._lock:
            return self.downloaded_chapters.get(book_id, {}).copy()
    
    def clear_downloaded_chapters(self, book_id: str):
        """Clear the cache of downloaded chapters

        Args:
            book_id: Book ID
        """
        with self._lock:
            if book_id in self.downloaded_chapters:
                del self.downloaded_chapters[book_id]


# Global Task Manager Instance
task_manager = TaskManager()


# Update download status - Supports multi-threaded downloading
update_download_status = {
    'is_downloading': False,
    'progress': 0,
    'message': '',
    'filename': '',
    'total_size': 0,
    'downloaded_size': 0,
    'completed': False,
    'error': None,
    'save_path': '',
    'temp_file_path': '',
    'thread_count': 1,
    'thread_progress': [],  # Progress of each thread [{'downloaded': 50, 'total': 100, 'percent': 50, 'speed': 1024}, ...]
    'merging': False  # Are files being merged?
}
update_lock = threading.Lock()

def get_update_status():
    """Retrieve update download status"""
    with update_lock:
        return update_download_status.copy()

def set_update_status(**kwargs):
    """Set/update download status"""
    with update_lock:
        for key, value in kwargs.items():
            if key in update_download_status:
                update_download_status[key] = value

def test_url_connectivity(url, timeout=8):
    """Test URL connectivity"""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    test_url = f"{parsed.scheme}://{parsed.netloc}"
    
    # requests The system proxy is used by default; you can proceed directly with testing.
    print(f'[DEBUG] Testing connection to: {test_url}')
    try:
        resp = requests.head(test_url, timeout=timeout, allow_redirects=True)
        if resp.status_code < 500:
            print(f'[DEBUG] Connection OK (status: {resp.status_code})')
            return True
    except Exception as e:
        print(f'[DEBUG] Connection failed: {e}')
    
    return False

def download_chunk_adaptive(url, start, end, chunk_id, temp_file, progress_dict, total_size, cancel_flag):
    """Download a chunk of a file (adaptive version)"""
    headers = {'Range': f'bytes={start}-{end}'}
    try:
        response = requests.get(url, headers=headers, stream=True, timeout=120, allow_redirects=True)
        response.raise_for_status()
        
        chunk_size = 32768  # 32KB chunks for better throughput measurement
        downloaded = 0
        chunk_total = end - start + 1
        last_time = time.time()
        last_downloaded = 0
        
        with open(temp_file, 'wb') as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if cancel_flag.get('cancelled'):
                    return {'success': False, 'reason': 'cancelled'}
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    
                    # Calculation speed (updated once per second)
                    now = time.time()
                    if now - last_time >= 0.5:
                        speed = (downloaded - last_downloaded) / (now - last_time)
                        last_time = now
                        last_downloaded = downloaded
                        progress_dict[chunk_id] = {
                            'downloaded': downloaded,
                            'total': chunk_total,
                            'percent': int((downloaded / chunk_total) * 100) if chunk_total > 0 else 0,
                            'speed': speed
                        }
                    else:
                        progress_dict[chunk_id] = {
                            'downloaded': downloaded,
                            'total': chunk_total,
                            'percent': int((downloaded / chunk_total) * 100) if chunk_total > 0 else 0,
                            'speed': progress_dict.get(chunk_id, {}).get('speed', 0)
                        }
        
        return {'success': True, 'chunk_id': chunk_id}
    except Exception as e:
        print(f'[DEBUG] Chunk {chunk_id} download error: {e}')
        return {'success': False, 'reason': str(e), 'chunk_id': chunk_id}

def update_download_worker(url, save_path, filename):
    """Update Download Worker Threads - Adaptive Multi-threaded Downloading"""
    print(f'[DEBUG] update_download_worker started (adaptive multi-threaded)')
    print(f'[DEBUG]   url: {url}')
    print(f'[DEBUG]   save_path: {save_path}')
    print(f'[DEBUG]   filename: {filename}')
    
    MIN_THREADS = 1
    MAX_THREADS = 64
    MIN_CHUNK_SIZE = 256 * 1024  # Minimum chunk size: 256 KB
    SPEED_CHECK_INTERVAL = 1.0  # Speed ​​check interval (in seconds)
    SPEED_THRESHOLD = 0.8  # Bandwidth utilization threshold; increase threads if below this value
    
    try:
        set_update_status(
            is_downloading=True, 
            progress=0, 
            message=t('web_update_status_connect'), 
            filename=filename,
            completed=False,
            error=None,
            save_path=save_path,
            thread_count=MIN_THREADS,
            thread_progress=[],
            merging=False
        )
        
        import tempfile
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        # Test connectivity first.
        set_update_status(message=t('web_update_status_connect'))
        if not test_url_connectivity(url, timeout=8):
            raise Exception(t('web_update_connect_fail'))
        
        # Get file size
        print(f'[DEBUG] Getting file info from: {url}')
        head_response = requests.get(url, stream=True, timeout=30, allow_redirects=True)
        head_response.raise_for_status()
        
        final_url = head_response.url
        print(f'[DEBUG] Final URL after redirect: {final_url}')
        
        total_size = int(head_response.headers.get('content-length', 0))
        supports_range = head_response.headers.get('accept-ranges', '').lower() == 'bytes'
        head_response.close()
        
        print(f'[DEBUG] Total size: {total_size} bytes, supports_range: {supports_range}')
        
        temp_dir = tempfile.gettempdir()
        temp_filename = filename + '.new'
        full_path = os.path.join(temp_dir, temp_filename)
        
        # Chunking is not supported or the file is too small; using a single thread.
        if total_size == 0 or not supports_range or total_size < 1024 * 1024:
            print(f'[DEBUG] Using single-threaded download')
            set_update_status(thread_count=1, total_size=total_size, message=t('web_update_status_start'))
            
            response = requests.get(final_url, stream=True, timeout=120, allow_redirects=True)
            response.raise_for_status()
            
            if total_size == 0:
                total_size = int(response.headers.get('content-length', 0))
            
            downloaded = 0
            with open(full_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if not get_update_status()['is_downloading']:
                        break
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        progress = int((downloaded / total_size) * 100) if total_size > 0 else min(99, downloaded // 100000)
                        set_update_status(
                            progress=progress,
                            downloaded_size=downloaded,
                            total_size=total_size,
                            thread_progress=[{'downloaded': downloaded, 'total': total_size or downloaded, 'percent': progress, 'speed': 0}],
                            message=t('web_update_status_dl', progress) if total_size > 0 else f'已下载 {downloaded // 1024} KB'
                        )
        else:
            # Adaptive multi-threaded downloading
            print(f'[DEBUG] Using adaptive multi-threaded download')
            set_update_status(total_size=total_size, message=t('web_update_status_start'))
            
            cancel_flag = {'cancelled': False}
            progress_dict = {}
            active_chunks = []  # [(chunk_id, start, end, temp_file, future), ...]
            completed_chunks = []  # [(chunk_id, temp_file), ...]
            next_chunk_id = 0
            remaining_start = 0
            current_threads = MIN_THREADS
            last_speed_check = time.time()
            last_total_downloaded = 0
            peak_speed = 0
            
            def get_next_chunk(chunk_id, start, size):
                """Get the next chunk"""
                end = min(start + size - 1, total_size - 1)
                temp_file = os.path.join(temp_dir, f'{filename}.part{chunk_id}')
                return (chunk_id, start, end, temp_file)
            
            def update_progress():
                """Overall Progress Update"""
                total_downloaded = sum(p.get('downloaded', 0) for p in progress_dict.values())
                total_downloaded += sum(total_size // len(completed_chunks) if completed_chunks else 0 for _ in [])
                # Calculate the size of the completed chunks
                for cid, _ in completed_chunks:
                    if cid in progress_dict:
                        total_downloaded = sum(p.get('downloaded', 0) for p in progress_dict.values())
                        break
                
                overall_progress = int((total_downloaded / total_size) * 100) if total_size > 0 else 0
                overall_progress = min(99, overall_progress)  # Download reaches a maximum of 99%
                
                thread_progress = [
                    {'downloaded': p.get('downloaded', 0), 'total': p.get('total', 0), 'percent': p.get('percent', 0), 'speed': p.get('speed', 0)}
                    for p in progress_dict.values()
                ]
                
                set_update_status(
                    progress=overall_progress,
                    downloaded_size=total_downloaded,
                    thread_count=current_threads,
                    thread_progress=thread_progress,
                    message=t('web_update_status_dl', overall_progress)
                )
                return total_downloaded
            
            # Initial block size
            initial_chunk_size = max(MIN_CHUNK_SIZE, total_size // 4)
            
            with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
                # Start the initial thread
                for _ in range(MIN_THREADS):
                    if remaining_start < total_size:
                        chunk = get_next_chunk(next_chunk_id, remaining_start, initial_chunk_size)
                        chunk_id, start, end, temp_file = chunk
                        remaining_start = end + 1
                        next_chunk_id += 1
                        
                        future = executor.submit(
                            download_chunk_adaptive, final_url, start, end, 
                            chunk_id, temp_file, progress_dict, total_size, cancel_flag
                        )
                        active_chunks.append((chunk_id, start, end, temp_file, future))
                
                # Main loop: Monitor progress and dynamically adjust threads
                while active_chunks or remaining_start < total_size:
                    if not get_update_status()['is_downloading']:
                        cancel_flag['cancelled'] = True
                        break
                    
                    # Check completed tasks
                    still_active = []
                    for chunk_info in active_chunks:
                        chunk_id, start, end, temp_file, future = chunk_info
                        if future.done():
                            result = future.result()
                            if result.get('success'):
                                completed_chunks.append((chunk_id, temp_file))
                                print(f'[DEBUG] Chunk {chunk_id} completed')
                            else:
                                print(f'[DEBUG] Chunk {chunk_id} failed: {result.get("reason")}')
                                # Retry failed chunks
                                if not cancel_flag['cancelled']:
                                    new_future = executor.submit(
                                        download_chunk_adaptive, final_url, start, end,
                                        chunk_id, temp_file, progress_dict, total_size, cancel_flag
                                    )
                                    still_active.append((chunk_id, start, end, temp_file, new_future))
                        else:
                            still_active.append(chunk_info)
                    active_chunks = still_active
                    
                    # Update Progress
                    total_downloaded = update_progress()
                    
                    # Speed ​​detection and thread adjustment
                    now = time.time()
                    if now - last_speed_check >= SPEED_CHECK_INTERVAL:
                        current_speed = (total_downloaded - last_total_downloaded) / (now - last_speed_check)
                        last_speed_check = now
                        last_total_downloaded = total_downloaded
                        
                        if current_speed > peak_speed:
                            peak_speed = current_speed
                        
                        # If the current speed is below the peak speed threshold and there is remaining data, increase the number of threads.
                        if (peak_speed > 0 and current_speed < peak_speed * SPEED_THRESHOLD and 
                            remaining_start < total_size and current_threads < MAX_THREADS and
                            len(active_chunks) < MAX_THREADS):
                            
                            # Calculate the new chunk size (distribute the remaining data evenly among the new threads)
                            remaining_size = total_size - remaining_start
                            threads_to_add = min(
                                MAX_THREADS - current_threads,
                                max(1, remaining_size // MIN_CHUNK_SIZE),
                                4  # Add up to 4 threads at a time
                            )
                            
                            new_chunk_size = max(MIN_CHUNK_SIZE, remaining_size // (threads_to_add + 1))
                            
                            for _ in range(threads_to_add):
                                if remaining_start < total_size:
                                    chunk = get_next_chunk(next_chunk_id, remaining_start, new_chunk_size)
                                    chunk_id, start, end, temp_file = chunk
                                    remaining_start = end + 1
                                    next_chunk_id += 1
                                    current_threads += 1
                                    
                                    future = executor.submit(
                                        download_chunk_adaptive, final_url, start, end,
                                        chunk_id, temp_file, progress_dict, total_size, cancel_flag
                                    )
                                    active_chunks.append((chunk_id, start, end, temp_file, future))
                                    print(f'[DEBUG] Added thread {current_threads}, chunk {chunk_id}')
                    
                    time.sleep(0.1)
                
                # Wait for all tasks to complete
                for chunk_info in active_chunks:
                    chunk_id, start, end, temp_file, future = chunk_info
                    try:
                        result = future.result(timeout=60)
                        if result.get('success'):
                            completed_chunks.append((chunk_id, temp_file))
                    except Exception as e:
                        print(f'[DEBUG] Chunk {chunk_id} final error: {e}')
            
            if cancel_flag['cancelled']:
                # Clean up temporary files
                for _, temp_file in completed_chunks:
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
                print(f'[DEBUG] Download was cancelled')
                return
            
            # Merge files
            if completed_chunks and get_update_status()['is_downloading']:
                print(f'[DEBUG] Merging {len(completed_chunks)} chunks...')
                set_update_status(
                    progress=100,
                    message=t('web_update_status_merging'),
                    merging=True
                )
                
                # Sort by chunk_id
                completed_chunks.sort(key=lambda x: x[0])
                
                with open(full_path, 'wb') as outfile:
                    for chunk_id, temp_file in completed_chunks:
                        if os.path.exists(temp_file):
                            with open(temp_file, 'rb') as infile:
                                outfile.write(infile.read())
                            os.remove(temp_file)
                            print(f'[DEBUG] Merged chunk {chunk_id}')
            else:
                # Clean up temporary files
                for _, temp_file in completed_chunks:
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
        
        if get_update_status()['is_downloading']:
            print(f'[DEBUG] Download completed successfully!')
            print(f'[DEBUG] File saved to: {full_path}')
            if os.path.exists(full_path):
                print(f'[DEBUG] File size: {os.path.getsize(full_path)} bytes')
            set_update_status(
                is_downloading=False, 
                completed=True, 
                progress=100, 
                message=t('web_update_complete'),
                temp_file_path=full_path,
                merging=False
            )
        else:
            print(f'[DEBUG] Download was cancelled')
            if os.path.exists(full_path):
                os.remove(full_path)
                
    except Exception as e:
        import traceback
        print(f'[DEBUG] Download failed with exception:')
        print(f'[DEBUG]   {type(e).__name__}: {str(e)}')
        traceback.print_exc()
        set_update_status(
            is_downloading=False, 
            error=str(e), 
            message=t('web_update_fail', str(e)),
            merging=False
        )

# Lazy-load heavy modules
api = None
api_manager = None
novel_downloader = None
downloader_instance = None

def init_modules(skip_api_select=False):
    """Initialize core modules"""
    global api, api_manager, novel_downloader, downloader_instance
    try:
        # If no interface is specified, one will be automatically selected (this step can be skipped to speed up startup)
        if not skip_api_select:
            _ensure_api_base_url()

        from novel_downloader import NovelDownloader, get_api_manager
        novel_downloader = __import__('novel_downloader')
        api = NovelDownloader()
        api_manager = get_api_manager()
        downloader_instance = api
        return True
    except Exception as e:
        print(t("msg_module_fail", e))
        return False


def _get_api_sources() -> list:
    """Retrieve the list of optional API interfaces from the configuration."""
    try:
        from config import CONFIG
        sources = CONFIG.get('api_sources') or []
        normalized = []
        for s in sources:
            if isinstance(s, dict):
                base_url = _normalize_base_url(s.get('base_url') or s.get('api_base_url') or '')
                if base_url:
                    normalized.append({
                        'name': s.get('name') or base_url,
                        'base_url': base_url
                    })
            elif isinstance(s, str):
                base_url = _normalize_base_url(s)
                if base_url:
                    normalized.append({'name': base_url, 'base_url': base_url})

        # Fallback: Must include at least the current base_url
        base = _normalize_base_url(str(CONFIG.get('api_base_url', '') or ''))
        if base and not any(x['base_url'] == base for x in normalized):
            normalized.insert(0, {'name': base, 'base_url': base})

        # Remove duplicates
        seen = set()
        deduped = []
        for s in normalized:
            if s['base_url'] in seen:
                continue
            seen.add(s['base_url'])
            deduped.append(s)

        return deduped
    except Exception:
        return []


def _probe_api_source(base_url: str, timeout: float = 1.5) -> dict:
    """HTTP health check (pings only the domain root path, with a short timeout)"""
    import requests
    from urllib.parse import urlparse

    base_url = _normalize_base_url(base_url)
    parsed = urlparse(base_url)
    ping_url = f"{parsed.scheme}://{parsed.netloc}/"

    start = time.perf_counter()
    try:
        resp = requests.head(ping_url, timeout=timeout, allow_redirects=True)
        latency_ms = int((time.perf_counter() - start) * 1000)

        # If a connection can be established, it is considered usable.
        available = resp.status_code < 500
        return {
            'available': available,
            'latency_ms': latency_ms,
            'status_code': resp.status_code,
            'error': None
        }
    except Exception as e:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {
            'available': False,
            'latency_ms': latency_ms,
            'status_code': None,
            'error': str(e)
        }


def _apply_api_base_url(base_url: str) -> None:
    """Apply the API base_url to the runtime (CONFIG + APIManager)"""
    from config import CONFIG

    base_url = _normalize_base_url(base_url)
    if not base_url:
        return

    CONFIG['api_base_url'] = base_url

    global api_manager
    if api_manager:
        api_manager.base_url = base_url
        # Reset the thread-local session to prevent issues caused by connection reuse.
        if hasattr(api_manager, '_tls'):
            api_manager._tls = threading.local()


def _ensure_api_base_url(force_mode=None) -> str:
    """
    Ensures that CONFIG.api_base_url is set; if it is empty or unavailable, the fastest node is automatically selected.
    
    Returns:
        str: The current/selected base_url (may be empty).
    """
    from config import CONFIG

    sources = _get_api_sources()
    if not sources:
        return _normalize_base_url(str(CONFIG.get('api_base_url', '') or ''))

    local_cfg = _read_local_config()
    mode = str(local_cfg.get('api_base_url_mode', 'auto') or 'auto').lower()
    if force_mode:
        mode = str(force_mode).lower()

    current = _normalize_base_url(str(CONFIG.get('api_base_url', '') or ''))

    # Manual mode takes precedence
    if mode == 'manual':
        manual_url = _normalize_base_url(str(local_cfg.get('api_base_url', '') or ''))
        if manual_url:
            probe = _probe_api_source(manual_url, timeout=1.5)
            if probe.get('available'):
                _apply_api_base_url(manual_url)
                return manual_url

    # Probe all targets concurrently and select the available one with the lowest latency.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = []
    with ThreadPoolExecutor(max_workers=min(10, len(sources))) as ex:
        fut_map = {ex.submit(_probe_api_source, s['base_url'], 1.5): s for s in sources}
        for fut in as_completed(fut_map):
            src = fut_map[fut]
            try:
                probe = fut.result()
            except Exception as e:
                probe = {'available': False, 'latency_ms': None, 'status_code': None, 'error': str(e)}
            results.append({**src, **probe})

    # Sort by latency and select the fastest available node
    available = [r for r in results if r.get('available')]
    available.sort(key=lambda x: (x.get('latency_ms') or 999999))

    if available:
        best = available[0]['base_url']
        _apply_api_base_url(best)
        _write_local_config({'api_base_url_mode': 'auto', 'api_base_url': best})
        return best

    # If no items are available, return the current one (which may be empty)
    return current

def get_status():
    """Get current download status"""
    with status_lock:
        status = current_download_status.copy()
        # 获取并清空消息队列
        status['messages'] = current_download_status['messages'].copy()
        current_download_status['messages'] = []
        return status

def update_status(progress=None, message=None, **kwargs):
    """Update download status"""
    with status_lock:
        if progress is not None:
            current_download_status['progress'] = progress
        if message is not None:
            current_download_status['message'] = message
            # Add the message to the queue (for displaying the full log in the frontend)
            current_download_status['messages'].append(message)
            # Limit queue length to prevent memory overflow
            if len(current_download_status['messages']) > 100:
                current_download_status['messages'] = current_download_status['messages'][-50:]
        for key, value in kwargs.items():
            if key in current_download_status:
                current_download_status[key] = value

def download_worker():
    """Background download worker thread"""
    while True:
        try:
            task = download_queue.get(timeout=1)
            if task is None:
                break
            
            book_id = task.get('book_id')
            save_path = task.get('save_path', os.getcwd())
            file_format = task.get('file_format', 'txt')
            start_chapter = task.get('start_chapter', None)
            end_chapter = task.get('end_chapter', None)
            selected_chapters = task.get('selected_chapters', None)

            # If it is a queued task, update the current sequence number.
            queue_current = 0
            with status_lock:
                queue_total = int(current_download_status.get('queue_total', 0) or 0)
                queue_done = int(current_download_status.get('queue_done', 0) or 0)
                if queue_total > 0:
                    queue_current = min(queue_done + 1, queue_total)
                    current_download_status['queue_current'] = queue_current

            update_status(is_downloading=True, progress=0, message=t('web_init'))
            
            if not api:
                update_status(message=t('web_api_not_init'), progress=0, is_downloading=False)
                continue
            
            try:
                # Set progress callback
                def progress_callback(progress, message):
                    if progress >= 0:
                        update_status(progress=progress, message=message)
                    else:
                        update_status(message=message)
                
                # Force-refresh the API instance to prevent cross-thread session contamination.
                if hasattr(api_manager, '_tls'):
                    api_manager._tls = threading.local()
                
                # Retrieve book information
                update_status(message=t('web_connecting_book'))
                
                # Add a timeout retry mechanism
                book_detail = None
                for _ in range(3):
                    book_detail = api_manager.get_book_detail(book_id)
                    if book_detail:
                        break
                    time.sleep(1)
                
                if not book_detail:
                    update_status(message=t('web_book_info_fail_check'), is_downloading=False)
                    continue
                
                # Check for errors (such as books being removed from the catalog)
                if isinstance(book_detail, dict) and book_detail.get('_error'):
                    error_type = book_detail.get('_error')
                    if error_type == 'BOOK_REMOVE':
                        update_status(message='This book has been removed and is no longer available for download.', is_downloading=False)
                    else:
                        update_status(message=f'Failed to retrieve book information: {error_type}', is_downloading=False)
                    continue
                
                book_name = book_detail.get('book_name', book_id)
                update_status(book_name=book_name, message=t('web_preparing_download', book_name))
                
                # Execute download
                update_status(message=t('web_starting_engine'))
                success = api.run_download(book_id, save_path, file_format, start_chapter, end_chapter, selected_chapters, progress_callback)

                # Update queue progress
                has_more = False
                queue_total = 0
                queue_done = 0
                with status_lock:
                    queue_total = int(current_download_status.get('queue_total', 0) or 0)
                    if queue_total > 0:
                        queue_done = int(current_download_status.get('queue_done', 0) or 0)
                        queue_done = min(queue_done + 1, queue_total)
                        current_download_status['queue_done'] = queue_done
                        has_more = queue_done < queue_total

                if success:
                    if has_more:
                        update_status(
                            progress=0,
                            message=t('web_queue_next', queue_done, queue_total),
                            is_downloading=True,
                            queue_current=min(queue_done + 1, queue_total)
                        )
                    else:
                        if queue_total > 0:
                            update_status(
                                progress=100,
                                message=t('web_queue_complete', queue_total, save_path),
                                is_downloading=False,
                                queue_total=0,
                                queue_done=0,
                                queue_current=0
                            )
                        else:
                            update_status(progress=100, message=t('web_download_success_path', save_path), is_downloading=False)
                else:
                    if has_more:
                        update_status(
                            progress=0,
                            message=t('web_queue_next_fail', queue_done, queue_total),
                            is_downloading=True,
                            queue_current=min(queue_done + 1, queue_total)
                        )
                    else:
                        if queue_total > 0:
                            update_status(
                                progress=0,
                                message=t('web_queue_complete_fail', queue_total, save_path),
                                is_downloading=False,
                                queue_total=0,
                                queue_done=0,
                                queue_current=0
                            )
                        else:
                            update_status(message=t('web_download_interrupted'), progress=0, is_downloading=False)
                    
            except Exception as e:
                import traceback
                traceback.print_exc()
                error_str = str(e)
                update_status(message=t('web_download_exception', error_str), progress=0, is_downloading=False)
                print(f"Download error: {error_str}")
        
        except queue.Empty:
            continue
        except Exception as e:
            error_str = str(e)
            update_status(message=t('web_worker_error', error_str), progress=0, is_downloading=False)
            print(f"Worker thread exception: {error_str}")

# Start background download thread
download_thread = threading.Thread(target=download_worker, daemon=True)
download_thread.start()

# ===================== Access Control Middleware =====================

@app.before_request
def check_access():
    """Validate the access token before the request"""
    # Static files do not require authentication.
    if request.path.startswith('/static/'):
        return None
    
    # Verify token
    if ACCESS_TOKEN is not None:
        token = request.args.get('token') or request.headers.get('X-Access-Token')
        if token != ACCESS_TOKEN:
            return jsonify({'error': 'Forbidden'}), 403
    
    return None

# ===================== API 路由 =====================

@app.route('/')
def index():
    """Home page"""
    from config import __version__
    token = request.args.get('token', '')
    return render_template('index.html', version=__version__, access_token=token)

@app.route('/api/init', methods=['POST'])
def api_init():
    """Initialization module (skips node detection and is called separately by the front end /api/api-sources）"""
    if init_modules(skip_api_select=True):
        return jsonify({'success': True, 'message': t('web_module_loaded')})
    return jsonify({'success': False, 'message': t('web_module_fail_msg')}), 500

@app.route('/api/version', methods=['GET'])
def api_version():
    """Get the current version number"""
    from config import __version__
    return jsonify({'success': True, 'version': __version__})

@app.route('/api/status', methods=['GET'])
def api_status():
    """Get download status"""
    return jsonify(get_status())


@app.route('/api/api-sources', methods=['GET'])
def api_api_sources():
    """Retrieve a list of available download endpoints and return the results of availability probes (performed concurrently)."""
    from config import CONFIG
    from concurrent.futures import ThreadPoolExecutor, as_completed

    local_cfg = _read_local_config()
    mode = str(local_cfg.get('api_base_url_mode', 'auto') or 'auto').lower()
    sources = _get_api_sources()

    # Probe all nodes concurrently
    timeout = min(float(CONFIG.get('request_timeout', 10) or 10), 2.0)
    probed = []
    with ThreadPoolExecutor(max_workers=min(10, len(sources) or 1)) as ex:
        fut_map = {ex.submit(_probe_api_source, s['base_url'], timeout): s for s in sources}
        for fut in as_completed(fut_map):
            src = fut_map[fut]
            try:
                probe = fut.result()
            except Exception as e:
                probe = {'available': False, 'latency_ms': None, 'status_code': None, 'error': str(e)}
            probed.append({**src, **probe})

    # Sort by latency
    probed.sort(key=lambda x: (not x.get('available'), x.get('latency_ms') or 999999))

    # Select the fastest available node in automatic mode
    current = _normalize_base_url(str(CONFIG.get('api_base_url', '') or ''))
    if mode == 'auto':
        available = [p for p in probed if p.get('available')]
        if available:
            best = available[0]['base_url']
            if best != current:
                _apply_api_base_url(best)
                _write_local_config({'api_base_url_mode': 'auto', 'api_base_url': best})
            current = best

    return jsonify({
        'success': True,
        'mode': mode,
        'current': current,
        'sources': probed
    })


@app.route('/api/api-sources/select', methods=['POST'])
def api_api_sources_select():
    """Select a download interface (manual/auto), with automatic availability detection upon selection."""
    data = request.get_json() or {}
    mode = str(data.get('mode', 'auto') or 'auto').lower()

    if mode not in ['auto', 'manual']:
        mode = 'auto'

    if mode == 'auto':
        selected = _ensure_api_base_url(force_mode='auto')
        if not selected:
            return jsonify({'success': False, 'message': 'No available interface found'}), 500
        _write_local_config({'api_base_url_mode': 'auto', 'api_base_url': selected})
        return jsonify({'success': True, 'mode': 'auto', 'current': selected})

    # manual
    base_url = _normalize_base_url(str(data.get('base_url', '') or ''))
    if not base_url:
        return jsonify({'success': False, 'message': 'base_url required'}), 400

    probe = _probe_api_source(base_url)
    if not probe.get('available'):
        err = probe.get('error') or 'unavailable'
        return jsonify({'success': False, 'message': f'Interface unavailable: {base_url} ({err})', 'probe': probe}), 400

    _apply_api_base_url(base_url)
    _write_local_config({'api_base_url_mode': 'manual', 'api_base_url': base_url})

    return jsonify({'success': True, 'mode': 'manual', 'current': base_url, 'probe': probe})

@app.route('/api/search', methods=['POST'])
def api_search():
    """Search for books"""
    data = request.get_json()
    keyword = data.get('keyword', '').strip()
    offset = data.get('offset', 0)
    
    if not keyword:
        return jsonify({'success': False, 'message': t('web_search_keyword_empty')}), 400
    
    if not api_manager:
        return jsonify({'success': False, 'message': t('web_api_not_init')}), 500
    
    try:
        result = api_manager.search_books(keyword, offset)
        if result and result.get('data'):
            # Analyze search results
            search_data = result.get('data', {})
            books = []
            has_more = False
            
            # New API Data Structure: data.search_tabs[].data[].book_data[]
            # Need to find the tab with tab_type=3 (books)
            search_tabs = search_data.get('search_tabs', [])
            for tab in search_tabs:
                if tab.get('tab_type') == 3:  # Books tab
                    has_more = tab.get('has_more', False)
                    tab_data = tab.get('data', [])
                    if isinstance(tab_data, list):
                        for item in tab_data:
                            # Each item contains a `book_data` array.
                            book_data_list = item.get('book_data', [])
                            for book in book_data_list:
                                if isinstance(book, dict):
                                    # Parse the character count (may be a string)
                                    word_count = book.get('word_number', 0) or book.get('word_count', 0)
                                    if isinstance(word_count, str):
                                        try:
                                            word_count = int(word_count)
                                        except:
                                            word_count = 0
                                    
                                    # Number of chapters to parse
                                    chapter_count = book.get('serial_count', 0) or book.get('chapter_count', 0)
                                    if isinstance(chapter_count, str):
                                        try:
                                            chapter_count = int(chapter_count)
                                        except:
                                            chapter_count = 0
                                    
                                    # Parsing status (0 = completed, 1 = ongoing, 2 = finished)
                                    status_code = book.get('creation_status', '')
                                    # Convert to a string for comparison
                                    status_code_str = str(status_code) if status_code is not None else ''
                                    if status_code_str == '0':
                                        status = t('dl_status_finished')
                                    elif status_code_str == '1':
                                        status = t('dl_status_serializing')
                                    elif status_code_str == '2':
                                        status = t('dl_status_completed_2')
                                    else:
                                        status = ''
                                    
                                    books.append({
                                        'book_id': str(book.get('book_id', '')),
                                        'book_name': book.get('book_name', t('dl_unknown_book')),
                                        'author': book.get('author', t('dl_unknown_author')),
                                        'abstract': book.get('abstract', '') or book.get('book_abstract_v2', t('dl_no_intro')),
                                        'cover_url': book.get('thumb_url', '') or book.get('cover', ''),
                                        'word_count': word_count,
                                        'chapter_count': chapter_count,
                                        'status': status,
                                        'category': book.get('category', '') or book.get('genre', '')
                                    })
                    break  # Exit after locating the Books tab
            
            return jsonify({
                'success': True,
                'data': {
                    'books': books,
                    'total': len(books),
                    'offset': offset,
                    'has_more': has_more
                }
            })
        else:
            return jsonify({
                'success': True,
                'data': {
                    'books': [],
                    'total': 0,
                    'offset': offset,
                    'has_more': False
                }
            })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': t('web_search_fail', str(e))}), 500

@app.route('/api/book-info', methods=['POST'])
def api_book_info():
    """Retrieve book details and chapter list"""
    print(f"[DEBUG] Received book-info request: {request.data}")
    data = request.get_json()
    book_id = data.get('book_id', '').strip()
    
    if not book_id:
        return jsonify({'success': False, 'message': t('web_book_id_empty')}), 400
    
    # Extract ID from URL
    if 'fanqienovel.com' in book_id:
        match = re.search(r'/page/(\d+)', book_id)
        if match:
            book_id = match.group(1)
        else:
            return jsonify({'success': False, 'message': t('web_url_error')}), 400
    
    # Verify that book_id is a number
    if not book_id.isdigit():
        return jsonify({'success': False, 'message': t('web_id_not_digit')}), 400
    
    if not api:
        return jsonify({'success': False, 'message': t('web_api_not_init')}), 500
    
    try:
        # Retrieve book information
        print(f"[DEBUG] calling get_book_detail for {book_id}")
        book_detail = api_manager.get_book_detail(book_id)
        print(f"[DEBUG] book_detail result: {str(book_detail)[:100]}")
        if not book_detail:
            return jsonify({'success': False, 'message': t('web_book_info_fail')}), 400
        
        # Check for errors (such as books being removed from the catalog)
        if isinstance(book_detail, dict) and book_detail.get('_error'):
            error_type = book_detail.get('_error')
            if error_type == 'BOOK_REMOVE':
                return jsonify({'success': False, 'message': 'This book has been removed and is no longer available for download.'}), 400
            return jsonify({'success': False, 'message': f'Failed to retrieve book information: {error_type}'}), 400
        
        # Get chapter list
        print(f"[DEBUG] calling get_chapter_list for {book_id}")
        chapters_data = api_manager.get_chapter_list(book_id)
        print(f"[DEBUG] chapters_data type: {type(chapters_data)}")
        if not chapters_data:
            return jsonify({'success': False, 'message': t('web_chapter_list_fail')}), 400
        
        chapters = []
        if isinstance(chapters_data, dict):
            all_item_ids = chapters_data.get("allItemIds", [])
            chapter_list = chapters_data.get("chapterListWithVolume", [])
            
            if chapter_list:
                idx = 0
                for volume in chapter_list:
                    if isinstance(volume, list):
                        for ch in volume:
                            if isinstance(ch, dict):
                                item_id = ch.get("itemId") or ch.get("item_id")
                                title = ch.get("title", t("dl_chapter_title", idx+1))
                                if item_id:
                                    chapters.append({"id": str(item_id), "title": title, "index": idx})
                                    idx += 1
            else:
                for idx, item_id in enumerate(all_item_ids):
                    chapters.append({"id": str(item_id), "title": t("dl_chapter_title", idx+1), "index": idx})
        elif isinstance(chapters_data, list):
            for idx, ch in enumerate(chapters_data):
                item_id = ch.get("item_id") or ch.get("chapter_id")
                title = ch.get("title", t("dl_chapter_title", idx+1))
                if item_id:
                    chapters.append({"id": str(item_id), "title": title, "index": idx})
        
        print(f"[DEBUG] Found {len(chapters)} chapters")

        # Return book information and the list of chapters
        return jsonify({
            'success': True,
            'data': {
                'book_id': book_id,
                'book_name': book_detail.get('book_name', t('dl_unknown_book')),
                'author': book_detail.get('author', t('dl_unknown_author')),
                'abstract': book_detail.get('abstract', t('dl_no_intro')),
                'cover_url': book_detail.get('thumb_url', ''),
                'chapters': chapters
            }
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': t('web_get_info_fail', str(e))}), 500

@app.route('/api/download', methods=['POST'])
def api_download():
    """Start download"""
    data = request.get_json()
    
    if get_status()['is_downloading']:
        return jsonify({'success': False, 'message': t('web_download_exists')}), 400
    
    book_id = data.get('book_id', '').strip()
    save_path = data.get('save_path', get_default_download_path()).strip()
    file_format = data.get('file_format', 'txt')
    start_chapter = data.get('start_chapter')
    end_chapter = data.get('end_chapter')
    selected_chapters = data.get('selected_chapters')
    
    if not book_id:
        return jsonify({'success': False, 'message': t('web_book_id_empty')}), 400
    
    # Extract ID from URL
    if 'fanqienovel.com' in book_id:
        match = re.search(r'/page/(\d+)', book_id)
        if match:
            book_id = match.group(1)
        else:
            return jsonify({'success': False, 'message': t('web_url_error')}), 400
    
    # Verify that book_id is a number
    if not book_id.isdigit():
        return jsonify({'success': False, 'message': t('web_id_not_digit')}), 400
    
    # Ensure the path exists
    try:
        os.makedirs(save_path, exist_ok=True)
    except Exception as e:
        return jsonify({'success': False, 'message': t('web_save_path_error', str(e))}), 400
    
    # Add to download queue
    task = {
        'book_id': book_id,
        'save_path': save_path,
        'file_format': file_format,
        'start_chapter': start_chapter,
        'end_chapter': end_chapter,
        'selected_chapters': selected_chapters
    }
    download_queue.put(task)
    update_status(is_downloading=True, progress=0, message=t('web_task_added'))
    
    return jsonify({'success': True, 'message': t('web_task_started')})


@app.route('/api/queue/start', methods=['POST'])
def api_queue_start():
    """Add to download queue and start downloading (batch enqueue)"""
    data = request.get_json() or {}

    if get_status()['is_downloading']:
        return jsonify({'success': False, 'message': t('web_download_exists')}), 400

    tasks = data.get('tasks', [])
    save_path = str(data.get('save_path', get_default_download_path())).strip()
    file_format = str(data.get('file_format', 'txt')).strip().lower()

    if not tasks or not isinstance(tasks, list):
        return jsonify({'success': False, 'message': t('web_provide_ids')}), 400

    if file_format not in ['txt', 'epub']:
        file_format = 'txt'

    # Ensure the path exists
    try:
        os.makedirs(save_path, exist_ok=True)
    except Exception as e:
        return jsonify({'success': False, 'message': t('web_save_path_error', str(e))}), 400

    # Clear the old queue (for safety)
    try:
        while True:
            download_queue.get_nowait()
    except queue.Empty:
        pass

    cleaned_tasks = []
    for task in tasks:
        if not isinstance(task, dict):
            continue

        book_id = str(task.get('book_id', '')).strip()
        if not book_id:
            continue

        # 从URL中提取ID
        if 'fanqienovel.com' in book_id:
            match = re.search(r'/page/(\d+)', book_id)
            if match:
                book_id = match.group(1)
            else:
                continue

        if not book_id.isdigit():
            continue

        start_chapter = task.get('start_chapter')
        end_chapter = task.get('end_chapter')
        selected_chapters = task.get('selected_chapters')

        # Chapter ranges are 1-based (consistent with the downloader).
        try:
            if start_chapter is not None:
                start_chapter = int(start_chapter)
                if start_chapter <= 0:
                    start_chapter = None
            if end_chapter is not None:
                end_chapter = int(end_chapter)
                if end_chapter <= 0:
                    end_chapter = None
        except Exception:
            start_chapter = None
            end_chapter = None

        if selected_chapters is not None:
            try:
                if isinstance(selected_chapters, list):
                    selected_chapters = [int(x) for x in selected_chapters]
                else:
                    selected_chapters = None
            except Exception:
                selected_chapters = None

        cleaned_tasks.append({
            'book_id': book_id,
            'save_path': save_path,
            'file_format': file_format,
            'start_chapter': start_chapter,
            'end_chapter': end_chapter,
            'selected_chapters': selected_chapters
        })

    if not cleaned_tasks:
        return jsonify({'success': False, 'message': t('web_no_valid_ids')}), 400

    # Set queue status and enqueue in batches
    update_status(
        is_downloading=True,
        progress=0,
        message=t('web_queue_submitted', len(cleaned_tasks)),
        book_name='',
        queue_total=len(cleaned_tasks),
        queue_done=0,
        queue_current=1
    )
    for task in cleaned_tasks:
        download_queue.put(task)

    return jsonify({'success': True, 'count': len(cleaned_tasks)})


@app.route('/api/queue/status', methods=['GET'])
def api_queue_status():
    """Retrieve Queue Status

    Returns detailed information about all tasks in the queue, including their status, progress, etc.
    """
    status = task_manager.get_queue_status()
    return jsonify({
        'success': True,
        'data': status
    })


@app.route('/api/queue/skip', methods=['POST'])
def api_queue_skip():
    """Skip current task

    Stop the download of the current book and begin downloading the next book in the queue
    """
    result = task_manager.skip_current()
    if result:
        return jsonify({
            'success': True,
            'message': 'Current task skipped'
        })
    else:
        return jsonify({
            'success': False,
            'message': 'Cannot skip: No tasks are currently downloading'
        }), 400


@app.route('/api/queue/retry', methods=['POST'])
def api_queue_retry():
    """Retry a specific task or all failed tasks

    Request body:
    - task_id: ID of a specific task (optional)
    - retry_all: Whether to retry all failed tasks (optional)
    """
    data = request.get_json() or {}
    task_id = data.get('task_id')
    retry_all = data.get('retry_all', False)
    
    if retry_all:
        count = task_manager.retry_all_failed()
        if count > 0:
            return jsonify({
                'success': True,
                'message': f'{count} failed task(s) require a retry',
                'count': count
            })
        else:
            return jsonify({
                'success': False,
                'message': 'No failed tasks require a retry.'
            }), 400
    elif task_id:
        result = task_manager.retry_task(task_id)
        if result:
            return jsonify({
                'success': True,
                'message': 'Task has been reset; awaiting re-download'
            })
        else:
            return jsonify({
                'success': False,
                'message': 'Cannot retry: the task does not exist or its status is not "failed."'
            }), 400
    else:
        return jsonify({
            'success': False,
            'message': 'Please provide a task_id or set retry_all=true'
        }), 400


@app.route('/api/queue/force-save', methods=['POST'])
def api_queue_force_save():
    """Force-save current progress

    Save the currently downloaded chapter content to a file
    """
    result = task_manager.force_save()
    if result['success']:
        return jsonify(result)
    else:
        return jsonify(result), 400


@app.route('/api/download/resume-check', methods=['POST'])
def api_download_resume_check():
    """Check for a resumable download state

    Request body:
    - book_id: Book ID
    
    Response:
    - has_saved_state: Whether a saved download state exists
    - downloaded_chapters: Number of chapters already downloaded
    - total_chapters: Total number of chapters (if known)
    """
    data = request.get_json() or {}
    book_id = str(data.get('book_id', '')).strip()
    
    if not book_id:
        return jsonify({
            'success': False,
            'message': '请提供 book_id'
        }), 400
    
    # Import state-loading functions from novel_downloader
    try:
        from novel_downloader import load_status, _get_status_file_path
        
        downloaded_ids = load_status(book_id)
        has_saved_state = len(downloaded_ids) > 0
        
        return jsonify({
            'success': True,
            'data': {
                'has_saved_state': has_saved_state,
                'downloaded_chapters': len(downloaded_ids),
                'book_id': book_id
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Failed to check download status: {str(e)}'
        }), 500


@app.route('/api/cancel', methods=['POST'])
def api_cancel():
    """Cancel download"""
    if downloader_instance:
        try:
            downloader_instance.cancel_download()
            update_status(is_downloading=False, progress=0, message=t('web_batch_cancelled_msg'))
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)}), 400
    return jsonify({'success': False}), 400

# ===================== Batch Download Status =====================
batch_download_status = {
    'is_downloading': False,
    'current_index': 0,
    'total_count': 0,
    'current_book': '',
    'results': [],
    'message': ''
}
batch_lock = threading.Lock()

def get_batch_status():
    """Get batch download status"""
    with batch_lock:
        return batch_download_status.copy()

def update_batch_status(**kwargs):
    """Update batch download status"""
    with batch_lock:
        for key, value in kwargs.items():
            if key in batch_download_status:
                batch_download_status[key] = value

def batch_download_worker(book_ids, save_path, file_format):
    """Batch download worker threads"""
    from novel_downloader import batch_downloader
    
    def progress_callback(current, total, book_name, status, message):
        update_batch_status(
            current_index=current,
            total_count=total,
            current_book=book_name,
            message=f'[{current}/{total}] {book_name}: {message}'
        )
    
    try:
        update_batch_status(
            is_downloading=True,
            current_index=0,
            total_count=len(book_ids),
            results=[],
            message='Start batch download...'
        )
        
        result = batch_downloader.run_batch(
            book_ids, save_path, file_format,
            progress_callback=progress_callback,
            delay_between_books=2.0
        )
        
        update_batch_status(
            is_downloading=False,
            results=result.get('results', []),
            message=f"✅ Batch download complete: {result['message']}"
        )
        
    except Exception as e:
        update_batch_status(
            is_downloading=False,
            message=f'❌ Batch download failed: {str(e)}'
        )

@app.route('/api/batch-download', methods=['POST'])
def api_batch_download():
    """Start batch download"""
    data = request.get_json()
    
    if get_batch_status()['is_downloading']:
        return jsonify({'success': False, 'message': t('web_batch_running')}), 400
    
    book_ids = data.get('book_ids', [])
    save_path = data.get('save_path', get_default_download_path()).strip()
    file_format = data.get('file_format', 'txt')
    
    if not book_ids:
        return jsonify({'success': False, 'message': t('web_provide_ids')}), 400
    
    # Clean and validate book_ids
    cleaned_ids = []
    for bid in book_ids:
        bid = str(bid).strip()
        # 从URL提取ID
        if 'fanqienovel.com' in bid:
            match = re.search(r'/page/(\d+)', bid)
            if match:
                bid = match.group(1)
        if bid.isdigit():
            cleaned_ids.append(bid)
    
    if not cleaned_ids:
        return jsonify({'success': False, 'message': t('web_no_valid_ids')}), 400
    
    # Ensure the save directory exists
    os.makedirs(save_path, exist_ok=True)
    
    # Start batch download threads
    t = threading.Thread(
        target=batch_download_worker,
        args=(cleaned_ids, save_path, file_format),
        daemon=True
    )
    t.start()
    
    return jsonify({
        'success': True,
        'message': t('web_batch_start_count', len(cleaned_ids)),
        'count': len(cleaned_ids)
    })

@app.route('/api/batch-status', methods=['GET'])
def api_batch_status():
    """Get batch download status"""
    return jsonify(get_batch_status())

@app.route('/api/batch-cancel', methods=['POST'])
def api_batch_cancel():
    """Cancel batch download"""
    from novel_downloader import batch_downloader
    
    try:
        batch_downloader.cancel()
        update_batch_status(
            is_downloading=False,
            message=t('web_batch_cancelled_msg')
        )
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/language', methods=['GET', 'POST'])
def api_language():
    """Get/set language configuration"""
    from locales import get_current_lang, set_current_lang
    
    if request.method == 'GET':
        return jsonify({'language': get_current_lang()})
    else:
        data = request.get_json()
        lang = data.get('language', 'zh')
        if lang not in ['zh', 'en']:
            lang = 'zh'
        if set_current_lang(lang):
            return jsonify({'success': True, 'language': lang})
        else:
            return jsonify({'success': False, 'message': 'Failed to save language'}), 500

@app.route('/api/config/save-path', methods=['GET', 'POST'])
def api_config_save_path():
    """Retrieve/save download path configuration"""
    
    if request.method == 'GET':
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    return jsonify({'path': config.get('save_path', get_default_download_path())})
        except:
            pass
        return jsonify({'path': get_default_download_path()})
    
    else:
        data = request.get_json()
        path = data.get('path', get_default_download_path())
        
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            else:
                config = {}
            
            config['save_path'] = path
            
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/list-directory', methods=['POST'])
def api_list_directory():
    """List the contents of the specified directory"""
    try:
        data = request.get_json() or {}
        path = data.get('path', '')
        
        # If no path is specified, the default download path is used.
        if not path:
            path = get_default_download_path()
        
        # Standardization Path
        path = os.path.normpath(os.path.expanduser(path))
    
        # Check if the path exists
        if not os.path.exists(path):
            return jsonify({
                'success': False,
                'message': '目录不存在'
            })
        
        # Check if it is a directory
        if not os.path.isdir(path):
            return jsonify({
                'success': False,
                'message': '路径不是目录'
            })
    
        # Get directory list
        directories = []
        for item in os.listdir(path):
            item_path = os.path.join(path, item)
            if os.path.isdir(item_path):
                directories.append({
                    'name': item,
                    'path': item_path
                })
        
        # Sort by name
        directories.sort(key=lambda x: x['name'].lower())
        
        # Get parent directory
        parent_path = os.path.dirname(path)
        is_root = (parent_path == path) or (path in ['/', '\\'])
        
        # Windows List of drives
        drives = []
        if os.name == 'nt':
            import string
            for letter in string.ascii_uppercase:
                drive = f'{letter}:\\'
                if os.path.exists(drive):
                    drives.append({
                        'name': f'{letter}:',
                        'path': drive
                    })
        
        # Shortcut Paths (Frequently Used User Folders)
        quick_paths = []
        home = os.path.expanduser('~')
        
        # Windows Special folders
        if os.name == 'nt':
            shell_folders = [
                ('Desktop', 'Desktop', 'line-md:computer'),
                ('Downloads', 'Downloads', 'line-md:download-loop'),
                ('Documents', 'Documents', 'line-md:document'),
                ('Pictures', 'Pictures', 'line-md:image'),
                ('Music', 'Music', 'line-md:play'),
                ('Videos', 'Videos', 'line-md:play-filled'),
            ]
            for name, folder, icon in shell_folders:
                folder_path = os.path.join(home, folder)
                if os.path.exists(folder_path):
                    quick_paths.append({
                        'name': name,
                        'path': folder_path,
                        'icon': icon
                    })
        else:
            # Linux/macOS
            unix_folders = [
                ('Desktop', 'Desktop', 'line-md:computer'),
                ('Downloads', 'Downloads', 'line-md:download-loop'),
                ('Documents', 'Documents', 'line-md:document'),
                ('Pictures', 'Pictures', 'line-md:image'),
                ('Music', 'Music', 'line-md:play'),
                ('Videos', 'Videos', 'line-md:play-filled'),
            ]
            for name, folder, icon in unix_folders:
                folder_path = os.path.join(home, folder)
                if os.path.exists(folder_path):
                    quick_paths.append({
                        'name': name,
                        'path': folder_path,
                        'icon': icon
                    })
        
        return jsonify({
            'success': True,
            'data': {
                'current_path': path,
                'parent_path': parent_path if not is_root else None,
                'directories': directories,
                'is_root': is_root,
                'drives': drives if os.name == 'nt' else None,
                'quick_paths': quick_paths
            }
        })
    except PermissionError:
        return jsonify({
            'success': False,
            'message': 'No permission to access this directory'
        })
    except Exception as e:
        import traceback
        print(f"[ERROR] list-directory: {e}")
        traceback.print_exc()
        return jsonify({
            'success': False,
            'message': f'Failed to load directory: {str(e)}'
        })


@app.route('/api/select-folder', methods=['POST'])
def api_select_folder():
    """Save the selected folder path"""
    data = request.get_json() or {}
    selected_path = data.get('path', '')
    
    if not selected_path:
        return jsonify({'success': False, 'message': 'No folder selected'})
    
    # Verify that the path exists and is a directory.
    if not os.path.exists(selected_path) or not os.path.isdir(selected_path):
        return jsonify({'success': False, 'message': 'Invalid directory path'})
    
    # Save the selected path to the configuration
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
        else:
            config = {}
        
        config['save_path'] = selected_path
        
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        
        return jsonify({'success': True, 'path': selected_path})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/check-update', methods=['GET'])
def api_check_update():
    """Check for updates"""
    try:
        import sys
        
        # Does not check for updates when running from source code
        if not getattr(sys, 'frozen', False):
            return jsonify({
                'success': True,
                'has_update': False,
                'is_source': True,
                'message': 'Source code execution mode; does not check for updates'
            })
        
        from updater import check_and_notify
        from config import __version__, __github_repo__
        
        update_info = check_and_notify(__version__, __github_repo__, silent=True)
        
        if update_info:
            return jsonify({
                'success': True,
                'has_update': update_info.get('has_update', False),
                'data': update_info
            })
        else:
            return jsonify({
                'success': True,
                'has_update': False
            })
    except Exception as e:
        return jsonify({'success': False, 'message': t('web_check_update_fail', str(e))}), 500

@app.route('/api/get-update-assets', methods=['GET'])
def api_get_update_assets():
    """Get download options for update files"""
    try:
        from updater import get_latest_release, parse_release_assets
        from config import __github_repo__
        import platform
        
        # Get the latest version information
        latest_info = get_latest_release(__github_repo__)
        if not latest_info:
            return jsonify({'success': False, 'message': 'Unable to retrieve version information'}), 500
        
        # Detect the current platform
        system = platform.system().lower()
        if system == 'darwin':
            platform_name = 'macos'
        elif system == 'linux':
            platform_name = 'linux'
        else:
            platform_name = 'windows'
        
        # Parsing assets
        assets = parse_release_assets(latest_info, platform_name)
        
        return jsonify({
            'success': True,
            'platform': platform_name,
            'assets': assets,
            'release_url': latest_info.get('html_url', '')
        })
    except Exception as e:
        return jsonify({'success': False, 'message': f'Failed to retrieve download options: {str(e)}'}), 500

@app.route('/api/download-update', methods=['POST'])
def api_download_update():
    """Starting download of update package"""
    data = request.get_json()
    url = data.get('url')
    filename = data.get('filename')
    
    if not url or not filename:
        return jsonify({'success': False, 'message': 'Parameter error'}), 400
        
    # Use the default download path or the configured path
    save_path = get_default_download_path()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                save_path = config.get('save_path', save_path)
        except:
            pass
            
    if not os.path.exists(save_path):
        try:
            os.makedirs(save_path)
        except:
            save_path = get_default_download_path()

    # Start download thread
    t = threading.Thread(
        target=update_download_worker, 
        args=(url, save_path, filename),
        daemon=True
    )
    t.start()
    
    return jsonify({'success': True, 'message': 'Start download'})

@app.route('/api/update-status', methods=['GET'])
def api_get_update_status_route():
    """Retrieve download status updates"""
    return jsonify(get_update_status())

@app.route('/api/can-auto-update', methods=['GET'])
def api_can_auto_update():
    """Check for support for automatic updates"""
    try:
        from updater import can_auto_update
        return jsonify({
            'success': True,
            'can_auto_update': can_auto_update()
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/apply-update', methods=['POST'])
def api_apply_update():
    """Apply downloaded updates (supports Windows/Linux/macOS)"""
    print('[DEBUG] api_apply_update called')
    try:
        from updater import apply_update, can_auto_update
        import sys
        
        print(f'[DEBUG] sys.frozen: {getattr(sys, "frozen", False)}')
        print(f'[DEBUG] sys.executable: {sys.executable}')
        
        # Check for support for automatic updates
        can_update = can_auto_update()
        print(f'[DEBUG] can_auto_update: {can_update}')
        if not can_update:
            return jsonify({
                'success': False, 
                'message': t('web_auto_update_unsupported')
            }), 400
        
        # Retrieve information about downloaded update files
        status = get_update_status()
        print(f'[DEBUG] update_status: {status}')
        if not status.get('completed'):
            return jsonify({
                'success': False, 
                'message': t('web_update_not_ready')
            }), 400
        
        # Use a temporary file path
        new_file_path = status.get('temp_file_path', '')
        print(f'[DEBUG] temp_file_path: {new_file_path}')
        
        print(f'[DEBUG] new_file_path: {new_file_path}')
        
        if not new_file_path:
            return jsonify({
                'success': False, 
                'message': t('web_update_info_incomplete')
            }), 400
        
        print(f'[DEBUG] file exists: {os.path.exists(new_file_path)}')
        
        if not os.path.exists(new_file_path):
            return jsonify({
                'success': False, 
                'message': t('web_update_file_missing', new_file_path)
            }), 400
        
        print(f'[DEBUG] file size: {os.path.getsize(new_file_path)} bytes')
        
        # Application Updates (Automated Detection Platform)
        print('[DEBUG] Calling apply_update...')
        if apply_update(new_file_path):
            # Update successfully initiated; preparing to exit the program.
            # Wait long enough to ensure the update script has launched and begun monitoring the process.
            def delayed_exit():
                import time
                print('[DEBUG] Waiting for update script to start...')
                time.sleep(3)  # Allow sufficient startup time for the update script.
                print('[DEBUG] Exiting application for update...')
                os._exit(0)
            
            # Use non-daemon threads to ensure that exit logic can complete.
            exit_thread = threading.Thread(target=delayed_exit, daemon=False)
            exit_thread.start()
            
            return jsonify({
                'success': True, 
                'message': t('web_update_start_success')
            })
        else:
            return jsonify({
                'success': False, 
                'message': t('web_update_start_fail')
            }), 500
            
    except Exception as e:
        return jsonify({'success': False, 'message': t('web_apply_update_fail', str(e))}), 500

@app.route('/api/open-folder', methods=['POST'])
def api_open_folder():
    """Open folder"""
    data = request.get_json()
    path = data.get('path')
    
    if not path or not os.path.exists(path):
        return jsonify({'success': False, 'message': t('web_path_not_exist')}), 400
        
    try:
        if os.name == 'nt':
            os.startfile(path)
        elif os.name == 'posix':
            subprocess.call(['open', path])
        else:
            subprocess.call(['xdg-open', path])
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

if __name__ == '__main__':
    print(f'Configuration file location: {CONFIG_FILE}')
    webapp_port
    print(t('web_server_started')+f'on port {webapp_port}')
    app.run(host='0.0.0.0', port=webapp_port, debug=False)
