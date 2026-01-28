# Implementation Task: Data Sync Automatic Enrichment + QBO Time Validation

## IMPORTANT: Response Protocol

**Before implementing ANYTHING, you must:**
1. Read and understand all the documentation and code
2. Examine real field data examples from the Data Sync folder
3. Present a complete implementation plan to me for review
4. Wait for my approval before making any code changes

**Your first response should be a detailed plan covering:**
- What you found when examining the current code
- What you found when examining real field data
- Exactly what changes you plan to make
- Any concerns or questions you have
- How you'll verify the fix preserves all data

I will review your plan and tell you whether to proceed.

---

## Background Documents

Read these files first:
1. `ANALYSIS_DATA_SYNC_ENRICHMENT.md` - Complete technical analysis of the enrichment bug
2. `PROMPT_FOR_VERIFICATION_AND_FIX.md` - Verification steps and safety considerations
3. `test_field_status.py` - Shows the designed data structure and aggregation logic

---

## Task 1: Fix Data Sync Automatic Enrichment

### The Problem

The job index is supposed to be a "living document" that stays automatically enriched when:
- Field data is uploaded FROM the controller (field crew returns data)
- Layouts are uploaded FROM the PC to Data Sync (sent to controller)

Currently, the notification_watcher detects these events but the enrichment silently fails - the data is dropped and never written to the job index.

**Claude can still answer questions correctly** because `job_get_status` reads LIVE from the Data Sync folder every time. But the job index itself stays stale until nightly refresh or until someone asks Claude about the job.

### Understanding the Upload Types

The field_status.json has an `upload_type` field that's critical to understand:

| upload_type | Meaning | How to Handle |
|-------------|---------|---------------|
| `"complete"` | End-of-day upload, field work session finished | Count toward visits, aggregate time |
| `"intermediate"` | Mid-day data review (lunch break check) | Extract CSV data, but DON'T count as visit |
| `"reupload"` | Replacing a previous upload (corrected data) | Extract CSV data, DON'T count as visit, note replaces_folder |

**Backward compatibility:** Older uploads have `is_reupload: true/false` instead of `upload_type`. The code derives: `upload_type = "reupload" if is_reupload else "complete"`

### The Fix

Have notification_watcher call `_job_get_status()` after detecting events. This:
1. Reads LIVE from Data Sync folder (same as Claude)
2. Calculates layout_summary and field_summary
3. Triggers the full enrichment pipeline via `_enrich_job_in_index`

### Data That Must Be Preserved

From `field_status.json`:
```json
{
  "_schema_version": "1.0",
  "job_number": "26-001",
  "operator": "Joe",
  "job_type": "survey_rpr",
  "upload_type": "complete",
  "is_reupload": false,
  "replaces_folder": null,
  "time_spent": {
    "field_hours": 6.0,
    "travel_hours_oneway": 1.0
  },
  "field_work_done": true,
  "estimated_time_remaining": null,
  "pins_found": "yes",
  "pins_placed": "yes",
  "notes": "All corners found and marked",
  "construction_tasks_completed": ["excavation_layout", "foundation_check"],
  "custom_tasks_completed": ["Septic layout"],
  "qbo_sync": {
    "time_synced": true,
    "time_entry_id": "123456",
    "synced_at": "2026-01-21T10:00:00Z"
  },
  "csv_extracted": {
    "total_points": 45,
    "evidence_found": ["IPF N corner lot 12", "IPF S corner lot 12"],
    "pins_placed": ["IP #1234 NE corner"],
    "evidence_not_found": 2,
    "evidence_to_find": 0,
    "monument_checks": ["Mon #5432 found in place"]
  },
  "upload_timestamp": "2026-01-21T16:30:00Z"
}
```

From `job_info.json`:
```json
{
  "address": "123 Main Street, Halifax",
  "description": "Boundary survey - residential",
  "operator": "Allan",
  "created": "2026-01-18T09:00:00Z",
  "referenceNumber": "REF-2026-001"
}
```

**ALL of this must flow into the job index when events occur.**

---

## Task 2: QBO Time Sync Validation (New Feature)

### The Problem

Time entries are posted to QBO automatically when field data is uploaded. But there's no validation to catch:
- Duplicate time entries (same hours entered twice)
- Mismatched hours (Data Sync says 6 hours, QBO has 8 hours)
- Missing time entries (field data has time but QBO doesn't)

### Requirements

1. **When syncing time to QBO**, compare what's being posted against what's already in QBO for that job/date/employee

2. **If a mismatch is detected**:
   - Log the discrepancy
   - If it's significant (more than 0.5 hour difference), flag for review
   - Send an email to Claude (or trigger a notification) asking about the discrepancy

3. **Operator-specific handling**:
   - If it's Joe's time that has a discrepancy, email Joe directly asking him to clarify
   - Joe (or the operator) can reply to Claude with the correct hours
   - Claude can then adjust the time entry in QBO and Data Sync

4. **Time Override Tracking**:
   - When time is manually corrected based on user input, record it:
   ```json
   "time_override": {
     "original_hours": 6.0,
     "corrected_hours": 4.5,
     "corrected_by": "Joe",
     "corrected_at": "2026-01-22T09:00:00Z",
     "reason": "Lunch break was longer than recorded"
   }
   ```
   - This creates an audit trail for time corrections

5. **Validation checks**:
   - Total daily hours per employee shouldn't exceed reasonable limit (e.g., 12 hours)
   - Warn if same job has multiple complete uploads on same day with overlapping times
   - Detect if QBO already has an entry for this exact upload (prevent duplicates)

### Implementation Approach

This is a NEW feature, so:
1. First understand how `_post_time_to_qbo` currently works
2. Identify where QBO time entries are retrieved/compared
3. Design the validation logic
4. Design the notification/email flow for discrepancies
5. Design the time_override storage structure

---

## Task 3: Verify QBO Employee Mapping Fix

The employee mapping error may have been fixed in the current code. Verify:
1. How does the system map operators (Joe, Allan, Nick) to QBO employee IDs?
2. Is there error handling for unmapped operators?
3. Are there any remaining issues with the QBO time sync?

---

## Step-by-Step Instructions

### Phase 1: Examine Current State

1. **Read the analysis documents** listed above

2. **Examine real field data** - Find a job (NOT 26-010) with completed field uploads:
   - Read the `field_status.json` files
   - Read the `job_info.json` file
   - Note all the fields and their actual values

3. **Examine current notification_watcher.py**:
   - Find `_handle_field_data_uploaded`
   - Find `_handle_layout_job_created`
   - Check if they already call `_job_get_status`
   - Check QBO time posting logic

4. **Examine current tool_executor.py**:
   - Find `enrich_job_index` - does it handle all data_sync fields?
   - Find `_job_get_status` - does it still enrich at the end?
   - Find `_post_time_to_qbo` - how does it work?

5. **Examine QBO integration**:
   - How are time entries posted?
   - How are existing entries retrieved?
   - Is there duplicate detection?

### Phase 2: Present Your Plan

**Stop here and present your findings and plan to me.** Include:

1. **Field Data Findings**
   - Which job(s) did you examine?
   - What fields were present in field_status.json?
   - Any differences from the expected structure?

2. **Code Findings**
   - Is the enrichment bug still present?
   - What's the current state of QBO time posting?
   - Any fixes already in place?

3. **Proposed Changes for Task 1 (Enrichment)**
   - Exact code changes with file/line locations
   - How you'll verify data preservation

4. **Proposed Changes for Task 2 (QBO Validation)**
   - Design for the validation logic
   - Design for the notification flow
   - Design for time_override storage
   - Which files need to be modified

5. **Concerns or Questions**
   - Anything unclear?
   - Any potential issues with the approach?
   - Any dependencies or prerequisites?

### Phase 3: Implementation (After Approval)

Only proceed after I review your plan and approve.

---

## Safety Requirements

1. **Preserve all existing functionality**:
   - QBO time posting must continue to work
   - Email linking must continue to work
   - All existing enrichment calls must remain

2. **No data loss**:
   - Every field from field_status.json must be preserved
   - Every calculated summary must be preserved
   - No format mismatches between what Claude sees and what's in the index

3. **Error handling**:
   - Wrap new code in try/except
   - Failures shouldn't break the notification pipeline
   - Log errors clearly for debugging

4. **Backward compatibility**:
   - Handle both `upload_type` and legacy `is_reupload` fields
   - Handle jobs with and without csv_extracted data
   - Handle missing optional fields gracefully

---

## Verification Checklist

Before saying you're done, verify:

### For Enrichment Fix:
- [ ] Field upload triggers full index enrichment
- [ ] Layout upload triggers full index enrichment
- [ ] field_summary is calculated and saved
- [ ] layout_summary for each controller job is saved
- [ ] All field_status.json fields are preserved
- [ ] All job_info.json fields are preserved
- [ ] csv_extracted data is complete (not truncated)
- [ ] upload_type handling is correct (complete/intermediate/reupload)
- [ ] QBO time posting still works
- [ ] No errors in logs

### For QBO Time Validation:
- [ ] Duplicate entries are detected
- [ ] Mismatched hours are flagged
- [ ] Notification is sent for significant discrepancies
- [ ] time_override is recorded when corrections are made
- [ ] Audit trail is complete

---

## Questions to Answer in Your Plan

1. What job(s) did you examine for field data? What did you find?

2. Is the enrichment bug confirmed in the current code?

3. What's the current state of QBO time posting? Any issues?

4. What exact changes do you propose for the enrichment fix?

5. What's your design for QBO time validation?

6. Are there any concerns or blockers?

7. What testing approach do you recommend?

---

## Summary

**Goal:** Make the job index a true "living document" that automatically stays current with all Data Sync events, and add safeguards for QBO time entry accuracy.

**Key principle:** The job index should contain the SAME complete data that Claude gets when calling `job_get_status`. Every field, every value, every structure - preserved exactly as designed.

**Remember:** Present your plan FIRST. Don't implement until I approve.
