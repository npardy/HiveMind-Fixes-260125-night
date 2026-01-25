"""
Tool Executor - Implements all Claude tools
==========================================
Full access to QBO, Email, and NAS (no delete operations).
"""

import os
import json
import shutil
import fnmatch
import logging
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from config_loader import get_config


class ToolExecutor:
    """
    Executes tool calls from Claude.
    Full access to QBO, Email, and NAS file system.
    NO DELETE operations.
    """

    # Paths loaded from config (set in __init__)
    JOBS_FOLDER = None
    DATA_SYNC_FOLDER = None
    FLAGGED_FOLDER = None
    JOB_INDEX_FILE = None
    PROPOSALS_FOLDER = None
    
    METRO_AREAS = [
        "st. john's", "st johns", "saint john's", "mount pearl", "mt. pearl",
        "paradise", "conception bay south", "cbs", "torbay",
        "portugal cove-st. philip's", "portugal cove", "pcsp",
        "logy bay-middle cove-outer cove", "logy bay"
    ]
    
    def __init__(self, qbo, email_service):
        self.qbo = qbo
        self.email = email_service
        self.logger = logging.getLogger(__name__)

        # Load paths from config
        cfg = get_config()
        self.JOBS_FOLDER = cfg.jobs_folder
        self.DATA_SYNC_FOLDER = cfg.data_sync_folder
        self.FLAGGED_FOLDER = cfg.flagged_folder
        self.JOB_INDEX_FILE = cfg.job_index_file
        self.PROPOSALS_FOLDER = cfg.proposals_folder

        self.job_index = self._load_job_index()
        self.current_email = None  # Set when processing an email

        # Track replies sent in current session to prevent duplicates
        self.replied_message_ids = set()

        # Detect if running on NAS (Linux) vs Windows
        self._is_nas = os.name != 'nt'

        # Callback for tracking [Hive Mind] conversation_ids (set by orchestrator)
        self._hive_mind_tracker_callback = None

        # Load persistent tracking of Claude-sent message IDs
        self._sent_messages_file = os.path.join(os.path.dirname(self.JOB_INDEX_FILE), "claude_sent_messages.json")
        self._sent_message_ids = self._load_sent_message_ids()

    def _translate_path(self, path: str) -> str:
        """
        Translate paths between Windows (Z:\) and NAS (/volume1/Pardy Surveys/).

        Job index stores Windows paths (created by nightly_refresh on Windows).
        When running in Docker on NAS, we need to convert to Linux paths.
        """
        if not path:
            return path

        if self._is_nas:
            # Running on NAS - convert Windows to Linux paths
            # Z:\Jobs\... -> /volume1/Pardy Surveys/Jobs/...
            # Z:\Data Sync\... -> /volume1/Pardy Surveys/Data Sync/...
            # Z:\Pardy Surveys\... -> /volume1/Pardy Surveys/...
            if path.startswith('Z:\\') or path.startswith('Z:/'):
                # Remove Z:\ or Z:/
                rel_path = path[3:]
                # Convert backslashes to forward slashes
                rel_path = rel_path.replace('\\', '/')
                # Check if it's a direct subpath (Jobs, Data Sync) or nested (Pardy Surveys/...)
                if rel_path.startswith('Pardy Surveys/'):
                    return f"/volume1/{rel_path}"
                else:
                    return f"/volume1/Pardy Surveys/{rel_path}"
        else:
            # Running on Windows - convert Linux to Windows paths
            if path.startswith('/volume1/Pardy Surveys/'):
                rel_path = path[23:]  # Remove /volume1/Pardy Surveys/
                rel_path = rel_path.replace('/', '\\')
                return f"Z:\\{rel_path}"

        return path

    def _load_sent_message_ids(self) -> set:
        """Load the set of message IDs that Claude has sent."""
        try:
            if os.path.exists(self._sent_messages_file):
                with open(self._sent_messages_file, 'r') as f:
                    data = json.load(f)
                    return set(data.get('sent_ids', []))
        except Exception as e:
            self.logger.warning(f"Failed to load sent message IDs: {e}")
        return set()

    def _save_sent_message_ids(self):
        """Save the set of message IDs that Claude has sent."""
        try:
            os.makedirs(os.path.dirname(self._sent_messages_file), exist_ok=True)
            with open(self._sent_messages_file, 'w') as f:
                json.dump({
                    'sent_ids': list(self._sent_message_ids),
                    'last_updated': datetime.now().isoformat()
                }, f, indent=2)
        except Exception as e:
            self.logger.warning(f"Failed to save sent message IDs: {e}")

    def _track_sent_message(self, message_id: str):
        """Track a message ID as sent by Claude."""
        if message_id:
            self._sent_message_ids.add(message_id)
            self._save_sent_message_ids()
            self.logger.info(f"Tracked Claude-sent message: {message_id[:30]}...")

    def is_claude_sent_message(self, message_id: str) -> bool:
        """Check if a message was sent by Claude (for orchestrator to use)."""
        return message_id in self._sent_message_ids

    def set_current_email(self, email_msg):
        """Set the email currently being processed (for context)."""
        self.current_email = email_msg

    def reset_session_state(self):
        """Reset session state when starting to process a new email."""
        self.replied_message_ids = set()
        self._sent_emails = []  # Reset duplicate content guard for new email

    def set_hive_mind_tracker(self, callback):
        """Set callback function for tracking [Hive Mind] conversation_ids.

        The callback should accept a conversation_id string and store it
        for detecting Nicholas's replies to [Hive Mind] emails.
        """
        self._hive_mind_tracker_callback = callback
    
    def _load_job_index(self) -> dict:
        if os.path.exists(self.JOB_INDEX_FILE):
            try:
                with open(self.JOB_INDEX_FILE, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}
    
    def _save_job_index(self):
        os.makedirs(os.path.dirname(self.JOB_INDEX_FILE), exist_ok=True)
        with open(self.JOB_INDEX_FILE, 'w') as f:
            json.dump(self.job_index, f, indent=2, default=str)
    
    def enrich_job_index(self, job_number: str, section: str, data: dict):
        """
        CENTRAL ENRICHMENT FUNCTION - Call from any tool after read/write.
        
        Args:
            job_number: The job to enrich (e.g., "26-001")
            section: Which section to update ("qbo", "emails", "delivery", etc.)
            data: The data to merge into that section
        
        Example:
            # After getting invoice from QBO:
            self.enrich_job_index("26-001", "qbo", {
                "invoice_id": "123",
                "invoice_status": "Sent",
                "balance": 575.00,
                "paid": False
            })
        
        Handles:
            - Finding or creating the job entry
            - Merging data into the right section
            - Setting section `_updated` timestamp
            - Saving the index
        """
        if not job_number or job_number.startswith("TBD"):
            return
        
        job = self._get_or_create_job_entry(job_number)
        now = datetime.now().isoformat()
        
        # Initialize section if needed
        if section not in job:
            job[section] = {}
        
        # Handle different section types
        if section == "emails":
            # Emails has linked array
            if not isinstance(job[section], dict):
                job[section] = {"linked": [], "_updated": now}
            if "linked" not in job[section]:
                job[section]["linked"] = []
            
            # Check for duplicate by message_id
            msg_id = data.get("message_id")
            existing_ids = [e.get("message_id") for e in job[section]["linked"]]
            if msg_id and msg_id not in existing_ids:
                job[section]["linked"].append(data)
        
        elif section == "data_sync":
            # Data sync has jobs array and field_summary
            if not isinstance(job[section], dict):
                job[section] = {"jobs": [], "field_summary": {}, "_updated": now}
            if "jobs" not in job[section]:
                job[section]["jobs"] = []
            
            # If data has jobs array, replace it
            if "jobs" in data:
                job[section]["jobs"] = data["jobs"]
            if "field_summary" in data:
                job[section]["field_summary"] = data["field_summary"]
            
        elif section == "time_entries":
            # Time entries is a list
            if not isinstance(job[section], list):
                job[section] = []
            job[section].append(data)
            
        elif section == "delivery":
            # Delivery tracking - merge and track history
            if "history" not in job[section]:
                job[section]["history"] = []
            
            # Update current state
            for key, val in data.items():
                if key != "event":
                    job[section][key] = val
            
            # Log event if provided
            if data.get("event"):
                job[section]["history"].append({
                    "event": data["event"],
                    "timestamp": now
                })
        
        elif section == "pending":
            # Async Q&A - Claude asking questions
            if not isinstance(job[section], dict):
                job[section] = {"status": None, "_updated": None}
            
            # If clearing pending (status is None), reset all fields
            if data.get("status") is None:
                job[section] = {
                    "status": None,
                    "question": None,
                    "question_type": None,
                    "context": {},
                    "email_id": None,
                    "asked_at": None,
                    "_updated": now
                }
            else:
                # Setting pending - merge in the data
                job[section].update(data)
                job[section]["_updated"] = now
        
        else:
            # Default: merge data into section dict
            if isinstance(job[section], dict):
                job[section].update(data)
            else:
                job[section] = data
        
        # Set section timestamp
        if isinstance(job[section], dict):
            job[section]["_updated"] = now
        
        # Track overall update
        job["last_updated"] = now
        self._save_job_index()
    
    # =========================================================================
    # HIVE INTELLIGENCE - Modular Job Index Enrichment
    # =========================================================================
    # Each enrichment function handles one data source. Add new sources by
    # creating new _enrich_job_from_X methods and calling them from 
    # _enrich_job_in_index or the appropriate tool.
    # =========================================================================
    
    def _get_or_create_job_entry(self, job_number: str) -> dict:
        """Get existing job entry or create new one with base structure."""
        now = datetime.now().isoformat()
        if job_number not in self.job_index:
            self.job_index[job_number] = {
                "job_number": job_number,
                "created": now,
                # Core identity
                "job_folder": None,
                "property_address": None,
                "client_business": None,
                "community": None,
                # Linked data (dict structure with _updated timestamps)
                "data_sync": {"jobs": [], "field_summary": {}, "_updated": now},
                "reference_files": {"_updated": now},
                "emails": {"linked": [], "_updated": now},
                "qbo": {"_updated": None},  # Only set when actually queried
                "document_control": {"_updated": None},
                # Async Q&A - Claude asks questions, awaits your reply
                "pending": {
                    "status": None,  # "awaiting_input" when Claude has asked a question
                    "question": None,  # What Claude is asking
                    "question_type": None,  # e.g., "community_classification", "pricing", "clarification"
                    "context": {},  # Original request details for continuity
                    "email_id": None,  # Message ID of the question email Claude sent
                    "asked_at": None,  # Timestamp when question was sent
                    "_updated": None
                },
                # Status flags
                "has_layout": False,
                "has_field_data": False,
                "has_drawings": False,
                "has_reports": False,
            }
        return self.job_index[job_number]
    
    def _enrich_job_in_index(self, job_number: str, status_data: dict):
        """
        MAIN ENRICHMENT - Called after job_get_status gathers data.
        Delegates to modular enrichment functions for each data source.
        """
        if not job_number or job_number.startswith("TBD"):
            return
        
        # Only enrich if job was actually found
        if not status_data.get("found"):
            return
        
        job = self._get_or_create_job_entry(job_number)
        
        # Enrich from each data source
        self._enrich_job_identity(job, status_data)
        self._enrich_job_from_data_sync(job, status_data)
        self._enrich_job_from_nas_folder(job, status_data)
        
        # Track update
        job["last_updated"] = datetime.now().isoformat()
        self._save_job_index()
    
    def _enrich_job_identity(self, job: dict, status_data: dict):
        """Enrich core identity fields (folder, address, client)."""
        if status_data.get("job_folder"):
            job["job_folder"] = status_data["job_folder"]
        if status_data.get("property_address"):
            job["property_address"] = status_data["property_address"]
        if status_data.get("client_business"):
            job["client_business"] = status_data["client_business"]
        if status_data.get("community"):
            job["community"] = status_data["community"]
        if status_data.get("due_date"):
            job["due_date"] = status_data["due_date"]
    
    def _enrich_job_from_data_sync(self, job: dict, status_data: dict):
        """Enrich from Trimble Data Sync (controller jobs, field data)."""
        data_sync = status_data.get("data_sync", {})
        now = datetime.now().isoformat()
        
        if data_sync.get("data_sync"):
            job["data_sync"] = data_sync["data_sync"]
            job["data_sync_folder"] = data_sync.get("sync_folder")
        
        # Save field_summary (job-wide aggregation) with timestamp
        if data_sync.get("field_summary"):
            job["field_summary"] = data_sync["field_summary"]
            job["field_summary"]["_updated"] = now
        
        job["has_layout"] = status_data.get("has_layout", False)
        job["has_field_data"] = status_data.get("has_field_data", False)
    
    def _enrich_job_from_nas_folder(self, job: dict, status_data: dict):
        """Enrich from NAS job folder (drawings, reports, reference files)."""
        nas = status_data.get("nas_folder", {})
        
        # Drawing and report status
        job["has_drawings"] = status_data.get("has_drawings", False)
        job["has_reports"] = status_data.get("has_reports", False)
        
        if nas.get("drawing_files"):
            job["drawing_files"] = nas["drawing_files"]
        if nas.get("report_files"):
            job["report_files"] = nas["report_files"]
        
        # Reference & Research files (deeds, plans, research docs)
        if nas.get("reference_files"):
            job["reference_files"] = nas["reference_files"]
        
        # Saved emails in folder
        if nas.get("saved_emails"):
            job["saved_email_files"] = nas["saved_emails"]
        
        # Document Control (stamped/sent deliverables)
        if status_data.get("document_control"):
            job["document_control"] = status_data["document_control"]
    
    def _enrich_job_from_qbo(self, job_number: str, qbo_data: dict):
        """
        Enrich from QuickBooks (customer, invoice, payment status).
        Called when QBO operations are performed.
        
        Tracks:
        - invoice_id, invoice_number, amount, balance, due_date
        - status: "Created", "Sent", "Paid"
        - sent_count: how many times invoice was sent
        - sent_to: last recipient email
        - sent_date: last send date
        - paid: boolean
        - paid_date: when payment was recorded
        - _updated: when this data was last verified
        """
        if not job_number:
            return
        
        # Create job entry if it doesn't exist (not just skip)
        job = self._get_or_create_job_entry(job_number)
        
        if "qbo" not in job:
            job["qbo"] = {}
        
        # Update QBO fields
        if qbo_data.get("customer_id"):
            job["qbo"]["customer_id"] = qbo_data["customer_id"]
        if qbo_data.get("project_id"):
            job["qbo"]["project_id"] = qbo_data["project_id"]
        if qbo_data.get("invoice_id"):
            job["qbo"]["invoice_id"] = qbo_data["invoice_id"]
        if qbo_data.get("invoice_number"):
            job["qbo"]["invoice_number"] = qbo_data["invoice_number"]
        if qbo_data.get("amount"):
            job["qbo"]["amount"] = qbo_data["amount"]
        if qbo_data.get("balance") is not None:
            job["qbo"]["balance"] = qbo_data["balance"]
        if qbo_data.get("due_date"):
            job["qbo"]["due_date"] = qbo_data["due_date"]
        
        # Status tracking
        if qbo_data.get("status"):
            job["qbo"]["status"] = qbo_data["status"]
            
            # Track sent count (increment when status is "Sent")
            if qbo_data["status"] == "Sent":
                job["qbo"]["sent_count"] = job["qbo"].get("sent_count", 0) + 1
            
            # Track paid status
            if qbo_data["status"] == "Paid":
                job["qbo"]["paid"] = True
                job["qbo"]["paid_date"] = qbo_data.get("paid_date") or datetime.now().isoformat()
        
        # Determine paid from balance if not explicitly set
        if qbo_data.get("balance") is not None and float(qbo_data["balance"]) == 0:
            job["qbo"]["paid"] = True
            if not job["qbo"].get("paid_date"):
                job["qbo"]["paid_date"] = datetime.now().isoformat()
        
        if qbo_data.get("sent_to"):
            job["qbo"]["sent_to"] = qbo_data["sent_to"]
        if qbo_data.get("sent_date"):
            job["qbo"]["sent_date"] = qbo_data["sent_date"]
        
        # Section timestamp
        job["qbo"]["_updated"] = datetime.now().isoformat()
        job["last_updated"] = datetime.now().isoformat()
        self._save_job_index()
    
    def _link_email_to_job(self, job_number: str, email_info: dict,
                           summary: str = None, key_info: dict = None):
        """
        Link an email to a job in the index with content for future reference.

        EMAIL STORAGE IN JOB INDEX
        ==========================
        Each linked email stores:
        - message_id: Unique Graph API ID (for fetching via email_get_by_id)
        - conversation_id: Thread ID (all emails in chain share this)
        - subject, from, to, date, direction: Metadata
        - summary: Claude's 1-2 sentence summary (CRITICAL for context)
        - key_info: Structured extracted data (dates, names, amounts)
        - body_stored: FULL email body (NO truncation) for later retrieval

        WHY STORE BOTH SUMMARY AND BODY?
        ================================
        - Summary: Quick context for follow-up emails. When a new email arrives
          in a known thread, Claude sees summaries instead of re-reading everything.
          This saves tokens on initial processing.

        - Body: FULL content stored so Claude can retrieve exact wording later -
          drafting replies, checking specific details, etc. NO truncation here.
          Truncation only happens when PRESENTING to Claude, not when STORING.

        RELATED CODE:
        - _job_link_email(): Prepares email_info with body_stored field
        - _get_thread_context() in claude_agent.py: Uses summaries for quick context
        - job_get_linked_email tool (TODO): Would retrieve stored body on demand

        Args:
            job_number: The job to link to
            email_info: Dict with message_id, subject, from, to, date, direction, body_stored
            summary: Claude's summary of the email content (REQUIRED for good context)
            key_info: Extracted key information (closing_date, purchaser, etc.)
        """
        if not job_number:
            return

        now = datetime.now().isoformat()

        # Create job entry if it doesn't exist
        job = self._get_or_create_job_entry(job_number)

        # Ensure emails is dict structure with linked array
        if not isinstance(job.get("emails"), dict):
            # Migrate from list to dict structure
            old_list = job.get("emails", [])
            job["emails"] = {
                "linked": old_list if isinstance(old_list, list) else [],
                "_updated": now
            }

        # Check if email already linked (by message_id)
        existing_ids = [e.get("message_id") for e in job["emails"]["linked"]]
        if email_info.get("message_id") in existing_ids:
            return  # Already linked

        # Build email record with all available data
        email_record = {
            "message_id": email_info.get("message_id"),
            "conversation_id": email_info.get("conversation_id"),
            "subject": email_info.get("subject"),
            "from": email_info.get("from"),
            "to": email_info.get("to"),
            "date": email_info.get("date"),
            "direction": email_info.get("direction", "unknown"),
            "linked_at": now
        }

        # Add content fields
        if summary:
            email_record["summary"] = summary
        if key_info:
            email_record["key_info"] = key_info
        if email_info.get("body_stored"):
            email_record["body_stored"] = email_info["body_stored"]

        job["emails"]["linked"].append(email_record)
        job["emails"]["_updated"] = now
        job["last_email_date"] = email_info.get("date")
        job["last_updated"] = now

        self._save_job_index()
    
    def _link_conversation_to_job(self, job_number: str, conversation_id: str):
        """
        Link ALL emails in a conversation thread to a job.
        Uses Graph API to find all messages in the conversation.
        
        Call this when linking an email to auto-link the whole thread.
        """
        if not job_number or not conversation_id or not self.email:
            return {"linked": 0}
        
        try:
            # Search for all emails in this conversation
            # Graph API filter by conversationId
            emails = self.email.search_emails(
                query=f"conversationId:{conversation_id}",
                max_results=50
            )
            
            linked_count = 0
            for email in emails:
                email_info = {
                    "message_id": email.message_id,
                    "conversation_id": email.conversation_id,
                    "subject": email.subject,
                    "from": email.sender_email,
                    "to": getattr(email, 'to_recipients', ''),
                    "date": str(email.received_date),
                    "direction": "inbound" if "@pardysurveys.ca" not in email.sender_email else "outbound"
                }
                
                # _link_email_to_job handles deduplication
                self._link_email_to_job(job_number, email_info)
                linked_count += 1
            
            return {"linked": linked_count, "conversation_id": conversation_id}
        except Exception as e:
            return {"linked": 0, "error": str(e)}
    
    def _get_job_from_index(self, job_number: str) -> Optional[dict]:
        """Get job from index if it exists (for quick lookups)."""
        return self.job_index.get(job_number)
    
    def _find_job_by_email(self, email_address: str) -> List[dict]:
        """Find all jobs linked to an email address (for incoming email routing)."""
        matches = []
        for job_num, job in self.job_index.items():
            if not isinstance(job, dict):
                continue  # Skip metadata keys
            # Check linked emails (handle both old list and new dict structure)
            emails_section = job.get("emails", {})
            if isinstance(emails_section, dict):
                email_list = emails_section.get("linked", [])
            else:
                email_list = emails_section if isinstance(emails_section, list) else []
            
            for email in email_list:
                if email.get("from") == email_address or email.get("to") == email_address:
                    matches.append(job)
                    break
            # Check client email if stored
            if job.get("client_email") == email_address:
                if job not in matches:
                    matches.append(job)
        return matches
    
    def _parse_document_control(self, doc_control_path: str, job_number: str) -> dict:
        """
        Parse Document Control folder to understand what's been stamped and sent.
        
        Naming conventions:
        - 25-180-1.pdf     = Description (stamped)
        - 25-180--1.pdf    = Drawing (double hyphen = drawing)
        - Something (25-180-1).pdf = Final sent document (parentheses)
        - 25-180-1-R1.pdf  = Revision 1 of description
        - 25-180--1-R2.pdf = Revision 2 of drawing
        
        Returns structured data showing what documents exist and their status.
        """
        result = {
            "documents": {},  # Keyed by doc number (1, 2, etc.)
            "other_files": [],
            "total_documents": 0,
            "total_sent": 0
        }
        
        try:
            # Walk through all subfolders to find PDFs (some jobs have subfolders in Document Control)
            files = []
            for root, dirs, filenames in os.walk(doc_control_path):
                for f in filenames:
                    if f.lower().endswith('.pdf') and f.lower() != 'thumbs.db':
                        files.append(f)
            
            # Patterns to match
            # Description: 25-180-1.pdf or 25-180-1-R1.pdf
            desc_pattern = re.compile(rf'^{re.escape(job_number)}-(\d+)(?:-R(\d+))?\.pdf$', re.IGNORECASE)
            # Drawing: 25-180--1.pdf or 25-180--1-R1.pdf (double hyphen)
            draw_pattern = re.compile(rf'^{re.escape(job_number)}--(\d+)(?:-R(\d+))?\.pdf$', re.IGNORECASE)
            # Sent: Something (25-180-1).pdf or (25-180-1-R1).pdf
            sent_pattern = re.compile(rf'\({re.escape(job_number)}-(\d+)(?:-R(\d+))?\)\.pdf$', re.IGNORECASE)
            
            for f in files:
                matched = False
                
                # Check for description
                m = desc_pattern.match(f)
                if m:
                    doc_num = m.group(1)
                    revision = m.group(2)
                    if doc_num not in result["documents"]:
                        result["documents"][doc_num] = {
                            "doc_number": int(doc_num),
                            "description": None,
                            "drawing": None,
                            "sent_file": None,
                            "latest_revision": None
                        }
                    
                    doc = result["documents"][doc_num]
                    if revision:
                        doc["description"] = f
                        rev_num = int(revision)
                        if doc["latest_revision"] is None or rev_num > doc["latest_revision"]:
                            doc["latest_revision"] = rev_num
                    else:
                        # Original version
                        if doc["description"] is None or not "-R" in (doc["description"] or ""):
                            doc["description"] = f
                    matched = True
                
                # Check for drawing (double hyphen)
                m = draw_pattern.match(f)
                if m:
                    doc_num = m.group(1)
                    revision = m.group(2)
                    if doc_num not in result["documents"]:
                        result["documents"][doc_num] = {
                            "doc_number": int(doc_num),
                            "description": None,
                            "drawing": None,
                            "sent_file": None,
                            "latest_revision": None
                        }
                    
                    doc = result["documents"][doc_num]
                    if revision:
                        doc["drawing"] = f
                        rev_num = int(revision)
                        if doc["latest_revision"] is None or rev_num > doc["latest_revision"]:
                            doc["latest_revision"] = rev_num
                    else:
                        if doc["drawing"] is None or not "-R" in (doc["drawing"] or ""):
                            doc["drawing"] = f
                    matched = True
                
                # Check for sent file (parentheses pattern)
                m = sent_pattern.search(f)
                if m:
                    doc_num = m.group(1)
                    if doc_num not in result["documents"]:
                        result["documents"][doc_num] = {
                            "doc_number": int(doc_num),
                            "description": None,
                            "drawing": None,
                            "sent_file": None,
                            "latest_revision": None
                        }
                    result["documents"][doc_num]["sent_file"] = f
                    matched = True
                
                if not matched:
                    result["other_files"].append(f)
            
            # Convert to list sorted by doc number and compute totals
            doc_list = sorted(result["documents"].values(), key=lambda x: x["doc_number"])
            result["documents"] = doc_list
            result["total_documents"] = len(doc_list)
            result["total_sent"] = sum(1 for d in doc_list if d["sent_file"])
            result["all_sent"] = result["total_sent"] == result["total_documents"] and result["total_documents"] > 0
            
        except Exception as e:
            result["error"] = str(e)
        
        return result
    
    def execute(self, tool_name: str, params: dict) -> str:
        """Execute a tool and return JSON result."""
        self.logger.info(f"    Tool: {tool_name}")
        
        try:
            result = self._dispatch_tool(tool_name, params)
            return json.dumps(result, default=str)
        except Exception as e:
            self.logger.error(f"    Tool error: {e}")
            return json.dumps({"error": str(e)})
    
    def _dispatch_tool(self, name: str, params: dict) -> dict:
        """Route to appropriate tool handler."""
        
        # QBO Tools
        if name == "qbo_search_invoices":
            return self._qbo_search_invoices(params)
        elif name == "qbo_get_invoice":
            return self._qbo_get_invoice(params)
        elif name == "qbo_send_invoice":
            return self._qbo_send_invoice(params)
        elif name == "qbo_create_invoice":
            return self._qbo_create_invoice(params)
        # Estimates/Quotes
        elif name == "qbo_create_estimate":
            return self._qbo_create_estimate(params)
        elif name == "qbo_send_estimate":
            return self._qbo_send_estimate(params)
        elif name == "qbo_get_estimate":
            return self._qbo_get_estimate(params)
        elif name == "qbo_search_estimates":
            return self._qbo_search_estimates(params)
        elif name == "qbo_convert_estimate_to_invoice":
            return self._qbo_convert_estimate_to_invoice(params)
        elif name == "qbo_search_customers":
            return self._qbo_search_customers(params)
        elif name == "qbo_create_customer":
            return self._qbo_create_customer(params)
        elif name == "qbo_search_projects":
            return self._qbo_search_projects(params)
        elif name == "qbo_create_project":
            return self._qbo_create_project(params)
        elif name == "qbo_get_next_job_number":
            return self._qbo_get_next_job_number(params)
        elif name == "qbo_get_service_items":
            return self._qbo_get_service_items(params)
        elif name == "qbo_record_payment":
            return self._qbo_record_payment(params)
        elif name == "qbo_create_time_entry":
            return self._qbo_create_time_entry(params)
        elif name == "qbo_get_time_entries":
            return self._qbo_get_time_entries(params)
        elif name == "qbo_record_expense":
            return self._qbo_record_expense(params)

        # Email Tools
        elif name == "email_get_unread":
            return self._email_get_unread(params)
        elif name == "email_get_by_id":
            return self._email_get_by_id(params)
        elif name == "email_search":
            return self._email_search(params)
        elif name == "email_get_history_with_contact":
            return self._email_get_history(params)
        elif name == "email_list_folders":
            return self._email_list_folders(params)
        elif name == "email_send_reply":
            return self._email_send_reply(params)
        elif name == "email_create_draft":
            return self._email_create_draft(params)
        elif name == "email_send_new":
            return self._email_send_new(params)
        elif name == "email_move_to_folder":
            return self._email_move_to_folder(params)
        elif name == "email_mark_read":
            return self._email_mark_read(params)
        elif name == "email_mark_unread":
            return self._email_mark_unread(params)
        elif name == "email_flag_important":
            return self._email_flag_important(params)
        elif name == "email_create_folder":
            return self._email_create_folder(params)
        elif name == "email_get_attachments":
            return self._email_get_attachments(params)
        elif name == "email_save_attachment":
            return self._email_save_attachment(params)
        elif name == "email_send_with_attachment":
            return self._email_send_with_attachment(params)
        elif name == "email_sync_sent_to_jobs":
            return self._email_sync_sent_to_jobs(params)

        # NAS File Tools
        elif name == "nas_list_directory":
            return self._nas_list_directory(params)
        elif name == "nas_read_file":
            return self._nas_read_file(params)
        elif name == "nas_file_exists":
            return self._nas_file_exists(params)
        elif name == "nas_get_file_info":
            return self._nas_get_file_info(params)
        elif name == "nas_search_files":
            return self._nas_search_files(params)
        elif name == "nas_create_directory":
            return self._nas_create_directory(params)
        elif name == "nas_write_file":
            return self._nas_write_file(params)
        elif name == "nas_copy_file":
            return self._nas_copy_file(params)
        elif name == "nas_copy_directory":
            return self._nas_copy_directory(params)
        elif name == "nas_move_file":
            return self._nas_move_file(params)
        
        # Job Tools
        elif name == "job_create":
            return self._job_create(params)
        elif name == "job_search":
            return self._job_search(params)
        elif name == "job_get_status":
            return self._job_get_status(params)
        elif name == "job_save_email":
            return self._job_save_email(params)
        elif name == "job_link_email":
            return self._job_link_email(params)
        elif name == "job_list_recent":
            return self._job_list_recent(params)
        elif name == "job_get_thread_emails":
            return self._job_get_thread_emails(params)
        elif name == "job_get_email_body":
            return self._job_get_email_body(params)
        elif name == "job_set_pending":
            return self._job_set_pending(params)
        elif name == "job_clear_pending":
            return self._job_clear_pending(params)
        elif name == "job_get_pending":
            return self._job_get_pending(params)

        # Proposal Tracking
        elif name == "proposal_create":
            return self._proposal_create(params)
        elif name == "proposal_update":
            return self._proposal_update(params)
        elif name == "proposal_get":
            return self._proposal_get(params)
        elif name == "proposal_search":
            return self._proposal_search(params)
        elif name == "proposal_list":
            return self._proposal_list(params)
        elif name == "proposal_convert_to_job":
            return self._proposal_convert_to_job(params)

        # Utility Tools
        elif name == "flag_for_attention":
            return self._flag_for_attention(params)
        elif name == "log_system_error":
            return self._log_system_error_tool(params)
        elif name == "get_current_datetime":
            return self._get_current_datetime(params)
        elif name == "parse_date":
            return self._parse_date(params)

        else:
            return {"error": f"Unknown tool: {name}"}
    
    # =========================================================================
    # QBO IMPLEMENTATIONS
    # =========================================================================
    
    def _qbo_search_invoices(self, params: dict) -> dict:
        if not self.qbo.access_token:
            return {"error": "QBO not connected"}
        
        invoices = []
        
        if params.get('invoice_number'):
            inv = self.qbo.find_invoice_by_number(params['invoice_number'])
            if inv:
                invoices = [inv]
        
        elif params.get('address'):
            recent = self.qbo.get_recent_invoices(50)
            inv = self.qbo.search_invoice_by_address(params['address'], recent)
            if inv:
                invoices = [inv]
        
        elif params.get('customer_email'):
            invoices = self.qbo.find_invoices_by_customer_email(params['customer_email'])
        
        elif params.get('company_domain'):
            invoices = self.qbo.find_invoices_by_company_domain(params['company_domain'])
        
        else:
            count = min(params.get('recent_count', 20), 50)
            invoices = self.qbo.get_recent_invoices(count)
        
        # Format
        results = []
        for inv in invoices[:20]:
            lines = inv.get('Line', [])
            descriptions = [l.get('Description', '') for l in lines if l.get('Description')]
            
            invoice_data = {
                "id": inv.get('Id'),
                "number": inv.get('DocNumber'),
                "amount": inv.get('TotalAmt'),
                "balance": inv.get('Balance'),
                "due_date": inv.get('DueDate'),
                "customer": inv.get('CustomerRef', {}).get('name', ''),
                "descriptions": descriptions,
                "status": "Paid" if float(inv.get('Balance', 1)) == 0 else "Unpaid"
            }
            results.append(invoice_data)
            
            # ENRICH: If invoice number looks like a job number, update the index
            job_number = inv.get('DocNumber')
            if job_number and re.match(r'^\d{2}-\d{3}$', job_number):
                self._enrich_job_from_qbo(job_number, {
                    "invoice_id": inv.get('Id'),
                    "invoice_number": job_number,
                    "amount": inv.get('TotalAmt'),
                    "balance": inv.get('Balance'),
                    "due_date": inv.get('DueDate'),
                    "customer_id": inv.get('CustomerRef', {}).get('value'),
                    "status": invoice_data["status"]
                })
        
        return {"found": len(results), "invoices": results}
    
    def _qbo_get_invoice(self, params: dict) -> dict:
        inv = self.qbo.get_invoice_by_id(params['invoice_id'])
        if not inv:
            return {"found": False}
        
        lines = inv.get('Line', [])
        descriptions = [l.get('Description', '') for l in lines if l.get('Description')]
        
        status = "Paid" if float(inv.get('Balance', 1)) == 0 else "Unpaid"
        
        # ENRICH: If invoice number looks like a job number, update the index
        job_number = inv.get('DocNumber')
        if job_number and re.match(r'^\d{2}-\d{3}$', job_number):
            self._enrich_job_from_qbo(job_number, {
                "invoice_id": inv.get('Id'),
                "invoice_number": job_number,
                "amount": inv.get('TotalAmt'),
                "balance": inv.get('Balance'),
                "due_date": inv.get('DueDate'),
                "customer_id": inv.get('CustomerRef', {}).get('value'),
                "status": status
            })
        
        return {
            "found": True,
            "invoice": {
                "id": inv.get('Id'),
                "number": inv.get('DocNumber'),
                "amount": inv.get('TotalAmt'),
                "balance": inv.get('Balance'),
                "due_date": inv.get('DueDate'),
                "customer_id": inv.get('CustomerRef', {}).get('value'),
                "customer": inv.get('CustomerRef', {}).get('name', ''),
                "descriptions": descriptions,
                "email": inv.get('BillEmail', {}).get('Address'),
                "status": status
            }
        }
    
    # TODO: PLANNER UI - Invoice sending is a final step in job completion workflow.
    # Future planner UI should have a button/trigger for this that:
    # 1. Verifies job is complete (field work done, documents delivered)
    # 2. Sends invoice via this function
    # 3. Updates job status/progress to "Invoiced" or "Awaiting Payment"
    # This helps track job lifecycle and ensures invoices aren't sent prematurely.
    def _qbo_send_invoice(self, params: dict) -> dict:
        try:
            invoice_id = params['invoice_id']
            recipient = params['recipient_email']
            
            # Get invoice details first to extract job number
            inv = self.qbo.get_invoice_by_id(invoice_id)
            job_number = inv.get('DocNumber') if inv else None
            
            self.qbo.send_invoice(invoice_id, recipient)
            
            # Enrich job index with sent status
            if job_number:
                self._enrich_job_from_qbo(job_number, {
                    "invoice_id": invoice_id,
                    "invoice_number": job_number,
                    "status": "Sent",
                    "sent_to": recipient,
                    "sent_date": datetime.now().isoformat()
                })
            
            return {"success": True, "message": f"Invoice sent to {recipient}"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _qbo_create_invoice(self, params: dict) -> dict:
        try:
            job_number = params.get('invoice_number')  # Invoice number = job number
            
            inv = self.qbo.create_job_invoice(
                customer_id=params['customer_id'],
                job_number=job_number,
                item_name=params.get('item_name', 'Boundary Survey & Real Property Report'),
                amount=params.get('amount'),
                project_id=params.get('project_id'),
                due_date=params.get('due_date'),
                property_address=params.get('description', '')
            )
            if inv:
                # Enrich the job index with QBO data
                if job_number:
                    self._enrich_job_from_qbo(job_number, {
                        "invoice_id": inv.get('Id'),
                        "invoice_number": inv.get('DocNumber'),
                        "customer_id": params.get('customer_id'),
                        "project_id": params.get('project_id'),
                        "amount": inv.get('TotalAmt'),
                        "due_date": inv.get('DueDate'),
                        "status": "Created"
                    })
                
                return {"success": True, "invoice_id": inv['Id'], "number": inv.get('DocNumber')}
            return {"success": False, "error": "Creation returned None"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # =========================================================================
    # QBO - ESTIMATES/QUOTES
    # =========================================================================

    def _qbo_create_estimate(self, params: dict) -> dict:
        """Create an estimate/quote in QBO."""
        try:
            # Get next estimate number with community suffix
            community = params.get('community')
            doc_number = self.qbo.get_next_estimate_number(community)

            # Calculate expiration date
            exp_days = params.get('expiration_days', 30)
            exp_date = (datetime.now() + timedelta(days=exp_days)).strftime('%Y-%m-%d')

            # Build line items
            line_items = [{
                'amount': params['amount'],
                'description': params['description'],
                'quantity': 1
            }]

            estimate = self.qbo.create_estimate(
                customer_id=params['customer_id'],
                line_items=line_items,
                doc_number=doc_number,
                expiration_date=exp_date,
                customer_memo=params.get('customer_memo')
            )

            if estimate:
                return {
                    "success": True,
                    "estimate_id": estimate['Id'],
                    "doc_number": estimate.get('DocNumber'),
                    "amount": estimate.get('TotalAmt'),
                    "expiration_date": exp_date,
                    "message": f"Estimate {doc_number} created for ${params['amount']}"
                }
            return {"success": False, "error": "Estimate creation returned None"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _qbo_send_estimate(self, params: dict) -> dict:
        """Send an estimate via QBO email."""
        try:
            success = self.qbo.send_estimate(
                params['estimate_id'],
                params.get('recipient_email')
            )
            if success:
                return {
                    "success": True,
                    "message": f"Estimate sent to {params.get('recipient_email', 'customer email')}"
                }
            return {"success": False, "error": "Failed to send estimate"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _qbo_get_estimate(self, params: dict) -> dict:
        """Get estimate details by ID."""
        try:
            estimate = self.qbo.get_estimate(params['estimate_id'])
            if estimate:
                return {
                    "success": True,
                    "estimate": {
                        "id": estimate.get('Id'),
                        "doc_number": estimate.get('DocNumber'),
                        "customer_id": estimate.get('CustomerRef', {}).get('value'),
                        "customer_name": estimate.get('CustomerRef', {}).get('name'),
                        "amount": estimate.get('TotalAmt'),
                        "status": estimate.get('TxnStatus', 'Pending'),
                        "expiration_date": estimate.get('ExpirationDate'),
                        "created_date": estimate.get('TxnDate'),
                        "lines": [
                            {
                                "description": line.get('Description', ''),
                                "amount": line.get('Amount', 0)
                            }
                            for line in estimate.get('Line', [])
                            if line.get('DetailType') == 'SalesItemLineDetail'
                        ]
                    }
                }
            return {"success": False, "error": "Estimate not found"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _qbo_search_estimates(self, params: dict) -> dict:
        """Search for estimates."""
        try:
            estimates = self.qbo.search_estimates(
                customer_id=params.get('customer_id'),
                doc_number=params.get('doc_number'),
                status=params.get('status'),
                recent_count=params.get('recent_count', 20)
            )

            results = [{
                "id": e.get('Id'),
                "doc_number": e.get('DocNumber'),
                "customer_name": e.get('CustomerRef', {}).get('name'),
                "amount": e.get('TotalAmt'),
                "status": e.get('TxnStatus', 'Pending'),
                "date": e.get('TxnDate'),
                "expiration": e.get('ExpirationDate')
            } for e in estimates]

            return {
                "success": True,
                "count": len(results),
                "estimates": results
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _qbo_convert_estimate_to_invoice(self, params: dict) -> dict:
        """Convert an accepted estimate to an invoice."""
        try:
            invoice = self.qbo.convert_estimate_to_invoice(params['estimate_id'])

            if invoice:
                # Try to find the job number from the estimate or description
                job_number = invoice.get('DocNumber')

                # Enrich job index if we have a job number
                if job_number and job_number in self.job_index:
                    self._enrich_job_from_qbo(job_number, {
                        "invoice_id": invoice.get('Id'),
                        "invoice_number": invoice.get('DocNumber'),
                        "amount": invoice.get('TotalAmt'),
                        "status": "Created from Estimate",
                        "estimate_id": params['estimate_id']
                    })

                return {
                    "success": True,
                    "invoice_id": invoice['Id'],
                    "invoice_number": invoice.get('DocNumber'),
                    "amount": invoice.get('TotalAmt'),
                    "message": f"Invoice created from estimate {params['estimate_id']}"
                }
            return {"success": False, "error": "Failed to convert estimate"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _qbo_search_customers(self, params: dict) -> dict:
        customers = []
        
        if params.get('email'):
            cust = self.qbo.find_customer_by_email(params['email'])
            if cust:
                customers = [cust]
        elif params.get('name'):
            cust = self.qbo.find_customer_by_name(params['name'])
            if cust:
                customers = [cust]
        
        results = [{
            "id": c.get('Id'),
            "name": c.get('DisplayName'),
            "email": c.get('PrimaryEmailAddr', {}).get('Address')
        } for c in customers]
        
        return {"found": len(results), "customers": results}
    
    def _qbo_create_customer(self, params: dict) -> dict:
        try:
            # find_or_create_customer returns just the ID string
            customer_id = self.qbo.find_or_create_customer(
                name=params['name'],
                email=params['email']
            )
            if customer_id:
                return {"success": True, "customer_id": customer_id, "name": params['name']}
            return {"success": False, "error": "Creation returned None"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _qbo_search_projects(self, params: dict) -> dict:
        if params.get('name'):
            proj = self.qbo.find_project_by_name(params['name'])
            if proj:
                return {"found": 1, "projects": [{"id": proj['Id'], "name": proj.get('DisplayName')}]}
        return {"found": 0, "projects": []}
    
    def _qbo_create_project(self, params: dict) -> dict:
        try:
            # create_project returns just the project ID string
            # Project name = job number (e.g., "26-001")
            job_number = params['name']
            
            project_id = self.qbo.create_project(
                customer_id=params['customer_id'],
                project_name=job_number,
                description=params.get('description', '')
            )
            if project_id:
                # Enrich job index - project is always for a job
                if re.match(r'^\d{2}-\d{3}$', job_number):
                    self._enrich_job_from_qbo(job_number, {
                        "project_id": project_id,
                        "customer_id": params['customer_id']
                    })
                
                return {"success": True, "project_id": project_id}
            return {"success": False, "error": "Creation returned None"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _qbo_get_next_job_number(self, params: dict) -> dict:
        try:
            next_num = self.qbo.get_next_job_number()
            return {"job_number": next_num}  # Changed from next_job_number
        except Exception as e:
            return {"error": str(e)}
    
    def _qbo_get_service_items(self, params: dict) -> dict:
        items = self.qbo.get_items()
        results = [{"name": i.get('Name'), "price": i.get('UnitPrice'), "id": i.get('Id')} for i in items[:30]]
        return {"count": len(results), "items": results}  # Added count
    
    def _qbo_record_payment(self, params: dict) -> dict:
        """Record payment for an invoice (by job number or invoice ID)."""
        try:
            results = []
            invoice_numbers = params.get('invoice_numbers', [])
            payment_date = params.get('payment_date')  # Optional, defaults to today
            
            for inv_num in invoice_numbers:
                inv_num = inv_num.strip()
                if not inv_num:
                    continue
                
                # Record the payment
                payment = self.qbo.record_payment_by_invoice_number(
                    invoice_number=inv_num,
                    payment_date=payment_date
                )
                
                if payment:
                    if payment.get('already_paid'):
                        results.append({
                            "invoice_number": inv_num,
                            "status": "already_paid",
                            "message": f"Invoice {inv_num} already has zero balance"
                        })
                    else:
                        # ENRICH: Update the job index with payment status
                        if re.match(r'^\d{2}-\d{3}$', inv_num):
                            self._enrich_job_from_qbo(inv_num, {
                                "invoice_number": inv_num,
                                "status": "Paid",
                                "paid_date": payment_date or datetime.now().strftime('%Y-%m-%d'),
                                "payment_id": payment.get('Id')
                            })
                        
                        results.append({
                            "invoice_number": inv_num,
                            "status": "paid",
                            "payment_id": payment.get('Id'),
                            "amount": payment.get('TotalAmt')
                        })
                else:
                    results.append({
                        "invoice_number": inv_num,
                        "status": "failed",
                        "message": f"Could not find or pay invoice {inv_num}"
                    })
            
            paid_count = sum(1 for r in results if r['status'] == 'paid')
            return {
                "processed": len(results),
                "paid": paid_count,
                "results": results
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _qbo_create_time_entry(self, params: dict) -> dict:
        """Create a time entry in QBO."""
        try:
            employee_name = params.get('employee_name')
            hours = params.get('hours')
            job_number = params.get('job_number')
            entry_type = params.get('entry_type', 'field')
            date = params.get('date')
            description = params.get('description')
            billable = params.get('billable')

            # Map entry type to service item ID
            SERVICE_ITEMS = {
                'field': '2',      # Field Time
                'travel': '111',   # Travel
                'drafting': '3',   # Drafting
                'research': '108', # Research
                'other': '106'     # Other
            }
            service_item_id = SERVICE_ITEMS.get(entry_type, '2')

            # Default billable based on type
            if billable is None:
                billable = entry_type not in ['travel']

            # Find employee
            employee = self.qbo.find_employee_by_name(employee_name)
            if not employee:
                return {"success": False, "error": f"Employee '{employee_name}' not found"}

            # Create the time entry
            entry = self.qbo.create_time_entry(
                employee_id=employee['Id'],
                hours=hours,
                job_number=job_number,
                service_item_id=service_item_id,
                description=description,
                date=date,
                billable=billable
            )

            if entry:
                # Enrich the job index with time entry info
                self.enrich_job_index(job_number, "time_entries", {
                    "entry_id": entry['Id'],
                    "employee": employee_name,
                    "hours": hours,
                    "type": entry_type,
                    "date": date or datetime.now().strftime('%Y-%m-%d'),
                    "description": description,
                    "source": "manual"
                })

                return {
                    "success": True,
                    "entry_id": entry['Id'],
                    "employee": employee['DisplayName'],
                    "hours": hours,
                    "job_number": job_number,
                    "type": entry_type
                }
            else:
                return {"success": False, "error": "Failed to create time entry in QBO"}

        except Exception as e:
            return {"success": False, "error": str(e)}

    def _qbo_get_time_entries(self, params: dict) -> dict:
        """Get time entries for a job."""
        try:
            job_number = params.get('job_number')
            entries = self.qbo.get_time_entries_for_job(job_number)

            results = []
            total_hours = 0

            for e in entries:
                hours = e.get('Hours', 0) + e.get('Minutes', 0) / 60
                total_hours += hours
                results.append({
                    "entry_id": e.get('Id'),
                    "date": e.get('TxnDate'),
                    "employee": e.get('EmployeeRef', {}).get('name'),
                    "hours": round(hours, 2),
                    "type": e.get('ItemRef', {}).get('name'),
                    "description": e.get('Description', '')[:100],
                    "billable": e.get('BillableStatus') == 'Billable'
                })

            # Enrich job index with time entry data
            if job_number and results:
                self.enrich_job_index(job_number, "qbo", {
                    "time_entries": results,
                    "total_hours": round(total_hours, 2),
                    "entry_count": len(results)
                })

            return {
                "job_number": job_number,
                "total_hours": round(total_hours, 2),
                "entry_count": len(results),
                "entries": results
            }

        except Exception as e:
            return {"success": False, "error": str(e)}

    def _qbo_record_expense(self, params: dict) -> dict:
        """Record an expense/receipt in QuickBooks."""
        try:
            vendor = params.get('vendor')
            amount = params.get('amount')
            category = params.get('category')
            description = params.get('description')
            date = params.get('date')
            payment_method = params.get('payment_method', 'Credit Card')
            receipt_email_id = params.get('receipt_email_id')

            if not vendor or not amount or not category:
                return {"success": False, "error": "Missing required fields: vendor, amount, category"}

            # Map friendly category names to QBO account names
            CATEGORY_MAP = {
                'Office Supplies': 'Office/General Administrative Expenses',
                'Equipment': 'Equipment',
                'Vehicle Expense': 'Automobile',
                'Software & Subscriptions': 'Computer and Internet Expenses',
                'Professional Services': 'Professional Fees',
                'Travel': 'Travel',
                'Meals': 'Meals and Entertainment',
                'Field Supplies': 'Job Supplies',
                'Insurance': 'Insurance',
                'Utilities': 'Utilities',
                'Other': 'Miscellaneous Expense'
            }

            # Get QBO account name
            account_name = CATEGORY_MAP.get(category, category)

            # Map payment method to QBO PaymentType
            PAYMENT_MAP = {
                'Credit Card': 'CreditCard',
                'Debit': 'Cash',
                'Cash': 'Cash',
                'EFT': 'Cash',
                'Other': 'Cash'
            }
            payment_type = PAYMENT_MAP.get(payment_method, 'Cash')

            # Build memo with reference to email if provided
            memo = description or ''
            if receipt_email_id:
                memo = f"{memo} [Email ID: {receipt_email_id}]" if memo else f"[Email ID: {receipt_email_id}]"

            # Create the expense in QBO
            expense = self.qbo.create_expense(
                vendor_name=vendor,
                amount=float(amount),
                account_name=account_name,
                description=description,
                txn_date=date,
                payment_type=payment_type,
                memo=memo
            )

            if expense:
                return {
                    "success": True,
                    "expense_id": expense.get('Id'),
                    "vendor": vendor,
                    "amount": amount,
                    "category": category,
                    "date": date or expense.get('TxnDate')
                }
            else:
                return {"success": False, "error": "Failed to create expense in QBO"}

        except Exception as e:
            return {"success": False, "error": str(e)}

    def _qbo_get_expense_categories(self, params: dict) -> dict:
        """Get available expense categories from QBO."""
        try:
            categories = self.qbo.get_expense_categories()
            return {"count": len(categories), "categories": categories}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # =========================================================================
    # EMAIL IMPLEMENTATIONS
    # =========================================================================
    
    def _email_get_unread(self, params: dict) -> dict:
        try:
            folder = params.get('folder', 'Inbox')
            max_results = params.get('max_results', 20)
            emails = self.email.get_unread_emails(folder=folder, max_results=max_results)
            
            results = [{
                "message_id": e.message_id,
                "conversation_id": e.conversation_id,
                "sender": e.sender,
                "sender_email": e.sender_email,
                "subject": e.subject,
                "received": str(e.received_date),
                "preview": e.body[:200] if e.body else ""
            } for e in emails]
            
            return {"count": len(results), "emails": results}
        except Exception as e:
            return {"error": str(e)}
    
    def _email_get_by_id(self, params: dict) -> dict:
        try:
            email = self.email.get_email_by_id(params['message_id'])
            if not email:
                return {"found": False}

            # Smart body truncation to prevent token overflow
            # Default: truncate to 20K chars (~5K tokens) - enough for most emails
            # If include_full_body=True, return up to 100K chars (~25K tokens)
            # This prevents 200K+ token overflows while allowing access to more content when needed
            include_full = params.get('include_full_body', False)
            MAX_BODY_CHARS = 100000 if include_full else 20000

            body = email.body or ""
            body_truncated = False
            original_length = len(body)

            if original_length > MAX_BODY_CHARS:
                body = body[:MAX_BODY_CHARS]
                body_truncated = True

            result = {
                "found": True,
                "email": {
                    "message_id": email.message_id,
                    "conversation_id": email.conversation_id,
                    "sender": email.sender,
                    "sender_email": email.sender_email,
                    "subject": email.subject,
                    "received": str(email.received_date),
                    "body": body,
                    # Strip attachment data (base64 content) to prevent token overflow
                    # Only return metadata - use email_save_attachment to get actual content
                    "attachments": [
                        {
                            "id": att.get("id"),
                            "name": att.get("name"),
                            "size": att.get("size"),
                            "contentType": att.get("contentType")
                            # Intentionally omitting 'data' (contentBytes) which can be huge
                        }
                        for att in (email.attachments or [])
                    ]
                }
            }

            # If truncated, add note so Claude knows it can request more
            if body_truncated:
                result["body_truncated"] = True
                result["body_original_chars"] = original_length
                result["body_shown_chars"] = MAX_BODY_CHARS
                if not include_full:
                    result["note"] = "Body was truncated. Call again with include_full_body=true if you need the complete content."

            return result
        except Exception as e:
            return {"error": str(e)}
    
    def _email_search(self, params: dict) -> dict:
        try:
            results = self.email.search_emails(
                query=params['query'],
                max_results=params.get('max_results', 20),
                folder=params.get('folder')
            )
            
            emails = [{
                "message_id": e.message_id,
                "sender": e.sender,
                "sender_email": e.sender_email,
                "subject": e.subject,
                "received": str(e.received_date),
                "preview": e.body[:200] if e.body else ""
            } for e in results]
            
            return {"count": len(emails), "emails": emails}
        except Exception as e:
            return {"error": str(e)}
    
    def _email_get_history(self, params: dict) -> dict:
        try:
            history = self.email.get_email_history_with_contact(
                params['email_address'],
                max_results=params.get('max_results', 10)
            )
            return {"count": len(history), "emails": history}
        except Exception as e:
            return {"error": str(e)}
    
    def _email_list_folders(self, params: dict) -> dict:
        try:
            folders = self.email.list_folders()
            return {"folders": folders}
        except Exception as e:
            return {"error": str(e)}
    
    def _email_send_reply(self, params: dict) -> dict:
        try:
            msg_id = params.get('message_id')
            if not msg_id and self.current_email:
                msg_id = self.current_email.message_id

            # GUARD: Prevent sending multiple replies to the same message in one session
            if msg_id in self.replied_message_ids:
                self.logger.warning(f"Blocked duplicate reply to {msg_id[:30]}... (already replied this session)")
                return {
                    "success": False,
                    "error": "Already replied to this message in this session. If you need to send additional information, use email_send_new instead.",
                    "skipped": True
                }

            # Default to claude signature for automated replies
            signature = params.get('signature', 'claude')
            result = self.email.reply_to_email(msg_id, params['body'], send_immediately=True, signature=signature)

            if not result.get('success'):
                return {"success": False, "error": "Failed to send reply"}

            # Track the sent message ID so we can skip it when processing emails
            sent_msg_id = result.get('message_id')
            if sent_msg_id:
                self._track_sent_message(sent_msg_id)

            # Track that we've replied to this message
            self.replied_message_ids.add(msg_id)

            # If we know which job this email relates to, link the reply to the job index
            # This ensures Claude's replies are tracked immediately, not just via sent mail sync
            job_number = params.get('job_number')
            if not job_number and self.current_email:
                # Try to find job by conversation_id
                conv_id = getattr(self.current_email, 'conversation_id', None)
                if conv_id:
                    for jnum, job_data in self.job_index.items():
                        if not isinstance(job_data, dict) or not jnum.startswith('2'):
                            continue
                        # Check if this conversation is already linked to this job
                        for email_entry in job_data.get('emails', {}).get('linked', []):
                            if isinstance(email_entry, dict) and email_entry.get('conversation_id') == conv_id:
                                job_number = jnum
                                break
                        if job_number:
                            break

            if job_number and job_number in self.job_index:
                self.enrich_job_index(job_number, "emails", {
                    "type": "claude_reply",
                    "in_reply_to": msg_id,
                    "conversation_id": getattr(self.current_email, 'conversation_id', None) if self.current_email else None,
                    "subject": f"RE: {self.current_email.subject}" if self.current_email else None,
                    "sent_date": datetime.now().isoformat(),
                    "preview": params['body'][:200],
                    "source": "claude_automated"
                })

            return {"success": True, "message": "Reply sent", "signature": signature}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _email_create_draft(self, params: dict) -> dict:
        try:
            msg_id = params.get('message_id')
            if not msg_id and self.current_email:
                msg_id = self.current_email.message_id
            
            # Default to nick signature for drafts (human will review and send)
            signature = params.get('signature', 'nick')
            self.email.reply_to_email(msg_id, params['body'], send_immediately=False, signature=signature)
            
            # Also flag it
            self._flag_internal(
                f"Draft created: {params.get('reason', 'Needs review')}",
                {"message_id": msg_id}
            )
            
            return {"success": True, "message": f"Draft created - {params.get('reason')}", "signature": signature}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _email_send_new(self, params: dict) -> dict:
        try:
            subject = params['subject']
            to_email = params['to']
            body = params['body']

            # GUARD: Prevent sending truly duplicate emails (same recipient + subject + similar content)
            if not hasattr(self, '_sent_emails'):
                self._sent_emails = []

            # Normalize body for comparison (first 200 chars, lowercased, stripped)
            body_preview = body[:200].lower().strip() if body else ""

            for sent in self._sent_emails:
                if sent['to'] == to_email and sent['subject'] == subject:
                    # Same recipient and subject - check if content is similar
                    if sent['body_preview'] == body_preview:
                        self.logger.warning(f"Blocked duplicate email to {to_email} with subject '{subject[:40]}...'")
                        return {
                            "success": False,
                            "error": "Already sent an identical email (same recipient, subject, and content) in this session.",
                            "skipped": True
                        }

            result = self.email.send_email(
                to_email=to_email,
                subject=subject,
                body=body,
                importance=params.get('importance'),  # "high", "normal", or "low"
                cc=params.get('cc'),  # List of CC addresses
                signature=params.get('signature'),  # "claude" or "nick"
                in_reply_to=params.get('in_reply_to')  # Thread with existing conversation
            )

            # Track successful sends to prevent duplicates
            if result.get("success"):
                self._sent_emails.append({
                    'to': to_email,
                    'subject': subject,
                    'body_preview': body_preview
                })
                # Track the sent message ID so we can skip it when processing emails
                sent_msg_id = result.get('message_id')
                if sent_msg_id:
                    self._track_sent_message(sent_msg_id)

            # Track [Hive Mind] emails so we can detect Nicholas's replies
            if result.get("success") and "[Hive Mind]" in subject and self._hive_mind_tracker_callback:
                import time
                time.sleep(2)  # Brief delay for email to appear in Sent Items
                conversation_id = self.email.get_recent_sent_email_conversation_id("[Hive Mind]", seconds_ago=120)
                if conversation_id:
                    self._hive_mind_tracker_callback(conversation_id)
                    self.logger.info(f"Tracked [Hive Mind] conversation_id for reply detection")

            # Track ALL outgoing emails to job index (if job_number provided or can be inferred)
            if result.get("success"):
                job_number = params.get('job_number')

                # Try to infer job from current email's conversation
                if not job_number and self.current_email:
                    conv_id = getattr(self.current_email, 'conversation_id', None)
                    if conv_id:
                        for jnum, job_data in self.job_index.items():
                            if not isinstance(job_data, dict) or not jnum.startswith('2'):
                                continue
                            for email_entry in job_data.get('emails', {}).get('linked', []):
                                if isinstance(email_entry, dict) and email_entry.get('conversation_id') == conv_id:
                                    job_number = jnum
                                    break
                            if job_number:
                                break

                if job_number and job_number in self.job_index:
                    email_type = "hive_mind" if "[Hive Mind]" in subject else "claude_outgoing"
                    self.enrich_job_index(job_number, "emails", {
                        "type": email_type,
                        "to": to_email,
                        "subject": subject,
                        "sent_date": datetime.now().isoformat(),
                        "preview": body[:200],
                        "source": "claude_automated",
                        "cc": params.get('cc'),
                        "importance": params.get('importance', 'normal')
                    })

            return {
                "success": result.get("success", False),
                "importance": params.get('importance', 'normal'),
                "cc": params.get('cc'),
                "signature": params.get('signature'),
                "threaded": params.get('in_reply_to') is not None
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _email_move_to_folder(self, params: dict) -> dict:
        try:
            self.email.move_to_folder(params['message_id'], params['folder_name'])
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _email_mark_read(self, params: dict) -> dict:
        try:
            self.email.mark_as_read(params['message_id'])
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _email_mark_unread(self, params: dict) -> dict:
        try:
            self.email.mark_as_unread(params['message_id'])
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _email_flag_important(self, params: dict) -> dict:
        try:
            self.email.flag_email(params['message_id'], 'important')
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _email_create_folder(self, params: dict) -> dict:
        try:
            self.email.create_folder(params['folder_name'])
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _email_get_attachments(self, params: dict) -> dict:
        try:
            attachments = self.email.get_attachments(params['message_id'])
            return {"attachments": attachments}
        except Exception as e:
            return {"error": str(e)}
    
    def _email_save_attachment(self, params: dict) -> dict:
        try:
            self.email.save_attachment_by_id(
                params['message_id'],
                params['attachment_id'],
                params['save_path']
            )
            return {"success": True, "saved_to": params['save_path']}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _email_send_with_attachment(self, params: dict) -> dict:
        """Send email with file attachment. ONLY to internal Pardy Surveys addresses."""
        try:
            result = self.email.send_email_with_attachment(
                to_email=params['to'],
                subject=params['subject'],
                body=params['body'],
                attachment_path=params['attachment_path'],
                signature=params.get('signature')
            )
            # Track the sent message ID so we can skip it when processing emails
            if result.get('success'):
                sent_msg_id = result.get('message_id')
                if sent_msg_id:
                    self._track_sent_message(sent_msg_id)
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _email_sync_sent_to_jobs(self, params: dict) -> dict:
        """
        Scan recent sent mail and link to jobs in the index.

        Matching methods (in priority order):
        1. Attachment filename pattern: "Address (26-009).pdf" - indicates deliverable
        2. Conversation thread: If email's conversation_id matches a job's linked emails

        Args:
            hours: How many hours back to scan (default 24)

        Returns:
            Count of emails linked to jobs
        """
        import re
        hours = params.get('hours', 24)

        # Track which sent emails we've already processed (persisted in job_index metadata)
        processed_sent = set(self.job_index.get('_synced_sent_ids', []))

        # Build a lookup of conversation_id -> job_number from existing job emails
        conversation_to_job = {}
        for job_num, job_data in self.job_index.items():
            if not isinstance(job_data, dict) or not job_num.startswith('2'):  # Skip metadata entries
                continue
            for email_entry in job_data.get('emails', []):
                if isinstance(email_entry, dict):
                    conv_id = email_entry.get('conversation_id')
                    if conv_id:
                        conversation_to_job[conv_id] = job_num

        try:
            sent_emails = self.email.get_recent_sent_mail(hours=hours)
            linked_count = 0
            skipped_count = 0
            results = []

            for email in sent_emails:
                # Skip if we've already processed this sent email
                if email.message_id in processed_sent:
                    skipped_count += 1
                    continue

                # Mark as processed regardless of outcome
                processed_sent.add(email.message_id)

                # Skip emails sent by Claude (already tracked via tool calls)
                if email.subject and "[Hive Mind]" in email.subject:
                    skipped_count += 1
                    continue

                job_number = None
                match_method = None
                matched_filename = None

                # Method 1: Check attachment filenames for job number pattern
                # Our deliverables are named like: "123 Main Street (26-009).pdf"
                if email.attachments:
                    attachment_pattern = r'\((\d{2}-\d{3})(?:-\d+)?\)'  # Matches (26-009) or (26-009-1)
                    for att in email.attachments:
                        filename = att.get('filename', '') or att.get('name', '')
                        att_match = re.search(attachment_pattern, filename)
                        if att_match:
                            job_number = att_match.group(1)
                            matched_filename = filename
                            match_method = "deliverable"
                            break

                # Method 2: Check if this email's conversation_id is already linked to a job
                if not job_number and email.conversation_id:
                    if email.conversation_id in conversation_to_job:
                        job_number = conversation_to_job[email.conversation_id]
                        match_method = "thread"

                if job_number and job_number in self.job_index:
                    # Record in job's history
                    email_record = {
                        "type": "deliverable_sent" if match_method == "deliverable" else "manual_reply",
                        "message_id": email.message_id,
                        "conversation_id": email.conversation_id,
                        "subject": email.subject,
                        "sent_date": str(email.received_date),
                        "source": "nick_manual"
                    }
                    if matched_filename:
                        email_record["filename"] = matched_filename

                    self.enrich_job_index(job_number, "emails", email_record)
                    linked_count += 1
                    results.append({
                        "job": job_number,
                        "match": match_method,
                        "subject": email.subject[:50] if email.subject else "(no subject)"
                    })

                    # Also add this conversation to our lookup for future emails in same thread
                    if email.conversation_id:
                        conversation_to_job[email.conversation_id] = job_number

            # Persist the processed IDs (keep last 500 to prevent unbounded growth)
            self.job_index['_synced_sent_ids'] = list(processed_sent)[-500:]
            self._save_job_index()

            return {
                "success": True,
                "scanned": len(sent_emails),
                "new_checked": len(sent_emails) - skipped_count,
                "linked": linked_count,
                "already_processed": skipped_count,
                "results": results
            }

        except Exception as e:
            self.logger.error(f"Error syncing sent mail: {e}")
            return {"success": False, "error": str(e)}

    # =========================================================================
    # NAS FILE IMPLEMENTATIONS
    # =========================================================================
    
    def _nas_list_directory(self, params: dict) -> dict:
        path = params['path']
        if not os.path.exists(path):
            return {"error": f"Path not found: {path}"}
        if not os.path.isdir(path):
            return {"error": f"Not a directory: {path}"}
        
        try:
            items = []
            for name in os.listdir(path)[:100]:  # Limit
                item_path = os.path.join(path, name)
                items.append({
                    "name": name,
                    "type": "folder" if os.path.isdir(item_path) else "file",
                    "path": item_path
                })
            return {"path": path, "count": len(items), "items": items}
        except Exception as e:
            return {"error": str(e)}
    
    def _nas_read_file(self, params: dict) -> dict:
        path = params['path']
        if not os.path.exists(path):
            return {"error": f"File not found: {path}"}
        if not os.path.isfile(path):
            return {"error": f"Not a file: {path}"}
        
        ext = os.path.splitext(path)[1].lower()
        max_lines = params.get('max_lines', 100)
        
        try:
            # Handle Word documents
            if ext == '.docx':
                try:
                    from docx import Document
                    doc = Document(path)
                    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
                    content = "\n".join(paragraphs[:max_lines])
                    return {"path": path, "type": "docx", "paragraphs": len(paragraphs), "content": content}
                except ImportError:
                    return {"error": "python-docx not installed. Run: pip install python-docx"}
            
            # Handle Excel files
            elif ext in ['.xlsx', '.xls']:
                try:
                    import openpyxl
                    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
                    sheet = wb.active
                    rows = []
                    for i, row in enumerate(sheet.iter_rows(values_only=True)):
                        if i >= max_lines:
                            break
                        rows.append([str(cell) if cell is not None else "" for cell in row])
                    wb.close()
                    return {"path": path, "type": "xlsx", "rows_read": len(rows), "content": rows}
                except ImportError:
                    return {"error": "openpyxl not installed. Run: pip install openpyxl"}
            
            # Handle PDF files
            elif ext == '.pdf':
                try:
                    import pdfplumber
                    with pdfplumber.open(path) as pdf:
                        text = ""
                        for page in pdf.pages[:10]:  # Max 10 pages
                            text += page.extract_text() or ""
                            text += "\n---PAGE BREAK---\n"
                    lines = text.split('\n')[:max_lines]
                    return {"path": path, "type": "pdf", "pages": len(pdf.pages), "content": "\n".join(lines)}
                except ImportError:
                    return {"error": "pdfplumber not installed. Run: pip install pdfplumber"}
            
            # Skip binary files
            elif ext in ['.dwg', '.dxf', '.jpg', '.jpeg', '.png', '.gif', '.zip', '.exe', '.dll', '.jxl', '.job']:
                return {"error": f"Cannot read binary file type: {ext}", "path": path}
            
            # Plain text files
            else:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = []
                    for i, line in enumerate(f):
                        if i >= max_lines:
                            break
                        lines.append(line.rstrip())
                return {"path": path, "type": "text", "lines_read": len(lines), "content": "\n".join(lines)}
                
        except Exception as e:
            return {"error": str(e)}
    
    def _nas_file_exists(self, params: dict) -> dict:
        path = params['path']
        exists = os.path.exists(path)
        return {
            "path": path,
            "exists": exists,
            "is_file": os.path.isfile(path) if exists else False,
            "is_directory": os.path.isdir(path) if exists else False
        }
    
    def _nas_get_file_info(self, params: dict) -> dict:
        path = params['path']
        if not os.path.exists(path):
            return {"error": f"Not found: {path}"}
        
        try:
            stat = os.stat(path)
            file_type = "directory" if os.path.isdir(path) else "file"
            return {
                "path": path,
                "type": file_type,
                "size": stat.st_size,
                "size_bytes": stat.st_size,
                "size_mb": round(stat.st_size / (1024*1024), 2),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                "is_file": os.path.isfile(path),
                "is_directory": os.path.isdir(path)
            }
        except Exception as e:
            return {"error": str(e)}
    
    def _nas_search_files(self, params: dict) -> dict:
        # Accept both 'directory' and 'path' for flexibility
        directory = params.get('directory') or params.get('path')
        pattern = params['pattern']
        max_results = params.get('max_results', 50)
        
        if not directory:
            return {"error": "Must provide 'directory' or 'path' parameter"}
        if not os.path.exists(directory):
            return {"error": f"Directory not found: {directory}"}
        
        matches = []
        try:
            for root, dirs, files in os.walk(directory):
                for name in files + dirs:
                    if fnmatch.fnmatch(name.lower(), pattern.lower()):
                        matches.append(os.path.join(root, name))
                        if len(matches) >= max_results:
                            break
                if len(matches) >= max_results:
                    break
            
            return {"pattern": pattern, "found": len(matches), "files": matches, "matches": matches}
        except Exception as e:
            return {"error": str(e)}
    
    def _nas_create_directory(self, params: dict) -> dict:
        path = params['path']
        try:
            os.makedirs(path, exist_ok=True)
            return {"success": True, "path": path}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _nas_write_file(self, params: dict) -> dict:
        path = params['path']
        content = params['content']
        
        try:
            dir_path = os.path.dirname(path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            return {"success": True, "path": path, "bytes": len(content)}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _nas_copy_file(self, params: dict) -> dict:
        source = params['source']
        dest = params['destination']
        
        if not os.path.exists(source):
            return {"success": False, "error": f"Source not found: {source}"}
        
        try:
            dest_dir = os.path.dirname(dest)
            if dest_dir:
                os.makedirs(dest_dir, exist_ok=True)
            shutil.copy2(source, dest)
            return {"success": True, "source": source, "destination": dest}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _nas_copy_directory(self, params: dict) -> dict:
        source = params['source']
        dest = params['destination']
        
        if not os.path.exists(source):
            return {"success": False, "error": f"Source not found: {source}"}
        if os.path.exists(dest):
            return {"success": False, "error": f"Destination already exists: {dest}"}
        
        try:
            shutil.copytree(source, dest)
            return {"success": True, "source": source, "destination": dest}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _nas_move_file(self, params: dict) -> dict:
        source = params['source']
        dest = params['destination']
        
        if not os.path.exists(source):
            return {"success": False, "error": f"Source not found: {source}"}
        
        try:
            dest_dir = os.path.dirname(dest)
            if dest_dir:
                os.makedirs(dest_dir, exist_ok=True)
            shutil.move(source, dest)
            return {"success": True, "source": source, "destination": dest}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    # =========================================================================
    # JOB IMPLEMENTATIONS
    # =========================================================================
    
    def _is_metro(self, community: str) -> bool:
        if not community:
            return False
        c = community.lower().strip()
        return any(m in c or c in m for m in self.METRO_AREAS)
    
    def _job_create(self, params: dict) -> dict:
        """
        Create complete job: QBO + NAS folder + Job Index entry.
        
        This is the entry point for every new job in the Hive Mind.
        Creates the job with proper structure so all enrichment works from day one.
        """
        now = datetime.now().isoformat()
        
        result = {
            "success": False,
            "job_number": None,
            "folder_path": None,
            "qbo_customer_id": None,
            "qbo_project_id": None,
            "qbo_invoice_id": None,
            "needs_quote": False,
            "errors": []
        }
        
        # Check metro
        is_metro = self._is_metro(params['community'])
        job_type = params.get('job_type', 'survey_and_rpr')
        
        if not is_metro or job_type == 'survey_only':
            result["needs_quote"] = True
        
        # Get job number
        try:
            job_number = self.qbo.get_next_job_number()
        except Exception as e:
            job_number = f"TBD-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            result["errors"].append(f"QBO job number failed: {e}")
        
        result["job_number"] = job_number
        
        # Create folder
        year = datetime.now().year
        client_display = params.get('client_business') or params['client_name']
        folder_name = f"{job_number} - {client_display} - {params['property_address']}, {params['community']}"
        folder_name = "".join(c for c in folder_name if c not in '<>:"/\\|?*\'').strip()
        folder_path = os.path.join(self.JOBS_FOLDER, str(year), folder_name)
        
        try:
            template = os.path.join(self.JOBS_FOLDER, str(year), f"{str(year)[2:]}-000 - Template")
            if os.path.exists(template):
                shutil.copytree(template, folder_path)
            else:
                os.makedirs(folder_path, exist_ok=True)
                result["errors"].append("Template not found, created empty folder")
            result["folder_path"] = folder_path
        except Exception as e:
            result["errors"].append(f"Folder creation failed: {e}")
            return result
        
        # QBO entries
        if not result["needs_quote"] and params.get('due_date') and self.qbo.access_token:
            try:
                # Customer - returns just the ID string
                customer_id = self.qbo.find_or_create_customer(
                    name=params.get('client_business') or params['client_name'],
                    email=params['client_email']
                )
                if customer_id:
                    result["qbo_customer_id"] = customer_id
                    
                    # Project
                    desc = f"{params['property_address']}, {params['community']}, NL"
                    if params.get('due_date'):
                        desc += f"\nClosing: {params['due_date']}"
                    if params.get('purchaser_name'):
                        desc += f"\nPurchaser: {params['purchaser_name']}"
                    
                    project_id = self.qbo.create_project(customer_id, job_number, desc)
                    if project_id:
                        result["qbo_project_id"] = project_id
                        
                        # Invoice
                        item = "Real Property Report - Basic" if job_type == 'rpr_only' else "Boundary Survey & Real Property Report"
                        invoice = self.qbo.create_job_invoice(
                            customer_id=customer_id,
                            job_number=job_number,
                            item_name=item,
                            project_id=project_id,
                            due_date=params.get('due_date'),
                            property_address=f"{params['property_address']}, {params['community']}, NL"
                        )
                        if invoice:
                            result["qbo_invoice_id"] = invoice['Id']
            except Exception as e:
                result["errors"].append(f"QBO error: {e}")
        
        # =================================================================
        # ADD TO JOB INDEX - Keyed by job_number with proper structure
        # =================================================================
        
        # Get conversation_id for email thread linking
        conv_id = params.get('conversation_id')
        if not conv_id and self.current_email:
            conv_id = self.current_email.conversation_id
        
        # Create job entry with proper structure (keyed by job_number)
        job = {
            "job_number": job_number,
            "created": now,
            
            # Core identity
            "job_folder": folder_path,
            "property_address": params['property_address'],
            "client_business": params.get('client_business'),
            "community": params['community'],
            
            # Client info
            "client_name": params['client_name'],
            "client_email": params['client_email'],
            
            # Job details
            "job_type": job_type,
            "due_date": params.get('due_date'),
            "purchaser_name": params.get('purchaser_name'),
            "is_metro": is_metro,
            "needs_quote": result["needs_quote"],
            
            # Email thread reference (for lookup, NOT the key)
            "origin_conversation_id": conv_id,
            "origin_message_id": params.get('message_id'),
            
            # === SECTIONS WITH _updated TIMESTAMPS ===
            
            # QBO section - populated with creation data
            "qbo": {
                "customer_id": result["qbo_customer_id"],
                "project_id": result["qbo_project_id"],
                "invoice_id": result["qbo_invoice_id"],
                "invoice_number": job_number,
                "status": "Created" if result["qbo_invoice_id"] else None,
                "_updated": now if result["qbo_invoice_id"] else None
            },
            
            # Emails section - will link originating email
            "emails": {
                "linked": [],
                "_updated": now
            },
            
            # Data Sync - empty until field work starts
            "data_sync": {
                "jobs": [],
                "field_summary": {},
                "_updated": now
            },
            
            # Reference files - empty until nightly refresh scans
            "reference_files": {
                "_updated": now
            },
            
            # Document control - empty until documents created
            "document_control": {
                "_updated": None
            },
            
            # Status flags
            "has_layout": False,
            "has_field_data": False,
            "has_drawings": False,
            "has_reports": False,
            
            # Timestamps
            "last_updated": now
        }
        
        # Link originating email if we have message info
        if params.get('message_id') or (self.current_email and self.current_email.message_id):
            email_record = {
                "message_id": params.get('message_id') or self.current_email.message_id,
                "conversation_id": conv_id,
                "subject": params.get('email_subject') or (self.current_email.subject if self.current_email else None),
                "from": params.get('client_email'),
                "date": now,
                "direction": "inbound",
                "linked_at": now,
                "note": "Original job request"
            }
            job["emails"]["linked"].append(email_record)
        
        # Store in index keyed by job_number
        self.job_index[job_number] = job
        self._save_job_index()
        
        result["success"] = True
        return result
    
    def _job_search(self, params: dict) -> dict:
        """
        Search for jobs in the index. Returns ALL matches for disambiguation.

        Index is keyed by job_number. Also searches:
        - origin_conversation_id field (for email thread lookup)
        - property_address, client_email fields
        - Falls back to disk folder search

        Returns:
        - found: True if any matches
        - matches: List of all matching jobs with metadata
        - job: First/best match (for backward compatibility)
        """
        matches = []

        # Helper to translate job_folder path for current environment
        def translate_job_info(info):
            if info and info.get('job_folder'):
                info = dict(info)  # Don't modify original
                info['job_folder'] = self._translate_path(info['job_folder'])
            return info

        # Direct lookup by job_number (primary key) - exact match
        if params.get('job_number'):
            job_info = self.job_index.get(params['job_number'])
            if job_info:
                matches.append({
                    "job_number": params['job_number'],
                    "method": "job_number_direct",
                    **translate_job_info(job_info)
                })

        # Search by conversation_id
        if not matches and params.get('conversation_id'):
            conv_id = params['conversation_id']
            if conv_id in self.job_index:
                info = self.job_index[conv_id]
                matches.append({"job_number": conv_id, "method": "conversation_id_legacy_key", **translate_job_info(info)})
            else:
                for job_num, info in self.job_index.items():
                    if not isinstance(info, dict):
                        continue
                    if info.get('origin_conversation_id') == conv_id:
                        matches.append({"job_number": job_num, "method": "conversation_id_field", **translate_job_info(info)})

        # Search by address - returns ALL matches
        if not matches and params.get('address'):
            addr = params['address'].lower()
            for job_num, info in self.job_index.items():
                if not isinstance(info, dict):
                    continue
                if addr in info.get('property_address', '').lower():
                    matches.append({"job_number": job_num, "method": "address_index", **translate_job_info(info)})

        # Search by client email - returns ALL matches
        if not matches and params.get('client_email'):
            email = params['client_email'].lower()
            for job_num, info in self.job_index.items():
                if not isinstance(info, dict):
                    continue
                if info.get('client_email', '').lower() == email:
                    matches.append({"job_number": job_num, "method": "client_email", **translate_job_info(info)})

        # If not found in index, search disk folders
        if not matches and (params.get('job_number') or params.get('address')):
            job_info, method = self._search_job_folders(params)
            if job_info:
                matches.append({"method": method, **job_info})

        # Sort matches: active/recent first
        # Priority: current year > last year > older; in-progress > completed > paid
        def sort_key(m):
            job_num = m.get('job_number', '')
            status = m.get('status', '').lower()
            # Year from job number (26-xxx = 2026)
            year = 0
            if job_num and '-' in job_num:
                try:
                    year = int('20' + job_num.split('-')[0])
                except:
                    pass
            # Status priority (lower = better)
            status_priority = 3  # default
            if 'active' in status or 'progress' in status or 'pending' in status:
                status_priority = 0
            elif 'complete' in status:
                status_priority = 1
            elif 'paid' in status or 'closed' in status:
                status_priority = 2
            return (-year, status_priority, job_num)

        matches.sort(key=sort_key)

        if matches:
            return {
                "found": True,
                "match_count": len(matches),
                "matches": matches,
                "job": matches[0]  # Best match for backward compat
            }
        return {"found": False, "match_count": 0, "matches": []}
    
    def _search_job_folders(self, params: dict):
        """Search job folders on disk when not in index."""
        job_number = params.get('job_number')
        address = params.get('address', '').lower()
        
        # Determine year(s) to search
        years_to_search = []
        if job_number:
            try:
                year_prefix = job_number.split('-')[0]
                years_to_search = [f"20{year_prefix}"]
            except:
                pass
        
        if not years_to_search:
            # Search current and previous year
            current_year = datetime.now().year
            years_to_search = [str(current_year), str(current_year - 1)]
        
        for year in years_to_search:
            year_path = os.path.join(self.JOBS_FOLDER, year)
            if not os.path.exists(year_path):
                continue
            
            for folder_name in os.listdir(year_path):
                if folder_name.endswith(" - Template"):
                    continue
                
                # Parse folder name: "26-001 - Client - Address, Community"
                parts = folder_name.split(' - ', 2)
                if len(parts) < 2:
                    continue
                
                folder_job_num = parts[0]
                folder_client = parts[1] if len(parts) > 1 else ""
                folder_address = parts[2] if len(parts) > 2 else ""
                
                # Match by job number
                if job_number and folder_job_num == job_number:
                    return {
                        "job_number": folder_job_num,
                        "job_folder": os.path.join(year_path, folder_name),
                        "client_business": folder_client,
                        "property_address": folder_address
                    }, "job_number_disk"
                
                # Match by address
                if address and address in folder_address.lower():
                    return {
                        "job_number": folder_job_num,
                        "job_folder": os.path.join(year_path, folder_name),
                        "client_business": folder_client,
                        "property_address": folder_address
                    }, "address_disk"
        
        return None, None
    
    def _calculate_layout_summary(self, data_sync_entry: dict) -> dict:
        """
        Calculate summary for a single layout folder (controller job).
        Only "complete" uploads count toward totals.
        """
        summary = {
            "visits": 0,
            "total_field_hours": 0,
            "total_travel_hours": 0,
            "operators": set(),
            "field_work_done": None,
            "needs_return_visit": False,
            "estimated_time_remaining": None,
            "last_visit": None,
            "intermediate_count": 0,
            "reupload_count": 0,
            "unsynced_time_entries": 0,
            "pins_status": {"found": None, "placed": None},
            "construction_tasks": {},
            "custom_tasks": [],
            "latest_notes": None,
            "csv_extracted": {
                "total_points": 0,
                "evidence_found": [],
                "pins_placed": [],
                "evidence_not_found": 0,
                "evidence_to_find": 0,
                "monument_checks": []
            }
        }
        
        for upload in data_sync_entry.get("field_uploads", []):
            fs = upload.get("field_status", {})
            if not fs:
                continue
            
            upload_type = fs.get("upload_type", "complete")
            
            # Count by type
            if upload_type == "intermediate":
                summary["intermediate_count"] += 1
                # Still extract CSV data from intermediate uploads
                self._merge_csv_extracted(summary, fs.get("csv_extracted"))
                continue
            elif upload_type == "reupload":
                summary["reupload_count"] += 1
                # Still extract CSV data from reuploads (updated files)
                self._merge_csv_extracted(summary, fs.get("csv_extracted"))
                continue
            
            # Only "complete" uploads count for visits/time
            summary["visits"] += 1
            
            # Time tracking
            time_spent = fs.get("time_spent") or {}
            if time_spent.get("field_hours"):
                summary["total_field_hours"] += time_spent["field_hours"]
            if time_spent.get("travel_hours_oneway"):
                summary["total_travel_hours"] += time_spent["travel_hours_oneway"]
            
            # Operators
            if fs.get("operator"):
                summary["operators"].add(fs["operator"])
            
            # Construction tasks (count occurrences)
            for task in fs.get("construction_tasks_completed", []):
                summary["construction_tasks"][task] = summary["construction_tasks"].get(task, 0) + 1
            
            # Custom tasks (collect unique)
            for task in fs.get("custom_tasks_completed", []):
                if task not in summary["custom_tasks"]:
                    summary["custom_tasks"].append(task)
            
            # QBO sync tracking
            qbo_sync = fs.get("qbo_sync") or {}
            if not qbo_sync.get("time_synced"):
                summary["unsynced_time_entries"] += 1
            
            # Latest values (from most recent complete upload)
            if fs.get("notes"):
                summary["latest_notes"] = fs["notes"]
            if fs.get("pins_found"):
                summary["pins_status"]["found"] = fs["pins_found"]
            if fs.get("pins_placed"):
                summary["pins_status"]["placed"] = fs["pins_placed"]
            if fs.get("field_work_done") is not None:
                summary["field_work_done"] = fs["field_work_done"]
            if fs.get("estimated_time_remaining"):
                summary["estimated_time_remaining"] = fs["estimated_time_remaining"]
            
            # Last visit date (extract from upload_timestamp)
            timestamp = fs.get("upload_timestamp")
            if timestamp:
                try:
                    # "2026-01-21T16:50:34.146Z" -> "2026-01-21"
                    visit_date = timestamp.split("T")[0]
                    if not summary["last_visit"] or visit_date > summary["last_visit"]:
                        summary["last_visit"] = visit_date
                except:
                    pass
            
            # CSV extracted data
            self._merge_csv_extracted(summary, fs.get("csv_extracted"))
        
        # Determine needs_return_visit
        if summary["field_work_done"] == False:
            summary["needs_return_visit"] = True
        if summary["pins_status"]["placed"] in ["no", "partial"]:
            summary["needs_return_visit"] = True
        
        # Convert set to list for JSON
        summary["operators"] = list(summary["operators"])
        
        # Clean up csv_extracted if empty
        csv = summary["csv_extracted"]
        has_csv = (csv["total_points"] > 0 or csv["evidence_found"] or 
                   csv["pins_placed"] or csv["evidence_not_found"] > 0 or
                   csv["evidence_to_find"] > 0 or csv["monument_checks"])
        if not has_csv:
            del summary["csv_extracted"]
        
        # Always return summary - even if no uploads yet (layout awaiting field data)
        if summary["visits"] == 0 and summary["intermediate_count"] == 0 and summary["reupload_count"] == 0:
            summary["awaiting_field_data"] = True
        
        return summary
    
    def _merge_csv_extracted(self, summary: dict, csv_data: dict):
        """Merge csv_extracted data into summary."""
        if not csv_data:
            return
        
        csv = summary["csv_extracted"]
        
        # Total points (take max - represents latest state)
        if csv_data.get("total_points"):
            csv["total_points"] = max(csv["total_points"], csv_data["total_points"])
        
        # Evidence found (merge unique)
        for item in csv_data.get("evidence_found", []):
            if item not in csv["evidence_found"]:
                csv["evidence_found"].append(item)
        
        # Pins placed (merge unique)
        for item in csv_data.get("pins_placed", []):
            if item not in csv["pins_placed"]:
                csv["pins_placed"].append(item)
        
        # Evidence not found (sum)
        csv["evidence_not_found"] += csv_data.get("evidence_not_found", 0)
        
        # Evidence to find (sum)
        csv["evidence_to_find"] += csv_data.get("evidence_to_find", 0)
        
        # Monument checks (merge unique)
        for item in csv_data.get("monument_checks", []):
            if item not in csv["monument_checks"]:
                csv["monument_checks"].append(item)
    
    def _rollup_field_summary(self, layout_summaries: list) -> dict:
        """
        Roll up multiple layout_summaries into a job-wide field_summary.
        """
        rollup = {
            "total_visits": 0,
            "total_field_hours": 0,
            "total_travel_hours": 0,
            "operators": set(),
            "field_complete": None,  # True if ALL layouts have field_work_done=True
            "needs_return_visit": False,
            "estimated_time_remaining": None,
            "last_visit": None,
            "intermediate_count": 0,
            "reupload_count": 0,
            "layouts_awaiting_field_data": 0,
            "layouts_with_field_data": 0,
            "layouts_field_complete": 0,
            "unsynced_time_entries": 0,
            "pins_status": {"found": None, "placed": None},
            "construction_tasks": {},
            "custom_tasks": [],
            "latest_notes": None,
            "csv_extracted": {
                "total_points": 0,
                "evidence_found": [],
                "pins_placed": [],
                "evidence_not_found": 0,
                "evidence_to_find": 0,
                "monument_checks": []
            }
        }
        
        estimated_times = []  # Collect for summing/listing
        
        for ls in layout_summaries:
            # Count layouts awaiting field data
            if ls.get("awaiting_field_data"):
                rollup["layouts_awaiting_field_data"] += 1
            else:
                # Has field data - track completion status
                rollup["layouts_with_field_data"] += 1
                if ls.get("field_work_done") == True:
                    rollup["layouts_field_complete"] += 1

            # Sum counts
            rollup["total_visits"] += ls.get("visits", 0)
            rollup["total_field_hours"] += ls.get("total_field_hours", 0)
            rollup["total_travel_hours"] += ls.get("total_travel_hours", 0)
            rollup["intermediate_count"] += ls.get("intermediate_count", 0)
            rollup["reupload_count"] += ls.get("reupload_count", 0)
            rollup["unsynced_time_entries"] += ls.get("unsynced_time_entries", 0)
            
            # Merge operators
            for op in ls.get("operators", []):
                rollup["operators"].add(op)
            
            # needs_return_visit - true if ANY layout needs it
            if ls.get("needs_return_visit"):
                rollup["needs_return_visit"] = True
            
            # Collect estimated times
            if ls.get("estimated_time_remaining"):
                estimated_times.append(ls["estimated_time_remaining"])
            
            # Last visit - take latest across all layouts
            if ls.get("last_visit"):
                if not rollup["last_visit"] or ls["last_visit"] > rollup["last_visit"]:
                    rollup["last_visit"] = ls["last_visit"]
            
            # Pins status - take latest non-None values
            if ls.get("pins_status", {}).get("found"):
                rollup["pins_status"]["found"] = ls["pins_status"]["found"]
            if ls.get("pins_status", {}).get("placed"):
                rollup["pins_status"]["placed"] = ls["pins_status"]["placed"]
            
            # Construction tasks (sum counts)
            for task, count in ls.get("construction_tasks", {}).items():
                rollup["construction_tasks"][task] = rollup["construction_tasks"].get(task, 0) + count
            
            # Custom tasks (collect unique)
            for task in ls.get("custom_tasks", []):
                if task not in rollup["custom_tasks"]:
                    rollup["custom_tasks"].append(task)
            
            # Latest notes (from most recent layout)
            if ls.get("latest_notes"):
                rollup["latest_notes"] = ls["latest_notes"]
            
            # Merge csv_extracted
            if ls.get("csv_extracted"):
                self._merge_csv_extracted(rollup, ls["csv_extracted"])
        
        # Handle estimated_time_remaining
        if estimated_times:
            if len(estimated_times) == 1:
                rollup["estimated_time_remaining"] = estimated_times[0]
            else:
                # Multiple layouts need time - list them or sum
                rollup["estimated_time_remaining"] = estimated_times
        
        # Convert set to list
        rollup["operators"] = list(rollup["operators"])
        
        # Clean up csv_extracted if empty
        csv = rollup["csv_extracted"]
        has_csv = (csv["total_points"] > 0 or csv["evidence_found"] or
                   csv["pins_placed"] or csv["evidence_not_found"] > 0 or
                   csv["evidence_to_find"] > 0 or csv["monument_checks"])
        if not has_csv:
            del rollup["csv_extracted"]

        # Calculate overall field_complete status
        # Field is complete if:
        # - ALL layouts have field data (none awaiting)
        # - ALL layouts with field data have field_work_done=True
        total_layouts = len(layout_summaries)
        if total_layouts > 0:
            if rollup["layouts_awaiting_field_data"] > 0:
                # Some layouts haven't had field work done yet
                rollup["field_complete"] = False
            elif rollup["layouts_with_field_data"] > 0:
                # All layouts have field data - check if all are complete
                rollup["field_complete"] = (
                    rollup["layouts_field_complete"] == rollup["layouts_with_field_data"]
                )
            # else: no field data yet, field_complete stays None

        return rollup

    def _job_get_status(self, params: dict) -> dict:
        """
        Get comprehensive job status - THE CENTRAL HUB for all job information.
        
        =======================================================================
        ARCHITECTURE: This function connects all job-related data sources
        =======================================================================
        
        DATA SYNC (Z:\\Data Sync\\office-jobs\\{range}\\{job})
        └── {job}-{date}\\           ← "Job File" - one per layout sent to controller
            ├── job_info.json        ← What this job file is for (address, description)
            ├── *.job, *.jxl, *.csv  ← Layout files synced TO controller
            └── Field_Data\\         ← EXISTS if field work uploaded BACK
                └── {YYMMDD-time}\\   ← Each upload session (can be multiple!)
                    ├── field_status.json  ← Rich status from operator form
                    └── *.csv, *.dxf, *.jpg  ← Field collected data
        
        field_status.json contains:
        - operator: "Joe", "Allan", "Nick"
        - field_work_done: true/false (actual completion, not just uploaded)
        - estimated_time_remaining: "2 hours" if incomplete
        - time_spent: {field_hours, travel_hours_oneway}
        - pins_found/pins_placed: "yes", "no", "partial", "n/a"
        - notes: Free text from operator
        - is_reupload: true if replacing a previous upload
        - construction_tasks_completed: for building construction jobs
        
        NAS JOB FOLDER (Z:\\Jobs\\{year}\\{job_folder})
        └── Data\\
            ├── Layout\\             ← Should mirror what was synced to controller
            └── Field Data\\         ← Should contain imported field data
        └── Reference & Research\\   ← Research documents
            ├── CADO & Registry of Deeds\\  ← Deed documents
            ├── Plans\\              ← Survey plans
            ├── Crown Lands\\        ← Crown grant docs
            ├── Emails\\             ← Saved correspondence
            └── *.xlsx, *.docx       ← Research spreadsheets/checklists
        └── Drawings\\               ← CAD drawings (.dwg, .dxf, .pdf)
        └── Description & Reports\\  ← Reports and descriptions
        
        =======================================================================
        FUTURE PLANNING/SCHEDULING INTEGRATION:
        =======================================================================
        This function is the PRIMARY integration point for scheduling.
        The job_index will expand to include: scheduled dates, assigned
        surveyor, priority, estimated completion. The data_sync array 
        maps directly to scheduled vs completed work.
        
        Search "FUTURE PLANNING" to find all integration points.
        =======================================================================
        """
        job_number = params['job_number']
        
        status = {
            "job_number": job_number,
            "found": False,
            
            # === JOB IDENTITY (from index or disk) ===
            "job_folder": None,
            "property_address": None,
            "client_business": None,
            "due_date": None,
            "community": None,
            
            # === CONTROLLER/FIELD WORK (from Data Sync) ===
            "data_sync": {
                "sync_folder": None,
                "data_sync": [],  # Each job file sent to controller
                "total_data_sync": 0,
                "jobs_with_field_data": 0,
                "total_field_uploads": 0,
                "all_complete": False  # True if every controller job has field data
            },
            
            # === NAS JOB FOLDER STATUS ===
            "nas_folder": {
                "layout_files": [],      # Files in Data\Layout
                "field_data_files": [],  # Files in Data\Field Data
                "drawing_files": [],     # Files in Drawings
                "report_files": [],      # Files in Description & Reports
                "reference_files": {},   # Files in Reference & Research (by subfolder)
                "saved_emails": []       # Files in Reference & Research\Emails
            },
            
            # === DOCUMENT CONTROL (stamped/sent deliverables) ===
            # Parsed from naming conventions:
            # - 25-180-1.pdf = description, 25-180--1.pdf = drawing (double hyphen)
            # - Something (25-180-1).pdf = sent to client
            # - 25-180-1-R1.pdf = revision
            "document_control": None,  # Populated if folder exists
            
            # === QUICK STATUS FLAGS ===
            "has_layout": False,
            "has_field_data": False,
            "has_drawings": False,
            "has_reports": False
        }
        
        # =====================================================================
        # 1. Find job identity from index or disk
        # =====================================================================
        # Direct lookup - jobs are keyed by job_number
        info = self.job_index.get(job_number)
        if info:
            status["found"] = True
            # Translate path for current environment (Windows vs NAS)
            status["job_folder"] = self._translate_path(info.get("job_folder"))
            status["due_date"] = info.get("due_date")
            status["property_address"] = info.get("property_address")
            status["community"] = info.get("community")
            status["client_business"] = info.get("client_business")
        
        # Fallback to disk search if not in index
        if not status["job_folder"]:
            disk_result, method = self._search_job_folders({"job_number": job_number})
            if disk_result:
                status["found"] = True
                status["job_folder"] = disk_result.get("job_folder")
                status["property_address"] = disk_result.get("property_address")
                status["client_business"] = disk_result.get("client_business")
        
        # =====================================================================
        # 2. Check Data Sync folder - Controller jobs and field data
        # =====================================================================
        if job_number and not job_number.startswith("TBD"):
            try:
                parts = job_number.split('-')
                if len(parts) >= 2:
                    year_prefix = parts[0]
                    job_num = int(parts[1])
                    range_start = (job_num // 50) * 50
                    range_end = range_start + 50
                    range_folder = f"{year_prefix}-{range_start:03d}-{range_end:03d}"
                    
                    sync_job_path = os.path.join(self.DATA_SYNC_FOLDER, range_folder, job_number)
                    
                    if os.path.exists(sync_job_path):
                        status["found"] = True
                        status["data_sync"]["sync_folder"] = sync_job_path
                        
                        # Each subfolder is a "controller job" (job file sent to controller)
                        for item in sorted(os.listdir(sync_job_path)):
                            item_path = os.path.join(sync_job_path, item)
                            
                            if os.path.isdir(item_path) and item.startswith(job_number):
                                data_sync_entry = {
                                    "folder_name": item,
                                    "date_code": item.replace(f"{job_number}-", ""),
                                    "job_info": None,
                                    "field_data_complete": False,
                                    "field_uploads": []
                                }
                                
                                # Read job_info.json - tells us what this controller job is for
                                job_info_path = os.path.join(item_path, "job_info.json")
                                if os.path.exists(job_info_path):
                                    try:
                                        with open(job_info_path, 'r') as f:
                                            ji = json.load(f)
                                            data_sync_entry["job_info"] = {
                                                "address": ji.get("address"),
                                                "description": ji.get("description"),
                                                "operator": ji.get("operator"),
                                                "created": ji.get("created"),
                                                "reference_number": ji.get("referenceNumber")
                                            }
                                            # Get field uploads from JSON if available
                                            if ji.get("fieldDataUploads"):
                                                for upload in ji["fieldDataUploads"]:
                                                    data_sync_entry["field_uploads"].append({
                                                        "timestamp": upload.get("timestamp"),
                                                        "folder": upload.get("folder"),
                                                        "file_count": upload.get("fileCount")
                                                    })
                                    except:
                                        pass
                                
                                # Check Field_Data folder and read field_status.json for rich data
                                fd_path = os.path.join(item_path, "Field_Data")
                                if os.path.exists(fd_path):
                                    status["has_field_data"] = True
                                    
                                    # Scan each upload folder for field_status.json
                                    for fd_item in sorted(os.listdir(fd_path)):
                                        fd_item_path = os.path.join(fd_path, fd_item)
                                        if not os.path.isdir(fd_item_path):
                                            continue
                                        
                                        upload_entry = {
                                            "folder": fd_item,
                                            "file_count": len([f for f in os.listdir(fd_item_path) 
                                                              if os.path.isfile(os.path.join(fd_item_path, f))])
                                        }
                                        
                                        # Read field_status.json if present
                                        status_file = os.path.join(fd_item_path, "field_status.json")
                                        if os.path.exists(status_file):
                                            try:
                                                with open(status_file, 'r') as f:
                                                    fs = json.load(f)
                                                    
                                                    # Handle upload_type with backward compatibility for is_reupload
                                                    # New schema: upload_type = "complete", "intermediate", "reupload"
                                                    # Old schema: is_reupload = true/false
                                                    upload_type = fs.get("upload_type")
                                                    if upload_type is None:
                                                        # Backward compatibility: derive from is_reupload
                                                        upload_type = "reupload" if fs.get("is_reupload", False) else "complete"
                                                    
                                                    upload_entry["field_status"] = {
                                                        "operator": fs.get("operator"),
                                                        "job_type": fs.get("job_type"),
                                                        "field_work_done": fs.get("field_work_done"),
                                                        "estimated_time_remaining": fs.get("estimated_time_remaining"),
                                                        "time_spent": fs.get("time_spent"),
                                                        "pins_found": fs.get("pins_found"),
                                                        "pins_placed": fs.get("pins_placed"),
                                                        "notes": fs.get("notes"),
                                                        "upload_type": upload_type,  # New field with backward compat
                                                        "is_reupload": upload_type == "reupload",  # Keep for backward compat
                                                        "replaces_folder": fs.get("replaces_folder"),
                                                        "construction_tasks_completed": fs.get("construction_tasks_completed", []),
                                                        "custom_tasks_completed": fs.get("custom_tasks_completed", []),
                                                        "upload_timestamp": fs.get("upload_timestamp"),
                                                        "qbo_sync": fs.get("qbo_sync"),
                                                        "csv_extracted": fs.get("csv_extracted")  # New field
                                                    }
                                            except:
                                                pass
                                        
                                        data_sync_entry["field_uploads"].append(upload_entry)
                                    
                                    # Determine completion based on field_status.json data
                                    # - Only "complete" uploads count toward job completion
                                    # - "intermediate" uploads are mid-day data reviews (skip)
                                    # - "reupload" replaces a previous upload (skip)
                                    complete_uploads = [
                                        u for u in data_sync_entry["field_uploads"]
                                        if u.get("field_status", {}).get("upload_type", "complete") == "complete"
                                    ]
                                    
                                    if complete_uploads:
                                        # Get the latest complete upload to determine job completion
                                        latest = complete_uploads[-1]  # Sorted, so last is latest
                                        fs = latest.get("field_status", {})
                                        
                                        # Use field_work_done from JSON if available, else assume complete
                                        if fs.get("field_work_done") is not None:
                                            data_sync_entry["field_data_complete"] = fs["field_work_done"]
                                        else:
                                            # Fallback: if no JSON, assume complete if folder exists
                                            data_sync_entry["field_data_complete"] = True
                                        
                                        # Bubble up key status info for quick access
                                        data_sync_entry["latest_field_status"] = {
                                            "operator": fs.get("operator"),
                                            "field_work_done": fs.get("field_work_done"),
                                            "estimated_time_remaining": fs.get("estimated_time_remaining"),
                                            "notes": fs.get("notes"),
                                            "pins_found": fs.get("pins_found"),
                                            "pins_placed": fs.get("pins_placed")
                                        }
                                    else:
                                        data_sync_entry["field_data_complete"] = True
                                    
                                    if data_sync_entry["field_data_complete"]:
                                        status["data_sync"]["jobs_with_field_data"] += 1
                                    
                                    status["data_sync"]["total_field_uploads"] += len(data_sync_entry["field_uploads"])
                                
                                # =============================================================
                                # Calculate layout_summary for THIS controller job folder
                                # =============================================================
                                layout_summary = self._calculate_layout_summary(data_sync_entry)
                                if layout_summary:
                                    data_sync_entry["layout_summary"] = layout_summary
                                
                                status["data_sync"]["data_sync"].append(data_sync_entry)
                        
                        # Summary stats
                        total = len(status["data_sync"]["data_sync"])
                        status["data_sync"]["total_data_sync"] = total
                        status["has_layout"] = total > 0
                        
                        if total > 0:
                            status["data_sync"]["all_complete"] = all(
                                cj["field_data_complete"] for cj in status["data_sync"]["data_sync"]
                            )
                        
                        # =============================================================
                        # Roll up layout_summaries into job-wide field_summary
                        # =============================================================
                        layout_summaries = [
                            cj["layout_summary"] for cj in status["data_sync"]["data_sync"]
                            if cj.get("layout_summary")
                        ]
                        
                        if layout_summaries:
                            status["data_sync"]["field_summary"] = self._rollup_field_summary(layout_summaries)
            except Exception as e:
                status["data_sync"]["error"] = str(e)
        
        # =====================================================================
        # 3. Check NAS job folder structure
        # =====================================================================
        if status.get("job_folder") and os.path.exists(status["job_folder"]):
            jf = status["job_folder"]
            
            # Data\Layout - should correspond to what was synced to controller
            layout_path = os.path.join(jf, "Data", "Layout")
            if os.path.exists(layout_path):
                status["nas_folder"]["layout_files"] = [
                    f for f in os.listdir(layout_path) 
                    if os.path.isfile(os.path.join(layout_path, f))
                ]
            
            # Data\Field Data - should correspond to imported field data
            field_data_path = os.path.join(jf, "Data", "Field Data")
            if os.path.exists(field_data_path):
                # List files and folders
                for item in os.listdir(field_data_path):
                    item_path = os.path.join(field_data_path, item)
                    if os.path.isfile(item_path):
                        status["nas_folder"]["field_data_files"].append(item)
                    elif os.path.isdir(item_path):
                        status["nas_folder"]["field_data_files"].append(f"{item}/ (folder)")
            
            # Drawings - recurse into subfolders
            drawings_path = os.path.join(jf, "Drawings")
            if os.path.exists(drawings_path):
                drawing_files = []
                for root, dirs, files in os.walk(drawings_path):
                    for f in files:
                        if f.endswith(('.dwg', '.pdf', '.dxf')):
                            # Include relative path if in subfolder
                            rel_path = os.path.relpath(os.path.join(root, f), drawings_path)
                            drawing_files.append(rel_path)
                status["nas_folder"]["drawing_files"] = drawing_files
                status["has_drawings"] = len(drawing_files) > 0

            # Description & Reports - recurse into subfolders
            reports_path = os.path.join(jf, "Description & Reports")
            if os.path.exists(reports_path):
                report_files = []
                for root, dirs, files in os.walk(reports_path):
                    for f in files:
                        if f.endswith(('.pdf', '.docx')):
                            rel_path = os.path.relpath(os.path.join(root, f), reports_path)
                            report_files.append(rel_path)
                status["nas_folder"]["report_files"] = report_files
                status["has_reports"] = len(report_files) > 0
            
            # Reference & Research - ALL research materials
            ref_path = os.path.join(jf, "Reference & Research")
            if os.path.exists(ref_path):
                status["nas_folder"]["reference_files"] = {}
                
                for item in os.listdir(ref_path):
                    item_path = os.path.join(ref_path, item)
                    
                    if os.path.isfile(item_path):
                        # Root level files (spreadsheets, checklists)
                        if "root" not in status["nas_folder"]["reference_files"]:
                            status["nas_folder"]["reference_files"]["root"] = []
                        status["nas_folder"]["reference_files"]["root"].append(item)
                    
                    elif os.path.isdir(item_path):
                        # Subfolders (CADO, Plans, Crown Lands, etc.)
                        folder_files = [
                            f for f in os.listdir(item_path) 
                            if os.path.isfile(os.path.join(item_path, f)) and not f.startswith('.')
                            and f.lower() != 'thumbs.db'
                        ]
                        if folder_files:
                            status["nas_folder"]["reference_files"][item] = folder_files
                
                # Special case: Emails subfolder (also track separately for easy access)
                emails_path = os.path.join(ref_path, "Emails")
                if os.path.exists(emails_path):
                    status["nas_folder"]["saved_emails"] = [
                        f for f in os.listdir(emails_path) 
                        if os.path.isfile(os.path.join(emails_path, f))
                    ]
            
            # Document Control - stamped/sent documents
            doc_control_path = os.path.join(jf, "Document Control")
            if os.path.exists(doc_control_path):
                status["document_control"] = self._parse_document_control(
                    doc_control_path, job_number
                )
        
        # =====================================================================
        # 4. ENRICH THE HIVE - Save discoveries back to job index
        # =====================================================================
        # This builds accumulated knowledge over time. Every call enriches the index.
        self._enrich_job_in_index(job_number, status)
        
        # =====================================================================
        # 5. Add linked emails and QBO data from index (hive knowledge)
        # =====================================================================
        if job_number in self.job_index:
            idx_job = self.job_index[job_number]
            status["emails"] = idx_job.get("emails", [])
            status["qbo"] = idx_job.get("qbo", {})

            # Include refresh timestamps so Claude can decide if data is stale
            status["_timestamps"] = {
                "last_status_check": datetime.now().isoformat(),
                "job_last_updated": idx_job.get("last_updated"),
                "qbo_last_updated": idx_job.get("qbo", {}).get("_updated"),
                "emails_last_updated": idx_job.get("emails", {}).get("_updated") if isinstance(idx_job.get("emails"), dict) else None,
                "data_sync_live": True  # This data was just read from disk
            }
        else:
            status["emails"] = []
            status["qbo"] = {}
            status["_timestamps"] = {
                "last_status_check": datetime.now().isoformat(),
                "data_sync_live": True
            }

        return status
    
    def _job_save_email(self, params: dict) -> dict:
        job_number = params['job_number']
        message_id = params.get('message_id')
        
        # Find job folder - direct lookup by job_number
        info = self.job_index.get(job_number)
        job_folder = self._translate_path(info.get('job_folder')) if info else None

        if not job_folder:
            return {"success": False, "error": f"Job {job_number} not found"}
        
        emails_folder = os.path.join(job_folder, "Reference & Research", "Emails")
        os.makedirs(emails_folder, exist_ok=True)
        
        # Get email
        email_data = None
        if message_id:
            email_data = self.email.get_email_by_id(message_id)
        elif self.current_email:
            email_data = self.current_email
        
        if not email_data:
            return {"success": False, "error": "No email to save"}
        
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        safe_subj = "".join(c for c in email_data.subject[:50] if c.isalnum() or c in ' -_')
        file_path = os.path.join(emails_folder, f"{timestamp}_{safe_subj}.txt")
        
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(f"From: {email_data.sender} <{email_data.sender_email}>\n")
                f.write(f"Subject: {email_data.subject}\n")
                f.write(f"Date: {email_data.received_date}\n\n")
                f.write(email_data.body)
            
            # Also link email to job in the hive index
            self._link_email_to_job(
                job_number=job_number,
                email_info={
                    "message_id": email_data.message_id if hasattr(email_data, 'message_id') else None,
                    "conversation_id": email_data.conversation_id if hasattr(email_data, 'conversation_id') else None,
                    "subject": email_data.subject,
                    "from": email_data.sender_email,
                    "to": None,  # Inbound email
                    "date": str(email_data.received_date),
                    "direction": "inbound"
                }
            )
            
            return {"success": True, "saved_to": file_path, "linked_to_index": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _job_link_email(self, params: dict) -> dict:
        """
        Link an email to a job in the hive index WITHOUT saving the file to disk.
        Use this when you've identified which job an email relates to.

        STORAGE STRATEGY (for token efficiency):
        =========================================
        We store BOTH summary AND full body in the job index:

        1. summary (required) - Claude's 1-2 sentence summary of the email content.
           Used for quick context when processing follow-up emails in the same thread.
           If Claude has good summaries, we can skip re-reading the full quoted chain.

        2. body_stored - FULL email body (NO truncation).
           Stored so Claude can retrieve exact wording later without hitting Graph API.
           Useful for drafting replies that need to reference specific content.
           NOTE: Truncation only happens when PRESENTING to Claude, not when STORING.

        3. key_info (optional) - Structured data extracted from email:
           - closing_date, purchaser_name, mortgage_amount (real estate)
           - revision_requested, deadline (survey revisions)
           - quote_amount, scope (proposals)

        WHY THIS MATTERS:
        - Email chains can be 50K+ chars with all the quoted replies
        - We don't want to send all that to Claude every time (token cost)
        - But Claude needs full content when drafting replies or checking details
        - Solution: Store full body, present summaries, let Claude dig deeper if needed

        RELATED CODE:
        - claude_agent.py:_get_thread_context() - retrieves summaries for incoming emails
        - claude_agent.py:process_email() - decides whether to strip quoted chains
        - tools_definition.py:job_link_email - tool definition Claude sees

        If link_conversation=True, also links all other emails in the same thread.
        """
        job_number = params['job_number']
        summary = params.get('summary')
        key_info = params.get('key_info', {})
        link_conversation = params.get('link_conversation', False)

        # Get email data
        message_id = params.get('message_id')
        email_data = None

        if message_id:
            email_data = self.email.get_email_by_id(message_id)
        elif self.current_email:
            email_data = self.current_email

        if not email_data:
            return {"success": False, "error": "No email to link"}

        # Determine direction
        our_email = "info@pardysurveys.ca"  # Could make configurable
        direction = "outbound" if email_data.sender_email == our_email else "inbound"

        # Store FULL body - no truncation
        # The whole point is Claude can retrieve this later if it needs exact wording
        # Truncation only happens when PRESENTING to Claude, not when STORING
        body_for_storage = ""
        if hasattr(email_data, 'body') and email_data.body:
            body_for_storage = email_data.body  # Full body, no truncation

        # Link to index
        self._link_email_to_job(
            job_number=job_number,
            email_info={
                "message_id": email_data.message_id if hasattr(email_data, 'message_id') else message_id,
                "conversation_id": email_data.conversation_id if hasattr(email_data, 'conversation_id') else None,
                "subject": email_data.subject,
                "from": email_data.sender_email,
                "to": getattr(email_data, 'to_email', None),
                "date": str(email_data.received_date),
                "direction": direction,
                "body_stored": body_for_storage  # Full body for later retrieval
            },
            summary=summary,
            key_info=key_info
        )
        
        result = {
            "success": True, 
            "job_number": job_number,
            "linked": True,
            "summary_saved": summary is not None
        }
        
        # Auto-link whole conversation if requested
        if link_conversation and hasattr(email_data, 'conversation_id') and email_data.conversation_id:
            conv_result = self._link_conversation_to_job(job_number, email_data.conversation_id)
            result["conversation_linked"] = conv_result.get("linked", 0)
        
        return result
    
    def _job_list_recent(self, params: dict) -> dict:
        count = params.get('count', 10)

        # Jobs are keyed by job_number - skip metadata keys like _synced_sent_ids
        jobs = [v for v in self.job_index.values() if isinstance(v, dict) and 'job_number' in v]

        jobs.sort(key=lambda x: x.get('created', ''), reverse=True)
        return {"count": len(jobs[:count]), "jobs": jobs[:count]}

    # =========================================================================
    # THREAD EMAIL TOOLS - Efficient access to stored email content
    # =========================================================================
    # These tools let Claude access emails stored in the job index without
    # hitting the Graph API. Show summaries first, expand to full body on demand.
    #
    # FLOW:
    # 1. job_get_thread_emails(job_number) -> list with summaries
    # 2. job_get_email_body(job_number, index) -> full body of specific email
    #
    # WHY THIS EXISTS:
    # - Emails are stored in job index when linked (see _job_link_email)
    # - Summaries provide quick context without token cost
    # - Full body available instantly without Graph API call
    # - Claude can "drill down" into specific emails as needed
    # =========================================================================

    def _job_get_thread_emails(self, params: dict) -> dict:
        """
        Get all emails linked to a job, formatted for easy scanning.

        Returns summaries by default. Use job_get_email_body to expand
        specific emails when you need the full content.
        """
        job_number = params.get('job_number')
        conversation_id = params.get('conversation_id')  # Optional filter

        if not job_number:
            return {"error": "job_number required"}

        job_data = self.job_index.get(job_number)
        if not job_data or not isinstance(job_data, dict):
            return {"found": False, "error": f"Job {job_number} not found in index"}

        linked_emails = job_data.get('emails', {}).get('linked', [])
        if not linked_emails:
            return {"found": True, "job_number": job_number, "email_count": 0, "emails": []}

        # Filter by conversation_id if provided
        if conversation_id:
            linked_emails = [e for e in linked_emails if e.get('conversation_id') == conversation_id]

        # Build formatted list with index numbers
        formatted_emails = []
        for i, email in enumerate(linked_emails, 1):
            entry = {
                "index": i,  # 1-based for human readability
                "date": email.get('date', '?')[:10] if email.get('date') else '?',
                "direction": email.get('direction', 'unknown'),
                "from": email.get('from', '?'),
                "subject": email.get('subject', '(no subject)'),
                "summary": email.get('summary', '(no summary stored - use job_get_email_body for full content)'),
                "has_body_stored": bool(email.get('body_stored'))
            }

            # Include key_info if present
            if email.get('key_info'):
                entry["key_info"] = email['key_info']

            formatted_emails.append(entry)

        return {
            "found": True,
            "job_number": job_number,
            "property_address": job_data.get('property_address'),
            "client": job_data.get('client_business'),
            "email_count": len(formatted_emails),
            "emails": formatted_emails,
            "note": "Use job_get_email_body(job_number, email_index) to get full content of any email"
        }

    def _job_get_email_body(self, params: dict) -> dict:
        """
        Get the full stored body of a specific email from the job index.

        Use after job_get_thread_emails to expand a specific email.
        This retrieves from stored data - no Graph API call needed.
        """
        job_number = params.get('job_number')
        email_index = params.get('email_index')  # 1-based

        if not job_number:
            return {"error": "job_number required"}
        if not email_index:
            return {"error": "email_index required (from job_get_thread_emails)"}

        job_data = self.job_index.get(job_number)
        if not job_data or not isinstance(job_data, dict):
            return {"found": False, "error": f"Job {job_number} not found in index"}

        linked_emails = job_data.get('emails', {}).get('linked', [])
        if not linked_emails:
            return {"found": False, "error": f"No emails linked to job {job_number}"}

        # Convert 1-based index to 0-based
        idx = email_index - 1
        if idx < 0 or idx >= len(linked_emails):
            return {"found": False, "error": f"Email index {email_index} out of range (1-{len(linked_emails)})"}

        email = linked_emails[idx]

        # Return full email data including body
        result = {
            "found": True,
            "job_number": job_number,
            "email_index": email_index,
            "date": email.get('date'),
            "direction": email.get('direction'),
            "from": email.get('from'),
            "to": email.get('to'),
            "subject": email.get('subject'),
            "message_id": email.get('message_id'),
            "conversation_id": email.get('conversation_id'),
            "summary": email.get('summary'),
            "key_info": email.get('key_info')
        }

        # Include full body if stored
        if email.get('body_stored'):
            result["body"] = email['body_stored']
        else:
            result["body"] = None
            result["note"] = "No body stored. Use email_get_by_id with message_id to fetch from Graph API."
            result["message_id_for_fetch"] = email.get('message_id')

        return result

    def _job_set_pending(self, params: dict) -> dict:
        """
        Mark a job as awaiting human input. Use when you've sent a question
        to Nicholas and need to track that this job is waiting for an answer.
        
        Required:
            job_number: The job this question relates to
            question: Clear description of what you're asking
            question_type: Category (community_classification, pricing, clarification, access, other)
            email_id: Message ID of the question email you sent
            
        Optional:
            context: Dict with original request details for continuity
        """
        job_number = params.get('job_number')
        question = params.get('question')
        question_type = params.get('question_type', 'other')
        email_id = params.get('email_id')
        context = params.get('context', {})
        
        if not job_number:
            return {"success": False, "error": "job_number required"}
        if not question:
            return {"success": False, "error": "question required"}
        if not email_id:
            return {"success": False, "error": "email_id required (the message_id of your question email)"}
        
        now = datetime.now().isoformat()
        
        # Update the pending section
        self.enrich_job_index(job_number, "pending", {
            "status": "awaiting_input",
            "question": question,
            "question_type": question_type,
            "context": context,
            "email_id": email_id,
            "asked_at": now
        })
        
        return {
            "success": True,
            "job_number": job_number,
            "status": "awaiting_input",
            "message": f"Job {job_number} marked as awaiting input. When Nicholas replies, use job_clear_pending to continue."
        }
    
    def _job_clear_pending(self, params: dict) -> dict:
        """
        Clear pending status after receiving and processing Nicholas's answer.
        Call this after you've continued work on a job that was awaiting input.
        
        Required:
            job_number: The job to clear pending status for
            
        Optional:
            resolution: Brief note on how the question was resolved
        """
        job_number = params.get('job_number')
        resolution = params.get('resolution', 'Answered')
        
        if not job_number:
            return {"success": False, "error": "job_number required"}
        
        # Get current pending info before clearing
        job = self._get_or_create_job_entry(job_number)
        previous_question = job.get('pending', {}).get('question')
        
        # Clear by setting status to None (enrich_job_index handles full reset)
        self.enrich_job_index(job_number, "pending", {"status": None})
        
        return {
            "success": True,
            "job_number": job_number,
            "status": "cleared",
            "previous_question": previous_question,
            "resolution": resolution
        }
    
    def _job_get_pending(self, params: dict) -> dict:
        """
        Get all jobs currently awaiting input.
        Use this at the start of processing to check if any questions have been answered.
        """
        pending_jobs = []
        
        for job_number, job in self.job_index.items():
            if not isinstance(job, dict):
                continue  # Skip metadata keys like _synced_sent_ids
            pending = job.get('pending', {})
            if pending.get('status') == 'awaiting_input':
                pending_jobs.append({
                    "job_number": job_number,
                    "question": pending.get('question'),
                    "question_type": pending.get('question_type'),
                    "email_id": pending.get('email_id'),
                    "asked_at": pending.get('asked_at'),
                    "context": pending.get('context', {})
                })
        
        # Sort by asked_at (oldest first - they've been waiting longest)
        pending_jobs.sort(key=lambda x: x.get('asked_at', ''))
        
        return {
            "count": len(pending_jobs),
            "pending_jobs": pending_jobs
        }
    
    # =========================================================================
    # UTILITY IMPLEMENTATIONS
    # =========================================================================
    
    # =========================================================================
    # PROPOSAL TRACKING (Pre-Job Inquiries)
    # =========================================================================

    def _proposal_create(self, params: dict) -> dict:
        """Create a new proposal JSON file for a pre-job inquiry."""
        os.makedirs(self.PROPOSALS_FOLDER, exist_ok=True)

        client_name = params.get('client_name', 'Unknown')
        client_email = params.get('client_email', '')
        property_address = params.get('property_address', '')

        # Generate proposal ID: YYYY-MM-DD_ClientName_Address
        date_str = datetime.now().strftime("%Y-%m-%d")
        # Sanitize for filename
        safe_client = "".join(c for c in client_name[:30] if c.isalnum() or c in ' -_').strip().replace(' ', '_')
        safe_address = "".join(c for c in property_address[:30] if c.isalnum() or c in ' -_').strip().replace(' ', '_')

        if safe_address:
            proposal_id = f"{date_str}_{safe_client}_{safe_address}"
        else:
            proposal_id = f"{date_str}_{safe_client}"

        # Check if proposal already exists
        file_path = os.path.join(self.PROPOSALS_FOLDER, f"{proposal_id}.json")
        counter = 1
        while os.path.exists(file_path):
            proposal_id = f"{date_str}_{safe_client}_{counter}"
            file_path = os.path.join(self.PROPOSALS_FOLDER, f"{proposal_id}.json")
            counter += 1

        proposal = {
            "proposal_id": proposal_id,
            "created": datetime.now().isoformat(),
            "updated": datetime.now().isoformat(),
            "status": params.get('status', 'new'),
            "client": {
                "name": client_name,
                "email": client_email,
                "phone": params.get('client_phone')
            },
            "property": {
                "address": property_address,
                "community": params.get('community'),
                "purchaser_name": params.get('purchaser_name')
            },
            "service": {
                "requested": params.get('service_requested'),
                "closing_date": params.get('closing_date')
            },
            "quote": {
                "price": None,
                "sent_date": None,
                "estimate_id": None
            },
            "missing_info": params.get('missing_info', []),
            "notes": params.get('notes'),
            "communications": [],
            "source_email_id": params.get('source_email_id'),
            "conversation_id": params.get('conversation_id'),
            "job_number": None  # Filled when converted to job
        }

        # Log initial communication if we have email context
        if self.current_email:
            proposal["communications"].append({
                "date": datetime.now().isoformat(),
                "direction": "inbound",
                "summary": f"Initial inquiry: {self.current_email.subject}"
            })

        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(proposal, f, indent=2, default=str)

        self.logger.info(f"    Created proposal: {proposal_id}")
        return {
            "success": True,
            "proposal_id": proposal_id,
            "file_path": file_path,
            "status": proposal["status"]
        }

    def _proposal_update(self, params: dict) -> dict:
        """Update an existing proposal."""
        proposal_id = params.get('proposal_id')
        if not proposal_id:
            return {"success": False, "error": "proposal_id required"}

        file_path = os.path.join(self.PROPOSALS_FOLDER, f"{proposal_id}.json")
        if not os.path.exists(file_path):
            return {"success": False, "error": f"Proposal not found: {proposal_id}"}

        with open(file_path, 'r', encoding='utf-8') as f:
            proposal = json.load(f)

        # Update fields
        if 'status' in params:
            proposal['status'] = params['status']
        if 'property_address' in params:
            proposal['property']['address'] = params['property_address']
        if 'community' in params:
            proposal['property']['community'] = params['community']
        if 'closing_date' in params:
            proposal['service']['closing_date'] = params['closing_date']
        if 'purchaser_name' in params:
            proposal['property']['purchaser_name'] = params['purchaser_name']
        if 'quoted_price' in params:
            proposal['quote']['price'] = params['quoted_price']
        if 'quote_sent_date' in params:
            proposal['quote']['sent_date'] = params['quote_sent_date']
        if 'estimate_id' in params:
            proposal['quote']['estimate_id'] = params['estimate_id']
        if 'missing_info' in params:
            proposal['missing_info'] = params['missing_info']
        if 'notes' in params:
            # Append to existing notes
            existing = proposal.get('notes') or ''
            if existing:
                proposal['notes'] = f"{existing}\n{datetime.now().strftime('%Y-%m-%d')}: {params['notes']}"
            else:
                proposal['notes'] = params['notes']

        # Log communication if provided
        if 'communication' in params:
            comm = params['communication']
            proposal['communications'].append({
                "date": datetime.now().isoformat(),
                "direction": comm.get('direction', 'outbound'),
                "summary": comm.get('summary', '')
            })

        proposal['updated'] = datetime.now().isoformat()

        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(proposal, f, indent=2, default=str)

        self.logger.info(f"    Updated proposal: {proposal_id} -> {proposal['status']}")

        # Build response with folder reminder for status changes
        result = {"success": True, "proposal_id": proposal_id, "status": proposal['status']}

        # Remind Claude to move email to matching folder
        if 'status' in params:
            status_folder_map = {
                'new': 'Proposals/New',
                'awaiting_info': 'Proposals/Awaiting Info',
                'awaiting_pricing': 'Proposals/Awaiting Pricing',
                'quoted': 'Proposals/Quoted'
            }
            folder = status_folder_map.get(params['status'])
            if folder:
                result['reminder'] = f"Move associated email to '{folder}' folder to track pipeline"

        return result

    def _proposal_get(self, params: dict) -> dict:
        """Get a specific proposal by ID."""
        proposal_id = params.get('proposal_id')
        if not proposal_id:
            return {"success": False, "error": "proposal_id required"}

        file_path = os.path.join(self.PROPOSALS_FOLDER, f"{proposal_id}.json")
        if not os.path.exists(file_path):
            return {"success": False, "error": f"Proposal not found: {proposal_id}"}

        with open(file_path, 'r', encoding='utf-8') as f:
            proposal = json.load(f)

        return {"success": True, "proposal": proposal}

    def _proposal_search(self, params: dict) -> dict:
        """Search for proposals by various criteria."""
        if not os.path.exists(self.PROPOSALS_FOLDER):
            return {"success": True, "proposals": [], "count": 0}

        results = []
        for filename in os.listdir(self.PROPOSALS_FOLDER):
            if not filename.endswith('.json'):
                continue

            file_path = os.path.join(self.PROPOSALS_FOLDER, filename)
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    proposal = json.load(f)

                # Apply filters
                if params.get('client_email'):
                    if proposal.get('client', {}).get('email', '').lower() != params['client_email'].lower():
                        continue

                if params.get('client_name'):
                    if params['client_name'].lower() not in proposal.get('client', {}).get('name', '').lower():
                        continue

                if params.get('property_address'):
                    if params['property_address'].lower() not in proposal.get('property', {}).get('address', '').lower():
                        continue

                if params.get('status'):
                    if proposal.get('status') != params['status']:
                        continue

                if params.get('conversation_id'):
                    if proposal.get('conversation_id') != params['conversation_id']:
                        continue

                results.append(proposal)
            except Exception as e:
                self.logger.warning(f"Error reading proposal {filename}: {e}")
                continue

        # Sort by created date (most recent first)
        results.sort(key=lambda x: x.get('created', ''), reverse=True)

        return {"success": True, "proposals": results, "count": len(results)}

    def _proposal_list(self, params: dict) -> dict:
        """List recent proposals, optionally filtered by status."""
        if not os.path.exists(self.PROPOSALS_FOLDER):
            return {"success": True, "proposals": [], "count": 0}

        results = []
        status_filter = params.get('status')
        max_count = params.get('count', 20)

        for filename in os.listdir(self.PROPOSALS_FOLDER):
            if not filename.endswith('.json'):
                continue

            file_path = os.path.join(self.PROPOSALS_FOLDER, filename)
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    proposal = json.load(f)

                if status_filter and proposal.get('status') != status_filter:
                    continue

                # Return summary info
                results.append({
                    "proposal_id": proposal.get('proposal_id'),
                    "created": proposal.get('created'),
                    "status": proposal.get('status'),
                    "client_name": proposal.get('client', {}).get('name'),
                    "client_email": proposal.get('client', {}).get('email'),
                    "property_address": proposal.get('property', {}).get('address'),
                    "community": proposal.get('property', {}).get('community'),
                    "quoted_price": proposal.get('quote', {}).get('price')
                })
            except Exception as e:
                self.logger.warning(f"Error reading proposal {filename}: {e}")
                continue

        # Sort by created date (most recent first)
        results.sort(key=lambda x: x.get('created', ''), reverse=True)
        results = results[:max_count]

        return {"success": True, "proposals": results, "count": len(results)}

    def _proposal_convert_to_job(self, params: dict) -> dict:
        """Convert an accepted proposal to a full job."""
        proposal_id = params.get('proposal_id')
        if not proposal_id:
            return {"success": False, "error": "proposal_id required"}

        # Get proposal
        file_path = os.path.join(self.PROPOSALS_FOLDER, f"{proposal_id}.json")
        if not os.path.exists(file_path):
            return {"success": False, "error": f"Proposal not found: {proposal_id}"}

        with open(file_path, 'r', encoding='utf-8') as f:
            proposal = json.load(f)

        # Check we have required info
        client_name = proposal.get('client', {}).get('name')
        client_email = proposal.get('client', {}).get('email')
        property_address = proposal.get('property', {}).get('address')
        community = proposal.get('property', {}).get('community')

        if not all([client_name, client_email, property_address, community]):
            missing = []
            if not client_name: missing.append('client_name')
            if not client_email: missing.append('client_email')
            if not property_address: missing.append('property_address')
            if not community: missing.append('community')
            return {"success": False, "error": f"Missing required info: {', '.join(missing)}"}

        # Get closing date and purchaser (from params or proposal)
        closing_date = params.get('closing_date') or proposal.get('service', {}).get('closing_date')
        purchaser_name = params.get('purchaser_name') or proposal.get('property', {}).get('purchaser_name')
        price = params.get('price') or proposal.get('quote', {}).get('price')

        # Create job using job_create
        job_params = {
            "client_name": client_name,
            "client_email": client_email,
            "property_address": property_address,
            "community": community,
            "closing_date": closing_date,
            "purchaser_name": purchaser_name,
            "conversation_id": proposal.get('conversation_id'),
            "message_id": proposal.get('source_email_id')
        }

        job_result = self._job_create(job_params)

        if not job_result.get('success'):
            return {"success": False, "error": f"Failed to create job: {job_result.get('error')}"}

        job_number = job_result.get('job_number')

        # Update proposal with job number and mark as accepted
        proposal['status'] = 'accepted'
        proposal['job_number'] = job_number
        proposal['updated'] = datetime.now().isoformat()
        proposal['communications'].append({
            "date": datetime.now().isoformat(),
            "direction": "system",
            "summary": f"Converted to job {job_number}"
        })

        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(proposal, f, indent=2, default=str)

        self.logger.info(f"    Converted proposal {proposal_id} to job {job_number}")
        return {
            "success": True,
            "proposal_id": proposal_id,
            "job_number": job_number,
            "job_result": job_result
        }

    # =========================================================================
    # FLAGGING / ATTENTION
    # =========================================================================

    def _log_system_error(self, error_type: str, message: str, context: dict = None):
        """
        Log system errors to a JSONL file for tracking issues.

        This is for system/infrastructure problems, not for flagging emails.
        Examples: path not found, API errors, permission denied, tool failures.
        """
        log_dir = os.path.dirname(self.JOB_INDEX_FILE)  # H:\data or /volume1/HiveMind/data
        error_log = os.path.join(log_dir, "system_errors.jsonl")

        entry = {
            "timestamp": datetime.now().isoformat(),
            "error_type": error_type,
            "message": message,
            "context": context or {},
        }

        # Add email context if available
        if self.current_email:
            entry["email"] = {
                "subject": self.current_email.subject,
                "sender": self.current_email.sender_email,
                "message_id": self.current_email.message_id
            }

        try:
            with open(error_log, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, default=str) + "\n")
            self.logger.warning(f"System error logged: {error_type} - {message}")
        except Exception as e:
            self.logger.error(f"Failed to log system error: {e}")

    def _flag_internal(self, reason: str, context: dict = None):
        """Internal method to flag for attention."""
        os.makedirs(self.FLAGGED_FOLDER, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        sender = ""
        if self.current_email:
            sender = self.current_email.sender_email or "unknown"
        sender = "".join(c for c in sender[:40] if c.isalnum() or c in '@._-')
        
        file_path = os.path.join(self.FLAGGED_FOLDER, f"{timestamp}_{sender}.txt")
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(f"REASON: {reason}\n")
            f.write("=" * 60 + "\n\n")
            
            if context:
                f.write(f"Context: {json.dumps(context, default=str)}\n\n")
            
            if self.current_email:
                f.write(f"From: {self.current_email.sender} <{self.current_email.sender_email}>\n")
                f.write(f"Subject: {self.current_email.subject}\n")
                f.write(f"Date: {self.current_email.received_date}\n\n")
                f.write(self.current_email.body[:5000] if self.current_email.body else "")
    
    def _flag_for_attention(self, params: dict) -> dict:
        """
        Flag an email for Nicholas's attention.

        This will:
        1. Save details to flagged folder (backup/audit)
        2. Move email to "Needs Attention" folder in Outlook
        3. Keep email UNREAD so Nicholas sees it

        Nicholas will see the email in his "Needs Attention" folder, unread.
        """
        self._flag_internal(
            params['reason'],
            {"email_info": params.get('email_info'), "context": params.get('context')}
        )
        self.logger.info(f"    Flagged: {params['reason']}")

        # Move to "Email/Needs Attention" folder and keep unread
        if self.current_email and self.email:
            try:
                # Move to folder (creates if doesn't exist)
                self._email_move_to_folder({
                    "message_id": self.current_email.message_id,
                    "folder_name": "Email/Needs Attention"
                })
                self.logger.info(f"    Moved to 'Email/Needs Attention' folder")

                # Ensure it stays UNREAD
                self._email_mark_unread({"message_id": self.current_email.message_id})
                self.logger.info(f"    Kept unread for visibility")
            except Exception as e:
                self.logger.error(f"    Failed to move/mark email: {e}")

        return {"success": True, "message": "Flagged - moved to 'Email/Needs Attention' folder (unread)"}

    def _log_system_error_tool(self, params: dict) -> dict:
        """
        Tool for Claude to log system errors.

        Use this when encountering infrastructure issues like:
        - Path not found / file not accessible
        - API errors or unexpected responses
        - Permission denied errors
        - Tool failures that shouldn't happen
        """
        error_type = params.get('error_type', 'unknown')
        message = params.get('message', 'No message provided')
        context = params.get('context', {})

        self._log_system_error(error_type, message, context)

        return {
            "success": True,
            "logged": True,
            "message": f"Error logged: {error_type}"
        }

    def _get_current_datetime(self, params: dict) -> dict:
        now = datetime.now()
        return {
            "datetime": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "day": now.strftime("%A"),
            "timezone": "America/St_Johns",
            "iso": now.isoformat()
        }
    
    def _parse_date(self, params: dict) -> dict:
        # Accept both 'date_text' and 'date_string' for flexibility
        date_input = params.get('date_text') or params.get('date_string')
        if not date_input:
            return {"error": "Must provide 'date_text' or 'date_string' parameter"}
        
        text = date_input.lower().strip()
        today = datetime.now()
        
        # Month patterns
        months = [
            (r'jan(?:uary)?\s*(\d{1,2})(?:st|nd|rd|th)?(?:[,\s]*(\d{4}))?', 1),
            (r'feb(?:ruary)?\s*(\d{1,2})(?:st|nd|rd|th)?(?:[,\s]*(\d{4}))?', 2),
            (r'mar(?:ch)?\s*(\d{1,2})(?:st|nd|rd|th)?(?:[,\s]*(\d{4}))?', 3),
            (r'apr(?:il)?\s*(\d{1,2})(?:st|nd|rd|th)?(?:[,\s]*(\d{4}))?', 4),
            (r'may\s*(\d{1,2})(?:st|nd|rd|th)?(?:[,\s]*(\d{4}))?', 5),
            (r'jun(?:e)?\s*(\d{1,2})(?:st|nd|rd|th)?(?:[,\s]*(\d{4}))?', 6),
            (r'jul(?:y)?\s*(\d{1,2})(?:st|nd|rd|th)?(?:[,\s]*(\d{4}))?', 7),
            (r'aug(?:ust)?\s*(\d{1,2})(?:st|nd|rd|th)?(?:[,\s]*(\d{4}))?', 8),
            (r'sep(?:t(?:ember)?)?\s*(\d{1,2})(?:st|nd|rd|th)?(?:[,\s]*(\d{4}))?', 9),
            (r'oct(?:ober)?\s*(\d{1,2})(?:st|nd|rd|th)?(?:[,\s]*(\d{4}))?', 10),
            (r'nov(?:ember)?\s*(\d{1,2})(?:st|nd|rd|th)?(?:[,\s]*(\d{4}))?', 11),
            (r'dec(?:ember)?\s*(\d{1,2})(?:st|nd|rd|th)?(?:[,\s]*(\d{4}))?', 12),
        ]
        
        for pattern, month in months:
            match = re.search(pattern, text)
            if match:
                day = int(match.group(1))
                year = int(match.group(2)) if match.group(2) else today.year
                try:
                    dt = datetime(year, month, day)
                    if dt < today and not match.group(2):
                        dt = datetime(year + 1, month, day)
                    return {"parsed_date": dt.strftime("%Y-%m-%d"), "parsed": dt.strftime("%Y-%m-%d"), "original": date_input}
                except:
                    pass
        
        # Relative
        if 'today' in text:
            return {"parsed_date": today.strftime("%Y-%m-%d"), "parsed": today.strftime("%Y-%m-%d"), "original": date_input}
        if 'tomorrow' in text:
            return {"parsed_date": (today + timedelta(days=1)).strftime("%Y-%m-%d"), "parsed": (today + timedelta(days=1)).strftime("%Y-%m-%d"), "original": date_input}
        if 'next week' in text:
            return {"parsed_date": (today + timedelta(days=7)).strftime("%Y-%m-%d"), "parsed": (today + timedelta(days=7)).strftime("%Y-%m-%d"), "original": date_input}
        
        return {"error": f"Could not parse: {params['date_text']}", "original": params['date_text']}
