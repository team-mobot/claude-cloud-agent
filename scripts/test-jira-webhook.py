#!/usr/bin/env python3
"""
Test script to simulate a JIRA webhook for the claude-dev label.

Usage:
    python3 test-jira-webhook.py [--dry-run]
"""

import argparse
import hashlib
import hmac
import json
import subprocess
import sys

import requests


def get_jira_secret():
    """Get JIRA secret from AWS Secrets Manager."""
    import os
    # Try boto3 first, fall back to environment variable
    try:
        import boto3
        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId="claude-cloud-agent/jira")
        return json.loads(response["SecretString"])
    except ImportError:
        # Fall back to environment variable
        secret_json = os.environ.get("JIRA_SECRET")
        if secret_json:
            return json.loads(secret_json)
        raise RuntimeError("boto3 not available and JIRA_SECRET env var not set")


def create_test_payload(issue_key="AGNTS-TEST", project_key="AGNTS"):
    """Create a test JIRA webhook payload for label addition."""
    return {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": issue_key,
            "fields": {
                "project": {"key": project_key},
                "summary": "Test issue for JIRA integration",
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "This is a test issue to verify the Claude Cloud Agent JIRA integration is working correctly."
                                }
                            ]
                        },
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Please create a simple test file to confirm the integration."
                                }
                            ]
                        }
                    ]
                }
            }
        },
        "changelog": {
            "items": [
                {
                    "field": "labels",
                    "fieldtype": "jira",
                    "fromString": "",
                    "toString": "claude-dev"
                }
            ]
        }
    }


def sign_payload(payload_bytes: bytes, secret: str) -> str:
    """Create HMAC-SHA256 signature for the payload."""
    sig = hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()
    return f"sha256={sig}"


def main():
    parser = argparse.ArgumentParser(description="Test JIRA webhook integration")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be sent without sending")
    parser.add_argument("--issue-key", default="AGNTS-TEST", help="JIRA issue key to simulate")
    args = parser.parse_args()

    print("Getting JIRA secret...")
    jira_secret = get_jira_secret()
    webhook_secret = jira_secret["webhook_secret"]

    print(f"Creating test payload for {args.issue_key}...")
    payload = create_test_payload(issue_key=args.issue_key)
    payload_bytes = json.dumps(payload).encode("utf-8")

    signature = sign_payload(payload_bytes, webhook_secret)

    headers = {
        "Content-Type": "application/json",
        "x-atlassian-webhook-identifier": "test-webhook-123",
        "x-hub-signature": signature
    }

    url = "https://wp6fm4rcug.execute-api.us-east-1.amazonaws.com/webhook"

    print(f"\nRequest details:")
    print(f"  URL: {url}")
    print(f"  Headers: {json.dumps(headers, indent=4)}")
    print(f"  Payload preview: {json.dumps(payload, indent=2)[:500]}...")

    if args.dry_run:
        print("\n[DRY RUN] Would send webhook to Lambda")
        return

    print(f"\nSending webhook to {url}...")
    response = requests.post(url, headers=headers, data=payload_bytes, timeout=30)

    print(f"\nResponse status: {response.status_code}")
    print(f"Response body: {response.text}")

    if response.status_code == 200:
        result = response.json()
        if "session_id" in result:
            print(f"\n[SUCCESS] Session created: {result['session_id']}")
            if "pr_number" in result:
                print(f"  PR number: {result['pr_number']}")
            if "task_arn" in result:
                print(f"  Task ARN: {result['task_arn']}")
        else:
            print(f"\nResult: {result.get('message', result)}")
    else:
        print(f"\n[ERROR] Webhook failed with status {response.status_code}")
        sys.exit(1)


if __name__ == "__main__":
    main()
