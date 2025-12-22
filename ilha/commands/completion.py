"""
Shell completion management for ilha CLI.

This module provides commands for installing, uninstalling, and managing
shell completion scripts for Bash and Zsh.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any

from ..config.settings import get_script_dir
from ..utils.logging import log_info, log_success, log_warning, log_error
from ..utils.file_utils import prompt_yes_no


class CompletionManager:
    """Manages shell completion installation and configuration."""
    
    def __init__(self):
        """Initialize completion manager."""
        self.script_dir = get_script_dir()
        self.completions_dir = self.script_dir / "completions"
        self.home_dir = Path.home()
        
        # Shell configuration files
        self.shell_configs = {
            'bash': self.home_dir / '.bashrc',
            'zsh': self.home_dir / '.zshrc'
        }
        
        # Completion script paths (source)
        self.completion_scripts = {
            'bash': self.completions_dir / 'ilha.bash',
            'zsh': self.completions_dir / '_ilha'
        }
        
        # Zsh completion installation directory
        self.zsh_completion_dir = self.home_dir / '.zsh' / 'completions'
        self.zsh_completion_install_path = self.zsh_completion_dir / '_ilha'
    
    def detect_shell(self) -> Optional[str]:
        """Detect the user's current shell."""
        shell = os.environ.get('SHELL', '')
        if 'bash' in shell:
            return 'bash'
        elif 'zsh' in shell:
            return 'zsh'
        return None
    
    def get_completion_source_line(self, shell: str) -> str:
        """Get the source line to add to shell configuration."""
        if shell == 'bash':
            script_path = self.completion_scripts['bash']
            return f"source {script_path}"
        elif shell == 'zsh':
            # For Zsh, we add the user's completion directory to fpath
            return f"fpath=(~/.zsh/completions $fpath)"
        return ""
    
    def is_completion_installed(self, shell: str) -> bool:
        """Check if completion is already installed for the shell."""
        if shell == 'bash':
            config_file = self.shell_configs.get(shell)
            if not config_file or not config_file.exists():
                return False
            
            try:
                content = config_file.read_text()
                source_line = self.get_completion_source_line(shell)
                return source_line in content
            except Exception:
                return False
        
        elif shell == 'zsh':
            # Check if completion file is installed and .zshrc is configured
            if not self.zsh_completion_install_path.exists():
                return False
            
            config_file = self.shell_configs.get(shell)
            if not config_file or not config_file.exists():
                return False
            
            try:
                content = config_file.read_text()
                # Check for fpath and compinit
                has_fpath = '~/.zsh/completions' in content or str(self.zsh_completion_dir) in content
                has_compinit = 'compinit' in content
                return has_fpath and has_compinit
            except Exception:
                return False
        
        return False
    
    def backup_shell_config(self, shell: str) -> bool:
        """Create a backup of the shell configuration file."""
        config_file = self.shell_configs.get(shell)
        if not config_file or not config_file.exists():
            return True  # No file to backup
        
        backup_file = config_file.with_suffix(f'{config_file.suffix}.backup')
        try:
            shutil.copy2(config_file, backup_file)
            log_info(f"Created backup: {backup_file}")
            return True
        except Exception as e:
            log_warning(f"Failed to create backup: {e}")
            return False
    
    def install_completion(self, shell: Optional[str] = None) -> bool:
        """Install shell completion for the specified shell."""
        if not shell:
            shell = self.detect_shell()
            if not shell:
                log_error("Could not detect shell. Please specify: ilha completion install <bash|zsh>")
                return False
        
        if shell not in ['bash', 'zsh']:
            log_error("Unsupported shell. Supported shells: bash, zsh")
            return False
        
        log_info(f"Installing {shell} completion...")
        
        # Check if already installed
        if self.is_completion_installed(shell):
            log_info(f"{shell} completion is already installed")
            return True
        
        # Verify completion script exists
        script_path = self.completion_scripts.get(shell)
        if not script_path or not script_path.exists():
            log_error(f"Completion script not found: {script_path}")
            return False
        
        # Create backup
        self.backup_shell_config(shell)
        
        try:
            if shell == 'bash':
                # Bash: source the completion script from .bashrc
                config_file = self.shell_configs[shell]
                source_line = self.get_completion_source_line(shell)
                
                # Read existing content
                if config_file.exists():
                    content = config_file.read_text()
                else:
                    content = ""
                
                # Add completion if not already present
                if source_line not in content:
                    if content and not content.endswith('\n'):
                        content += '\n'
                    content += f"\n# ilha CLI completion\n{source_line}\n"
                    
                    # Write back to file
                    config_file.write_text(content)
                    log_success(f"Added {shell} completion to {config_file}")
                else:
                    log_info(f"{shell} completion already configured")
                
                # Make completion script executable
                script_path.chmod(0o755)
                
            elif shell == 'zsh':
                # Zsh: copy completion file to user's completion directory
                # Create ~/.zsh/completions if it doesn't exist
                self.zsh_completion_dir.mkdir(parents=True, exist_ok=True)
                
                # Copy the completion file
                shutil.copy2(script_path, self.zsh_completion_install_path)
                log_success(f"Copied completion script to {self.zsh_completion_install_path}")
                
                # Update .zshrc with fpath and compinit
                config_file = self.shell_configs[shell]
                if config_file.exists():
                    content = config_file.read_text()
                else:
                    content = ""
                
                # Check and add fpath
                has_fpath = '~/.zsh/completions' in content or str(self.zsh_completion_dir) in content
                has_compinit = 'compinit' in content
                
                additions = []
                if not has_fpath:
                    additions.append("fpath=(~/.zsh/completions $fpath)")
                if not has_compinit:
                    additions.append("autoload -Uz compinit && compinit")
                
                if additions:
                    if content and not content.endswith('\n'):
                        content += '\n'
                    content += f"\n# ilha CLI completion\n"
                    content += '\n'.join(additions) + '\n'
                    config_file.write_text(content)
                    log_success(f"Added completion configuration to {config_file}")
                else:
                    log_info("Zsh completion configuration already present")
            
            log_success(f"{shell} completion installed successfully")
            log_info("Restart your shell or run 'source ~/.bashrc' (bash) or 'source ~/.zshrc' (zsh) to activate")
            return True
            
        except Exception as e:
            log_error(f"Failed to install {shell} completion: {e}")
            return False
    
    def uninstall_completion(self) -> bool:
        """Remove shell completion from all shells."""
        log_info("Uninstalling shell completion...")
        
        success = True
        for shell in ['bash', 'zsh']:
            if not self.is_completion_installed(shell):
                log_info(f"{shell} completion not installed")
                continue
            
            config_file = self.shell_configs[shell]
            if not config_file.exists():
                continue
            
            try:
                if shell == 'bash':
                    # Remove bash completion from .bashrc
                    content = config_file.read_text()
                    source_line = self.get_completion_source_line(shell)
                    
                    # Remove completion lines
                    lines = content.split('\n')
                    filtered_lines = []
                    skip_next = False
                    
                    for line in lines:
                        if skip_next:
                            skip_next = False
                            continue
                        
                        # Skip lines related to ilha completion
                        if 'ilha CLI completion' in line:
                            skip_next = True
                            continue
                        elif source_line in line:
                            continue
                        
                        filtered_lines.append(line)
                    
                    # Write back filtered content
                    new_content = '\n'.join(filtered_lines)
                    config_file.write_text(new_content)
                    log_success(f"Removed {shell} completion from {config_file}")
                
                elif shell == 'zsh':
                    # Remove zsh completion file and clean up .zshrc
                    if self.zsh_completion_install_path.exists():
                        self.zsh_completion_install_path.unlink()
                        log_success(f"Removed completion file: {self.zsh_completion_install_path}")
                    
                    # Clean up .zshrc (only remove ilha-specific additions)
                    content = config_file.read_text()
                    lines = content.split('\n')
                    filtered_lines = []
                    in_ilha_section = False
                    
                    for line in lines:
                        # Check if we're entering ilha completion section
                        if 'ilha CLI completion' in line:
                            in_ilha_section = True
                            continue
                        
                        # Skip lines in ilha section
                        if in_ilha_section:
                            # Check if this line is part of our additions
                            if ('fpath=(~/.zsh/completions' in line or 
                                'autoload -Uz compinit' in line):
                                continue
                            else:
                                # End of ilha section
                                in_ilha_section = False
                        
                        filtered_lines.append(line)
                    
                    new_content = '\n'.join(filtered_lines)
                    config_file.write_text(new_content)
                    log_success(f"Removed {shell} completion configuration from {config_file}")
                
            except Exception as e:
                log_error(f"Failed to remove {shell} completion: {e}")
                success = False
        
        if success:
            log_success("Shell completion uninstalled successfully")
        else:
            log_warning("Some completion configurations may not have been removed")
        
        return success
    
    def show_completion_status(self) -> None:
        """Show the installation status of shell completion."""
        log_info("Shell completion status:")
        
        for shell in ['bash', 'zsh']:
            if self.is_completion_installed(shell):
                log_success(f"✓ {shell} completion is installed")
            else:
                log_info(f"✗ {shell} completion is not installed")
        
        # Show current shell
        current_shell = self.detect_shell()
        if current_shell:
            log_info(f"Current shell: {current_shell}")
        else:
            log_warning("Could not detect current shell")
        
        # Show completion script locations
        log_info("Completion script locations:")
        for shell, script_path in self.completion_scripts.items():
            if script_path.exists():
                log_info(f"  {shell}: {script_path}")
            else:
                log_warning(f"  {shell}: {script_path} (not found)")
    
    def get_completion_info(self) -> Dict[str, Any]:
        """Get detailed information about completion installation."""
        info = {
            'current_shell': self.detect_shell(),
            'shells': {}
        }
        
        for shell in ['bash', 'zsh']:
            info['shells'][shell] = {
                'installed': self.is_completion_installed(shell),
                'config_file': str(self.shell_configs[shell]),
                'script_path': str(self.completion_scripts[shell]),
                'script_exists': self.completion_scripts[shell].exists()
            }
        
        return info
    
    def prompt_install_completion(self) -> bool:
        """Prompt user to install completion during setup."""
        current_shell = self.detect_shell()
        if not current_shell:
            log_warning("Could not detect shell for completion installation")
            return False
        
        if self.is_completion_installed(current_shell):
            log_info(f"{current_shell} completion is already installed")
            return True
        
        log_info("Shell completion provides tab completion for ilha commands and subcommands.")
        
        if prompt_yes_no(f"Would you like to install {current_shell} completion?", default=True):
            return self.install_completion(current_shell)
        else:
            log_info("Skipped completion installation")
            log_info("You can install it later with: ilha completion install")
            return True
