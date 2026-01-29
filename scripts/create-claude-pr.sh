#!/bin/bash
# Create a PR that triggers claude-cloud-agent without needing a local checkout
#
# Usage:
#   ./create-claude-pr.sh -r owner/repo -t "Title" -b branch-name < prompt.md
#   ./create-claude-pr.sh -r owner/repo -t "Title" -b branch-name -f prompt.md
#   echo "prompt" | ./create-claude-pr.sh -r owner/repo -t "Title" -b branch-name
#
# Examples:
#   ./create-claude-pr.sh -r team-mobot/test_tickets -t "Add dark mode" -b add-dark-mode <<< "Implement dark mode toggle"
#   ./create-claude-pr.sh -r team-mobot/test_tickets -t "Fix bug" -b fix-login -f prompt.md

set -e

usage() {
    echo "Usage: $0 -r REPO -t TITLE -b BRANCH [-f PROMPT_FILE] [-B BASE_BRANCH]"
    echo ""
    echo "Options:"
    echo "  -r REPO         Repository (owner/repo)"
    echo "  -t TITLE        PR title"
    echo "  -b BRANCH       Branch name to create"
    echo "  -f FILE         Read prompt from file (otherwise reads from stdin)"
    echo "  -B BASE         Base branch (default: main)"
    echo "  -h              Show this help"
    echo ""
    echo "The prompt is used as both the PR body and stored in .claude-prompt file"
    exit 1
}

REPO=""
TITLE=""
BRANCH=""
PROMPT_FILE=""
BASE_BRANCH="main"

while getopts "r:t:b:f:B:h" opt; do
    case $opt in
        r) REPO="$OPTARG" ;;
        t) TITLE="$OPTARG" ;;
        b) BRANCH="$OPTARG" ;;
        f) PROMPT_FILE="$OPTARG" ;;
        B) BASE_BRANCH="$OPTARG" ;;
        h) usage ;;
        *) usage ;;
    esac
done

if [ -z "$REPO" ] || [ -z "$TITLE" ] || [ -z "$BRANCH" ]; then
    echo "Error: -r, -t, and -b are required"
    usage
fi

# Read prompt from file or stdin
if [ -n "$PROMPT_FILE" ]; then
    PROMPT=$(cat "$PROMPT_FILE")
else
    PROMPT=$(cat)
fi

if [ -z "$PROMPT" ]; then
    echo "Error: No prompt provided"
    exit 1
fi

echo "==> Creating branch '$BRANCH' from '$BASE_BRANCH'..."

# Get the SHA of the base branch
SHA=$(gh api "repos/$REPO/git/refs/heads/$BASE_BRANCH" --jq '.object.sha')
if [ -z "$SHA" ]; then
    echo "Error: Could not get SHA for $BASE_BRANCH"
    exit 1
fi

# Get the tree SHA
TREE=$(gh api "repos/$REPO/git/commits/$SHA" --jq '.tree.sha')

# Create a blob with the prompt content
echo "==> Creating commit with prompt..."
BLOB=$(gh api "repos/$REPO/git/blobs" -f content="$PROMPT" -f encoding="utf-8" --jq '.sha')

# Create a new tree with the .claude-prompt file
NEW_TREE=$(gh api "repos/$REPO/git/trees" \
    -f base_tree="$TREE" \
    -f "tree[][path]=.claude-prompt" \
    -f "tree[][mode]=100644" \
    -f "tree[][type]=blob" \
    -f "tree[][sha]=$BLOB" \
    --jq '.sha')

# Create a commit
NEW_COMMIT=$(gh api "repos/$REPO/git/commits" \
    -f message="$TITLE" \
    -f tree="$NEW_TREE" \
    -f "parents[]=$SHA" \
    --jq '.sha')

# Delete existing branch if it exists (ignore errors)
gh api "repos/$REPO/git/refs/heads/$BRANCH" -X DELETE 2>/dev/null || true

# Create the branch
gh api "repos/$REPO/git/refs" -f ref="refs/heads/$BRANCH" -f sha="$NEW_COMMIT" > /dev/null

echo "==> Creating PR..."
PR_URL=$(gh pr create -R "$REPO" --head "$BRANCH" --base "$BASE_BRANCH" --title "$TITLE" --label claude-dev --body "$PROMPT")

echo "==> Done!"
echo "$PR_URL"
