"""
Streaming utilities for SSH output in ilha CLI.

This module provides reusable streaming logic for SSH command output
with support for verbose and non-verbose modes.
"""

import subprocess
import threading
import time
from typing import Optional, Callable, List
from contextlib import contextmanager

from .logging import log_info, log_error, log_success, is_verbose


@contextmanager
def stream_ssh_output(process: subprocess.Popen, 
                     timeout: Optional[int] = None,
                     progress_interval: int = 30,
                     prefix: str = "",
                     filter_keywords: Optional[List[str]] = None):
    """Context manager for streaming SSH command output.
    
    Args:
        process: Subprocess Popen object
        timeout: Maximum execution time in seconds
        progress_interval: Seconds between progress updates (non-verbose mode)
        prefix: Prefix for log messages
        filter_keywords: Keywords to filter output in non-verbose mode
        
    Yields:
        Tuple of (stdout_lines, stderr_lines, return_code)
    """
    verbose = is_verbose()
    stdout_lines: List[str] = []
    stderr_lines: List[str] = []
    stdout_done = threading.Event()
    stderr_done = threading.Event()
    output_lock = threading.Lock()
    
    def read_stdout():
        """Read stdout lines."""
        try:
            for line in iter(process.stdout.readline, ''):
                if line:
                    line = line.rstrip()
                    with output_lock:
                        stdout_lines.append(line)
                    if verbose:
                        log_info(f"{prefix}{line}")
                    elif filter_keywords:
                        # Show lines with important keywords
                        line_lower = line.lower()
                        if any(keyword in line_lower for keyword in filter_keywords):
                            log_info(f"{prefix}{line}")
        finally:
            stdout_done.set()
    
    def read_stderr():
        """Read stderr lines and log them appropriately."""
        try:
            for line in iter(process.stderr.readline, ''):
                if line:
                    line = line.rstrip()
                    with output_lock:
                        stderr_lines.append(line)
                    # Check if line contains error indicators
                    line_lower = line.lower()
                    if any(indicator in line_lower for indicator in ['✗', 'error', 'failed', 'fatal', 'exception']):
                        log_error(f"{prefix}{line}")
                    elif any(indicator in line for indicator in ['✓', 'SUCCESS', 'successfully']):
                        if verbose:
                            log_success(f"{prefix}{line}")
                        else:
                            log_info(f"{prefix}{line}")
                    else:
                        # Informational message (log, docker ps output, etc.)
                        if verbose:
                            log_info(f"{prefix}{line}")
                        # In non-verbose mode, only show errors
                        # (informational stderr is suppressed)
        finally:
            stderr_done.set()
    
    # Start reading threads
    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    
    # Monitor process with timeout
    start_time = time.time()
    last_progress_time = start_time
    
    try:
        while process.poll() is None:
            elapsed = time.time() - start_time
            
            # Check timeout
            if timeout and elapsed > timeout:
                log_error(f"Process timed out after {timeout}s")
                process.kill()
                process.wait()
                yield stdout_lines, stderr_lines, process.returncode
                return
            
            # Show progress in non-verbose mode
            if not verbose and time.time() - last_progress_time > progress_interval:
                elapsed_min = int(elapsed / 60)
                elapsed_sec = int(elapsed % 60)
                log_info(f"{prefix}Still running... ({elapsed_min}m {elapsed_sec}s elapsed)")
                last_progress_time = time.time()
            
            time.sleep(1)
        
        # Wait for threads to finish reading
        stdout_done.wait(timeout=10)
        stderr_done.wait(timeout=10)
        
        yield stdout_lines, stderr_lines, process.returncode
        
    finally:
        # Ensure threads are done
        stdout_done.wait(timeout=5)
        stderr_done.wait(timeout=5)


def execute_with_streaming(ssh_cmd: List[str], script: Optional[str] = None,
                          timeout: Optional[int] = None,
                          progress_interval: int = 30,
                          prefix: str = "",
                          filter_keywords: Optional[List[str]] = None) -> tuple:
    """Execute SSH command with streaming output.
    
    Args:
        ssh_cmd: SSH command as list
        script: Script content to send via stdin (optional)
        timeout: Maximum execution time in seconds
        progress_interval: Seconds between progress updates
        prefix: Prefix for log messages
        filter_keywords: Keywords to filter output in non-verbose mode
        
    Returns:
        Tuple of (success: bool, stdout_lines: List[str], stderr_lines: List[str])
    """
    try:
        process = subprocess.Popen(
            ssh_cmd,
            stdin=subprocess.PIPE if script else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1  # Line buffered
        )
        
        # Write script if provided
        if script:
            process.stdin.write(script)
            process.stdin.close()
        
        # Stream output
        with stream_ssh_output(process, timeout, progress_interval, prefix, filter_keywords) as (stdout_lines, stderr_lines, returncode):
            success = returncode == 0
            return success, stdout_lines, stderr_lines
            
    except Exception as e:
        log_error(f"Error executing command: {e}")
        return False, [], []

