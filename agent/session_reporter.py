"""
Session reporter for DynamoDB updates.

Reports container IP and status to the sessions table.
"""

import logging
import os
import time
from typing import Optional

import boto3
import requests

logger = logging.getLogger(__name__)


class SessionReporter:
    """
    Reports session state to DynamoDB.

    Called by the agent container to:
    1. Report its public IP when ready
    2. Update status transitions
    3. Record activity timestamps
    """

    def __init__(self):
        """Initialize session reporter from environment."""
        self.session_id = os.environ.get("SESSION_ID", "")
        self.table_name = os.environ.get("SESSIONS_TABLE", "")
        self._dynamodb = None
        self._table = None

    @property
    def dynamodb(self):
        """Get DynamoDB resource."""
        if self._dynamodb is None:
            self._dynamodb = boto3.resource("dynamodb")
        return self._dynamodb

    @property
    def table(self):
        """Get DynamoDB table."""
        if self._table is None:
            self._table = self.dynamodb.Table(self.table_name)
        return self._table

    def discover_private_ip(self) -> Optional[str]:
        """
        Discover this container's private IP for VPC communication.

        Uses ECS metadata endpoint to get the container's private IP address,
        which is required for security group-based access from the UAT proxy.

        Returns:
            Private IP address or None
        """
        metadata_uri = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
        if metadata_uri:
            try:
                # Get task metadata which contains network info
                task_response = requests.get(f"{metadata_uri}/task", timeout=5)
                task_data = task_response.json()

                # Get private IP from containers array
                containers = task_data.get("Containers", [])
                for container in containers:
                    networks = container.get("Networks", [])
                    for network in networks:
                        ipv4_addresses = network.get("IPv4Addresses", [])
                        if ipv4_addresses:
                            ip = ipv4_addresses[0]
                            logger.info(f"Discovered private IP from ECS metadata: {ip}")
                            return ip

                logger.warning("No private IP found in ECS metadata")

            except Exception as e:
                logger.warning(f"Failed to get ECS metadata: {e}")

        # Fallback: Try to get from network interfaces
        try:
            import socket
            hostname = socket.gethostname()
            ip = socket.gethostbyname(hostname)
            if ip and not ip.startswith("127."):
                logger.info(f"Discovered private IP from hostname: {ip}")
                return ip
        except Exception as e:
            logger.warning(f"Failed to get IP from hostname: {e}")

        return None

    def discover_public_ip(self) -> Optional[str]:
        """
        Discover this container's public IP (for informational purposes).

        Returns:
            Public IP address or None
        """
        try:
            response = requests.get("https://api.ipify.org", timeout=10)
            ip = response.text.strip()
            logger.info(f"Discovered public IP: {ip}")
            return ip
        except Exception as e:
            logger.warning(f"Failed to get public IP: {e}")
            return None

    def discover_and_report_ip(self) -> Optional[str]:
        """
        Discover private IP and report to DynamoDB.

        Uses private IP for VPC communication with the UAT proxy.
        Also marks session as RUNNING.

        Returns:
            Private IP address or None
        """
        ip = self.discover_private_ip()
        if not ip:
            logger.error("Failed to discover private IP")
            return None

        # Update DynamoDB
        try:
            now = int(time.time())
            uat_url = f"https://{self.session_id}.{os.environ.get('UAT_DOMAIN_SUFFIX', 'uat.teammobot.dev')}"

            self.table.update_item(
                Key={"session_id": self.session_id},
                UpdateExpression="SET #status = :status, #container_ip = :ip, #uat_url = :uat_url, #updated_at = :updated, #last_activity = :activity",
                ExpressionAttributeNames={
                    "#status": "status",
                    "#container_ip": "container_ip",
                    "#uat_url": "uat_url",
                    "#updated_at": "updated_at",
                    "#last_activity": "last_activity"
                },
                ExpressionAttributeValues={
                    ":status": "RUNNING",
                    ":ip": ip,
                    ":uat_url": uat_url,
                    ":updated": now,
                    ":activity": now
                }
            )
            logger.info(f"Reported IP {ip} to DynamoDB, session is now RUNNING")
            return ip

        except Exception as e:
            logger.exception(f"Failed to update DynamoDB: {e}")
            return ip  # Return IP even if DynamoDB update failed

    def update_activity(self) -> None:
        """Update last activity timestamp."""
        try:
            now = int(time.time())
            self.table.update_item(
                Key={"session_id": self.session_id},
                UpdateExpression="SET #last_activity = :activity, #updated_at = :updated",
                ExpressionAttributeNames={
                    "#last_activity": "last_activity",
                    "#updated_at": "updated_at"
                },
                ExpressionAttributeValues={
                    ":activity": now,
                    ":updated": now
                }
            )
        except Exception as e:
            logger.warning(f"Failed to update activity: {e}")

    def mark_completed(self) -> None:
        """Mark session as completed."""
        try:
            now = int(time.time())
            ttl = now + (7 * 24 * 60 * 60)  # 7 days

            self.table.update_item(
                Key={"session_id": self.session_id},
                UpdateExpression="SET #status = :status, #updated_at = :updated, #ttl = :ttl",
                ExpressionAttributeNames={
                    "#status": "status",
                    "#updated_at": "updated_at",
                    "#ttl": "ttl"
                },
                ExpressionAttributeValues={
                    ":status": "COMPLETED",
                    ":updated": now,
                    ":ttl": ttl
                }
            )
            logger.info("Session marked as COMPLETED")
        except Exception as e:
            logger.exception(f"Failed to mark completed: {e}")

    def mark_failed(self, error: str = "") -> None:
        """Mark session as failed."""
        try:
            now = int(time.time())
            ttl = now + (7 * 24 * 60 * 60)  # 7 days

            update_expr = "SET #status = :status, #updated_at = :updated, #ttl = :ttl"
            expr_names = {
                "#status": "status",
                "#updated_at": "updated_at",
                "#ttl": "ttl"
            }
            expr_values = {
                ":status": "FAILED",
                ":updated": now,
                ":ttl": ttl
            }

            if error:
                update_expr += ", #error = :error"
                expr_names["#error"] = "error"
                expr_values[":error"] = error

            self.table.update_item(
                Key={"session_id": self.session_id},
                UpdateExpression=update_expr,
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=expr_values
            )
            logger.info(f"Session marked as FAILED: {error}")
        except Exception as e:
            logger.exception(f"Failed to mark failed: {e}")
