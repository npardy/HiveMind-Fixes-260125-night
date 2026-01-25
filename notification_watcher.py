"""
Notification Queue Watcher
===========================
Monitors the Data Sync notification queue (events.jsonl) and processes events.
This integrates Hive Mind with the field data sync system.

Events supported:
- layout_job_created: A new layout job folder was created from email
- field_data_uploaded: Field crew uploaded data for a job
- job_deleted: A job folder was deleted

When field data includes time_spent hours (field_hours, travel_hours_oneway),
time entries are posted directly to QuickBooks via the QBO integration.
"""

import os
import json
import time
import logging
from datetime import datetime
from typing import Optional, Dict, Any

from config_loader import get_config


class NotificationWatcher:
    """Watches the notification queue and processes events."""

    def __init__(self, notification_file: str, tool_executor=None, agent=None):
        """
        Initialize the notification watcher.

        Args:
            notification_file: Path to events.jsonl
            tool_executor: ToolExecutor instance for taking actions
            agent: ClaudeAgent for complex reasoning (optional)
        """
        self.notification_file = notification_file
        self.tool_executor = tool_executor
        self.agent = agent
        self.logger = logging.getLogger(__name__)
        self.cfg = get_config()

        # Track which events we've processed (by ID)
        # Store in HiveMind data folder (read-write) instead of Data Sync folder (read-only)
        data_folder = os.path.dirname(self.cfg.job_index_file)
        self.processed_file = os.path.join(data_folder, 'notification_events_processed.json')
        self.processed_ids = self._load_processed_ids()


    def _load_processed_ids(self) -> set:
        """Load set of already-processed event IDs."""
        if os.path.exists(self.processed_file):
            try:
                with open(self.processed_file, 'r') as f:
                    return set(json.load(f))
            except:
                pass
        return set()

    def _save_processed_ids(self):
        """Save processed event IDs."""
        try:
            # Keep only last 1000 IDs to prevent unbounded growth
            ids_list = list(self.processed_ids)[-1000:]
            with open(self.processed_file, 'w') as f:
                json.dump(ids_list, f)
        except Exception as e:
            self.logger.error(f"Failed to save processed IDs: {e}")

    def _read_new_events(self) -> list:
        """Read unprocessed events from the notification file."""
        events = []

        if not os.path.exists(self.notification_file):
            return events

        try:
            with open(self.notification_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        event = json.loads(line)
                        event_id = event.get('id')

                        # Skip already-processed events
                        if event_id and event_id not in self.processed_ids:
                            events.append(event)
                    except json.JSONDecodeError:
                        self.logger.warning(f"Invalid JSON in notification file: {line[:50]}...")
        except Exception as e:
            self.logger.error(f"Error reading notification file: {e}")

        return events

    def process_event(self, event: Dict[str, Any]) -> bool:
        """
        Process a single notification event.

        Returns True if successfully processed, False otherwise.
        """
        event_type = event.get('event')
        event_id = event.get('id')
        data = event.get('data', {})
        timestamp = event.get('timestamp')

        self.logger.info(f"Processing event: {event_type} - {data.get('job_number', 'N/A')}")

        try:
            if event_type == 'layout_job_created':
                return self._handle_layout_job_created(data)
            elif event_type == 'field_data_uploaded':
                return self._handle_field_data_uploaded(data)
            elif event_type == 'job_deleted':
                return self._handle_job_deleted(data)
            else:
                self.logger.warning(f"Unknown event type: {event_type}")
                return True  # Mark as processed to avoid retry loop

        except Exception as e:
            self.logger.error(f"Error processing event {event_id}: {e}")
            return False

    def _handle_layout_job_created(self, data: Dict[str, Any]) -> bool:
        """
        Handle layout_job_created event.

        This event fires when Data Sync creates a new job folder from email.
        We should enrich the job index with this information.
        """
        job_number = data.get('job_number')
        job_path = data.get('job_path')
        created_at = data.get('created_at')
        source_email = data.get('source_email')  # Email ID that triggered creation

        if not job_number:
            self.logger.warning("layout_job_created event missing job_number")
            return True

        self.logger.info(f"  Layout job created: {job_number}")
        self.logger.info(f"  Path: {job_path}")

        # Enrich job index with folder creation info
        if self.tool_executor:
            self.tool_executor.enrich_job_index(job_number, "data_sync", {
                "folder_created": True,
                "folder_path": job_path,
                "created_at": created_at,
                "source": "data_sync_layout"
            })

        return True

    def _handle_field_data_uploaded(self, data: Dict[str, Any]) -> bool:
        """
        Handle field_data_uploaded event.

        This event fires when field crew uploads data for a job.
        We should:
        1. Update job index with upload info
        2. Extract field_status data (operator, time_spent, etc.)
        3. Track for potential QBO timesheet entry

        Event data structure:
        {
            "job_number": "26-004",
            "job_path": "...",
            "folder_name": "260123-1131AM",
            "field_status_exists": true,
            "field_status": {
                "operator": "Allan",
                "job_type": "survey_rpr",
                "upload_type": "intermediate",
                "time_spent": null,  # Hours entered by field crew
                "notes": "TOPO",
                "csv_extracted": { ... }
            },
            "file_count": 13,
            "total_bytes": 47664930
        }
        """
        job_number = data.get('job_number')
        file_count = data.get('file_count', 0)
        total_bytes = data.get('total_bytes', 0)
        folder_name = data.get('folder_name', '')
        job_path = data.get('job_path', '')

        # Extract field_status (nested inside event data)
        field_status = data.get('field_status', {})
        operator = field_status.get('operator', 'unknown')
        upload_type = field_status.get('upload_type', 'unknown')
        job_type = field_status.get('job_type', '')
        # time_spent can be a dict: {"field_hours": 6, "travel_hours_oneway": 1}
        # or a simple number (legacy format)
        time_spent_raw = field_status.get('time_spent')
        field_hours = 0
        travel_hours_oneway = 0
        if isinstance(time_spent_raw, dict):
            field_hours = time_spent_raw.get('field_hours', 0) or 0
            travel_hours_oneway = time_spent_raw.get('travel_hours_oneway', 0) or 0
        elif isinstance(time_spent_raw, (int, float)) and time_spent_raw > 0:
            field_hours = time_spent_raw  # Legacy: single number = field hours
        notes = field_status.get('notes', '')
        csv_extracted = field_status.get('csv_extracted', {})

        if not job_number:
            self.logger.warning("field_data_uploaded event missing job_number")
            return True

        self.logger.info(f"  Field data uploaded for: {job_number}")
        self.logger.info(f"  Operator: {operator}, Type: {upload_type}, Files: {file_count}")
        if field_hours > 0 or travel_hours_oneway > 0:
            self.logger.info(f"  Time: {field_hours}h field, {travel_hours_oneway}h travel (one-way)")
        if notes:
            self.logger.info(f"  Notes: {notes}")

        # Enrich job index
        if self.tool_executor:
            # Get existing field_uploads or start fresh
            existing = self.tool_executor.job_index.get(job_number, {})
            ds_section = existing.get('data_sync', {})
            if isinstance(ds_section, list):
                # Handle legacy format where data_sync was a list
                ds_section = {}
            field_uploads = ds_section.get('field_uploads', [])

            # Build comprehensive upload record
            upload_record = {
                "folder_name": folder_name,
                "upload_type": upload_type,
                "file_count": file_count,
                "total_bytes": total_bytes,
                "operator": operator,
                "job_type": job_type,
                "timestamp": data.get('upload_timestamp')
            }

            # Include time_spent if field crew entered it
            if field_hours > 0 or travel_hours_oneway > 0:
                upload_record["time_spent"] = {
                    "field_hours": field_hours,
                    "travel_hours_oneway": travel_hours_oneway
                }

            # Include notes if present
            if notes:
                upload_record["notes"] = notes

            # Include CSV extraction data (evidence found, points, etc.)
            if csv_extracted:
                upload_record["csv_data"] = {
                    "total_points": csv_extracted.get('total_points', 0),
                    "evidence_found": len(csv_extracted.get('evidence_found', [])),
                    "evidence_not_found": csv_extracted.get('evidence_not_found', 0),
                    "pins_placed": len(csv_extracted.get('pins_placed', []))
                }

            field_uploads.append(upload_record)

            # Determine status based on upload_type
            if upload_type == 'final':
                status = "field_complete"
            elif upload_type == 'intermediate':
                status = "field_in_progress"
            else:
                status = "field_data_received"

            self.tool_executor.enrich_job_index(job_number, "data_sync", {
                "field_uploads": field_uploads,
                "last_upload": data.get('upload_timestamp'),
                "last_operator": operator,
                "status": status
            })

            # Post time entries directly to QBO (if hours were recorded)
            if field_hours > 0 or travel_hours_oneway > 0:
                self._post_time_to_qbo(
                    job_number=job_number,
                    operator=operator,
                    field_hours=field_hours,
                    travel_hours_oneway=travel_hours_oneway,
                    notes=notes,
                    source_folder=folder_name,
                    upload_date=data.get('upload_timestamp', '')[:10]  # YYYY-MM-DD
                )

        return True

    def _post_time_to_qbo(
        self,
        job_number: str,
        operator: str,
        field_hours: float,
        travel_hours_oneway: float,
        notes: str = "",
        source_folder: str = "",
        upload_date: str = ""
    ):
        """
        Post field time entries directly to QuickBooks.

        Uses the existing create_field_time_entry method in QBOIntegration
        which handles both field hours and travel (doubling one-way to round-trip).

        The results are stored in the job index for tracking.

        Args:
            job_number: Job number (e.g., "26-004")
            operator: Field crew operator name
            field_hours: Hours on site
            travel_hours_oneway: One-way travel hours (will be doubled)
            notes: Field notes
            source_folder: Data Sync folder that triggered this
            upload_date: Date of the upload (YYYY-MM-DD)
        """
        # Need QBO access through tool_executor
        if not self.tool_executor or not self.tool_executor.qbo:
            self.logger.warning("  No QBO access - skipping time entry")
            return

        qbo = self.tool_executor.qbo

        # Check if QBO is authenticated
        if not qbo.access_token:
            self.logger.warning("  QBO not authenticated - skipping time entry")
            return

        try:
            self.logger.info(f"  Posting to QBO: {operator} - {field_hours}h field, {travel_hours_oneway}h travel")

            result = qbo.create_field_time_entry(
                employee_name=operator,
                field_hours=field_hours,
                travel_hours=travel_hours_oneway,  # Method doubles it for round trip
                job_number=job_number,
                date=upload_date or None,
                notes=notes
            )

            if result.get('success'):
                self.logger.info(f"  [OK] Posted to QBO: field={result.get('field_entry_id')}, travel={result.get('travel_entry_id')}")

                # Enrich job index with QBO entry IDs
                self.tool_executor.enrich_job_index(job_number, "qbo_time_entries", {
                    "latest_field_entry": result.get('field_entry_id'),
                    "latest_travel_entry": result.get('travel_entry_id'),
                    "source_folder": source_folder,
                    "posted_at": datetime.now().isoformat()
                })
            else:
                errors = result.get('errors', ['Unknown error'])
                self.logger.error(f"  Failed to post to QBO: {errors}")

                # Still record the attempt in job index
                self.tool_executor.enrich_job_index(job_number, "qbo_time_entries", {
                    "last_error": errors,
                    "last_attempt": datetime.now().isoformat(),
                    "pending_hours": {
                        "field": field_hours,
                        "travel": travel_hours_oneway,
                        "operator": operator
                    }
                })

        except Exception as e:
            self.logger.error(f"  Exception posting to QBO: {e}")

    def _handle_job_deleted(self, data: Dict[str, Any]) -> bool:
        """
        Handle job_deleted event.

        This event fires when a job folder is deleted via Data Sync.
        We should mark the job as deleted in the index (not remove it entirely,
        to preserve history).
        """
        job_number = data.get('job_number')
        folder_path = data.get('folder_path')
        deleted_at = data.get('deleted_at')

        if not job_number:
            self.logger.warning("job_deleted event missing job_number")
            return True

        self.logger.info(f"  Job deleted: {job_number}")
        self.logger.info(f"  Path was: {folder_path}")

        # Mark job as deleted in index (preserves history but marks as inactive)
        if self.tool_executor:
            self.tool_executor.enrich_job_index(job_number, "data_sync", {
                "folder_deleted": True,
                "deleted_at": deleted_at,
                "status": "deleted"
            })

        return True

    def check_and_process(self) -> int:
        """
        Check for new events and process them.

        Returns the number of events processed.
        """
        events = self._read_new_events()

        if not events:
            return 0

        processed_count = 0

        for event in events:
            event_id = event.get('id')

            if self.process_event(event):
                self.processed_ids.add(event_id)
                processed_count += 1

        # Save processed IDs after batch
        if processed_count > 0:
            self._save_processed_ids()

        return processed_count

    def watch(self, check_interval: int = 10):
        """
        Start watching the notification queue continuously.

        Args:
            check_interval: Seconds between checks
        """
        self.logger.info(f"Starting notification watcher on: {self.notification_file}")
        self.logger.info(f"Check interval: {check_interval}s")

        while True:
            try:
                count = self.check_and_process()
                if count > 0:
                    self.logger.info(f"Processed {count} notification event(s)")
            except Exception as e:
                self.logger.error(f"Error in notification watch loop: {e}")

            time.sleep(check_interval)
