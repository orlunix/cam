#!/bin/bash
# CM Executor V3 - TMUX Edition
# Stable session management using tmux instead of exec+pipe

set -euo pipefail

# Configuration
CM_DATA_DIR="${CM_DATA_DIR:-$HOME/.cm}"
CM_SOCKET_DIR="${CM_SOCKET_DIR:-${TMPDIR:-/tmp}/cm-tmux-sockets}"
SESSION_ID="${1:-}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log() {
    echo -e "${BLUE}[CM-TMUX]${NC} $*"
}

error() {
    echo -e "${RED}[ERROR]${NC} $*" >&2
}

success() {
    echo -e "${GREEN}[SUCCESS]${NC} $*"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

# Validate session ID
if [[ -z "$SESSION_ID" ]]; then
    error "Usage: $0 <session-id>"
    exit 1
fi

# Load session metadata
SESSION_FILE="$CM_DATA_DIR/sessions/active/${SESSION_ID}.json"
if [[ ! -f "$SESSION_FILE" ]]; then
    error "Session not found: $SESSION_ID"
    exit 1
fi

log "Loading session metadata: $SESSION_ID"

# Parse JSON (using jq if available, otherwise basic grep)
if command -v jq >/dev/null 2>&1; then
    CONTEXT_PATH=$(jq -r '.contextPath' "$SESSION_FILE")
    TOOL=$(jq -r '.tool' "$SESSION_FILE")
    TASK=$(jq -r '.task' "$SESSION_FILE")
    AUTO_CONFIRM=$(jq -r '.autoConfirm // true' "$SESSION_FILE")
else
    CONTEXT_PATH=$(grep -oP '"contextPath"\s*:\s*"\K[^"]+' "$SESSION_FILE")
    TOOL=$(grep -oP '"tool"\s*:\s*"\K[^"]+' "$SESSION_FILE")
    TASK=$(grep -oP '"task"\s*:\s*"\K[^"]+' "$SESSION_FILE")
    AUTO_CONFIRM=true
fi

log "Context: $CONTEXT_PATH"
log "Tool: $TOOL"
log "Task: $TASK"
log "Auto-confirm: $AUTO_CONFIRM"

# Setup TMUX socket
mkdir -p "$CM_SOCKET_DIR"
SOCKET="$CM_SOCKET_DIR/${SESSION_ID}.sock"
TMUX_SESSION="cm-${SESSION_ID}"

log "TMUX socket: $SOCKET"
log "TMUX session: $TMUX_SESSION"

# Create TMUX session
log "Creating TMUX session..."
if tmux -S "$SOCKET" has-session -t "$TMUX_SESSION" 2>/dev/null; then
    warn "Session already exists, killing..."
    tmux -S "$SOCKET" kill-session -t "$TMUX_SESSION"
fi

tmux -S "$SOCKET" new-session -d -s "$TMUX_SESSION" -n "$TOOL"
success "TMUX session created: $TMUX_SESSION"

# Start the coding tool
log "Starting $TOOL in $CONTEXT_PATH..."
TOOL_COMMAND="cd '$CONTEXT_PATH' && $TOOL"

# Add task to command based on tool
case "$TOOL" in
    codex)
        TOOL_COMMAND="$TOOL_COMMAND exec --full-auto '$TASK'"
        ;;
    claude)
        TOOL_COMMAND="$TOOL_COMMAND"
        # Task will be sent as input after startup
        ;;
    *)
        TOOL_COMMAND="$TOOL_COMMAND '$TASK'"
        ;;
esac

log "Command: $TOOL_COMMAND"
tmux -S "$SOCKET" send-keys -t "$TMUX_SESSION:0.0" "$TOOL_COMMAND" Enter

# Wait for tool to start
log "Waiting for tool to start..."
sleep 2

# Capture initial output
log "Initial output:"
tmux -S "$SOCKET" capture-pane -p -J -t "$TMUX_SESSION:0.0" -S -30 | tail -20

# For Claude, send the task as input
if [[ "$TOOL" == "claude" ]]; then
    log "Sending task to Claude..."
    sleep 2
    
    # Check if we need to handle startup prompts first
    OUTPUT=$(tmux -S "$SOCKET" capture-pane -p -J -t "$TMUX_SESSION:0.0" -S -30)
    
    if echo "$OUTPUT" | grep -q "trust this folder"; then
        log "Detected trust prompt, auto-confirming..."
        tmux -S "$SOCKET" send-keys -t "$TMUX_SESSION:0.0" Enter
        sleep 1
    fi
    
    # Send the actual task
    tmux -S "$SOCKET" send-keys -t "$TMUX_SESSION:0.0" -l -- "$TASK"
    sleep 0.2
    tmux -S "$SOCKET" send-keys -t "$TMUX_SESSION:0.0" Enter
    log "Task sent to Claude"
fi

# Update session status
log "Updating session status to 'running'..."
if command -v jq >/dev/null 2>&1; then
    TMP_FILE=$(mktemp)
    jq '.status = "running" | .state = "starting" | .tmuxSocket = "'"$SOCKET"'" | .tmuxSession = "'"$TMUX_SESSION"'"' \
        "$SESSION_FILE" > "$TMP_FILE"
    mv "$TMP_FILE" "$SESSION_FILE"
else
    # Fallback: just update status
    sed -i 's/"status": *"[^"]*"/"status": "running"/' "$SESSION_FILE"
fi

# Start monitoring loop
log "Starting monitoring loop..."
LOG_FILE="$CM_DATA_DIR/sessions/active/${SESSION_ID}.log"
LAST_OUTPUT=""
ITERATION=0

monitor_loop() {
    while true; do
        ITERATION=$((ITERATION + 1))
        
        # Check if session still exists
        if ! tmux -S "$SOCKET" has-session -t "$TMUX_SESSION" 2>/dev/null; then
            warn "TMUX session terminated"
            break
        fi
        
        # Capture current output
        CURRENT_OUTPUT=$(tmux -S "$SOCKET" capture-pane -p -J -t "$TMUX_SESSION:0.0" -S -50)
        
        # Only process if output changed
        if [[ "$CURRENT_OUTPUT" != "$LAST_OUTPUT" ]]; then
            # Append to log
            echo "=== Iteration $ITERATION $(date '+%H:%M:%S') ===" >> "$LOG_FILE"
            echo "$CURRENT_OUTPUT" >> "$LOG_FILE"
            echo "" >> "$LOG_FILE"
            
            # Check for prompts (auto-confirm logic)
            if [[ "$AUTO_CONFIRM" == "true" ]]; then
                # Pattern 1: Yes/No prompts
                if echo "$CURRENT_OUTPUT" | tail -5 | grep -qiE "(Do you want|Apply|Accept|Continue).*(y/n|\[Y/n\]|\(yes/no\))"; then
                    log "[AUTO] Detected yes/no prompt, sending 'y'"
                    tmux -S "$SOCKET" send-keys -t "$TMUX_SESSION:0.0" "y" Enter
                    sleep 0.5
                    continue
                fi
                
                # Pattern 2: Option selection (1/2/3)
                if echo "$CURRENT_OUTPUT" | tail -10 | grep -qE "❯.*1\..*Yes|^[[:space:]]*1\."; then
                    log "[AUTO] Detected option prompt, selecting '1'"
                    tmux -S "$SOCKET" send-keys -t "$TMUX_SESSION:0.0" Enter
                    sleep 0.5
                    continue
                fi
                
                # Pattern 3: Press Enter to continue
                if echo "$CURRENT_OUTPUT" | tail -5 | grep -qiE "(Press Enter|Hit return|Enter to continue)"; then
                    log "[AUTO] Detected Enter prompt, sending Enter"
                    tmux -S "$SOCKET" send-keys -t "$TMUX_SESSION:0.0" Enter
                    sleep 0.5
                    continue
                fi
                
                # Pattern 4: Trust folder prompt
                if echo "$CURRENT_OUTPUT" | tail -10 | grep -qE "trust this folder"; then
                    log "[AUTO] Detected trust prompt, confirming"
                    tmux -S "$SOCKET" send-keys -t "$TMUX_SESSION:0.0" Enter
                    sleep 0.5
                    continue
                fi
            fi
            
            # Detect state changes
            if echo "$CURRENT_OUTPUT" | tail -10 | grep -qiE "(Planning|Thinking|Analyzing)"; then
                STATE="planning"
            elif echo "$CURRENT_OUTPUT" | tail -10 | grep -qiE "(Editing|Writing|Making changes)"; then
                STATE="editing"
            elif echo "$CURRENT_OUTPUT" | tail -10 | grep -qiE "(Running tests|Testing)"; then
                STATE="testing"
            elif echo "$CURRENT_OUTPUT" | tail -10 | grep -qiE "(Done|Completed|Finished)"; then
                STATE="done"
                log "Task completed!"
                break
            elif echo "$CURRENT_OUTPUT" | tail -10 | grep -qiE "(Error|Failed)"; then
                STATE="failed"
                error "Task failed!"
                break
            else
                STATE="running"
            fi
            
            # Update session state
            if command -v jq >/dev/null 2>&1; then
                TMP_FILE=$(mktemp)
                jq --arg state "$STATE" '.state = $state | .updated = (now | todate)' \
                    "$SESSION_FILE" > "$TMP_FILE"
                mv "$TMP_FILE" "$SESSION_FILE"
            fi
            
            LAST_OUTPUT="$CURRENT_OUTPUT"
        fi
        
        # Poll interval
        sleep 2
    done
}

# Run monitoring in foreground
monitor_loop

# Cleanup
success "Monitoring complete"

# Final status update
if [[ "$STATE" == "done" ]]; then
    FINAL_STATUS="completed"
elif [[ "$STATE" == "failed" ]]; then
    FINAL_STATUS="failed"
else
    FINAL_STATUS="interrupted"
fi

log "Final status: $FINAL_STATUS"

if command -v jq >/dev/null 2>&1; then
    TMP_FILE=$(mktemp)
    jq --arg status "$FINAL_STATUS" --arg state "$STATE" \
        '.status = $status | .state = $state | .completed = (now | todate)' \
        "$SESSION_FILE" > "$TMP_FILE"
    mv "$TMP_FILE" "$SESSION_FILE"
fi

# Capture final output
log "Capturing final output..."
tmux -S "$SOCKET" capture-pane -p -J -t "$TMUX_SESSION:0.0" -S -200 > "${LOG_FILE}.final"

# Run completion hooks
HOOKS_DIR="$CM_DATA_DIR/hooks"
if [[ -f "$HOOKS_DIR/on_session_complete.sh" ]]; then
    log "Running completion hooks..."
    bash "$HOOKS_DIR/on_session_complete.sh" "$SESSION_ID" "$FINAL_STATUS" || true
fi

# Keep session alive for inspection
log "TMUX session kept alive for inspection"
log "Attach: tmux -S '$SOCKET' attach -t '$TMUX_SESSION'"
log "Kill:   tmux -S '$SOCKET' kill-session -t '$TMUX_SESSION'"

exit 0
