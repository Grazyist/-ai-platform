#!/bin/bash
# User isolation manager for AI Platform
# Creates system users with restricted SSH access and project directories

set -e

ACTION="$1"
USERNAME="$2"
PASSWORD="$3"  # platform password (also used for SSH initially)

HOME_BASE="/home"
PROJECTS_BASE="/home"

create_user() {
    local ssh_user="$1"
    local pass="$2"

    if id "$ssh_user" &>/dev/null; then
        echo "User $ssh_user already exists"
        return 0
    fi

    # Create system user with home directory
    useradd -m -d "$HOME_BASE/$ssh_user" -s /bin/bash "$ssh_user"
    echo "$ssh_user:$pass" | chpasswd

    # Create projects directory
    mkdir -p "$PROJECTS_BASE/$ssh_user/projects"
    chown -R "$ssh_user:$ssh_user" "$PROJECTS_BASE/$ssh_user/projects"

    # Restrict user to their home directory via SSH (optional chroot)
    # For now, use basic file permissions
    chmod 750 "$HOME_BASE/$ssh_user"

    echo "Created user $ssh_user with projects dir at $PROJECTS_BASE/$ssh_user/projects"
}

delete_user() {
    local ssh_user="$1"
    if id "$ssh_user" &>/dev/null; then
        userdel -r "$ssh_user" 2>/dev/null || true
        echo "Deleted user $ssh_user"
    fi
}

sync_project_files() {
    local ssh_user="$1"
    local project_name="$2"
    local project_dir="$PROJECTS_BASE/$ssh_user/projects/$project_name"
    mkdir -p "$project_dir"
    chown -R "$ssh_user:$ssh_user" "$project_dir"
    echo "Synced project dir: $project_dir"
}

case "$ACTION" in
    create)   create_user "$USERNAME" "$PASSWORD" ;;
    delete)   delete_user "$USERNAME" ;;
    sync)     sync_project_files "$USERNAME" "$2" ;;
    *)        echo "Usage: $0 {create|delete|sync} <username> [password|project_name]" ;;
esac
