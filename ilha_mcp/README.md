# ilha MCP Server

This directory contains the Model Context Protocol (MCP) server implementation for ilha, enabling AI assistants to manage isolated development environments through a standardized interface.

## Overview

The ilha MCP server exposes ilha functionality through the Model Context Protocol, allowing AI assistants like Claude to:

- Create and manage isolated development environments
- Start and stop worktree containers
- Manage volumes and backups
- Export and import environment packages
- Control the global Caddy proxy
- Access worktree information and status

## Installation

### Prerequisites

- Python 3.11+
- ilha installed and configured
- Docker running

### Install with MCP support

```bash
# Install ilha with MCP dependencies
pip install ilha[mcp]

# Or install from source
git clone https://github.com/catalpainternational/ilha.git
cd ilha
pip install -e .[mcp]
```

## Usage

### Running the MCP Server

```bash
# Run the MCP server
ilha-mcp

# Or run directly
python -m ilha_mcp.server
```

### Configuration

The MCP server can be configured using environment variables:

```bash
export ilha_WORKING_DIR="/path/to/your/project"
export ilha_TIMEOUT="300"
export ilha_VERBOSE="true"
```

### Integration with Claude Desktop

To use the ilha MCP server with Claude Desktop, add the following to your Claude Desktop configuration:

```json
{
  "mcpServers": {
    "ilha": {
      "command": "ilha-mcp",
      "args": [],
      "env": {
        "ilha_WORKING_DIR": "/path/to/your/project"
      }
    }
  }
}
```

## Working Directory Configuration

### Understanding Working Directory

The ilha MCP server can run from any location but needs to know which project directory to operate on. This is controlled by the `working_directory` parameter.

**Key Concepts:**
- **MCP Server Location**: Where the MCP server is installed (e.g., `/Users/ders/projects/ilha`)
- **Target Project Directory**: The project you want to manage (e.g., `/Users/ders/kenali/blank`)
- **Working Directory Parameter**: Tells the MCP server which project to operate on

### For AI Agents (Claude, Cursor, etc.)

**ALWAYS provide the `working_directory` parameter** when calling ilha MCP functions. This is critical for proper operation.

#### Example Usage for AI Agents

```python
# ✅ CORRECT: Always specify working_directory
create_worktree({
    "branch_name": "feature-auth",
    "working_directory": "/Users/ders/kenali/blank"  # Target project directory
})

# ✅ CORRECT: Use Cursor workspace path
list_worktrees({
    "working_directory": "/Users/ders/kenali/blank"  # Current workspace
})

# ❌ INCORRECT: Missing working_directory
create_worktree({"branch_name": "feature-auth"})  # Will use MCP server's directory
```

#### Best Practices for AI Agents

1. **Always use absolute paths** for `working_directory`
2. **Use the current workspace directory** (where the user is working)
3. **Verify the path exists** before calling functions
4. **Use consistent paths** across related operations

#### Common Scenarios

**Scenario 1: Managing a project in Cursor**
```python
# User is working in /Users/ders/kenali/blank
working_dir = "/Users/ders/kenali/blank"

# Create isolated environment
create_worktree({
    "branch_name": "feature-auth",
    "working_directory": working_dir
})

# Start the environment
start_worktree({
    "branch_name": "feature-auth", 
    "working_directory": working_dir
})
```

**Scenario 2: Managing multiple projects**
```python
# Project A
project_a = "/Users/ders/projects/project-a"
create_worktree({
    "branch_name": "feature-login",
    "working_directory": project_a
})

# Project B  
project_b = "/Users/ders/projects/project-b"
create_worktree({
    "branch_name": "feature-payment",
    "working_directory": project_b
})
```

**Scenario 3: Package Management**
```python
# Export environment as shareable package (includes code by default)
export_package({
    "branch_name": "feature-auth",
    "working_directory": "/Users/ders/kenali/blank"
})

# Export environment without code (smaller package)
export_package({
    "branch_name": "feature-auth",
    "working_directory": "/Users/ders/kenali/blank",
    "include_code": False
})

# List available packages
list_packages({
    "working_directory": "/Users/ders/kenali/blank"
})

# Import environment from package
import_package({
    "package_path": "/Users/ders/kenali/blank/packages/myapp-feature-auth-2024-01-15.ilha-package.tar.gz",
    "working_directory": "/Users/ders/kenali/blank"
})
```

### Troubleshooting Working Directory Issues

#### Common Problems

1. **"No docker-compose.yml found"**
   - **Cause**: Wrong working directory
   - **Solution**: Ensure `working_directory` points to project root with `docker-compose.yml`

2. **"Not a Git repository"**
   - **Cause**: Working directory is not a Git repository
   - **Solution**: Navigate to a Git repository or initialize one

3. **"Permission denied"**
   - **Cause**: No write access to working directory
   - **Solution**: Check permissions or use a different directory

4. **"Worktree already exists"**
   - **Cause**: Working in wrong project or branch already exists
   - **Solution**: Use `list_worktrees()` to see existing worktrees

#### Debugging Steps

1. **Verify working directory**:
   ```python
   # Check if directory exists and has required files
   list_worktrees({"working_directory": "/path/to/project"})
   ```

2. **Check project context**:
   ```python
   # Get detailed project information
   # Access ilha://project resource
   ```

3. **Validate setup**:
   ```python
   # Ensure ilha is initialized
   # Check for docker-compose.yml
   # Verify Git repository
   ```

### Environment Variables

The MCP server can also use environment variables for working directory:

```bash
# Set default working directory
export ilha_WORKING_DIR="/path/to/your/project"

# Run MCP server
ilha-mcp
```

**Note**: Environment variables are overridden by the `working_directory` parameter in function calls.

## Available Tools

### Worktree Management

- **create_worktree**: Create a new worktree for the specified branch
- **start_worktree**: Start containers for the specified worktree
- **stop_worktree**: Stop containers for the specified worktree
- **remove_worktree**: Remove worktree and containers but keep git branch
- **delete_worktree**: Delete worktree and branch completely
- **list_worktrees**: List all active worktrees
- **get_worktree_info**: Get detailed information about a specific worktree

### Volume Management

- **list_volumes**: List all worktree volumes
- **get_volume_sizes**: Get sizes of all worktree volumes
- **backup_volumes**: Backup volumes for a specific worktree
- **restore_volumes**: Restore volumes for a specific worktree from backup
- **clean_volumes**: Clean up volumes for a specific worktree

### Package Management

- **export_package**: Export worktree as shareable package
- **import_package**: Import environment from package
- **list_packages**: List available packages
- **validate_package**: Validate package integrity

### Caddy Proxy Management

- **start_proxy**: Start the global Caddy proxy container
- **stop_proxy**: Stop the global Caddy proxy container
- **get_proxy_status**: Get status of the global Caddy proxy

## Available Resources

### Read-Only Data Access

- **ilha://worktrees**: List of all active worktrees with their status
- **ilha://volumes**: List of all worktree volumes with sizes
- **ilha://proxy**: Status of the global Caddy proxy container

## Example Usage

### Creating a Development Environment

```python
# AI assistant can now do:
# 1. Create a worktree for a new feature
create_worktree({"branch_name": "feature-auth"})

# 2. Start the development environment
start_worktree({"branch_name": "feature-auth"})

# 3. Get the URL to access the environment
get_worktree_info({"branch_name": "feature-auth"})
```

### Managing Volumes

```python
# Backup volumes before making changes
backup_volumes({"branch_name": "feature-auth"})

# Clean up old volumes
clean_volumes({"branch_name": "feature-auth"})
```

### Proxy Management

```python
# Start the global proxy
start_proxy({})

# Check proxy status
get_proxy_status({})
```

## Architecture

The MCP server uses a hybrid approach:

1. **CLI Wrapper**: Invokes ilha CLI commands with `--json` flag
2. **Tool Handlers**: Process MCP tool calls and route to appropriate CLI commands
3. **Resource Handlers**: Provide read-only access to ilha data
4. **Configuration**: Environment-based configuration management

## Development

### Project Structure

```
ilha_mcp/
├── __init__.py
├── server.py              # Main MCP server
├── config.py              # Configuration management
├── tools/                 # MCP tool implementations
│   ├── worktree_tools.py
│   ├── volume_tools.py
│   └── caddy_tools.py
├── resources/             # MCP resource implementations
│   └── worktree_resources.py
└── utils/                 # Utility modules
    └── cli_wrapper.py
```

### Adding New Tools

1. Create tool implementation in `tools/`
2. Register tool in `server.py`
3. Add tool definition to `_list_tools()`
4. Add tests in `tests/mcp/`

### Adding New Resources

1. Create resource implementation in `resources/`
2. Register resource in `server.py`
3. Add resource definition to `_list_resources()`
4. Add tests in `tests/mcp/`

## Troubleshooting

### Common Issues

1. **ilha command not found**: Ensure ilha is installed and in PATH
2. **Permission denied**: Ensure Docker is running and accessible
3. **JSON parsing errors**: Check that ilha commands support `--json` flag

### Debug Mode

Enable verbose logging:

```bash
export ilha_VERBOSE="true"
ilha-mcp
```

### Testing

Run the test suite:

```bash
pytest tests/mcp/
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Submit a pull request

## License

Same license as ilha (see main project LICENSE file).


