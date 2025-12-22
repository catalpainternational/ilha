"""
Checksum utilities for ilha.

This module provides SHA256 checksum calculation and verification utilities
for file integrity validation in package operations.
"""

import hashlib
from pathlib import Path
from typing import Optional


def calculate_file_checksum(file_path: Path) -> str:
    """Calculate SHA256 checksum of file.
    
    Args:
        file_path: Path to the file to checksum
        
    Returns:
        SHA256 hex digest of the file
        
    Raises:
        FileNotFoundError: If file doesn't exist
        IOError: If file cannot be read
    """
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    
    sha256 = hashlib.sha256()
    try:
        with open(file_path, 'rb') as f:
            for block in iter(lambda: f.read(4096), b''):
                sha256.update(block)
    except IOError as e:
        raise IOError(f"Cannot read file {file_path}: {e}")
    
    return sha256.hexdigest()


def verify_file_checksum(file_path: Path, expected_checksum: str) -> bool:
    """Verify file checksum matches expected value.
    
    Args:
        file_path: Path to the file to verify
        expected_checksum: Expected SHA256 hex digest
        
    Returns:
        True if checksums match, False otherwise
    """
    try:
        actual = calculate_file_checksum(file_path)
        return actual == expected_checksum
    except (FileNotFoundError, IOError):
        return False


def calculate_directory_checksum(directory_path: Path, exclude_patterns: Optional[list] = None) -> str:
    """Calculate combined checksum for all files in a directory.
    
    Args:
        directory_path: Path to the directory to checksum
        exclude_patterns: List of glob patterns to exclude from checksum
        
    Returns:
        SHA256 hex digest of all files in directory
    """
    if exclude_patterns is None:
        exclude_patterns = []
    
    sha256 = hashlib.sha256()
    
    # Get all files in directory, sorted for consistent ordering
    all_files = sorted([f for f in directory_path.rglob('*') if f.is_file()])
    
    for file_path in all_files:
        # Check if file should be excluded
        should_exclude = False
        for pattern in exclude_patterns:
            if file_path.match(pattern):
                should_exclude = True
                break
        
        if should_exclude:
            continue
            
        try:
            with open(file_path, 'rb') as f:
                for block in iter(lambda: f.read(4096), b''):
                    sha256.update(block)
        except IOError:
            # Skip files that can't be read
            continue
    
    return sha256.hexdigest()


def verify_directory_checksum(directory_path: Path, expected_checksum: str, 
                            exclude_patterns: Optional[list] = None) -> bool:
    """Verify directory checksum matches expected value.
    
    Args:
        directory_path: Path to the directory to verify
        expected_checksum: Expected SHA256 hex digest
        exclude_patterns: List of glob patterns to exclude from checksum
        
    Returns:
        True if checksums match, False otherwise
    """
    try:
        actual = calculate_directory_checksum(directory_path, exclude_patterns)
        return actual == expected_checksum
    except Exception:
        return False
