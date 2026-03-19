"""
Tests for out-of-order event handling (HONOR_SOURCE_TIME feature).

When events arrive out-of-order due to network delays, source timestamps
should be honored to keep alert status and history consistent.
"""

import json
import unittest
from datetime import datetime, timedelta
from uuid import uuid4

from alerta.app import create_app, db
from alerta.models.alert import Alert
from alerta.models.enums import ChangeType
from alerta.utils.api import process_alert
from flask import g


class OutOfOrderTestCase(unittest.TestCase):
    """Tests for out-of-order event handling."""

    def setUp(self):
        test_config = {
            "TESTING": True,
            "AUTH_REQUIRED": False,
            "HONOR_SOURCE_TIME": True,  # Enable the feature
            "LATE_ARRIVAL_TOLERANCE_SECS": 0,
        }
        self.app = create_app(test_config)
        self.client = self.app.test_client()

        self.resource = str(uuid4()).upper()[:8]

        self.headers = {
            "Content-type": "application/json",
        }

    def tearDown(self):
        db.destroy()

    def test_late_arrival_detection_disabled_by_default(self):
        """Test that is_late_arrival returns False when HONOR_SOURCE_TIME is False."""
        # Create app with HONOR_SOURCE_TIME disabled
        test_config = {
            "TESTING": True,
            "AUTH_REQUIRED": False,
            "HONOR_SOURCE_TIME": False,  # Disabled
        }
        app = create_app(test_config)

        with app.app_context():
            g.login = "test_user"

            now = datetime.utcnow()
            older_time = now - timedelta(minutes=10)

            # Simulate an existing alert with recent source time
            existing = Alert(
                resource="test-resource",
                event="test-event",
                environment="Production",
                service=["TestService"],
                severity="critical",
                create_time=now,
            )

            # Simulate incoming event with older source time
            incoming = Alert(
                resource="test-resource",
                event="test-event",
                environment="Production",
                service=["TestService"],
                severity="normal",
                create_time=older_time,
            )

            # Should return False when feature is disabled
            self.assertFalse(incoming.is_late_arrival(existing))

        db.destroy()

    def test_late_arrival_detection_enabled(self):
        """Test that is_late_arrival correctly detects out-of-order events."""
        with self.app.app_context():
            g.login = "test_user"

            now = datetime.utcnow()
            older_time = now - timedelta(minutes=10)

            # Simulate an existing alert with recent source time
            existing = Alert(
                resource="test-resource",
                event="test-event",
                environment="Production",
                service=["TestService"],
                severity="critical",
                create_time=now,
            )

            # Simulate incoming event with older source time
            incoming = Alert(
                resource="test-resource",
                event="test-event",
                environment="Production",
                service=["TestService"],
                severity="normal",
                create_time=older_time,
            )

            # Should detect as late arrival
            self.assertTrue(incoming.is_late_arrival(existing))

    def test_not_late_arrival_when_newer(self):
        """Test that is_late_arrival returns False for newer events."""
        with self.app.app_context():
            g.login = "test_user"

            now = datetime.utcnow()
            newer_time = now + timedelta(minutes=10)

            # Simulate an existing alert with source time = now
            existing = Alert(
                resource="test-resource",
                event="test-event",
                environment="Production",
                service=["TestService"],
                severity="critical",
                create_time=now,
            )

            # Simulate incoming event with newer source time
            incoming = Alert(
                resource="test-resource",
                event="test-event",
                environment="Production",
                service=["TestService"],
                severity="normal",
                create_time=newer_time,
            )

            # Should not be detected as late arrival
            self.assertFalse(incoming.is_late_arrival(existing))

    def test_tolerance_window(self):
        """Test that LATE_ARRIVAL_TOLERANCE_SECS works correctly."""
        test_config = {
            "TESTING": True,
            "AUTH_REQUIRED": False,
            "HONOR_SOURCE_TIME": True,
            "LATE_ARRIVAL_TOLERANCE_SECS": 60,  # 60 second tolerance
        }
        app = create_app(test_config)

        with app.app_context():
            g.login = "test_user"

            now = datetime.utcnow()
            # Event source time is 30 seconds before existing source time - within tolerance
            within_tolerance = now - timedelta(seconds=30)
            # Event source time is 90 seconds before existing source time - outside tolerance
            outside_tolerance = now - timedelta(seconds=90)

            existing = Alert(
                resource="test-resource",
                event="test-event",
                environment="Production",
                service=["TestService"],
                severity="critical",
                create_time=now,
            )

            # Within tolerance - should NOT be late arrival
            incoming_within = Alert(
                resource="test-resource",
                event="test-event",
                environment="Production",
                service=["TestService"],
                severity="normal",
                create_time=within_tolerance,
            )
            self.assertFalse(incoming_within.is_late_arrival(existing))

            # Outside tolerance - should be late arrival
            incoming_outside = Alert(
                resource="test-resource",
                event="test-event",
                environment="Production",
                service=["TestService"],
                severity="normal",
                create_time=outside_tolerance,
            )
            self.assertTrue(incoming_outside.is_late_arrival(existing))

        db.destroy()

    def test_no_create_time_processes_normally(self):
        """Test that events without create_time are processed normally."""
        with self.app.app_context():
            g.login = "test_user"

            now = datetime.utcnow()

            existing = Alert(
                resource="test-resource",
                event="test-event",
                environment="Production",
                service=["TestService"],
                severity="critical",
                create_time=now,
            )

            # Incoming without create_time (None)
            incoming = Alert(
                resource="test-resource",
                event="test-event",
                environment="Production",
                service=["TestService"],
                severity="normal",
            )
            incoming.create_time = None

            # Should not be detected as late arrival
            self.assertFalse(incoming.is_late_arrival(existing))

    def test_out_of_order_correlated_alert_via_api(self):
        """
        Integration test: Send events out of order and verify status is correct.

        Scenario:
        1. Send CRITICAL alert with explicit source time T=0
        2. Send CLEARED alert with source time T=-5min (arrives second, but is older)

        Expected: Alert should remain OPEN because the CLEARED event is a late arrival.
        """
        with self.app.app_context():
            now = datetime.utcnow()
            current_time = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            older_time = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

            # First: Send CRITICAL alert with explicit createTime
            critical_alert = {
                "event": "node_status",
                "resource": self.resource,
                "environment": "Production",
                "service": ["Network"],
                "severity": "critical",
                "correlate": ["node_status"],
                "createTime": current_time,  # Explicit source time T=0
            }

            response = self.client.post(
                "/alert", data=json.dumps(critical_alert), headers=self.headers
            )
            self.assertEqual(response.status_code, 201)
            data = json.loads(response.data.decode("utf-8"))
            self.assertEqual(data["alert"]["status"], "open")
            self.assertEqual(data["alert"]["severity"], "critical")
            alert_id = data["id"]

            # Second: Send CLEARED alert with OLDER source time
            # This simulates an out-of-order event
            cleared_alert = {
                "event": "node_status",
                "resource": self.resource,
                "environment": "Production",
                "service": ["Network"],
                "severity": "cleared",
                "correlate": ["node_status"],
                "createTime": older_time,  # Older than the critical alert (T=-5min)
            }

            response = self.client.post(
                "/alert", data=json.dumps(cleared_alert), headers=self.headers
            )
            self.assertEqual(response.status_code, 201)
            data = json.loads(response.data.decode("utf-8"))

            # Verify history contains the late arrival
            response = self.client.get(f"/alert/{alert_id}?show-history=true")
            self.assertEqual(response.status_code, 200)
            data = json.loads(response.data.decode("utf-8"))

            # Alert should still be OPEN because the cleared event was late
            self.assertEqual(
                data["alert"]["status"],
                "open",
                f"Alert status should be open, got {data['alert']['status']}",
            )
            self.assertEqual(
                data["alert"]["severity"],
                "critical",
                f"Alert severity should be critical, got {data['alert']['severity']}",
            )

            # Find late_arrival entry in history (field is 'type' not 'changeType')
            history = data["alert"]["history"]
            late_arrivals = [h for h in history if h.get("type") == "late_arrival"]
            self.assertEqual(
                len(late_arrivals), 1, "Should have one late_arrival history entry"
            )

    def test_in_order_events_process_normally(self):
        """
        Test that events arriving in correct order still work normally.

        Scenario:
        1. Send CRITICAL alert at T=0
        2. Send CLEARED alert with source time T=+5min

        Expected: Alert should be CLOSED because events are in order.
        """
        with self.app.app_context():
            now = datetime.utcnow()
            future_time = (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

            # First: Send CRITICAL alert
            critical_alert = {
                "event": "node_status",
                "resource": self.resource,
                "environment": "Production",
                "service": ["Network"],
                "severity": "critical",
                "correlate": ["node_status"],
            }

            response = self.client.post(
                "/alert", data=json.dumps(critical_alert), headers=self.headers
            )
            self.assertEqual(response.status_code, 201)
            data = json.loads(response.data.decode("utf-8"))
            self.assertEqual(data["alert"]["status"], "open")

            # Second: Send CLEARED alert with NEWER source time
            cleared_alert = {
                "event": "node_status",
                "resource": self.resource,
                "environment": "Production",
                "service": ["Network"],
                "severity": "cleared",
                "correlate": ["node_status"],
                "createTime": future_time,
            }

            response = self.client.post(
                "/alert", data=json.dumps(cleared_alert), headers=self.headers
            )
            self.assertEqual(response.status_code, 201)
            data = json.loads(response.data.decode("utf-8"))

            # Alert should be CLOSED because events were in order
            self.assertEqual(data["alert"]["status"], "closed")
            self.assertEqual(data["alert"]["severity"], "cleared")


class OutOfOrderDisabledTestCase(unittest.TestCase):
    """Tests to ensure backward compatibility when feature is disabled."""

    def setUp(self):
        test_config = {
            "TESTING": True,
            "AUTH_REQUIRED": False,
            "HONOR_SOURCE_TIME": False,  # Feature disabled
        }
        self.app = create_app(test_config)
        self.client = self.app.test_client()

        self.resource = str(uuid4()).upper()[:8]

        self.headers = {
            "Content-type": "application/json",
        }

    def tearDown(self):
        db.destroy()

    def test_out_of_order_events_processed_by_arrival_when_disabled(self):
        """
        When HONOR_SOURCE_TIME is False, events should be processed by arrival order.

        This is the original behavior - last event received wins.
        """
        with self.app.app_context():
            now = datetime.utcnow()
            older_time = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

            # First: Send CRITICAL alert
            critical_alert = {
                "event": "node_status",
                "resource": self.resource,
                "environment": "Production",
                "service": ["Network"],
                "severity": "critical",
                "correlate": ["node_status"],
            }

            response = self.client.post(
                "/alert", data=json.dumps(critical_alert), headers=self.headers
            )
            self.assertEqual(response.status_code, 201)
            data = json.loads(response.data.decode("utf-8"))
            self.assertEqual(data["alert"]["status"], "open")

            # Second: Send CLEARED alert with OLDER source time
            # With feature disabled, this should still close the alert
            cleared_alert = {
                "event": "node_status",
                "resource": self.resource,
                "environment": "Production",
                "service": ["Network"],
                "severity": "cleared",
                "correlate": ["node_status"],
                "createTime": older_time,
            }

            response = self.client.post(
                "/alert", data=json.dumps(cleared_alert), headers=self.headers
            )
            self.assertEqual(response.status_code, 201)
            data = json.loads(response.data.decode("utf-8"))

            # Alert should be CLOSED because feature is disabled
            # (original behavior: last received event wins)
            self.assertEqual(data["alert"]["status"], "closed")
            self.assertEqual(data["alert"]["severity"], "cleared")


if __name__ == "__main__":
    unittest.main()
