#!/bin/bash
# Bash completion script for ilha CLI
# This script provides tab completion for ilha commands, worktree names, and options

_ilha_completion() {
    local cur prev words cword
    _init_completion || return
    
    # Main commands (including aliases)
    local commands="start-proxy stop-proxy start stop create delete remove remove-all delete-all list prune volumes packages droplet image secrets worker domains setup help completion -D -r"
    
    # Commands that need worktree names
    local worktree_cmds="create delete remove"
    
    # Commands that can be used with worktree names (new pattern)
    local worktree_actions="up down"
    
    # Docker compose passthrough commands
    local compose_commands="exec logs ps run build pull push restart start stop config images port top events kill pause unpause scale"
    
    # Volume subcommands
    local volume_subcmds="list size backup restore clean"
    
    # Completion subcommands
    local completion_cmds="install uninstall status"
    
    # Shell options for completion install
    local shells="bash zsh"
    
    # Flags
    local flags="--force -d --detach --help -h"
    
    # Handle main command completion
    if [[ $cword -eq 1 ]]; then
        # First position: complete with commands OR worktree names
        local worktrees=$(ilha _completion worktrees 2>/dev/null)
        local all_options="$commands $worktrees"
        COMPREPLY=( $(compgen -W "$all_options" -- "$cur") )
        return
    fi
    
    # Handle command-specific completion
    case "${words[1]}" in
        create)
            # For create, we can complete with existing git branches or suggest new names
            local git_branches=$(ilha _completion git 2>/dev/null)
            COMPREPLY=( $(compgen -W "$git_branches" -- "$cur") )
            ;;
        up|down)
            # For up/down commands, complete with worktree names or flags
            if [[ "$cur" == -* ]]; then
                if [[ "${words[1]}" == "up" ]]; then
                    COMPREPLY=( $(compgen -W "-d --detach" -- "$cur") )
                else
                    COMPREPLY=()
                fi
            else
                local worktrees=$(ilha _completion worktrees 2>/dev/null)
                COMPREPLY=( $(compgen -W "$worktrees" -- "$cur") )
            fi
            ;;
        delete|remove|-D|-r)
            # For delete/remove (including aliases), complete with worktree names and flags
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "--force" -- "$cur") )
            else
                local worktrees=$(ilha _completion worktrees 2>/dev/null)
                COMPREPLY=( $(compgen -W "$worktrees" -- "$cur") )
            fi
            ;;
        *)
            # Check if first word is a worktree name and second word is up/down or docker compose command
            if [[ $cword -eq 2 ]]; then
                # Check if first word is not a command (i.e., it's a worktree name)
                if [[ ! " $commands " =~ " ${words[1]} " ]]; then
                    # This is a worktree name, complete with up/down or docker compose commands
                    local all_worktree_options="$worktree_actions $compose_commands"
                    COMPREPLY=( $(compgen -W "$all_worktree_options" -- "$cur") )
                    return
                fi
            elif [[ $cword -eq 3 && ! " $commands " =~ " ${words[1]} " ]]; then
                # Third position after worktree_name up/down or docker compose command
                if [[ "${words[2]}" == "up" ]]; then
                    if [[ "$cur" == -* ]]; then
                        COMPREPLY=( $(compgen -W "-d --detach --secrets" -- "$cur") )
                    else
                        COMPREPLY=()
                    fi
                elif [[ "${words[2]}" == "exec" ]]; then
                    # After worktree_name exec, complete with service names
                    local services=$(ilha _completion services 2>/dev/null)
                    if [[ -z "$services" ]]; then
                        services="web db redis postgres mysql nginx apache"
                    fi
                    COMPREPLY=( $(compgen -W "$services" -- "$cur") )
                elif [[ "${words[2]}" == "logs" ]]; then
                    # After worktree_name logs, complete with service names and flags
                    if [[ "$cur" == -* ]]; then
                        COMPREPLY=( $(compgen -W "-f --follow --tail --since --until" -- "$cur") )
                    else
                        local services=$(ilha _completion services 2>/dev/null)
                        if [[ -z "$services" ]]; then
                            services="web db redis postgres mysql nginx apache"
                        fi
                        COMPREPLY=( $(compgen -W "$services" -- "$cur") )
                    fi
                elif [[ "${words[2]}" == "run" ]]; then
                    # After worktree_name run, complete with service names and flags
                    if [[ "$cur" == -* ]]; then
                        COMPREPLY=( $(compgen -W "--rm --no-deps --entrypoint" -- "$cur") )
                    else
                        local services=$(ilha _completion services 2>/dev/null)
                        if [[ -z "$services" ]]; then
                            services="web db redis postgres mysql nginx apache"
                        fi
                        COMPREPLY=( $(compgen -W "$services" -- "$cur") )
                    fi
                else
                    # For other docker compose commands, just pass through
                    COMPREPLY=()
                fi
                return
            fi
            ;;
        droplet)
            # Handle droplet subcommands
            local droplet_subcmds="create list sizes destroy info push"
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=( $(compgen -W "$droplet_subcmds" -- "$cur") )
            else
                COMPREPLY=()
            fi
            ;;
        image)
            local image_subcmds="build push publish list destroy"
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=( $(compgen -W "$image_subcmds" -- "$cur") )
            else
                COMPREPLY=()
            fi
            ;;
        secrets)
            local secrets_subcmds="list edit print exec materialize"
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=( $(compgen -W "$secrets_subcmds" -- "$cur") )
            else
                COMPREPLY=()
            fi
            ;;
        worker)
            local worker_subcmds="bundle deploy list destroy"
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=( $(compgen -W "$worker_subcmds" -- "$cur") )
            else
                COMPREPLY=()
            fi
            ;;
        volumes)
            # Handle volumes subcommands
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=( $(compgen -W "$volume_subcmds" -- "$cur") )
            elif [[ $cword -eq 3 ]]; then
                # For backup, restore, clean - complete with worktree names
                case "${words[2]}" in
                    backup|restore|clean)
                        local worktrees=$(ilha _completion worktrees 2>/dev/null)
                        COMPREPLY=( $(compgen -W "$worktrees" -- "$cur") )
                        ;;
                    *)
                        # For list, size - no additional completion needed
                        COMPREPLY=()
                        ;;
                esac
            else
                COMPREPLY=()
            fi
            ;;
        completion)
            # Handle completion subcommands
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=( $(compgen -W "$completion_cmds" -- "$cur") )
            elif [[ $cword -eq 3 && "${words[2]}" == "install" ]]; then
                COMPREPLY=( $(compgen -W "$shells" -- "$cur") )
            else
                COMPREPLY=()
            fi
            ;;
        remove-all|delete-all)
            # For remove-all/delete-all, only complete with flags
            COMPREPLY=( $(compgen -W "--force" -- "$cur") )
            ;;
        setup)
            # For setup, complete with flags
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "--project-name" -- "$cur") )
            else
                COMPREPLY=()
            fi
            ;;
        *)
            # For other commands, complete with flags
            COMPREPLY=( $(compgen -W "$flags" -- "$cur") )
            ;;
    esac
}

# Register the completion function
complete -F _ilha_completion ilha

# Also register for ilha if it's used as an alias
complete -F _ilha_completion ilha
