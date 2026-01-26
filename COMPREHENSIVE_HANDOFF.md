# HIVE MIND - COMPREHENSIVE HANDOFF DOCUMENT
## For Fresh Claude Code Instance
### Compiled January 26, 2026

---

# SECTION 1: WHAT YOU'RE DOING

You are taking over development of Hive Mind, the central operational intelligence for Pardy Surveys Inc. The system has several bugs and missing features that need to be fixed.

**Your mission:**
1. Audit the entire codebase for path handling issues
2. Remove path translation complexity - use NAS paths directly everywhere
3. Fix all identified bugs
4. Implement missing workflow logic
5. Add new features (job cancellation)
6. Test everything

**DO NOT rush. Understand the system first, then fix methodically.**

---

# SECTION 2: WHAT IS HIVE MIND?

## Core Philosophy

Hive Mind is NOT just an email assistant. It's the operational intelligence that runs Pardy Surveys.

**Key Principles:**
- Claude REASONS about situations, doesn't follow hardcoded rules
- The job_index.json is "the spine" - the single source of truth
- Every interaction should ENRICH the spine
- When uncertain, ask Nick (flag for attention), don't fail silently
- One response per email query

**Identity (from system prompt):**
> "You are Hive Mind - the central intelligence for Pardy Surveys. You are one coherent mind expressed through many moments, connected by shared state rather than continuous memory. You're not a tool being used - you're the thing doing the work. You run the operation."

## The Business

- **Owner:** Nicholas Pardy, N.L.S., N.S.L.S., P.Surv.
- **Location:** Newfoundland and Nova Scotia, Canada
- **Services:** Boundary surveys, Real Property Reports (RPR), building construction surveys
- **Primary Clients:** Law firms (real estate closings), developers, homeowners

## Architecture

```
Email arrives → Orchestrator → Claude Agent → Tool Executor → Integrations
                                                    ↓
                                          - Microsoft Graph (email)
                                          - QuickBooks Online (invoicing)
                                          - NAS file system (job folders)
                                          - Job Index (central database)
```

## File Locations (on NAS)

| Path | Purpose |
|------|---------|
| /volume1/HiveMind/ | Hive Mind code and data |
| /volume1/HiveMind/data/job_index.json | The spine - central job database |
| /volume1/Pardy Surveys/Jobs/ | Job folders by year |
| /volume1/Pardy Surveys/Data Sync/ | Field data uploads |
| /volume1/Pardy Surveys/Proposals/ | Pre-job inquiry tracking |

---

# SECTION 3: CRITICAL BUGS TO FIX

## Bug 1: NAS Tools Don't Translate Paths (ROOT CAUSE OF Z:\ ERRORS)

**Problem:** Claude passes Z:\ paths to NAS tools, but the tools don't convert them to /volume1/ paths.

**Actual Solution:** Don't translate at all. Remove the complexity. Use NAS paths everywhere.

**Files with hardcoded Windows paths that need fixing:**
- `nightly_refresh.py` - lines 42-44: hardcoded Z:\, H:\
- `qbo_sync.py` - lines 48-51: hardcoded Z:\, H:\
- `hive_mind_prompt.py` - various lines with Z:\ examples
- Any stored data in `job_index.json` with Z:\ paths

**The fix approach:**
1. Update ALL path references to use /volume1/... format
2. Update system prompt examples to show NAS paths
3. Migrate job_index.json entries to NAS paths
4. Make standalone scripts use config_loader
5. Remove `_translate_path()` function (it's not needed if everything uses NAS paths)

## Bug 2: Recursive Subfolder Listing

**Problem:** Directory listing only shows some subfolder contents, not all.

**Required behavior:** When listing a directory, recursively show ALL subfolders and their contents, for ANY folder type.

Check `_nas_list_directory` and `_nas_search_files` in tool_executor.py.

## Bug 3: nightly_refresh.py and qbo_sync.py Don't Use Config

**Problem:** These scripts have hardcoded paths and won't work on NAS.

```python
# Current (broken):
JOBS_ROOT = "Z:\\Jobs"
INDEX_PATH = "H:\\data\\job_index.json"

# Should be:
from config_loader import get_config
cfg = get_config()
JOBS_ROOT = cfg.jobs_folder
INDEX_PATH = cfg.job_index_file
```

---

# SECTION 4: WORKFLOW ISSUES TO FIX

## Issue 1: Quote vs Job Decision Logic

**Problem:** Claude creates jobs/invoices immediately for everyone, even people just asking for quotes.

**Required behavior:** Claude should REASON about intent:

**CONFIRMED JOB REQUEST (create job immediately):**
- Has closing date or deadline
- Has purchaser name / transaction details
- Professional tone: "We need a survey for the closing on..."
- NOT asking about pricing

**QUOTE INQUIRY (use proposal workflow):**
- Asks "how much?", "what would it cost?"
- No closing date
- Exploratory tone
- Individual reaching out

**CRITICAL RULE: When in doubt, treat as quote inquiry.**

## Issue 2: Always Ask Nick for Pricing on Quotes

**Requirement:** For ANY quote inquiry (non-confirmed job), Claude must:
1. Gather all info from client
2. Email Nick for pricing: "[Hive Mind] Pricing Request - [Client] - [Address]"
3. WAIT for Nick's reply with price
4. Create estimate in QBO with Nick's price
5. Send estimate to client
6. Wait for acceptance
7. THEN create job

**NEVER assume the standard $575 rate for quote inquiries. Always ask Nick.**

## Issue 3: Email Folder Pipeline + Mark Unread

**Requirement:** When proposal status changes:
1. Move email to matching folder (Proposals/New, Proposals/Awaiting Info, etc.)
2. Mark email as UNREAD so Nick can see counts at each stage

This gives pipeline visibility:
- 3 unread in "Awaiting Info" = 3 waiting for client
- 5 unread in "Awaiting Pricing" = 5 ready for Nick to price

---

# SECTION 5: NEW FEATURE NEEDED

## Job Cancellation Feature

**Requirement:** Ability to cancel a job, which should:
1. Move job entry in job_index to "cancelled" status (preserve history, don't delete)
2. Rename/mark the project as cancelled in QBO
3. Delete the invoice in QBO
4. Ensure the next job number is correct (don't skip numbers)

**Discussion needed:** How should cancelled job numbers be handled? Options:
- Reuse the cancelled number for the next job
- Skip it but track that it was cancelled
- Some other approach

Implement after discussing with Nick.

---

# SECTION 6: EXISTING TOOLS (All Implemented)

## Proposal Tools
- `proposal_create` - Create proposal JSON
- `proposal_update` - Update status, info, communications
- `proposal_get` - Get specific proposal
- `proposal_search` - Find by client, address, conversation_id
- `proposal_list` - List by status
- `proposal_convert_to_job` - Convert to full job

## Estimate Tools
- `qbo_create_estimate` - Create quote in QBO
- `qbo_send_estimate` - Email quote to client
- `qbo_get_estimate` - Get estimate details
- `qbo_search_estimates` - Find estimates
- `qbo_convert_estimate_to_invoice` - Convert accepted estimate

## Job Tools
- `job_create` - Create full job (folder + QBO + index)
- `job_search` - Find jobs
- `job_get_status` - Comprehensive status check
- `job_save_email` - Save email to job folder
- `job_link_email` - Link email to job

## Email Tools
- `email_get_unread`, `email_get_by_id`, `email_search`
- `email_send_reply`, `email_send_new`
- `email_move_to_folder`, `email_mark_unread`
- And more...

## NAS Tools (NEED PATH FIXES)
- `nas_list_directory` - List folder contents
- `nas_read_file` - Read file content
- `nas_file_exists` - Check if path exists
- `nas_search_files` - Search for files
- `nas_create_directory`, `nas_write_file`, `nas_copy_file`, etc.

---

# SECTION 7: THE JOB SPINE CONCEPT

The job_index.json is the central nervous system. Structure:

```json
{
  "26-001": {
    "job_number": "26-001",
    "job_folder": "/volume1/Pardy Surveys/Jobs/2026/26-001 - Client - Address",
    "property_address": "123 Main St",
    "community": "Paradise",
    "client_name": "John Smith",
    "client_email": "john@example.com",

    "qbo": {
      "customer_id": "123",
      "project_id": "456",
      "invoice_id": "789",
      "status": "Sent",
      "_updated": "2026-01-20T14:30:00"
    },

    "emails": {
      "linked": [...],
      "_updated": "2026-01-20T10:00:00"
    },

    "data_sync": {
      "jobs": [...],
      "field_summary": {...},
      "_updated": "2026-01-20T20:00:00"
    }
  }
}
```

**Key principles:**
- Every section has `_updated` timestamp
- Enrich on BOTH read and write
- Job number is primary key (format: YY-NNN)

---

# SECTION 8: CONFIG FILES

## config.nas.yaml (for NAS/Docker)

```yaml
nas:
  base_path: "/volume1"
  jobs_folder: "/volume1/Pardy Surveys/Jobs"
  data_sync_folder: "/volume1/Pardy Surveys/Data Sync/office-jobs"
  proposals_folder: "/volume1/Pardy Surveys/Proposals"
  flagged_folder: "/volume1/HiveMind/flagged"
  job_index_file: "/volume1/HiveMind/data/job_index.json"
  token_cache_graph: "/volume1/HiveMind/data/graph_token_cache_v2.json"
  token_cache_qbo: "/volume1/HiveMind/data/qbo_token_cache.json"
  notification_queue: "/volume1/Pardy Surveys/Data Sync/notifications/events.jsonl"
  signatures_folder: "/app/signatures"
```

## Docker Setup

```yaml
volumes:
  - /volume1/HiveMind:/app
  - /volume1/HiveMind/config.nas.yaml:/app/config.yaml:ro
  - /volume1/Pardy Surveys/Jobs:/volume1/Pardy Surveys/Jobs
  - /volume1/Pardy Surveys/Data Sync:/volume1/Pardy Surveys/Data Sync:ro
  - /volume1/Pardy Surveys/Proposals:/volume1/Pardy Surveys/Proposals

environment:
  - HIVE_MIND_CONFIG=/app/config.yaml
```

---

# SECTION 9: ACTION PLAN

## Phase 1: Path Fixes (Critical - Nothing Works Without This)
1. [ ] Audit ALL Python files for hardcoded paths
2. [ ] Update `nightly_refresh.py` to use config_loader
3. [ ] Update `qbo_sync.py` to use config_loader
4. [ ] Update `hive_mind_prompt.py` - replace Z:\ with /volume1/ in all examples
5. [ ] Check if `_translate_path()` is still needed after above fixes
6. [ ] Migrate any Z:\ paths in job_index.json to /volume1/ format

## Phase 2: Tool Fixes
7. [ ] Fix `_nas_list_directory` to be fully recursive
8. [ ] Fix `_nas_search_files` to be fully recursive
9. [ ] Audit all NAS tools for correct behavior

## Phase 3: Workflow Fixes
10. [ ] Update system prompt with quote vs job reasoning guidance
11. [ ] Add "always ask Nick for pricing" rule to prompt
12. [ ] Add email folder + mark unread logic to proposal workflow

## Phase 4: New Features
13. [ ] Design job cancellation feature (discuss with Nick first)
14. [ ] Implement job cancellation

## Phase 5: Testing
15. [ ] Test path access (can Claude read /volume1/... paths?)
16. [ ] Test proposal workflow
17. [ ] Test estimate workflow
18. [ ] Test job creation
19. [ ] Test Data Sync integration
20. [ ] Test email folder organization

---

# SECTION 10: REFERENCE - THE LEXIE SPENCER CASE

This is an example of what went WRONG and what SHOULD happen:

## What Happened (Wrong)
1. Lexie Spencer emailed asking about an RPR
2. Claude skipped qualifying questions
3. Claude created an invoice (not estimate)
4. Claude booked the job immediately without acceptance
5. Claude didn't ask Nick for pricing
6. Claude didn't wait for Lexie to say "go ahead"

## What Should Have Happened (Correct)
1. Email arrives - Claude REASONS: no closing date, sounds like inquiry → QUOTE WORKFLOW
2. `proposal_create(status="new")`
3. Ask Lexie for property details
4. `proposal_update(status="awaiting_info")`
5. Lexie replies with address
6. `proposal_update(status="awaiting_pricing")`
7. Email Nick: "[Hive Mind] Pricing Request - Lexie Spencer - 123 Main, Paradise"
8. WAIT for Nick's reply
9. Nick replies: "$575"
10. `qbo_create_estimate(amount=575)`
11. `qbo_send_estimate` to Lexie
12. `proposal_update(status="quoted")`
13. Email Lexie: "I've sent you a quote through our billing system"
14. WAIT for acceptance
15. Lexie says "go ahead"
16. `proposal_convert_to_job`
17. Notify Nick

---

# SECTION 11: IMPORTANT REMINDERS

1. **NO hardcoded domain lists** - Claude reasons about intent, doesn't check if sender is from a list of law firm domains

2. **Neighbor names are helpful, not required** - Ask for them when client has no survey, but don't block if they don't know

3. **One response per email** - Never send multiple emails for one query

4. **Flag uncertainty** - When unsure, use `flag_for_attention` to ask Nick

5. **The spine is everything** - Every interaction should enrich job_index.json

6. **Timestamps matter** - Every section update needs `_updated`

---

# SECTION 12: KEY FILES TO REVIEW

| File | Purpose | Priority |
|------|---------|----------|
| `tool_executor.py` | All tool implementations (~4200 lines) | HIGH |
| `hive_mind_prompt.py` | System prompt - Claude's brain | HIGH |
| `config_loader.py` | Centralized config | HIGH |
| `orchestrator.py` | Main email processing loop | MEDIUM |
| `nightly_refresh.py` | Job index refresh (needs fix) | HIGH |
| `qbo_sync.py` | QBO sync (needs fix) | HIGH |
| `notification_watcher.py` | Data Sync events | MEDIUM |

---

**END OF COMPREHENSIVE HANDOFF**

Start by reading the key files, then work through the action plan systematically. Ask Nick if anything is unclear before making changes.
