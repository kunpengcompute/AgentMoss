#!/bin/bash
# AgentGate Shell Wrapper for generic Agents
#
# Usage:
#   agent-gate-wrap <command>
#   AGENT_SESSION_ID=sess-001 agent-gate-wrap ls -la
#
# Environment variables:
#   AGENT_GATE_URL     - AgentGate service URL (default: http://127.0.0.1:9100)
#   AGENT_SESSION_ID   - Session identifier (default: auto-generated)
#   AGENT_TYPE         - Agent type (default: generic)

set -e

GATE_URL="${AGENT_GATE_URL:-http://127.0.0.1:9100}"
SESSION_ID="${AGENT_SESSION_ID:-shell-$(date +%s)-$$}"
AGENT_TYPE="${AGENT_TYPE:-generic}"
COMMAND="$*"

if [ -z "$COMMAND" ]; then
    echo "Usage: agent-gate-wrap <command>" >&2
    exit 1
fi

# Build JSON payload
PAYLOAD=$(cat <<EOF
{
    "agent_type": "${AGENT_TYPE}",
    "session_id": "${SESSION_ID}",
    "tool_name": "bash",
    "command": "${COMMAND}",
    "cwd": "$(pwd)"
}
EOF
)

# Call AgentGate
RESPONSE=$(curl -s -X POST "${GATE_URL}/api/v1/analyze" \
    -H "Content-Type: application/json" \
    -d "${PAYLOAD}" 2>/dev/null) || {
    echo "[AgentGate] Failed to reach AgentGate service at ${GATE_URL}" >&2
    exit 126
}

# Parse decision
DECISION=$(echo "$RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('decision','Deny'))" 2>/dev/null || echo "Deny")
REASON=$(echo "$RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('reason','Unknown'))" 2>/dev/null || echo "Unknown")

if [ "$DECISION" = "Deny" ]; then
    echo "[AgentGate] Operation blocked: ${REASON}" >&2
    exit 126
fi

# Execute the original command
exec /bin/bash -c "$COMMAND"
