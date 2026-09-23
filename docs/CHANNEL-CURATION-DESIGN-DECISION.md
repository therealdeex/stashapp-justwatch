# Channel curation — design decision (owner feedback)

Status: **PENDING OWNER FEEDBACK** — the five prototypes are live; this file
is recorded only when the owner actually responds. Per the implementation
assignment, the production frontend is NOT chosen by elapsed time, silence,
or implementer preference.

## The five prototypes (reviewable now)

Served by `python3 tools/serve_prototypes.py` (repo root) on the fixed port
**8462** — <http://localhost:8462/>, published over tailscale at
<https://dev-lab2.manx-teeth.ts.net:8462/>. Restart after a reboot with the
same command; stop publishing with `tailscale serve --https=8462 off`.

| Variant | URL | Workflow in one line |
| --- | --- | --- |
| A — Channel Studio | `/studio/` | searchable dial + focused side-by-side editor |
| B — Group Organizer | `/organizer/` | group-first cards, drag/bulk moves, group manager |
| C — Channel Library | `/library/` | dense filterable table, cross-filter bulk ops, CSV export |
| D — Visual Guide | `/guide/` | EPG browsing, contextual editing without losing your place |
| E — Guided Curator | `/curator/` | progressive steps ending in a review-then-Apply |

All five share the mock data (the real 513-network v4-final proposal plus
synthetic customs/fixtures), the same Apply/receipt semantics, and support
the full review script (search, rename, one-group assignment, rule editing,
simulated pool-vs-rotation preview, Apply/Discard, drafts that survive
navigation, bulk moves into new groups, recoverable error scenarios,
keyboard operation, narrow layout).

## Owner response

*(to be recorded verbatim after review — chosen option or combination,
requested changes, friction points, acceptance observations)*
