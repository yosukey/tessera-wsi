# Tour step Markdown reference

What a tour author may write in a step description, and exactly what the viewer
renders. Everything below is enforced by
[`frontend/src/components/SafeMarkdown.tsx`](../frontend/src/components/SafeMarkdown.tsx)
— the single component behind both the tour viewer and the admin preview.

Related documents:
- [`developer-guide.md` → Step editing feature](./developer-guide.md#step-editing-feature-admin) — the step CRUD API.
- [`architecture.md` → Storage layout](./architecture.md#storage-layout) — where tour assets live on disk.

## Where Markdown applies

| Field | Treated as | Shown in |
|---|---|---|
| Step `description_md` | **Markdown** | Tour viewer right panel; admin step list; the live preview in the step editor |
| Step `label` | Plain text, single line (control characters rejected, ≤ 255 chars); required to save a step from the step editor | Step heading above the description |
| Tour `title` / `description` | Plain text | Tour viewer header (title and description); tour index (title only) |
| Pin / shape `label` | Plain text, single line (control characters rejected, ≤ 255 chars) | On-canvas annotation labels |

Only `description_md` is parsed. Markdown typed into any other field is shown
verbatim, asterisks and all.

## How it is processed

```text
description_md → remark-parse → remark-gfm → remark-rehype → rehype-sanitize (allow-list) → React
```

Two consequences drive everything else on this page:

- **The sanitizer is an allow-list, not a blocklist.** An element that is not on
  the list is *unwrapped*: the tag disappears, its text survives. So unsupported
  syntax degrades to unstyled text rather than to an error.
- **Raw HTML is never rendered.** A block-level HTML tag takes its whole content
  with it — and any Markdown that follows it up to the next blank line; an
  inline one is dropped and leaves its text behind.

The admin editor's **Preview (sanitized)** pane runs the same component on the
same input, so what it shows is what the viewer gets.

## Supported syntax

| Feature | Write | Notes |
|---|---|---|
| Bold | `**bold**` | |
| Italic | `*italic*` or `_italic_` | |
| Bold + italic | `***both***` | |
| Inline code | `` `code()` `` | |
| Code block | A triple-backtick fence, optionally with a language tag | A `js` tag becomes `class="language-js"`; **no syntax highlighting is applied** |
| Bullet list | `- item` | Nest with a 2-space indent |
| Numbered list | `1. item` | Always renders starting at **1** — a `3.` start is not preserved |
| Blockquote | `> quoted` | Nestable |
| Horizontal rule | `---`, `***`, `___` | |
| Table | `\| a \| b \|` + `\|---\|---\|` | GFM pipe table; a header row is required |
| Link | `[text](https://example.com)` | Always opens in a new tab |
| Reference link | `[text][ref]` plus a `[ref]: https://example.com` line | Same as a link; the definition line is not shown |
| Autolink | Bare `https://…`, `www.…`, `me@example.com` | GFM; linkified automatically |
| Image | `![alt](/assets/tours/<tour_public_id>/file.png)` | Click to enlarge — see below |
| Line break | Two trailing spaces, or a trailing `\` | A single newline is just a space |
| Paragraph break | One blank line | |
| Escape | `\*not italic\*` | |

## Not supported — and what the reader sees instead

| You write | Rendered as | Why |
|---|---|---|
| `# Heading` … `###### Heading` | The text, **unstyled and with no spacing** | `h1`–`h6` are not on the allow-list. Put the step's title in the **Label** field (the viewer renders it as the panel heading), and use `**bold**` on its own line for sub-headings |
| `~~struck~~` | `struck` | `del` is not allowed |
| `- [ ] task` | An ordinary bullet | The checkbox `<input>` is removed |
| `<div>hi</div>`, `<img src=x onerror=…>` | Nothing at all — the inner text goes too | Block-level raw HTML is dropped whole |
| `one<br>two`, `<b>x</b>` | `onetwo`, `x` | Inline tags dropped, text kept |
| `<!-- note -->` | Nothing | But it is still **sent to the browser** — see the warning below |
| `text[^1]` footnotes | Links that open the same tour again in a new tab, plus an unstyled "Footnotes" line | Anchor `id`s are stripped, so the jump targets do not exist, and every link opens in a new tab |
| `\|:-:\|--:\|` alignment | Every cell left-aligned | Alignment is emitted as an `align` attribute, which is not on the allow-list |
| `![a](data:image/png;base64,…)` | An image with no source — most browsers show its alt text — that does nothing when clicked | `data:` is not an allowed image protocol |
| `[x](javascript:alert(1))` | Link-coloured text that does nothing when clicked | Only `http`, `https`, `mailto` and relative URLs survive |

> **`description_md` is public in full.** `GET /api/tours/{public_id}` returns
> the raw source, so HTML comments and any other "invisible" text reach every
> viewer's browser. Never park private notes in a step description.

## Images and tour assets

Upload images in the tour editor's **Tour images (assets)** panel, then use its
**Insert** button — it appends the ready-made `![name](url)` to the step being
edited. Copy names from that list rather than typing them: the server
canonicalises every uploaded filename (each run of characters outside
`[A-Za-z0-9._-]` becomes a single `_`, leading `.`, `_` and `-` are stripped, a name that lost non-ASCII characters gets a short
hash of the original appended, the name is capped at 200 bytes), so a name like
`標本 1.png` reaches disk in a different form (`1_<hash>.png`). Under the default
**rename** duplicate policy every upload also gets a random suffix
(`photo.png` → `photo-<8 hex>.png`), so re-uploading a file never replaces the
old one; **overwrite** keeps the canonical name and replaces an existing asset
of that name.

- **URL shape:** `/assets/tours/<tour_public_id>/<file>` — keyed by the tour's
  unguessable `public_id` token, never its numeric id.
- **Accepted content:** images only by default
  (`TOUR_ASSET_ALLOWED_MIME_PREFIXES=image/`), verified by magic bytes —
  PNG / JPEG / GIF / WebP / BMP. SVG is rejected outright (it can carry script).
  PDF, audio and video families are recognised by the validator but are accepted
  only where an operator has widened that setting; the upload panel still offers
  and previews images only.
- **Extension follows content:** the stored extension must belong to the detected
  family (it decides the served Content-Type). A mismatched upload such as a PNG
  named `scan.tiff` is saved as `scan-<suffix>.png` under the default
  "rename" policy, and refused with 415 under "overwrite". A file whose
  browser-reported type is outside the allowed prefixes (a PNG named
  `notes.html` arrives as `text/html`) is refused with 415 before its content
  is checked. Slide imports skip
  bundled assets that fail the same checks.
- **Quotas:** `TOUR_ASSET_MAX_BYTES` per file (2.5 MiB default; the editor
  shows the server's value, and `install.sh` raises nginx's body limit to fit
  a larger one),
  `TOUR_ASSET_MAX_FILES` (200) and `TOUR_ASSET_MAX_TOTAL_BYTES` (200 MiB) per tour.
  Across all tours, an upload is also refused (HTTP 507, "Not enough free disk
  space") once writing it would leave less than `TOUR_ASSET_MIN_FREE_BYTES`
  (1 GiB) free on the data volume — a new tour does not reset that.
- **In the viewer:** images render inline at their natural size, scaled down to fit the panel
  width; clicking one (or Enter
  / Space when focused) opens a full-viewport lightbox, dismissed with Esc, the
  close button, or a backdrop click. An image wrapped in a link —
  `[![alt](img)](url)` — is deliberately *not* zoomable: the click follows the link.
  A title — `![alt](url "caption")` — replaces the default "Click to enlarge" tooltip.
- **Remote images** (`https://…`, or protocol-relative `//host/x.png`) survive the
  sanitiser but **do not display** under the shipped nginx config: its
  Content-Security-Policy `img-src` allows only this origin, `data:`, `blob:`
  and the Google Analytics hosts used by the optional GA4 tag, so the browser
  blocks any other third-party image and the reader sees a broken image. Upload
  the image as a tour asset instead. (Adding an origin to `img-src` in
  `infra/nginx/wsi.conf` would allow it, at the cost of a third-party request on
  every view.)
- Deleting or overwriting an asset silently changes every step that references
  it. The URL is the only link between them.

## Links

Every rendered link gets `target="_blank"` and `rel="noreferrer noopener"`
forced on — a destination can neither reach back into the tour window via
`window.opener` nor read the tour URL from the `Referer` header. Relative in-app
targets such as `[this slide](/slides/abc123)` work and open in a new tab like
any other link.

## Styling notes

Tables (GitHub-style: a wide one scrolls horizontally inside its own box rather
than stretching the panel), table borders and horizontal rules are styled by
`SafeMarkdown.module.css`, so they look identical in the viewer and the admin
preview. Line height, paragraph spacing, list indentation and code/`pre`
backgrounds come from the page-level `.jp-prose` rules in `global.css`, which
wrap **only the tour viewer** — code blocks therefore look plainer in the admin
preview than they do in the finished tour.

## Limits

- **65,536 characters** per step description (`_MAX_STEP_DESCRIPTION_LEN` in
  `services/app/wsi_app/schemas.py`). The editor's textarea caps input at the
  same number; the API answers 422 above it.
- NUL bytes are rejected. Tabs, newlines and carriage returns are fine.

## Changing what is allowed

The allow-list is the `safeSchema` object at the top of `SafeMarkdown.tsx`:
`tagNames` (elements), `attributes` (per-element attributes — note that
`style` and arbitrary `class` are deliberately absent, so authored Markdown
cannot adopt app CSS) and `protocols` (URL schemes). Widening any of them is a
security decision; the rationale for each current restriction is in the comments
beside it.

## Example

```markdown
**Low-power view.** Note the lobular architecture before zooming in.

- Interlobular septa are thickened
- Ducts run with the vessels

![H&E, 2x](/assets/tours/aB3xQ9/overview.png)

> Compare with the [normal reference slide](/slides/abc123) before moving on.

| Structure | Stain | Appearance |
|---|---|---|
| Collagen | H&E | Pale pink |
| Nuclei | H&E | Blue-purple |
```
