# Test Plan: ilha Setup Command and Standalone Process

**Date:** October 9, 2025  
**Version:** 1.0.0  
**Status:** Ready for Implementation

## Overview

This test plan covers the new `ilha setup` command and the generalized standalone process that makes ilha work with any Docker Compose project.

## Test Objectives

1. **Setup Command Functionality**: Verify `ilha setup` works correctly
2. **Project Detection**: Test auto-detection of existing docker-compose.yml files
3. **Configuration Generation**: Validate config.yml and docker-compose.worktree.yml creation
4. **Standalone Installation**: Test pip installation and entry point functionality
5. **Backward Compatibility**: Ensure existing projects continue to work
6. **Error Handling**: Test various failure scenarios

## Test Categories

### 1. Setup Command Tests

#### 1.1 Basic Setup Functionality
- **Test ID**: SETUP-001
- **Description**: Test basic `ilha setup` command
- **Steps**:
  1. Navigate to a project with docker-compose.yml
  2. Run `ilha setup`
  3. Verify `.ilha/` directory is created
  4. Verify `config.yml` is generated
  5. Verify `docker-compose.worktree.yml` is created
  6. Verify `Caddyfile.ilha` is copied
- **Expected Result**: All files created successfully
- **Priority**: High

#### 1.2 Custom Project Name
- **Test ID**: SETUP-002
- **Description**: Test setup with custom project name
- **Steps**:
  1. Run `ilha setup --project-name myproject`
  2. Verify `config.yml` contains `project_name: myproject`
- **Expected Result**: Custom project name is used
- **Priority**: Medium

#### 1.3 Setup in Project Without Docker Compose
- **Test ID**: SETUP-003
- **Description**: Test setup in project without docker-compose.yml
- **Steps**:
  1. Navigate to project without docker-compose.yml
  2. Run `ilha setup`
  3. Verify minimal docker-compose.yml is created
  4. Verify setup completes successfully
- **Expected Result**: Minimal compose file created, setup succeeds
- **Priority**: Medium

#### 1.4 Setup in Already Configured Project
- **Test ID**: SETUP-004
- **Description**: Test setup in project already configured with ilha
- **Steps**:
  1. Run `ilha setup` in project with existing `.ilha/`
  2. Verify existing configuration is preserved or updated
- **Expected Result**: Graceful handling of existing configuration
- **Priority**: Medium

### 2. Project Detection Tests

#### 2.1 Docker Compose Detection
- **Test ID**: DETECT-001
- **Description**: Test detection of docker-compose.yml
- **Steps**:
  1. Create project with docker-compose.yml
  2. Run `ilha setup`
  3. Verify compose file is detected and used
- **Expected Result**: Existing compose file is detected
- **Priority**: High

#### 2.2 Docker Compose YAML Detection
- **Test ID**: DETECT-002
- **Description**: Test detection of docker-compose.yaml
- **Steps**:
  1. Create project with docker-compose.yaml
  2. Run `ilha setup`
  3. Verify yaml file is detected and used
- **Expected Result**: YAML file is detected
- **Priority**: Medium

#### 2.3 Multiple Compose Files
- **Test ID**: DETECT-003
- **Description**: Test behavior with multiple compose files
- **Steps**:
  1. Create project with both docker-compose.yml and docker-compose.yaml
  2. Run `ilha setup`
  3. Verify consistent behavior (prefer .yml)
- **Expected Result**: .yml file is preferred
- **Priority**: Low

### 3. Configuration Generation Tests

#### 3.1 Config.yml Generation
- **Test ID**: CONFIG-001
- **Description**: Test config.yml generation with detected services
- **Steps**:
  1. Create docker-compose.yml with services: web, db, redis
  2. Run `ilha setup`
  3. Verify config.yml contains all services
  4. Verify container_name_template format
- **Expected Result**: All services detected and configured
- **Priority**: High

#### 3.2 Volume Detection
- **Test ID**: CONFIG-002
- **Description**: Test volume detection in config.yml
- **Steps**:
  1. Create docker-compose.yml with volumes
  2. Run `ilha setup`
  3. Verify volumes are listed in config.yml
- **Expected Result**: All volumes detected
- **Priority**: High

#### 3.3 Environment Variables
- **Test ID**: CONFIG-003
- **Description**: Test environment variable detection
- **Steps**:
  1. Create docker-compose.yml with environment variables
  2. Run `ilha setup`
  3. Verify environment variables are preserved
- **Expected Result**: Environment variables maintained
- **Priority**: Medium

### 4. Docker Compose Transformation Tests

#### 4.1 Container Name Transformation
- **Test ID**: TRANSFORM-001
- **Description**: Test container name transformation
- **Steps**:
  1. Create docker-compose.yml with container_name
  2. Run `ilha setup`
  3. Verify container_name uses ${COMPOSE_PROJECT_NAME}
- **Expected Result**: Container names use variables
- **Priority**: High

#### 4.2 Port to Expose Conversion
- **Test ID**: TRANSFORM-002
- **Description**: Test port to expose conversion
- **Steps**:
  1. Create docker-compose.yml with ports
  2. Run `ilha setup`
  3. Verify ports are converted to expose
- **Expected Result**: Ports converted to expose
- **Priority**: High

#### 4.3 Caddy Labels Addition
- **Test ID**: TRANSFORM-003
- **Description**: Test Caddy labels addition to web services
- **Steps**:
  1. Create docker-compose.yml with web service
  2. Run `ilha setup`
  3. Verify Caddy labels are added
- **Expected Result**: Caddy labels added to web services
- **Priority**: High

#### 4.4 Volume Name Transformation
- **Test ID**: TRANSFORM-004
- **Description**: Test volume name transformation
- **Steps**:
  1. Create docker-compose.yml with named volumes
  2. Run `ilha setup`
  3. Verify volumes use branch-specific naming
- **Expected Result**: Volumes use ${COMPOSE_PROJECT_NAME} prefix
- **Priority**: Medium

### 5. Standalone Installation Tests

#### 5.1 Pip Installation
- **Test ID**: INSTALL-001
- **Description**: Test pip installation of ilha
- **Steps**:
  1. Create wheel package from ilha
  2. Install via pip
  3. Verify `ilha` command is available
  4. Test basic functionality
- **Expected Result**: Command available and functional
- **Priority**: High

#### 5.2 Entry Point Functionality
- **Test ID**: INSTALL-002
- **Description**: Test entry point works correctly
- **Steps**:
  1. Install ilha via pip
  2. Run `ilha --help`
  3. Run `ilha setup --help`
  4. Verify all commands are available
- **Expected Result**: All commands accessible
- **Priority**: High

#### 5.3 Git Submodule Installation
- **Test ID**: INSTALL-003
- **Description**: Test git submodule installation
- **Steps**:
  1. Add ilha as git submodule
  2. Run `python -m ilha.cli setup`
  3. Verify setup works
- **Expected Result**: Submodule installation works
- **Priority**: Medium

### 6. Integration Tests

#### 6.1 Complete Workflow Test
- **Test ID**: INTEGRATION-001
- **Description**: Test complete workflow from setup to worktree
- **Steps**:
  1. Run `ilha setup`
  2. Run `ilha start`
  3. Run `ilha create test-branch`
  4. Run `ilha up test-branch -d`
  5. Verify containers are running
  6. Test access via http://test-branch.localhost
  7. Run `ilha down test-branch`
  8. Run `ilha remove test-branch`
- **Expected Result**: Complete workflow works
- **Priority**: High

#### 6.2 Multiple Projects Test
- **Test ID**: INTEGRATION-002
- **Description**: Test ilha with different project types
- **Steps**:
  1. Test with Django project
  2. Test with Rails project
  3. Test with Node.js project
  4. Test with minimal project
- **Expected Result**: Works with all project types
- **Priority**: Medium

### 7. Error Handling Tests

#### 7.1 Permission Errors
- **Test ID**: ERROR-001
- **Description**: Test handling of permission errors
- **Steps**:
  1. Create directory with restricted permissions
  2. Run `ilha setup`
  3. Verify appropriate error message
- **Expected Result**: Clear error message
- **Priority**: Medium

#### 7.2 Invalid Docker Compose
- **Test ID**: ERROR-002
- **Description**: Test handling of invalid docker-compose.yml
- **Steps**:
  1. Create invalid docker-compose.yml
  2. Run `ilha setup`
  3. Verify graceful error handling
- **Expected Result**: Graceful error handling
- **Priority**: Medium

#### 7.3 Missing Dependencies
- **Test ID**: ERROR-003
- **Description**: Test handling of missing dependencies
- **Steps**:
  1. Run ilha without Docker
  2. Run ilha without Git
  3. Verify appropriate error messages
- **Expected Result**: Clear error messages
- **Priority**: High

### 8. Backward Compatibility Tests

#### 8.1 Legacy Project Support
- **Test ID**: COMPAT-001
- **Description**: Test existing projects continue to work
- **Steps**:
  1. Use existing ilha project
  2. Run existing commands
  3. Verify no regression
- **Expected Result**: Existing functionality preserved
- **Priority**: High

#### 8.2 Configuration Migration
- **Test ID**: COMPAT-002
- **Description**: Test migration from legacy to new config
- **Steps**:
  1. Run `ilha setup` in existing project
  2. Verify legacy config is preserved
  3. Verify new config is generated
- **Expected Result**: Smooth migration
- **Priority**: Medium

## Test Data Requirements

### Test Projects
1. **Minimal Project**: Just web service
2. **Django Project**: Full Django stack with postgres, redis
3. **Rails Project**: Rails with postgres
4. **Node.js Project**: Node.js microservices
5. **Complex Project**: Multiple services, volumes, networks

### Test Environments
1. **Clean Environment**: Fresh installation
2. **Existing Environment**: With existing ilha setup
3. **Conflicting Environment**: With port conflicts, existing containers
4. **Restricted Environment**: Limited permissions

## Test Execution Strategy

### Phase 1: Unit Tests
- Test individual components in isolation
- Mock external dependencies
- Focus on setup command logic

### Phase 2: Integration Tests
- Test complete workflows
- Use real Docker and Git
- Test with actual projects

### Phase 3: End-to-End Tests
- Test full user scenarios
- Test with different project types
- Test error conditions

### Phase 4: Performance Tests
- Test setup time with large projects
- Test memory usage
- Test concurrent operations

## Success Criteria

### Functional Requirements
- [ ] Setup command works with any Docker Compose project
- [ ] Configuration files are generated correctly
- [ ] Docker Compose transformation preserves functionality
- [ ] Pip installation works correctly
- [ ] Entry point provides all commands
- [ ] Backward compatibility maintained

### Non-Functional Requirements
- [ ] Setup completes in < 30 seconds for typical projects
- [ ] Memory usage < 100MB during setup
- [ ] Clear error messages for all failure scenarios
- [ ] Documentation is accurate and complete

## Test Automation

### Automated Tests
- Unit tests for setup command
- Integration tests for complete workflows
- Regression tests for backward compatibility
- Performance benchmarks

### Manual Tests
- User experience testing
- Cross-platform compatibility
- Documentation accuracy
- Error message clarity

## Risk Assessment

### High Risk
- Breaking existing functionality
- Docker Compose transformation errors
- Configuration file corruption

### Medium Risk
- Performance degradation
- Cross-platform issues
- User experience problems

### Low Risk
- Documentation updates
- Minor UI improvements
- Additional features

## Test Schedule

### Week 1: Unit Tests
- Setup command functionality
- Configuration generation
- Docker Compose transformation

### Week 2: Integration Tests
- Complete workflows
- Multiple project types
- Error handling

### Week 3: End-to-End Tests
- Real-world scenarios
- Performance testing
- User acceptance testing

### Week 4: Documentation and Release
- Documentation updates
- Release preparation
- Final validation

## Test Deliverables

1. **Test Results Report**: Detailed results for all test cases
2. **Performance Benchmarks**: Setup time and resource usage
3. **Bug Reports**: Issues found during testing
4. **Recommendations**: Improvements and optimizations
5. **Documentation Updates**: Based on test findings

## Test Environment Setup

### Prerequisites
- Docker and Docker Compose
- Git
- Python 3.11+
- Various test projects
- Clean test environments

### Test Data
- Sample docker-compose.yml files
- Test projects with different configurations
- Error scenarios for testing
- Performance test datasets

## Conclusion

This comprehensive test plan ensures the new setup command and standalone process work correctly across all scenarios. The tests cover functionality, integration, error handling, and backward compatibility to ensure a robust and reliable implementation.
