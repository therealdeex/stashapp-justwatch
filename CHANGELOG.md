# Changelog

## 0.2.0 (2026-09-05)
Published programming plus the reliability pass from the 2026-09-04 integration
audit (docs/AUDIT-2026-09-04.md). Contract v1 unchanged; all response additions
are additive fields/ops.

- Published programming: a channel can air a published 72-hour schedule instead
  of a raw rotation — fixed/explore/discovery modes, soft spacing, a repeat
  cooldown, and weekly studio/performer spotlight blocks. The `PrepareProgramming`
  task builds each publication (bounded index paging, only changed channels
  rebuilt); `Schedule`/`PreviewProgramming`/`ProgrammingDesk` serve it to the TV
  and the Channel Studio trial UI; `tools/systemd/` ships an hourly timer to
  keep schedules fresh.
- Autosave: one save coordinator for every edit (create/edit/relink/renumber/
  delete/settings). Saves submit against the last server-acknowledged revision, so
  edits made while a save is in flight serialize instead of conflicting; an external
  revision conflict keeps the local draft and offers reload vs deliberate overwrite.
- Save results: stored per requestId (retention-capped) instead of one overwritten
  file, job polling is bounded, request ids are UUIDs, and every handled save
  failure — including internal errors — publishes a correlated result. Failed saves
  leave a kept draft + Retry action.
- Catalog: stored files load strictly — broken shapes, future schema versions, or
  invalid channel records raise CatalogError instead of silently normalizing away
  (and being wiped by the next save). Draft validation reports malformed fields
  (numeric name, string source, null tag lists) as structured errors.
- Saves: the revision check + write now run under a process-safe catalog lock with
  unique temp files; concurrent saves yield one success and one revision_conflict.
- Seeds: existing channels keep their stored seed through SaveCatalog (changed or
  omitted drafts cannot reseed); only new channels take a fresh seed.
- Rotation policy (documented in Capabilities `features.rotation`): a channel airs a
  bounded, ordered, playable loop (size 50, scan ≤ 1000 raw rows) assembled
  server-side. Lineup, PreviewLineup, and health counts/loop lengths all derive from
  that same rotation; `rotationVersion` (ordering hash + catalog revision) lets
  clients drop stale lineups.
- Saved searches: the saved filter's text search (`q`) is now carried through
  Lineup, PreviewLineup, and health — a text-only saved search no longer becomes an
  unrestricted lineup.
- Cookie auth: reads the server's `SessionCookie.Name`/`Value` fields (Go
  http.Cookie marshaling) instead of lowercase `value`.
- Health: per-channel failure boundaries; distinguishes missing source (relink),
  off air, and temporarily unavailable (stale numbers kept, flagged); sources of
  every type are existence-checked; snapshot publication is revision-aware (older
  computations never replace newer).
- Settings honesty: the Tuning sheet now states these thresholds shape THIS page's
  automatic sections only — the TV tunes from its own settings — and the launch-mode
  control was removed from the web.
- Auto-tier numbering: ordinary studios/performers stop at base+97 so the spillover
  pin (399/499) never collides at larger directory sizes.

## 0.1.0
- Initial release: v1 contract, catalog + health snapshots, Channel Studio page, custom channels 1-99.
