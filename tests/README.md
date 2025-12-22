# ilha CLI Test Suite

This directory contains the comprehensive test suite for the ilha CLI tool.

## Test Structure

### Unit Tests (`unit/`)
Fast, isolated tests that don't require external dependencies:
- `test_config.py` - Configuration and settings tests
- `test_validation.py` - Input validation tests
- `test_docker_manager.py` - Docker manager unit tests
- `test_git_manager.py` - Git manager unit tests
- `test_environment_manager.py` - Environment manager tests
- `test_volume_manager.py` - Volume management tests
- `test_caddy_manager.py` - Caddy manager tests
- `test_worktree_manager.py` - Worktree manager tests
- `test_ilha_up_unit.py` - ilha up command unit tests

### Integration Tests (`integration/`)
Tests that require Docker but not a full environment:
- `test_simple_integration.py` - Basic Docker integration
- `test_docker_integration.py` - Docker operations
- `test_git_integration.py` - Git operations

### End-to-End Tests (`e2e/`)
Full environment tests with real containers:
- `test_simple_e2e.py` - Basic E2E functionality
- `test_ilha_up_command.py` - Comprehensive ilha up -d testing

## Test Categories

### By Scope
- **Unit**: Fast, isolated, no external dependencies
- **Integration**: Docker required, no full environment
- **E2E**: Full environment with real containers

### By Functionality
- **Core**: Basic CLI functionality
- **Worktree**: Worktree creation, management, lifecycle
- **Docker**: Container and volume management
- **Caddy**: Proxy and routing functionality
- **Environment**: Configuration and environment setup

## Running Tests

### All Tests
```bash
# Run all tests
python ilha/tests/run_tests.py

# Or using pytest directly
pytest ilha/tests/ -v
```

### By Category
```bash
# Unit tests only
pytest ilha/tests/unit/ -v

# Integration tests only
pytest ilha/tests/integration/ -v

# E2E tests only
pytest ilha/tests/e2e/ -v
```

### By Marker
```bash
# Run only unit tests
pytest -m unit

# Run only integration tests
pytest -m integration

# Run only E2E tests
pytest -m e2e

# Skip slow tests
pytest -m "not slow"
```

## Test Requirements

### Prerequisites
- Docker and Docker Compose
- Git repository
- Python 3.8+
- pytest, requests, click

### Environment Setup
- Docker daemon running
- Git repository initialized
- Proper permissions for Docker operations

## Test Data and Cleanup

### Test Branches
- All test branches use consistent naming: `test-{type}`
- Automatic cleanup after test completion
- Isolation between test runs

### Test Resources
- Docker containers with test-specific names
- Docker volumes with test-specific names
- Git worktrees in isolated directories
- Temporary files and directories

## Test Configuration

### pytest.ini
Contains pytest configuration including:
- Test discovery patterns
- Output formatting
- Markers for test categorization
- Timeout settings
- Logging configuration

### conftest.py
Contains shared fixtures and test configuration.

## Troubleshooting

### Common Issues
1. **Docker not running**: Ensure Docker daemon is started
2. **Permission denied**: Check Docker permissions
3. **Port conflicts**: Ensure ports 80, 443 are available
4. **Git issues**: Ensure in a Git repository

### Debug Mode
```bash
# Run with debug output
pytest ilha/tests/ -v -s --tb=long

# Run specific test with debug
pytest ilha/tests/e2e/test_ilha_up_command.py::TestilhaUpCommand::test_complete_worktree_lifecycle -v -s
```

### Cleanup
```bash
# Clean up test resources
docker system prune -f
docker volume prune -f
git worktree prune
```

## Test Coverage

The test suite covers:
- ✅ CLI command parsing and execution
- ✅ Worktree creation and management
- ✅ Docker container lifecycle
- ✅ Volume management
- ✅ Network configuration
- ✅ Environment configuration
- ✅ Caddy proxy functionality
- ✅ Error handling and edge cases
- ✅ Multi-worktree isolation
- ✅ Cleanup and resource management

## Contributing

When adding new tests:
1. Follow the existing naming conventions
2. Use appropriate markers (`@pytest.mark.unit`, `@pytest.mark.integration`, `@pytest.mark.e2e`)
3. Include proper cleanup in teardown
4. Add docstrings explaining test purpose
5. Use descriptive test names
6. Follow DRY principles
