# Claude Cloud Agent - Design Document

## Vision

An autonomous development agent that implements features and fixes bugs based on GitHub issues. When a user labels an issue with a trigger label, the system automatically creates a branch, opens a draft PR, and spins up a container running Claude Code. The agent implements the requested changes, commits to the branch, and posts progress as PR comments. Users interact with the agent by commenting on the PR and can test changes via a UAT dev server running in the same container.

## Problem Statement

Developers want to delegate routine implementation tasks to an AI agent while maintaining visibility and control. The agent should:

1. Work autonomously on well-defined tasks
2. Provide real-time progress updates
3. Accept feedback and iterate
4. Clean up when work is complete

## User Experience

### Starting a Session

1. User creates a GitHub issue describing the work to be done
2. User adds the `claude-dev` label to the issue
3. System creates a feature branch and draft PR
4. Agent container starts and reports its UAT URL
5. Agent begins implementing the requested changes

### During a Session

- Agent posts progress updates as PR comments
- User can view work-in-progress at the UAT URL (via existing UAT proxy infrastructure)
- User can provide feedback by commenting on the PR
- Agent incorporates feedback and continues working
- All changes are committed to the feature branch

### Ending a Session

- User merges or closes the PR
- Agent terminates gracefully
- Container resources are cleaned up

## Key Design Decisions

### 1. One Long-Running Container Per Issue

Each issue gets a single container that runs for the entire session lifecycle, from issue labeled to PR closed.

**Rationale:**
- Preserves Claude's conversation context across interactions
- Avoids startup overhead on each feedback cycle
- Keeps git working directory state intact
- Maintains stable UAT URL throughout session

**Alternative rejected:** Spawning a new container for each PR comment would lose context, change the UAT URL, and add startup time per interaction.

### 2. Agent and Dev Server in Same Container

The container runs both the Claude Code agent and the target application's dev server.

**Rationale:**
- Claude can test its own changes in real-time
- Users see work-in-progress without waiting for "done"
- Hot reload shows changes as Claude edits files
- When users provide feedback, they can reference current UI state

### 3. Prompt Queue Architecture

PR comments are routed to the running container via an internal API, not by launching new tasks.

**Flow:**
1. User comments on PR
2. Webhook handler looks up the container's IP from session state
3. Handler POSTs the comment to the container's prompt API
4. Agent queues the prompt and processes when ready

**Rationale:**
- No container startup delay for feedback
- Natural conversation flow with maintained context
- Container controls its own processing pace

### 4. Self-Reported Container IP

The container discovers and reports its own public IP to the session database, rather than the webhook handler waiting for AWS to assign it.

**Rationale:**
- Webhook handler can return immediately (fast user feedback)
- Avoids Lambda timeout waiting for ECS task startup
- Container reports "ready" only when actually ready

## Requirements

### Functional Requirements

| ID | Requirement |
|----|-------------|
| F1 | Adding trigger label to issue creates branch and draft PR |
| F2 | Agent processes issue body as initial prompt |
| F3 | PR comments are routed to running agent |
| F4 | Agent posts progress updates as PR comments |
| F5 | Agent commits changes to feature branch |
| F6 | Agent runs the target app's dev server for UAT access |
| F7 | Session terminates when PR is closed or merged |
| F8 | Session terminates after configurable idle timeout |
| F9 | Bot comments do not trigger feedback loops |

### Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| N1 | Session startup completes within 2 minutes |
| N2 | PR comment routing latency under 5 seconds |
| N3 | Session state survives temporary network issues |
| N4 | Failed sessions can be restarted by re-labeling |

## System Components

### Webhook Handler

Receives GitHub webhooks and orchestrates the system.

**Responsibilities:**
- Validate webhook signatures
- Create branches and draft PRs via GitHub API
- Launch agent containers for new sessions
- Route PR comments to running containers
- Detect and ignore bot comments

### Session Store

Maintains state for active sessions.

**Key Fields:**
- Session identifier
- Container IP and ports
- Session status (starting, running, completed, failed)
- Associated PR and branch

### Agent Container

Runs Claude Code and the target app's dev server.

**Responsibilities:**
- Report readiness and IP to session store
- Expose API for receiving prompts
- Process prompts using Claude Code with session continuity
- Post progress to PR comments
- Run the target app's dev server
- Monitor for PR closure

### UAT Proxy (Existing Infrastructure)

Routes wildcard subdomain traffic to the correct container. Already deployed at `*.uat.teammobot.dev`.

## Session States

```
STARTING → RUNNING → COMPLETED
              ↓
           FAILED
```

- **STARTING**: Container launched, waiting for IP self-report
- **RUNNING**: Container ready, accepting prompts
- **COMPLETED**: PR closed/merged, clean shutdown
- **FAILED**: Container crashed or unreachable

## Failure Handling

### Container Crash

1. Next PR comment routing attempt fails to reach container
2. Session marked as FAILED
3. Error posted to PR
4. User can re-label issue to restart

### Idle Timeout

1. No activity for configurable period (default: 60 minutes)
2. Warning posted to PR
3. Container shuts down gracefully
4. Session marked as COMPLETED
5. User can re-label to restart

## Security Considerations

- Webhook signatures validated before processing
- GitHub App credentials stored in secrets manager
- Container runs with minimal IAM permissions
- UAT preserves real user authentication (no bypass)
- Bot detection prevents infinite loops

## Future Considerations (Out of Scope for MVP)

- JIRA integration as additional trigger source
- Automatic restart after container failure
- Multiple containers per issue for parallel work
- Cost optimization via spot instances
- Session transfer between containers

## Implementation Notes

- The container must support full-stack development, running the entire stack
- ECS requires AMD64 images (not ARM64 from Mac builds)
- Claude Code runs against Bedrock
- UAT URLs are dynamic per session: `{session-id}.uat.teammobot.dev`
- Authentication flows through staging (app.teammobot.dev) for `.teammobot.dev` cookies
