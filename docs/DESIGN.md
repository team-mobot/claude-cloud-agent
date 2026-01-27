# Claude Cloud Agent - Design Document

## Vision

An autonomous development agent that implements features and fixes bugs based on GitHub issues. When a user labels an issue with a trigger label, the system automatically creates a branch, opens a draft PR, and spins up an agent that runs Claude Code. The agent implements the requested changes, commits to the branch, and posts progress as PR comments. Users interact with the agent by commenting on the PR.

## Problem Statement

Developers want to delegate routine implementation tasks to an AI agent while maintaining visibility and control. The agent should:

1. Work autonomously on well-defined tasks
2. Provide real-time progress updates
3. Accept feedback and iterate
4. Expose a running dev server for UAT testing
5. Clean up when work is complete

## User Experience

### Starting a Session

1. User creates a GitHub issue describing the work to be done
2. User adds the `claude-dev` label to the issue
3. System creates a feature branch and draft PR
4. System posts a comment with the UAT URL
5. Agent begins implementing the requested changes

### During a Session

- Agent posts progress updates as PR comments
- User can view work-in-progress at the UAT URL
- User can provide feedback by commenting on the PR
- Agent incorporates feedback and continues working
- All changes are committed to the feature branch

### Ending a Session

- User merges or closes the PR
- Agent terminates gracefully
- Resources are cleaned up

## Key Design Decisions

### 1. One Long-Running Container Per Issue

Each issue gets a single container that runs for the entire session lifecycle, from issue labeled to PR closed.

**Rationale:**
- Maintains consistent public IP for UAT access
- Preserves Claude's conversation context across interactions
- Avoids startup overhead on each feedback cycle
- Keeps git working directory state intact

**Alternative rejected:** Spawning a new container for each PR comment would lose context, change the UAT URL, and add ~2 minutes of startup time per interaction.

### 2. Agent and UAT Server in Same Container

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

### 5. UAT via Wildcard Subdomain

UAT is accessed via `{session-id}.uat.example.com` rather than raw IP addresses.

**Rationale:**
- Preserves authentication cookies from the parent domain
- Users stay logged in with their real identity
- TLS termination at the proxy
- Friendlier URLs in PR comments

## Requirements

### Functional Requirements

| ID | Requirement |
|----|-------------|
| F1 | Adding trigger label to issue creates branch and draft PR |
| F2 | Agent processes issue body as initial prompt |
| F3 | PR comments are routed to running agent |
| F4 | Agent posts progress updates as PR comments |
| F5 | Agent commits changes to feature branch |
| F6 | UAT server is accessible throughout session |
| F7 | Session terminates when PR is closed or merged |
| F8 | Session terminates after configurable idle timeout |
| F9 | Bot comments do not trigger feedback loops |

### Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| N1 | Session startup completes within 2 minutes |
| N2 | PR comment routing latency under 5 seconds |
| N3 | UAT URL remains stable throughout session |
| N4 | Session state survives temporary network issues |
| N5 | Failed sessions can be restarted by re-labeling |

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

Runs Claude Code and the UAT server.

**Responsibilities:**
- Report readiness and IP to session store
- Expose API for receiving prompts
- Process prompts using Claude Code with session continuity
- Post progress to PR comments
- Run the target app's dev server
- Monitor for PR closure

### UAT Proxy

Routes wildcard subdomain traffic to the correct container.

**Responsibilities:**
- TLS termination
- Extract session ID from hostname
- Look up container IP from session store
- Forward HTTP and WebSocket traffic
- Preserve cookies for authentication

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

### Things we've messed up before ###

- The container must support full-stack development, running the entire stack.
- The container's UAT environment must auth against staging (app.teammobot.dev) to get the proper .teamobot.dev cookie that then works against *.uat.teammobot.dev
- ECS needs AMD64 images, not the native AMD64 for the Mac we are building on
- Claude is running against Bedrock
- The UAT urls should be dynamic based on the issue.  AGNT-0.uat.teammobot.dev for example.  or xxyysession.uat.teammobot.dev.  Everything must be under .uat.teammobot.dev.
