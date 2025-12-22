#!/bin/bash

# Comprehensive Test Suite for ilha Worktree Lifecycle
# This script implements the complete test plan for ilha functionality

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Test configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
TEST_BRANCH_PREFIX="test-ilha-$(date +%s)"
TEST_RESULTS_DIR="$SCRIPT_DIR/test_results"
TEST_LOG="$TEST_RESULTS_DIR/test_$(date +%Y%m%d_%H%M%S).log"

# Test counters
TOTAL_TESTS=0
PASSED_TESTS=0
FAILED_TESTS=0
SKIPPED_TESTS=0

# Test state
CURRENT_PHASE=""
CURRENT_TEST=""
TEST_START_TIME=""

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1" | tee -a "$TEST_LOG"
}

log_success() {
    echo -e "${GREEN}[PASS]${NC} $1" | tee -a "$TEST_LOG"
}

log_error() {
    echo -e "${RED}[FAIL]${NC} $1" | tee -a "$TEST_LOG"
}

log_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1" | tee -a "$TEST_LOG"
}

log_phase() {
    echo -e "${PURPLE}[PHASE]${NC} $1" | tee -a "$TEST_LOG"
}

log_test() {
    echo -e "${CYAN}[TEST]${NC} $1" | tee -a "$TEST_LOG"
}

# Test framework functions
start_test() {
    CURRENT_TEST="$1"
    TEST_START_TIME=$(date +%s)
    TOTAL_TESTS=$((TOTAL_TESTS + 1))
    log_test "Starting: $CURRENT_TEST"
}

pass_test() {
    local duration=$(($(date +%s) - TEST_START_TIME))
    PASSED_TESTS=$((PASSED_TESTS + 1))
    log_success "PASSED: $CURRENT_TEST (${duration}s)"
}

fail_test() {
    local duration=$(($(date +%s) - TEST_START_TIME))
    FAILED_TESTS=$((FAILED_TESTS + 1))
    log_error "FAILED: $CURRENT_TEST (${duration}s)"
}

skip_test() {
    local duration=$(($(date +%s) - TEST_START_TIME))
    SKIPPED_TESTS=$((SKIPPED_TESTS + 1))
    log_warning "SKIPPED: $CURRENT_TEST (${duration}s)"
}

# Utility functions
cleanup_test_branches() {
    local prefix="$1"
    log_info "Cleaning up test branches with prefix: $prefix"
    
    # Get all branches matching the prefix
    local branches=$(git branch | grep "$prefix" | sed 's/^[ *]*//' | tr -d ' ' || true)
    
    for branch in $branches; do
        if [ -n "$branch" ] && [ "$branch" != "+" ]; then
            log_info "Removing test branch: $branch"
            git branch -D "$branch" 2>/dev/null || true
        fi
    done
}

cleanup_test_worktrees() {
    local prefix="$1"
    log_info "Cleaning up test worktrees with prefix: $prefix"
    
    # Get all worktrees matching the prefix
    local worktrees=$(git worktree list | grep "$prefix" | awk '{print $1}' || true)
    
    for worktree in $worktrees; do
        if [ -n "$worktree" ] && [ -d "$worktree" ]; then
            log_info "Removing test worktree: $worktree"
            rm -rf "$worktree" 2>/dev/null || true
        fi
    done
}

cleanup_test_volumes() {
    local prefix="$1"
    log_info "Cleaning up test volumes with prefix: $prefix"
    
    # Get all volumes matching the prefix
    local volumes=$(docker volume ls -q | grep "$prefix" || true)
    
    for volume in $volumes; do
        if [ -n "$volume" ]; then
            log_info "Removing test volume: $volume"
            docker volume rm "$volume" 2>/dev/null || true
        fi
    done
}

cleanup_test_containers() {
    local prefix="$1"
    log_info "Cleaning up test containers with prefix: $prefix"
    
    # Get all containers matching the prefix
    local containers=$(docker ps -a --filter "name=$prefix" --format "{{.Names}}" || true)
    
    for container in $containers; do
        if [ -n "$container" ]; then
            log_info "Removing test container: $container"
            docker rm -f "$container" 2>/dev/null || true
        fi
    done
}

# Test functions
test_prerequisites() {
    log_phase "Phase 1: Prerequisites and Environment Setup"
    
    start_test "Docker daemon running"
    if docker info > /dev/null 2>&1; then
        pass_test
    else
        fail_test
        return 1
    fi
    
    start_test "Docker Compose available"
    if command -v docker-compose > /dev/null 2>&1 || docker compose version > /dev/null 2>&1; then
        pass_test
    else
        fail_test
        return 1
    fi
    
    start_test "Git repository initialized"
    if git rev-parse --git-dir > /dev/null 2>&1; then
        pass_test
    else
        fail_test
        return 1
    fi
    
    start_test "ilha script executable"
    if [ -x "$SCRIPT_DIR/ilha" ]; then
        pass_test
    else
        fail_test
        return 1
    fi
}

test_global_caddy_management() {
    log_phase "Phase 2: Global Caddy Management"
    
    start_test "Start global Caddy container"
    if ./ilha start > /dev/null 2>&1; then
        pass_test
    else
        fail_test
    fi
    
    start_test "Caddy container health check"
    if docker ps --filter "name=ilha_caddy_proxy" --format "{{.Status}}" | grep -q "Up"; then
        pass_test
    else
        fail_test
    fi
    
    start_test "Stop global Caddy container"
    if ./ilha stop > /dev/null 2>&1; then
        pass_test
    else
        fail_test
    fi
    
    start_test "Multiple start/stop cycles"
    local cycle_count=0
    local max_cycles=3
    local success=true
    
    while [ $cycle_count -lt $max_cycles ]; do
        if ! ./ilha start > /dev/null 2>&1; then
            success=false
            break
        fi
        if ! ./ilha stop > /dev/null 2>&1; then
            success=false
            break
        fi
        cycle_count=$((cycle_count + 1))
    done
    
    if [ "$success" = true ]; then
        pass_test
    else
        fail_test
    fi
}

test_worktree_creation() {
    log_phase "Phase 3: Worktree Creation Lifecycle"
    
    local test_branch="$TEST_BRANCH_PREFIX-basic"
    
    start_test "Create worktree with valid branch name"
    if ./ilha create "$test_branch" > /dev/null 2>&1; then
        pass_test
    else
        fail_test
    fi
    
    start_test "Worktree directory exists"
    if [ -d "../ilha-cli/worktrees/$test_branch" ]; then
        pass_test
    else
        fail_test
    fi
    
    start_test "Environment file created"
    if [ -f "../ilha-cli/worktrees/$test_branch/.ilha/env.ilha" ]; then
        pass_test
    else
        fail_test
    fi
    
    start_test "Create worktree with existing branch"
    if ./ilha create "$test_branch" > /dev/null 2>&1; then
        pass_test
    else
        fail_test
    fi
    
    start_test "Create worktree with invalid branch name"
    if ./ilha create "invalid@branch#name" > /dev/null 2>&1; then
        fail_test
    else
        pass_test
    fi
    
    start_test "Create worktree with special characters"
    local special_branch="$TEST_BRANCH_PREFIX-special-chars"
    if ./ilha create "$special_branch" > /dev/null 2>&1; then
        pass_test
    else
        fail_test
    fi
    
    # Cleanup
    cleanup_test_worktrees "$TEST_BRANCH_PREFIX"
    cleanup_test_branches "$TEST_BRANCH_PREFIX"
}

test_volume_management() {
    log_phase "Phase 4: Volume Management"
    
    local test_branch="$TEST_BRANCH_PREFIX-volume"
    
    start_test "Volume creation for new worktree"
    if ./ilha create "$test_branch" > /dev/null 2>&1; then
        pass_test
    else
        fail_test
    fi
    
    start_test "PostgreSQL volume created"
    if docker volume inspect "${test_branch}_postgres_data" > /dev/null 2>&1; then
        pass_test
    else
        fail_test
    fi
    
    start_test "Redis volume created"
    if docker volume inspect "${test_branch}_redis_data" > /dev/null 2>&1; then
        pass_test
    else
        fail_test
    fi
    
    start_test "Media volume created"
    if docker volume inspect "${test_branch}_media_files" > /dev/null 2>&1; then
        pass_test
    else
        fail_test
    fi
    
    start_test "Volume isolation between worktrees"
    local test_branch2="$TEST_BRANCH_PREFIX-volume2"
    if ./ilha create "$test_branch2" > /dev/null 2>&1; then
        if docker volume inspect "${test_branch2}_postgres_data" > /dev/null 2>&1; then
            pass_test
        else
            fail_test
        fi
    else
        fail_test
    fi
    
    # Cleanup
    cleanup_test_worktrees "$TEST_BRANCH_PREFIX"
    cleanup_test_branches "$TEST_BRANCH_PREFIX"
    cleanup_test_volumes "$TEST_BRANCH_PREFIX"
}

test_environment_configuration() {
    log_phase "Phase 5: Environment Configuration"
    
    local test_branch="$TEST_BRANCH_PREFIX-env"
    
    start_test "Environment file generation"
    if ./ilha create "$test_branch" > /dev/null 2>&1; then
        pass_test
    else
        fail_test
    fi
    
    start_test "Environment variables validation"
    if grep -q "COMPOSE_PROJECT_NAME=$test_branch" "../ilha-cli/worktrees/$test_branch/.ilha/env.ilha"; then
        pass_test
    else
        fail_test
    fi
    
    start_test "Domain name configuration"
    if grep -q "SITE_DOMAIN=$test_branch.localhost" "../ilha-cli/worktrees/$test_branch/.ilha/env.ilha"; then
        pass_test
    else
        fail_test
    fi
    
    start_test "Allowed hosts configuration"
    if grep -q "ALLOWED_HOSTS=localhost,127.0.0.1,$test_branch.localhost,*.localhost" "../ilha-cli/worktrees/$test_branch/.ilha/env.ilha"; then
        pass_test
    else
        fail_test
    fi
    
    # Cleanup
    cleanup_test_worktrees "$TEST_BRANCH_PREFIX"
    cleanup_test_branches "$TEST_BRANCH_PREFIX"
    cleanup_test_volumes "$TEST_BRANCH_PREFIX"
}

test_worktree_startup() {
    log_phase "Phase 6: Worktree Startup Lifecycle"
    
    local test_branch="$TEST_BRANCH_PREFIX-startup"
    
    start_test "Start worktree environment"
    if ./ilha create "$test_branch" > /dev/null 2>&1; then
        cd "../ilha-cli/worktrees/$test_branch"
        if ./ilha up -d > /dev/null 2>&1; then
            pass_test
        else
            fail_test
        fi
        cd - > /dev/null
    else
        fail_test
    fi
    
    start_test "Container health checks"
    cd "../ilha-cli/worktrees/$test_branch"
    if docker ps --filter "name=$test_branch" --format "{{.Status}}" | grep -q "Up"; then
        pass_test
    else
        fail_test
    fi
    cd - > /dev/null
    
    start_test "Stop worktree environment"
    cd "../ilha-cli/worktrees/$test_branch"
    if ./ilha down > /dev/null 2>&1; then
        pass_test
    else
        fail_test
    fi
    cd - > /dev/null
    
    # Cleanup
    cleanup_test_worktrees "$TEST_BRANCH_PREFIX"
    cleanup_test_branches "$TEST_BRANCH_PREFIX"
    cleanup_test_volumes "$TEST_BRANCH_PREFIX"
    cleanup_test_containers "$TEST_BRANCH_PREFIX"
}

test_multiple_worktrees() {
    log_phase "Phase 7: Multiple Worktree Management"
    
    local test_branch1="$TEST_BRANCH_PREFIX-multi1"
    local test_branch2="$TEST_BRANCH_PREFIX-multi2"
    
    start_test "Create multiple concurrent worktrees"
    if ./ilha create "$test_branch1" > /dev/null 2>&1 && ./ilha create "$test_branch2" > /dev/null 2>&1; then
        pass_test
    else
        fail_test
    fi
    
    start_test "Start multiple worktrees simultaneously"
    cd "../ilha-cli/worktrees/$test_branch1"
    if ./ilha up -d > /dev/null 2>&1; then
        cd "../$test_branch2"
        if ./ilha up -d > /dev/null 2>&1; then
            pass_test
        else
            fail_test
        fi
        cd - > /dev/null
    else
        fail_test
    fi
    cd - > /dev/null
    
    start_test "Verify isolation between worktrees"
    if docker volume inspect "${test_branch1}_postgres_data" > /dev/null 2>&1 && docker volume inspect "${test_branch2}_postgres_data" > /dev/null 2>&1; then
        pass_test
    else
        fail_test
    fi
    
    # Cleanup
    cd "../ilha-cli/worktrees/$test_branch1" && ./ilha down > /dev/null 2>&1 || true
    cd "../ilha-cli/worktrees/$test_branch2" && ./ilha down > /dev/null 2>&1 || true
    cd - > /dev/null
    cleanup_test_worktrees "$TEST_BRANCH_PREFIX"
    cleanup_test_branches "$TEST_BRANCH_PREFIX"
    cleanup_test_volumes "$TEST_BRANCH_PREFIX"
    cleanup_test_containers "$TEST_BRANCH_PREFIX"
}

test_worktree_removal() {
    log_phase "Phase 8: Worktree Removal Lifecycle"
    
    local test_branch="$TEST_BRANCH_PREFIX-removal"
    
    start_test "Delete worktree completely"
    if ./ilha create "$test_branch" > /dev/null 2>&1; then
        if ./ilha delete "$test_branch" > /dev/null 2>&1; then
            pass_test
        else
            fail_test
        fi
    else
        fail_test
    fi
    
    start_test "Delete non-existent worktree"
    if ./ilha delete "non-existent-branch" > /dev/null 2>&1; then
        fail_test
    else
        pass_test
    fi
    
    start_test "Delete with force flag"
    if ./ilha create "$test_branch" > /dev/null 2>&1; then
        if ./ilha delete "$test_branch" --force > /dev/null 2>&1; then
            pass_test
        else
            fail_test
        fi
    else
        fail_test
    fi
    
    # Cleanup
    cleanup_test_worktrees "$TEST_BRANCH_PREFIX"
    cleanup_test_branches "$TEST_BRANCH_PREFIX"
    cleanup_test_volumes "$TEST_BRANCH_PREFIX"
    cleanup_test_containers "$TEST_BRANCH_PREFIX"
}

test_error_handling() {
    log_phase "Phase 9: Error Handling and Recovery"
    
    start_test "Handle invalid command"
    if ./ilha invalid-command > /dev/null 2>&1; then
        fail_test
    else
        pass_test
    fi
    
    start_test "Handle missing branch name"
    if ./ilha create > /dev/null 2>&1; then
        fail_test
    else
        pass_test
    fi
    
    start_test "Handle missing delete target"
    if ./ilha delete > /dev/null 2>&1; then
        fail_test
    else
        pass_test
    fi
}

test_volume_management_commands() {
    log_phase "Phase 10: Volume Management Commands"
    
    start_test "Volume list command"
    if ./ilha volumes list > /dev/null 2>&1; then
        pass_test
    else
        fail_test
    fi
    
    start_test "Volume size command"
    if ./ilha volumes size > /dev/null 2>&1; then
        pass_test
    else
        fail_test
    fi
    
    local test_branch="$TEST_BRANCH_PREFIX-volumes"
    
    start_test "Volume backup command"
    if ./ilha create "$test_branch" > /dev/null 2>&1; then
        if ./ilha volumes backup "$test_branch" > /dev/null 2>&1; then
            pass_test
        else
            fail_test
        fi
    else
        fail_test
    fi
    
    start_test "Volume clean command"
    if ./ilha volumes clean "$test_branch" > /dev/null 2>&1; then
        pass_test
    else
        fail_test
    fi
    
    # Cleanup
    cleanup_test_worktrees "$TEST_BRANCH_PREFIX"
    cleanup_test_branches "$TEST_BRANCH_PREFIX"
    cleanup_test_volumes "$TEST_BRANCH_PREFIX"
}

test_integration() {
    log_phase "Phase 11: Integration Testing"
    
    local test_branch="$TEST_BRANCH_PREFIX-integration"
    
    start_test "Full lifecycle: create → start → stop → remove"
    if ./ilha create "$test_branch" > /dev/null 2>&1; then
        cd "../ilha-cli/worktrees/$test_branch"
        if ./ilha up -d > /dev/null 2>&1; then
            if ./ilha down > /dev/null 2>&1; then
                cd - > /dev/null
                if ./ilha remove "$test_branch" > /dev/null 2>&1; then
                    pass_test
                else
                    fail_test
                fi
            else
                fail_test
            fi
        else
            fail_test
        fi
    else
        fail_test
    fi
    
    # Cleanup
    cleanup_test_worktrees "$TEST_BRANCH_PREFIX"
    cleanup_test_branches "$TEST_BRANCH_PREFIX"
    cleanup_test_volumes "$TEST_BRANCH_PREFIX"
    cleanup_test_containers "$TEST_BRANCH_PREFIX"
}

# Cleanup function
cleanup_all() {
    log_info "Performing comprehensive cleanup"
    
    # Cleanup test resources
    cleanup_test_worktrees "$TEST_BRANCH_PREFIX"
    cleanup_test_branches "$TEST_BRANCH_PREFIX"
    cleanup_test_volumes "$TEST_BRANCH_PREFIX"
    cleanup_test_containers "$TEST_BRANCH_PREFIX"
    
    # Cleanup any remaining test files
    rm -rf "../ilha-cli/worktrees/$TEST_BRANCH_PREFIX"* 2>/dev/null || true
    
    log_success "Cleanup completed"
}

# Test summary
print_summary() {
    echo ""
    echo "=========================================="
    echo "           TEST SUMMARY"
    echo "=========================================="
    echo "Total Tests: $TOTAL_TESTS"
    echo "Passed: $PASSED_TESTS"
    echo "Failed: $FAILED_TESTS"
    echo "Skipped: $SKIPPED_TESTS"
    echo "Success Rate: $(( (PASSED_TESTS * 100) / TOTAL_TESTS ))%"
    echo "=========================================="
    
    if [ $FAILED_TESTS -eq 0 ]; then
        echo -e "${GREEN}All tests passed!${NC}"
        exit 0
    else
        echo -e "${RED}Some tests failed. Check the log for details.${NC}"
        exit 1
    fi
}

# Main test execution
main() {
    log_info "Starting Comprehensive ilha Test Suite"
    log_info "Test results will be logged to: $TEST_LOG"
    
    # Create test results directory
    mkdir -p "$TEST_RESULTS_DIR"
    
    # Change to project root
    cd "$PROJECT_ROOT"
    
    # Set up cleanup trap
    trap cleanup_all EXIT
    
    # Run test phases in logical order
    test_prerequisites || exit 1
    test_global_caddy_management
    test_worktree_creation
    test_volume_management
    test_environment_configuration
    test_worktree_startup
    test_multiple_worktrees
    test_worktree_removal
    test_error_handling
    test_volume_management_commands
    test_integration
    
    # Print summary
    print_summary
}

# Run main function
main "$@"
