#!/bin/bash
#
# E2E Test Script for Claude Cloud Agent
#
# Tests three workflows:
# 1. JIRA Issue → Claude Agent → PR with commits
# 2. GitHub Issue → Claude Agent → PR with commits
# 3. GitHub PR → UAT Environment
#
# Usage:
#   ./e2e-test.sh              # Run all tests
#   ./e2e-test.sh jira         # Run only JIRA test
#   ./e2e-test.sh github       # Run only GitHub Issue test
#   ./e2e-test.sh uat          # Run only UAT test
#   ./e2e-test.sh --cleanup    # Clean up old test resources
#   ./e2e-test.sh --status     # Check status of recent tests
#

set -e

# Ensure PATH includes common locations for aws, gh, etc.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# Configuration
JIRA_SECRET_ARN="arn:aws:secretsmanager:us-east-1:678954237808:secret:claude-cloud-agent/jira-LfM9Px"
GITHUB_REPO="team-mobot/test_tickets"
SESSIONS_TABLE="claude-cloud-agent-sessions"
LAMBDA_LOG_GROUP="/aws/lambda/claude-cloud-agent-webhook"
REGION="us-east-1"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Results tracking (bash 3.2 compatible)
RESULT_JIRA=""
RESULT_GITHUB=""
RESULT_UAT=""

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

# Get JIRA credentials from Secrets Manager
get_jira_creds() {
    aws secretsmanager get-secret-value \
        --secret-id "$JIRA_SECRET_ARN" \
        --region "$REGION" \
        --query 'SecretString' \
        --output text
}

# Wait for Lambda to process (check logs)
wait_for_lambda() {
    local pattern="$1"
    local timeout="${2:-60}"
    local start_time=$(date +%s)

    log_info "Waiting for Lambda to process ($pattern)..."

    while true; do
        local current_time=$(date +%s)
        local elapsed=$((current_time - start_time))

        if [ $elapsed -gt $timeout ]; then
            log_error "Timeout waiting for Lambda ($pattern)"
            return 1
        fi

        local logs=$(aws logs filter-log-events \
            --log-group-name "$LAMBDA_LOG_GROUP" \
            --start-time $((start_time * 1000)) \
            --filter-pattern "$pattern" \
            --region "$REGION" \
            --query 'events[*].message' \
            --output text 2>/dev/null | head -5)

        if [ -n "$logs" ]; then
            if echo "$logs" | grep -q "started successfully\|Session.*started"; then
                log_success "Lambda processed successfully"
                return 0
            elif echo "$logs" | grep -q "ERROR\|Failed"; then
                log_error "Lambda error detected"
                echo "$logs" | head -3
                return 1
            fi
        fi

        sleep 5
    done
}

# Wait for session to be running
wait_for_session() {
    local session_id="$1"
    local timeout="${2:-120}"
    local start_time=$(date +%s)

    log_info "Waiting for session $session_id to be running..."

    while true; do
        local current_time=$(date +%s)
        local elapsed=$((current_time - start_time))

        if [ $elapsed -gt $timeout ]; then
            log_error "Timeout waiting for session $session_id"
            return 1
        fi

        local status=$(aws dynamodb get-item \
            --table-name "$SESSIONS_TABLE" \
            --key "{\"session_id\":{\"S\":\"$session_id\"}}" \
            --region "$REGION" \
            --query 'Item.status.S' \
            --output text 2>/dev/null)

        if [ "$status" = "RUNNING" ]; then
            log_success "Session $session_id is RUNNING"
            return 0
        elif [ "$status" = "FAILED" ] || [ "$status" = "STOPPED" ]; then
            log_error "Session $session_id failed with status: $status"
            return 1
        fi

        sleep 5
    done
}

# Wait for commits on a PR
wait_for_commits() {
    local pr_number="$1"
    local min_commits="${2:-2}"
    local timeout="${3:-300}"
    local start_time=$(date +%s)

    log_info "Waiting for agent to make commits on PR #$pr_number..."

    while true; do
        local current_time=$(date +%s)
        local elapsed=$((current_time - start_time))

        if [ $elapsed -gt $timeout ]; then
            log_error "Timeout waiting for commits on PR #$pr_number"
            return 1
        fi

        local commit_count=$(gh pr view "$pr_number" --repo "$GITHUB_REPO" \
            --json commits --jq '.commits | length' 2>/dev/null || echo "0")

        if [ "$commit_count" -ge "$min_commits" ]; then
            log_success "PR #$pr_number has $commit_count commits"

            # Check for implementation commit (not just session file)
            local last_commit=$(gh pr view "$pr_number" --repo "$GITHUB_REPO" \
                --json commits --jq '.commits[-1].messageHeadline' 2>/dev/null)

            if echo "$last_commit" | grep -qv "initialize Claude agent session"; then
                log_success "Agent made implementation commit: $last_commit"
                return 0
            fi
        fi

        sleep 10
    done
}

# Wait for UAT health check
wait_for_uat_health() {
    local session_id="$1"
    local timeout="${2:-180}"
    local start_time=$(date +%s)
    local url="https://${session_id}.uat.teammobot.dev"

    log_info "Waiting for UAT health at $url..."

    while true; do
        local current_time=$(date +%s)
        local elapsed=$((current_time - start_time))

        if [ $elapsed -gt $timeout ]; then
            log_warn "Timeout waiting for UAT health check"
            # Check ALB target health as fallback
            log_info "Checking ALB target health..."
            local tg_arn=$(aws elbv2 describe-target-groups \
                --region "$REGION" \
                --query "TargetGroups[?contains(TargetGroupName, '${session_id}')].TargetGroupArn" \
                --output text 2>/dev/null)

            if [ -n "$tg_arn" ]; then
                local health=$(aws elbv2 describe-target-health \
                    --target-group-arn "$tg_arn" \
                    --region "$REGION" \
                    --query 'TargetHealthDescriptions[0].TargetHealth.State' \
                    --output text 2>/dev/null)

                if [ "$health" = "healthy" ]; then
                    log_success "ALB target is healthy (direct URL may have network issues)"
                    return 0
                fi
            fi
            return 1
        fi

        # Try health endpoint
        local response=$(curl -s --connect-timeout 5 "${url}/api/health" 2>/dev/null || echo "")

        if [ -n "$response" ] && [ "$response" != "504 Gateway Time-out" ]; then
            log_success "UAT is healthy: $url"
            return 0
        fi

        sleep 10
    done
}

#############################################
# TEST: JIRA Issue → Claude Agent
#############################################
test_jira() {
    log_info "=========================================="
    log_info "TEST: JIRA Issue → Claude Agent"
    log_info "=========================================="

    # Get JIRA credentials
    local jira_creds=$(get_jira_creds)
    local jira_url=$(echo "$jira_creds" | jq -r '.base_url')
    local jira_email=$(echo "$jira_creds" | jq -r '.email')
    local jira_token=$(echo "$jira_creds" | jq -r '.api_token')

    # Create JIRA issue
    log_info "Creating JIRA issue in AGNTS project..."

    local issue_data=$(cat <<EOF
{
    "fields": {
        "project": {"key": "AGNTS"},
        "summary": "E2E Test: Badge ${TIMESTAMP}",
        "description": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Add a test badge showing 'E2E-JIRA-${TIMESTAMP}' in the sidebar footer. This is an automated E2E test."}
                    ]
                }
            ]
        },
        "issuetype": {"name": "Task"}
    }
}
EOF
)

    local issue_response=$(curl -s -X POST \
        -H "Content-Type: application/json" \
        -u "${jira_email}:${jira_token}" \
        -d "$issue_data" \
        "${jira_url}/rest/api/3/issue")

    local issue_key=$(echo "$issue_response" | jq -r '.key')

    if [ "$issue_key" = "null" ] || [ -z "$issue_key" ]; then
        log_error "Failed to create JIRA issue"
        echo "$issue_response"
        RESULT_JIRA="FAILED (create issue)"
        return 1
    fi

    log_success "Created JIRA issue: $issue_key"

    # Add claude-dev label
    log_info "Adding claude-dev label to $issue_key..."

    curl -s -X PUT \
        -H "Content-Type: application/json" \
        -u "${jira_email}:${jira_token}" \
        -d '{"update": {"labels": [{"add": "claude-dev"}]}}' \
        "${jira_url}/rest/api/3/issue/${issue_key}" > /dev/null

    # Wait for Lambda to process
    if ! wait_for_lambda "$issue_key" 60; then
        RESULT_JIRA="FAILED (lambda)"
        return 1
    fi

    # Find the PR number from session
    sleep 5
    local pr_number=$(aws dynamodb scan \
        --table-name "$SESSIONS_TABLE" \
        --filter-expression "contains(branch_name, :key)" \
        --expression-attribute-values "{\":key\":{\"S\":\"${issue_key,,}\"}}" \
        --region "$REGION" \
        --query 'Items[0].pr_number.N' \
        --output text 2>/dev/null)

    if [ -z "$pr_number" ] || [ "$pr_number" = "None" ]; then
        log_error "Could not find PR for $issue_key"
        RESULT_JIRA="FAILED (no PR)"
        return 1
    fi

    log_success "Found PR #$pr_number"

    # Wait for commits
    if ! wait_for_commits "$pr_number" 2 300; then
        RESULT_JIRA="FAILED (no commits)"
        return 1
    fi

    RESULT_JIRA="PASSED"
    log_success "JIRA test PASSED: $issue_key → PR #$pr_number"
    echo "  Issue: ${jira_url}/browse/${issue_key}"
    echo "  PR: https://github.com/${GITHUB_REPO}/pull/${pr_number}"
    return 0
}

#############################################
# TEST: GitHub Issue → Claude Agent
#############################################
test_github_issue() {
    log_info "=========================================="
    log_info "TEST: GitHub Issue → Claude Agent"
    log_info "=========================================="

    # Create GitHub issue
    log_info "Creating GitHub issue..."

    local issue_url=$(gh issue create \
        --repo "$GITHUB_REPO" \
        --title "E2E Test: Badge ${TIMESTAMP}" \
        --body "Add a test badge showing 'E2E-GITHUB-${TIMESTAMP}' in the sidebar footer. This is an automated E2E test." \
        --label "claude-dev" 2>&1)

    local issue_number=$(echo "$issue_url" | grep -oE '[0-9]+$')

    if [ -z "$issue_number" ]; then
        log_error "Failed to create GitHub issue"
        RESULT_GITHUB="FAILED (create issue)"
        return 1
    fi

    log_success "Created GitHub issue #$issue_number"

    # Wait for Lambda to process
    if ! wait_for_lambda "issue #$issue_number" 60; then
        RESULT_GITHUB="FAILED (lambda)"
        return 1
    fi

    # Find the PR number
    sleep 5
    local pr_number=$(gh pr list \
        --repo "$GITHUB_REPO" \
        --head "claude/$issue_number" \
        --json number \
        --jq '.[0].number' 2>/dev/null)

    if [ -z "$pr_number" ] || [ "$pr_number" = "null" ]; then
        log_error "Could not find PR for issue #$issue_number"
        RESULT_GITHUB="FAILED (no PR)"
        return 1
    fi

    log_success "Found PR #$pr_number"

    # Wait for commits
    if ! wait_for_commits "$pr_number" 2 300; then
        RESULT_GITHUB="FAILED (no commits)"
        return 1
    fi

    RESULT_GITHUB="PASSED"
    log_success "GitHub Issue test PASSED: Issue #$issue_number → PR #$pr_number"
    echo "  Issue: https://github.com/${GITHUB_REPO}/issues/${issue_number}"
    echo "  PR: https://github.com/${GITHUB_REPO}/pull/${pr_number}"
    return 0
}

#############################################
# TEST: GitHub PR → UAT Environment
#############################################
test_uat() {
    log_info "=========================================="
    log_info "TEST: GitHub PR → UAT Environment"
    log_info "=========================================="

    local branch_name="e2e-uat-${TIMESTAMP}"

    # Create branch and make a change
    log_info "Creating branch $branch_name..."

    # Clone if needed, or use existing
    local repo_dir="/tmp/e2e-test-$$"
    gh repo clone "$GITHUB_REPO" "$repo_dir" -- --depth 1 2>/dev/null || true
    cd "$repo_dir"

    git checkout -b "$branch_name"

    # Make a simple change
    echo "// E2E Test: ${TIMESTAMP}" >> src/App.tsx
    git add src/App.tsx
    git commit -m "test: E2E UAT verification ${TIMESTAMP}"
    git push origin "$branch_name"

    # Create PR
    log_info "Creating PR..."
    local pr_url=$(gh pr create \
        --repo "$GITHUB_REPO" \
        --title "E2E Test: UAT ${TIMESTAMP}" \
        --body "Automated E2E test for UAT environment." \
        --head "$branch_name" \
        --base main)

    local pr_number=$(echo "$pr_url" | grep -oE '[0-9]+$')

    if [ -z "$pr_number" ]; then
        log_error "Failed to create PR"
        RESULT_UAT="FAILED (create PR)"
        cd - > /dev/null
        rm -rf "$repo_dir"
        return 1
    fi

    log_success "Created PR #$pr_number"

    # Add uat label
    log_info "Adding uat label..."
    gh pr edit "$pr_number" --repo "$GITHUB_REPO" --add-label "uat"

    # Derive session ID
    local session_id="tt-$(echo "$branch_name" | tr '/' '-' | tr '_' '-' | tr '[:upper:]' '[:lower:]')"
    session_id="${session_id:0:50}"

    log_info "Expected session ID: $session_id"

    # Wait for Lambda to process
    if ! wait_for_lambda "PR #$pr_number" 60; then
        RESULT_UAT="FAILED (lambda)"
        cd - > /dev/null
        rm -rf "$repo_dir"
        return 1
    fi

    # Wait for session to be running
    if ! wait_for_session "$session_id" 120; then
        RESULT_UAT="FAILED (session)"
        cd - > /dev/null
        rm -rf "$repo_dir"
        return 1
    fi

    # Wait for health check
    if ! wait_for_uat_health "$session_id" 180; then
        RESULT_UAT="FAILED (health)"
        cd - > /dev/null
        rm -rf "$repo_dir"
        return 1
    fi

    RESULT_UAT="PASSED"
    log_success "UAT test PASSED: PR #$pr_number"
    echo "  PR: https://github.com/${GITHUB_REPO}/pull/${pr_number}"
    echo "  UAT: https://${session_id}.uat.teammobot.dev"

    cd - > /dev/null
    rm -rf "$repo_dir"
    return 0
}

#############################################
# Check status of recent E2E tests
#############################################
check_status() {
    log_info "Checking status of recent E2E tests..."
    echo ""

    # Get recent sessions
    log_info "Recent sessions:"
    aws dynamodb scan \
        --table-name "$SESSIONS_TABLE" \
        --region "$REGION" \
        --filter-expression "begins_with(branch_name, :claude) OR begins_with(session_id, :tt)" \
        --expression-attribute-values '{":claude":{"S":"claude/"},":tt":{"S":"tt-"}}' \
        --query 'Items | sort_by(@, &updated_at.S) | reverse(@) | [0:10].{session_id:session_id.S,status:status.S,pr_number:pr_number.N,branch:branch_name.S}' \
        --output table 2>/dev/null || log_warn "Could not get sessions"

    echo ""

    # Get recent E2E PRs
    log_info "Recent E2E PRs with commits:"
    gh pr list --repo "$GITHUB_REPO" --state all --limit 10 --json number,title,commits,state \
        --jq '.[] | select(.title | startswith("E2E Test") or startswith("[Claude]")) | "\(.number) [\(.state)] \(.title) - \(.commits | length) commits"' \
        2>/dev/null | head -10 || log_warn "Could not get PRs"

    echo ""

    # Check UAT health
    log_info "Checking UAT environments:"
    local uat_sessions=$(aws dynamodb scan \
        --table-name "$SESSIONS_TABLE" \
        --region "$REGION" \
        --filter-expression "begins_with(session_id, :tt) AND #s = :running" \
        --expression-attribute-names '{"#s":"status"}' \
        --expression-attribute-values '{":tt":{"S":"tt-"},":running":{"S":"RUNNING"}}' \
        --query 'Items[*].session_id.S' \
        --output text 2>/dev/null)

    for session_id in $uat_sessions; do
        local url="https://${session_id}.uat.teammobot.dev"
        local health=$(curl -s --connect-timeout 5 "${url}/api/health" 2>/dev/null || echo "timeout")
        if [ "$health" != "timeout" ] && [ -n "$health" ]; then
            echo -e "  ${GREEN}✓${NC} $session_id: healthy"
        else
            # Check ALB target
            local tg_arn=$(aws elbv2 describe-target-groups \
                --region "$REGION" \
                --query "TargetGroups[?contains(TargetGroupName, '${session_id}')].TargetGroupArn" \
                --output text 2>/dev/null)
            if [ -n "$tg_arn" ]; then
                local tg_health=$(aws elbv2 describe-target-health \
                    --target-group-arn "$tg_arn" \
                    --region "$REGION" \
                    --query 'TargetHealthDescriptions[0].TargetHealth.State' \
                    --output text 2>/dev/null)
                echo -e "  ${YELLOW}~${NC} $session_id: ALB target=$tg_health (direct URL timeout)"
            else
                echo -e "  ${RED}✗${NC} $session_id: no target group found"
            fi
        fi
    done
}

#############################################
# Cleanup old test resources
#############################################
cleanup() {
    log_info "Cleaning up old E2E test resources..."

    # Close old E2E PRs
    log_info "Closing old E2E test PRs..."
    local old_prs=$(gh pr list --repo "$GITHUB_REPO" --state open --json number,title \
        --jq '.[] | select(.title | startswith("E2E Test:")) | .number')

    for pr in $old_prs; do
        log_info "Closing PR #$pr"
        gh pr close "$pr" --repo "$GITHUB_REPO" --delete-branch 2>/dev/null || true
    done

    # Close old E2E issues
    log_info "Closing old E2E test issues..."
    local old_issues=$(gh issue list --repo "$GITHUB_REPO" --state open --json number,title \
        --jq '.[] | select(.title | startswith("E2E Test:")) | .number')

    for issue in $old_issues; do
        log_info "Closing issue #$issue"
        gh issue close "$issue" --repo "$GITHUB_REPO" 2>/dev/null || true
    done

    log_success "Cleanup complete"
}

#############################################
# Print summary
#############################################
print_summary() {
    echo ""
    log_info "=========================================="
    log_info "E2E TEST SUMMARY"
    log_info "=========================================="

    local all_passed=true

    if [ -n "$RESULT_JIRA" ]; then
        if [[ "$RESULT_JIRA" == "PASSED" ]]; then
            echo -e "  ${GREEN}✓${NC} jira: $RESULT_JIRA"
        else
            echo -e "  ${RED}✗${NC} jira: $RESULT_JIRA"
            all_passed=false
        fi
    fi

    if [ -n "$RESULT_GITHUB" ]; then
        if [[ "$RESULT_GITHUB" == "PASSED" ]]; then
            echo -e "  ${GREEN}✓${NC} github: $RESULT_GITHUB"
        else
            echo -e "  ${RED}✗${NC} github: $RESULT_GITHUB"
            all_passed=false
        fi
    fi

    if [ -n "$RESULT_UAT" ]; then
        if [[ "$RESULT_UAT" == "PASSED" ]]; then
            echo -e "  ${GREEN}✓${NC} uat: $RESULT_UAT"
        else
            echo -e "  ${RED}✗${NC} uat: $RESULT_UAT"
            all_passed=false
        fi
    fi

    echo ""
    if $all_passed; then
        log_success "All tests PASSED"
        return 0
    else
        log_error "Some tests FAILED"
        return 1
    fi
}

#############################################
# Main
#############################################
main() {
    echo ""
    log_info "Claude Cloud Agent E2E Tests"
    log_info "Timestamp: $TIMESTAMP"
    echo ""

    local run_jira=false
    local run_github=false
    local run_uat=false
    local run_cleanup=false

    # Parse arguments
    if [ $# -eq 0 ]; then
        run_jira=true
        run_github=true
        run_uat=true
    else
        for arg in "$@"; do
            case "$arg" in
                jira) run_jira=true ;;
                github) run_github=true ;;
                uat) run_uat=true ;;
                --cleanup) run_cleanup=true ;;
                --status) check_status; exit 0 ;;
                *) log_error "Unknown argument: $arg"; exit 1 ;;
            esac
        done
    fi

    # Run cleanup if requested
    if $run_cleanup; then
        cleanup
        exit 0
    fi

    # Check status if requested
    if [ "$1" = "--status" ]; then
        check_status
        exit 0
    fi

    # Run tests
    if $run_jira; then
        test_jira || true
        echo ""
    fi

    if $run_github; then
        test_github_issue || true
        echo ""
    fi

    if $run_uat; then
        test_uat || true
        echo ""
    fi

    # Print summary
    print_summary
}

main "$@"
