# Channel Studio — optional custom snippets

Two OPTIONAL personalization files live here. The Channel Studio page works
fully without them; they are presentation/notification only and can never
bypass the plugin's draft + explicit Apply flow.

| File | Where it goes | What it does |
| --- | --- | --- |
| `channel-studio-custom.css` | Stash → **Settings → Interface → Custom CSS** | Presentational overrides against the documented `.jw-*` selectors |
| `channel-studio-custom.js` | Stash → **Settings → Interface → Custom JavaScript** | Demonstrates the versioned `window.JWChannelStudio` hook (view events + announcements; no mutation surface) |

## Install (preserving your existing snippets)

1. Open Stash → **Settings → Interface**.
2. Scroll to **Custom CSS** (for the `.css` file) or **Custom JavaScript**
   (for the `.js` file). The labels are exactly these on current Stash —
   verified on the dev server.
3. If the box already has content, **append** — put your cursor at the very
   end, paste the file's contents below what is already there. Do **not**
   replace your existing snippets.
4. Save, then reload the Channel Studio page.

## Remove

1. Open the same box (Settings → Interface → Custom CSS / Custom JavaScript).
2. Delete exactly the lines you pasted (each file starts and ends with a
   clear comment banner, so the block is easy to find). Leave everything
   else untouched.
3. Save and reload. The plugin's built-in styles and behavior take over
   unchanged — there is nothing else to clean up. The JS hook also runs
   each extension's cleanup automatically when you leave the Channel
   Studio route or when Stash re-injects the snippet.

## Safety contract (what these snippets may and may not do)

- The CSS is scoped to `.jw-studio` and the plugin's own dialog roots
  (`.jw-sheet-organize`, `.jw-sheet-numres`). Do not widen the scope —
  restyling global Stash/Bootstrap classes is unsupported.
- Never change `.jw-dial-row` / `.jw-group-header` heights or paddings: the
  dial is virtualized at exactly 44px per row.
- The JS hook (`window.JWChannelStudio`, version 1) offers `on(event, cb)`
  view notifications (`staged`, `applied`, `selection`), a read-only
  `getViewState()`, and `announce(text)`. There is intentionally **no**
  mutation API: snippets cannot write the library, cannot trigger Apply,
  and cannot touch your drafts.
- Snippets must stay dependency-free (no second React, no external
  libraries, no React/PluginApi internals, no direct storage writes, no
  synthetic clicking).
- `registerExtension(name, …)` is idempotent per name and every `setup`
  must return a cleanup function; baseline functionality works with the
  snippets absent or removed.
