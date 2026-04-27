# Meshonator UX Redesign Implementation Plan

## Purpose

This document turns the Meshonator redesign direction into a concrete implementation blueprint for the current FastAPI + Jinja2 application. It is based on the existing route structure in `src/meshonator/api/app.py` and the current templates under `src/meshonator/templates/`.

The intent is to reduce operator cognitive load, separate abstraction layers cleanly, and introduce a safer operations model without treating the product as a greenfield rewrite.

## Current UX Diagnosis

### Structural problems in the current UI

- `Dashboard` mixes fleet health, workflow launchers, jobs, provider state, and inventory metrics in one surface.
- `Nodes` is simultaneously an inventory table, compare tool, bulk config tool, fleet baseline editor, and patch executor.
- `Groups` mixes manual assignment, dynamic filters, desired templates, and rollout actions inside one repeated card pattern.
- `Jobs` is a flat table of execution records rather than an operator-facing operations model.
- `Settings` mixes provider integrations with what should become reusable configuration profiles.

### UX problems visible in current templates

- JSON editing is the dominant path in `nodes.html`, `groups.html`, and `settings.html`.
- Bulk actions are form-heavy and detached from selection context.
- There is no mandatory preview or semantic diff before changes are applied.
- The system has weak action feedback: operators see queueing and logs, but not a reliable intent -> plan -> verify -> rollback flow.
- The app uses weak intent labels such as `Keep current`, which hide desired state instead of expressing it.

## Target Product Model

Meshonator should use four primary mental models:

1. `Monitor`: understand fleet posture and decide what needs action now.
2. `Inventory`: inspect and target nodes.
3. `Configure`: define desired state as reusable profiles.
4. `Execute`: preview, launch, verify, and roll back changes.

These are the product-level boundaries the UI should expose consistently.

## Target Information Architecture

### Primary navigation

1. `Dashboard`
2. `Nodes`
3. `Groups`
4. `Profiles`
5. `Operations`
6. `Audit`
7. `Settings`

### Responsibility split

#### Dashboard

- Only actionable insight and system readiness.
- No raw JSON editing.
- No generic metrics wall.
- No full inventory table.

#### Nodes

- Inventory table.
- Saved filters and bulk selection.
- Node detail and compare.
- Launch point for bulk operations, but not the operation editor itself.

#### Groups

- Static groups.
- Dynamic group rule builder.
- Membership preview.
- Group-based targeting.

#### Profiles

- Structured config templates.
- Baselines by device role/use case.
- Advanced raw mode as optional fallback only.

#### Operations

- Preview.
- Semantic diff.
- Approval.
- Execution pipeline.
- Verification.
- Rollback.

#### Audit

- Immutable historical record.
- Who/what/when/why.

#### Settings

- Provider configuration.
- Worker state and system integrations.
- Authentication and environment-level defaults.

## Target Route Map

This route map is intentionally aligned to the existing FastAPI structure so implementation can be incremental.

### Keep and repurpose existing routes

- `/` -> `Dashboard` as action-only monitor surface.
- `/nodes` -> `Nodes` inventory workspace.
- `/nodes/{node_id}` -> node detail workspace.
- `/groups` -> `Groups` workspace.
- `/audit` -> keep as audit surface.
- `/settings` -> narrow to system settings and integrations.

### Introduce new routes

- `/profiles` -> list reusable configuration profiles.
- `/profiles/{profile_id}` -> profile editor and version history.
- `/operations` -> operations board and pipeline view.
- `/operations/{operation_id}` -> operation detail, diff, verification, rollback.
- `/groups/{group_id}` -> dedicated group detail page with rule builder and membership preview.
- `/nodes/compare` -> optional dedicated compare page if compare complexity keeps growing.

### Decompose current action routes into clearer domains

Current routes such as:

- `/ui/nodes/batch-configure`
- `/ui/nodes/{node_id}/configure`
- `/ui/groups/{group_id}/apply-template`
- `/ui/settings/defaults/apply`

should converge into a single operations creation path conceptually:

- `/ui/operations/create-from-nodes`
- `/ui/operations/create-from-group`
- `/ui/operations/{operation_id}/preview`
- `/ui/operations/{operation_id}/execute`
- `/ui/operations/{operation_id}/rollback`

The backend services can stay where they are initially; the important change is to stop exposing execution mechanics directly inside inventory/config pages.

## Screen-Level Specifications

## 1. Dashboard

### Purpose

Answer only these questions:

1. What needs attention now?
2. What is currently running?
3. Is the system safe to operate?
4. What is the next recommended action?

### Proposed layout

#### Row 1: system posture strip

- `Fleet health`
- `Drift detected`
- `Operations in progress`
- `Provider / worker readiness`

Each item should be a compact status summary, not a card with secondary content.

#### Row 2: priority queue

Action cards such as:

- `7 nodes stale > 12h`
- `1 rollout failed verification`
- `14 nodes drift from profile`
- `Discovery older than 24h`

Each card needs:

- problem statement
- affected scope
- severity
- primary action
- inspect action

#### Row 3: live operations

- Show active operations with stage progress.
- Replace flat recent jobs emphasis with pipeline state.

#### Row 4: system readiness

- worker status
- provider connectivity
- queue backlog
- last successful sync/discovery

### Remove from dashboard

- large metric walls
- full job table
- full audit feed
- config forms
- mixed-level bulk actions

### Implementation mapping

- Replace the current `dashboard.html` with a leaner structure.
- Keep the route handler in `/` but reduce its data model to `priority items`, `active operations`, `drift summary`, `system readiness`, and `recommended actions`.
- Move deep activity detail to `/operations` and `/audit`.

## 2. Nodes

### Purpose

This becomes the main operational workspace for targeting and inspecting devices.

### Proposed layout

#### Top bar

- page title
- saved views
- search
- quick filters
- selection summary when rows are selected

#### Main region

- dense inventory table
- optional right-side node detail drawer

#### Bottom or side utilities

- compare selected nodes
- open operation preview

### Node table design

Recommended columns:

- selection
- health status
- short name
- provider
- role
- hardware model
- firmware
- drift state
- group membership
- last seen
- pending operation state

### Required UX patterns

- sticky bulk action bar only after selection
- saved views: `Needs attention`, `Offline`, `Drifted`, `Pending verification`, `Recently changed`
- filter chips instead of long forms
- no raw bulk JSON editing in the default view
- side drawer for inspection rather than repeated navigation

### Bulk operations model

Bulk actions from nodes should become:

- `Apply profile`
- `Change group membership`
- `Run sync`
- `Run discovery for selected scope`
- `Mark favorites policy`
- `Open preview`

None of these should execute immediately. They should open an operation draft.

### Implementation mapping

- Refactor `nodes.html` into:
  - table workspace
  - compare panel
  - selection toolbar
- Move `batch configure`, `favorites`, `NodeDB favorites`, and compare synchronization forms out of the default page body and into drawers/modals or operation drafts.
- Keep current backend endpoints temporarily, but wrap them behind a new `preview first` UI.

## 3. Node Detail

### Purpose

Inspect one node cleanly without forcing the operator into raw patch editing first.

### Proposed sections

- summary
- connectivity and endpoints
- current config state
- drift against assigned profile
- recent operations on this node
- advanced raw config view

### Editing model

- default mode: structured sections
- optional advanced mode: raw JSON
- every edit leads to preview/diff before execute

### Implementation mapping

- Evolve `node_detail.html` into a 2-column or drawer-based layout.
- Reuse the existing structured configure route as the default editing path.
- Keep raw JSON patch under an `Advanced` accordion or tab.

## 4. Groups

### Purpose

Groups should become safe targeting assets, not JSON containers.

### Group types

- `Static group`: explicit membership.
- `Dynamic group`: rule-based membership.

### Proposed layout

#### Groups index

- list of groups
- type badge
- member count
- last operation
- drift count

#### Group detail

- summary
- rule builder or membership list
- live membership preview
- linked profiles
- linked operations

### Dynamic rule builder

Supported conditions should cover:

- provider
- role
- firmware version
- reachable state
- favorite state
- hardware model
- location presence
- last seen age
- existing group membership

### Replace current UX

Current `dynamic_filter_json` and `desired_template_json` fields in `groups.html` should become:

- rule builder UI
- attached profile selection
- optional advanced JSON view for edge cases

### Implementation mapping

- Split `groups.html` into index + detail patterns.
- Keep existing group persistence model initially.
- Add server-side translation between rule-builder form input and current stored dynamic filter JSON until the data model is normalized.

## 5. Profiles

### Purpose

Profiles represent desired configuration state. They replace raw settings/baseline editing as the default operator experience.

### Profile types to support first

- `Router baseline`
- `Client baseline`
- `Fixed gateway`
- `Telemetry-light profile`
- `Field unit profile`

### Structured profile sections

- identity and role
- radio and transport
- channels
- telemetry
- position and GPS
- network integrations
- advanced

### Interaction rules

- use intent labels: `No change`, `Set`, `Override`, `Inherited`
- provide inline validation
- show affected fields count per section
- always support preview of profile application to a target scope

### Implementation mapping

- Extract the reusable baseline editing currently buried in `settings.html` into `/profiles`.
- Reuse the existing structured patch generation logic from the API layer where possible.
- `settings.html` should stop being the place where operators define fleet-wide desired config.

## 6. Operations

### Purpose

This becomes the core execution model for all risky or fleet-wide changes.

### Replace `Jobs` with `Operations`

Operators should think in terms of an operation lifecycle:

1. Draft
2. Scope validated
3. Diff reviewed
4. Approved
5. Queued
6. Applying
7. Verifying
8. Completed / Partial / Failed / Rolled back

Jobs remain system artifacts inside an operation.

### Operations index

Default view should be a pipeline board with columns:

- `Draft`
- `Ready`
- `Running`
- `Verifying`
- `Needs attention`
- `Completed`

Secondary view can be a table for power users.

### Operation detail page

Must contain:

- operator intent
- target scope
- semantic diff
- warnings and preflight results
- per-node progress
- result summary
- rollback availability
- audit trail

### Safe operations requirements

- no direct apply from edit forms
- mandatory preview
- blast radius summary
- semantic diff grouped by config section
- verification stage
- rollback snapshot

### Implementation mapping

- Transform `jobs.html` from a flat job table into an operations-first view.
- Keep current `JobModel` initially, but introduce an `operation view model` in the UI layer even before a DB-level `OperationModel` exists.
- Aggregate related jobs and job results into a single operation card/detail view.

## 7. Settings

### Purpose

Only system and integration concerns belong here.

### Keep in Settings

- providers and capability visibility
- worker mode and heartbeat
- auth and roles
- environment defaults
- integration health

### Remove from Settings

- reusable device config baselines
- large operator-facing rollout actions

### Implementation mapping

- Narrow `settings.html` to provider/integration administration.
- Move default Meshtastic config editing out to `/profiles`.

## Core UX Patterns

## Tables

- sticky headers
- density presets
- column visibility presets
- saved views
- bulk selection bar only when selection exists
- status chips rather than prose

## Forms

- sectioned layout
- progressive disclosure
- structured fields by default
- advanced raw mode separated visually and semantically
- inline validation
- no repeated `Keep current` labels

Use these labels instead:

- `No change`
- `Set to profile value`
- `Override`
- `Inherited`

## Actions

- primary action requires preview
- destructive or wide-scope action requires blast radius summary
- operations should show immediate post-submit state: `draft created`, `queued`, `verifying`, `failed`, `rollback available`

## Diff Visualization

Three levels are required:

1. summary: nodes, sections changed, warnings
2. section diff: channels, telemetry, position, role, network
3. field diff: semantic before/after changes

Raw JSON diff belongs under `Advanced details`, not as the default view.

## Terminology System

Use the following terms consistently:

- `Fleet`
- `Node`
- `Group`
- `Profile`
- `Operation`
- `Job`
- `Drift`
- `Snapshot`
- `Verification`

Avoid mixing:

- task / job / operation
- template / baseline / defaults for the same concept
- keep current / unchanged / inherit as if they meant the same thing

Recommended meaning:

- `Profile` = reusable desired-state definition
- `Operation` = operator intent and execution container
- `Job` = lower-level execution record inside an operation

## Example User Flows

## Flow A: Bulk telemetry change

1. Open `Profiles`.
2. Edit `Router baseline`.
3. Change telemetry interval in structured form.
4. Click `Preview rollout`.
5. Review scope and semantic diff.
6. Create operation.
7. Execute.
8. Watch verification and rollback availability in `Operations`.

## Flow B: Triage stale nodes

1. Open `Dashboard`.
2. Click `7 nodes stale > 12h`.
3. Land in `Nodes` with a saved filtered view.
4. Select affected nodes.
5. Launch `Run sync` or `Open operation preview`.
6. Track progress in `Operations`.

## Flow C: Create rollout target group

1. Open `Groups`.
2. Create dynamic group with rule builder.
3. Preview matching nodes.
4. Save group.
5. Use the group in a profile rollout.

## Component Inventory

These components should become reusable across pages.

### New UI components

- `ActionQueueCard`
- `StatusStrip`
- `SavedViewTabs`
- `BulkActionBar`
- `NodeTable`
- `NodeDetailDrawer`
- `RuleBuilder`
- `ProfileSectionForm`
- `OperationPipelineBoard`
- `OperationTimeline`
- `SemanticDiffPanel`
- `ScopeSummary`
- `VerificationBadge`
- `RollbackPanel`

### Mapping to current templates and views

- `dashboard.html`
  - future: `StatusStrip`, `ActionQueueCard`, `OperationPipelineBoard`, `ScopeSummary`
- `nodes.html`
  - future: `SavedViewTabs`, `NodeTable`, `BulkActionBar`, `NodeDetailDrawer`, `SemanticDiffPanel`
- `node_detail.html`
  - future: `ProfileSectionForm`, `SemanticDiffPanel`, `OperationTimeline`
- `groups.html`
  - future: `RuleBuilder`, `ScopeSummary`, `SavedViewTabs`
- `jobs.html`
  - future: `OperationPipelineBoard`, `OperationTimeline`, `RollbackPanel`
- `settings.html`
  - future: provider admin and worker health components only

## Phased Implementation Plan

## Phase 1: IA and navigation cleanup

- update top navigation in `base.html`
- add placeholders/routes for `Profiles` and `Operations`
- narrow dashboard scope
- stop adding more operator controls to dashboard

## Phase 2: Nodes workspace refactor

- redesign node table and selection UX
- replace inline bulk JSON editing with operation draft entry points
- keep compare, but move advanced sync operations out of the table page body

## Phase 3: Profiles extraction

- move reusable config editing from `settings.html` into `/profiles`
- define structured profile sections
- preserve advanced raw mode as fallback

## Phase 4: Operations model

- introduce operations-first UI on top of existing jobs
- build preview, semantic diff, and verification screens
- expose rollback state

## Phase 5: Groups builder

- replace JSON filter editing with rule-builder UI
- add membership preview and rule explanation

## Phase 6: Backend normalization

- introduce explicit `Operation` domain model if needed
- normalize profile storage
- reduce UI coupling to raw JSON payloads

## Delivery Guidance for the Current Codebase

### What should not be done next

- do not continue decorating the dashboard with more metrics
- do not add more JSON editors to `nodes.html` or `groups.html`
- do not make `jobs.html` richer without changing the underlying operator model

### What should be done next

1. Add `/profiles` and `/operations` as first-class destinations.
2. Move baseline/default config editing out of `settings.html`.
3. Convert direct execute forms into preview-backed operation drafts.
4. Refactor `nodes.html` into an inventory-first table with bulk action UX.
5. Rebuild `groups.html` around rule building and membership preview.

## Recommended First Implementation Slice

The smallest high-value slice is:

1. Add `Profiles` and `Operations` routes and templates.
2. Move current `Default Settings` form from `settings.html` into `profiles.html` as a structured baseline editor.
3. Change existing bulk configure/apply actions so they create a preview screen before queueing execution.
4. Reduce dashboard to priority queue + readiness + live operations summary.

This sequence gives the product a clearer mental model quickly without requiring a full rewrite of services or persistence.