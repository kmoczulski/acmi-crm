# ACMI CRM: Design Spec (from Attio walkthrough, 2026-10-13)

Source: observed behaviour of an Attio trial workspace (`acmi-properties`) through
its UI and the requests its web app makes. No Attio code was copied. This
document records *how Attio models things* and *what ACMI's version does*.

---

## 1. Data model

### What Attio does

Everything is a generic **object** with typed **attributes**. Records are
instances of an object. The API names make this explicit:

| Attio term | Meaning | ACMI equivalent |
|---|---|---|
| entity-definition | an object type (Companies, People, Deals, Tasks) | `Model` per object |
| attribute-definition | a field on an object | model field, or custom field |
| entity-instance | one record | a row |
| collection / list | a curated set of records with its *own* extra fields | `List` + `ListEntry` |

Attribute definitions carry flags that matter for the design:

- `type`: text, number, currency, date, timestamp, select, domain,
  email-address, phone-number, location, personal-name, entity-reference
  (link to another record), actor-reference (link to a user), interaction,
  pipeline (stage).
- `is_multiselect`: field holds a list of values (domains, emails, categories).
- `is_unique`: dedupe key (company domains, person emails).
- `is_historic`: every change is kept with a timestamp (deal stage, deal value,
  funding raised). That gives "time in stage" and stage-change reports for free.
- `system_attribute`: built-in field the app maintains itself.
- `relationship`: two-way links. Setting `person.company` also updates
  `company.team`.

### Standard objects observed

**Companies:** domains[] (unique), name, description, team (people[]),
categories[], primary_location, logo, social links, estimated ARR,
funding_raised (historic), foundation_date, employee_range, and computed
interaction fields (below), next_due_task, created_at/by.

**People:** name, email_addresses[] (unique), company, job_title,
phone_numbers[], location, social links, computed interaction fields,
next_due_task, created_at/by.

**Deals:** name, stage (pipeline, historic), owner (user), value (currency,
historic), associated_company, associated_people[], next_due_task.

**Tasks, Notes, Emails, Meetings, Calls:** activity objects linked to records.

### Computed "interaction" fields (high value)

Attio maintains these on every company and person automatically from synced
email and calendar data:

`first_interaction`, `last_interaction`, `next_interaction`,
`first/last_email_interaction`, `first/last/next_calendar_interaction`,
`strongest_connection_strength` (Weak/Medium/Strong) and
`strongest_connection_user` (which teammate knows them best).

This replaces the workbook's `# Touches`, `Last Outreach`, `Days Since Touch`
and `Last Response` columns, which today are maintained by scripts.

### ACMI's version

Use concrete Django models rather than a fully generic attribute engine. ACMI
knows its objects up front, and concrete tables are simpler to query and
maintain. Keep a JSON `custom_fields` column per object for ad-hoc fields.

```
Company        name, domains[], description, sector, categories[], priority
               (High/Mid/Low), fit_score, status (pipeline stage), owner,
               hq_location, website, linkedin, flags (Exclude, etc.),
               notes, custom_fields, + computed interaction fields
Person         name, emails[], phones[], title, company, linkedin,
               email_valid, flags, custom_fields, + computed interaction fields
Opportunity    company, campus (NSIH/MEIH/other), stage (historic), owner,
               sq_ft_need, acreage_need, power_need, timeline, value,
               associated_people[]          <- Attio's "Deal", ACMI-shaped
Campus         NSIH, MEIH, future campuses (location, acreage, status)
Site/Parcel    (phase 4) parcel id, geometry, county, state, owner,
               acreage, zoning, scores, linked opportunities
List/ListEntry curated sets with per-list stage + fields
FieldHistory   object, record, field, old, new, changed_at, changed_by
```

`FieldHistory` is how we copy `is_historic`: write a row whenever a tracked
field (stage, value, priority, owner) changes.

---

## 2. Automatic record creation and enrichment

### What Attio does

On signup it synced the mailbox and calendar (thousands of events) and
**created a company for every external email domain** it saw (324 within
minutes), plus people for the participants. Records created this way show
`created_by: system / automatic-records`. It then **enriched** each company:
description, country, founding year, social links, funding.

### ACMI's version

- **Import first**: `Master_Contacts` (2,670 rows), `Outreach_Log` (~20k),
  `Email_Templates`, `News_Outreach_Queue`, `News_Review_Log` from
  `Desktop\Prospecting\ACMI_Outreach_Engine.xlsx`.
- **Auto-create from Gmail** (phase 2): new external domains in sent and
  received mail become companies, gated by a blocklist (law firms, vendors,
  gmail.com, etc.). Attio's list above already includes noise like
  `thegreerlawfirm.com` and Google, so a review queue beats silent creation.
- **Enrichment**: through Clay, which ACMI already uses, rather than building
  our own.

---

## 3. Record page

### What Attio does

- **Left column**: record details (editable fields), "View all values", and
  **Lists** this record belongs to with an "Add to list" button.
- **Right column tabs**: Overview, Activity, Emails, Calls, plus others.
- **Overview** shows *highlight cards* (connection strength, next calendar
  interaction, team, ARR, funding, employee range), a short activity feed,
  recent emails, notes and tasks.
- Header actions: compose email, favourite, and quick-add note, task and list.
- Prev/next arrows step through the list you came from ("3 of 324").

### ACMI's version

Same layout. Highlight cards for ACMI: **Stage**, **Owner**, **Last touch /
days since**, **Next meeting**, **Fit score**, **Campus interest**, **Open
tasks**, **Latest news trigger**.

---

## 4. Timeline

### What Attio does

One endpoint returns a unified event feed for a record, paged by time, in two
calls: past (`direction: backwards`, limit 100) and upcoming (`forwards`).
Each event has:

```
event_type   entity-instance-created | attribute-values-updated |
             first-contact | meeting | email | note | task | call ...
started_at   timestamp
actor        {type: workspace-member | system, id}
is_background  true for low-signal events (field edits, auto-creation),
               hidden unless the user expands them
ref          pointer to the source (meeting_id, email id, interaction id)
```

### ACMI's version

One `Activity` table, the single source for every timeline:

```
Activity  id, occurred_at, kind (email_sent, email_received, reply, bounce,
          meeting, granola_note, call, note, task, stage_change, field_change,
          news_trigger, record_created), actor (user|system), company,
          person (nullable), opportunity (nullable), is_background,
          summary, body, source (gmail|granola|import|news_scan|user),
          source_id (for dedupe)
```

The computed interaction fields (section 1) are derived from this table.
`source + source_id` must be unique, so Gmail and Granola re-syncs never
duplicate entries.

---

## 5. Lists and views

### What Attio does

- **Saved views** per object: sort ("Sorted by Last interaction"), filters,
  visible columns, and summary calculations at the bottom of each column.
- **Lists** are curated sets of records (from one object) with list-only
  fields such as a stage, shown as a table or a kanban board. A company can be
  in many lists, each with a different stage.
- List templates are grouped by use case: Investing, Recruiting, Fundraising,
  Operations and others.

### ACMI's version

- Saved views on Companies and People: filters (priority, sector, owner,
  stage, days since touch, flag), sort and columns, saved per user or shared.
- Lists with per-entry stage and kanban: e.g. "NSIH Tenant Pipeline",
  "MEIH Energetics Targets", "Weekly News Approvals", "Broker Referrals".

---

## 6. Email, sequences, tasks, workflows

### What Attio does

- Each person connects a mailbox, and emails and meetings sync to records. There
  are visibility controls ("email grants") and **protected recipients**, i.e.
  addresses never synced or shown.
- Compose email from a record.
- **Sequences**: multi-step email cadences (empty in a fresh trial).
- **Workflows**: trigger → action automations (empty in a fresh trial).
- **Tasks** link to records. `next_due_task` is shown on each record.
- An AI assistant on Home ("Create a task for me to…", "Prep for next meeting").

### ACMI's version

- Per-user Gmail with Google sign-in (internal Workspace app). Replaces the
  shared app password.
- Templates, the send queue and priority tiers from `send_emails_v4.py`, and
  reply/bounce detection from `scan_responses.py`, write `Activity` rows.
- Protected-recipient list (internal ACMI domains, personal contacts).
- Simple sequences: step N sent after X days if no reply.
- Follow-up queues: "no reply in N days" and "met N days ago, nothing since".
- Tasks: from users, from Granola action items, and from rules.
- Defer a general workflow builder. Hard-code ACMI's few real rules first.

---

## 7. Features with no Attio equivalent (ACMI-specific)

- News triggers: the weekly prospect scan becomes `Activity(kind=news_trigger)`
  on the matched company, and the news review screen moves into the app.
- Opportunities tied to campuses (NSIH/MEIH) with space, acreage and power needs.
- Granola meetings matched by attendee email to people, then companies.
- Land search (TX, MD): parcels, overlays, buy-box scoring, map, "Pursue".

---

## 8. Phase 1 scope (build now)

1. Django project, Postgres-ready (SQLite locally), Google sign-in restricted
   to `acmigroup.com` (local dev uses a username login).
2. Models: Company, Person, Opportunity, Campus, Activity, Task, Note, List,
   ListEntry, SavedView, FieldHistory.
3. Workbook importer (idempotent, re-runnable, dry-run by default).
4. Screens: Companies table with filters and saved views, Company record page
   (layout from section 3), People table and page, Opportunity kanban by stage,
   Tasks.
5. Computed interaction fields derived from `Activity`.
