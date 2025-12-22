#!/bin/bash

# Test script for ilha functionality
# This script tests the basic ilha commands

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Test configuration
TEST_BRANCH="test-ilha-$(date +%s)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Logging functions
log_info() {
    echo -e "${BLUE}[TEST]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[PASS]${NC} $1"
}

log_error() {
    echo -e "${RED}[FAIL]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

# Test functions
test_help() {
    log_info "Testing help command"
    if ./ilha help > /dev/null 2>&1; then
        log_success "Help command works"
    else
        log_error "Help command failed"
        return 1
    fi
}

test_list() {
    log_info "Testing list command"
    if ./ilha list > /dev/null 2>&1; then
        log_success "List command works"
    else
        log_error "List command failed"
        return 1
    fi
}

test_network_creation() {
    log_info "Testing network creation"
    if docker network inspect ilha_caddy_proxy > /dev/null 2>&1; then
        log_success "Caddy proxy network exists"
    else
        log_warning "Caddy proxy network does not exist, will be created"
    fi
}

test_worktree_creation() {
    log_info "Testing worktree creation for branch: $TEST_BRANCH"
    
    # Create test branch
    git checkout -b "$TEST_BRANCH" 2>/dev/null || git checkout "$TEST_BRANCH"
    
    # Test worktree creation
    if ./ilha create "$TEST_BRANCH" > /dev/null 2>&1; then
        log_success "Worktree creation works"
        
        # Check if worktree directory exists
        if [ -d "../$TEST_BRANCH" ]; then
            log_success "Worktree directory created"
        else
            log_error "Worktree directory not found"
            return 1
        fi
        
        # Check if environment file exists
        if [ -f "../$TEST_BRANCH/.ilha/env.ilha" ]; then
            log_success "Environment file created"
        else
            log_error "Environment file not found"
            return 1
        fi
        
        # Check if volumes were created
        if docker volume inspect "${TEST_BRANCH}_postgres_data" > /dev/null 2>&1; then
            log_success "PostgreSQL volume created"
        else
            log_warning "PostgreSQL volume not found (may be expected if no existing data)"
        fi
        
        if docker volume inspect "${TEST_BRANCH}_redis_data" > /dev/null 2>&1; then
            log_success "Redis volume created"
        else
            log_warning "Redis volume not found (may be expected if no existing data)"
        fi
        
        if docker volume inspect "${TEST_BRANCH}_media_files" > /dev/null 2>&1; then
            log_success "Media volume created"
        else
            log_warning "Media volume not found (may be expected if no existing data)"
        fi
        
    else
        log_error "Worktree creation failed"
        return 1
    fi
}

test_worktree_removal() {
    log_info "Testing worktree removal for branch: $TEST_BRANCH"
    
    # Test worktree removal
    if ./ilha remove "$TEST_BRANCH" > /dev/null 2>&1; then
        log_success "Worktree removal works"
        
        # Check if worktree directory is removed
        if [ ! -d "../$TEST_BRANCH" ]; then
            log_success "Worktree directory removed"
        else
            log_error "Worktree directory still exists"
            return 1
        fi
        
        # Check if volumes were removed
        if ! docker volume inspect "${TEST_BRANCH}_postgres_data" > /dev/null 2>&1; then
            log_success "PostgreSQL volume removed"
        else
            log_warning "PostgreSQL volume still exists"
        fi
        
    else
        log_error "Worktree removal failed"
        return 1
    fi
}

test_volume_management() {
    log_info "Testing volume management commands"
    
    # Test volume list
    if ./ilha volumes list > /dev/null 2>&1; then
        log_success "Volume list command works"
    else
        log_error "Volume list command failed"
        return 1
    fi
    
    # Test volume size
    if ./ilha volumes size > /dev/null 2>&1; then
        log_success "Volume size command works"
    else
        log_error "Volume size command failed"
        return 1
    fi
}

# Cleanup function
cleanup() {
    log_info "Cleaning up test resources"
    
    # Remove test branch if it exists
    if git branch | grep -q "$TEST_BRANCH"; then
        git checkout main 2>/dev/null || git checkout master 2>/dev/null || true
        git branch -D "$TEST_BRANCH" 2>/dev/null || true
    fi
    
    # Remove test worktree if it exists
    if [ -d "../$TEST_BRANCH" ]; then
        rm -rf "../$TEST_BRANCH" 2>/dev/null || true
    fi
    
    # Remove test volumes if they exist
    docker volume rm "${TEST_BRANCH}_postgres_data" 2>/dev/null || true
    docker volume rm "${TEST_BRANCH}_redis_data" 2>/dev/null || true
    docker volume rm "${TEST_BRANCH}_media_files" 2>/dev/null || true
    
    log_success "Cleanup completed"
}

# Main test execution
main() {
    log_info "Starting ilha tests"
    
    # Change to project root
    cd "$PROJECT_ROOT"
    
    # Set up cleanup trap
    trap cleanup EXIT
    
    # Run tests
    test_help || exit 1
    test_list || exit 1
    test_network_creation || exit 1
    test_worktree_creation || exit 1
    test_worktree_removal || exit 1
    test_volume_management || exit 1
    
    log_success "All tests passed!"
}

# Run main function
main "$@"
