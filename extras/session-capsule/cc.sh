#!/bin/bash
# usage: cc.sh <config_dir> <cwd> <claude args...>
CFG="$1"; DIR="$2"; shift 2
cd "$DIR" || exit 99
# Unset identifiers inherited from the parent session so the child is a genuinely separate session.
env -u CLAUDE_CODE_SESSION_ID -u CLAUDECODE -u CLAUDE_CODE_REMOTE_SESSION_ID \
  CLAUDE_CONFIG_DIR="$CFG" timeout 180 claude "$@"
