# Hogwarts fleet avatars

Eleven SVG badges, one for each agent desk and script desk, drawn as one matching set. Each has transparent PNG exports at 512, 128 and 64 pixels in `png/<name>-<size>.png`.

Export PNGs with headless Chrome. qlmanage fills the corners with opaque white.

| File | Display name | Job | Emblem | Ring |
|---|---|---|---|---|
| mcgonagall.svg | McGonagall - Chief of Staff | Front desk on the frontier tier: writes TASK.md, routes work | Tabby cat head with square spectacles, stern emerald eyes and forehead tufts, on emerald tartan | Claude gold |
| harry.svg | Harry - Senior Engineer | Builds features and fixes on Codex | Round wire spectacles under a gold lightning bolt, on scarlet | Codex steel blue |
| hermione.svg | Hermione - Staff Engineer | Reviews Codex-written code and fact-checks bot review comments | Open book with a gold quill across the right page, on oxblood | Claude gold |
| ron.svg | Ron - Release Engineer | Sorts PR and CI changes, calls reds real or flaky, writes the morning lineup and weekly scoreboard | Staunton chess knight in profile, on Weasley orange | Claude gold |
| moody.svg | Moody - Security Reviewer | Read-only Codex review of Claude-written code, security first | Battered riveted pewter magical eye glancing aside, on a leather strap, on dark slate | Codex steel blue |
| snape.svg | Snape - Data Analyst | Read-only warehouse and observability reads with provenance | Corked Erlenmeyer flask two thirds full of green potion, on near-black with a green glow | Claude gold |
| portrait.svg | Dumbledore - Knowledge Manager | Weeknight review of the day that proposes memory changes | Gilded frame holding half-moon spectacles and a crescent moon, on a starry indigo field | Claude gold |
| map.svg | Marauder's Map - PR Watcher | Zero-token script that diffs PR and CI state | Two pairs of footprints on a dashed trail with a faint compass rose, on parchment | Script pewter |
| gringotts.svg | Gringotts - Backup | Nightly local backup script, with a restore drill | Diagonal gold vault key over three tilted coins, on deep bronze | Script pewter |
| owlpost.svg | Owl Post - Message Router | Script that moves owls between desks and stamps the sender | Sealed envelope with a red wax seal and an owl feather, on deep teal | Script pewter |
| ollivander.svg | Ollivander - Model Keeper | Daily script that keeps each desk on the model its role card asks for | Slim wand box with its lid slid half open and a wand across it throwing three sparks, on plum | Script pewter |

## Style rules

1. 512 viewBox, plain SVG 1.1 only (circle, rect, ellipse, line, polyline, polygon, path, g, gradients, clipPath), every shape with an explicit fill, no filters, masks, patterns, images, text, fonts, scripts or `<use>`, under 12KB.
2. Identical chrome: field `r=240` filled by radial gradient `field` (userSpaceOnUse, cx 176, cy 156, r 360, 8% white mix to base), then outer ring `r=240` at 16px and hairline `r=222` at 3px and 0.45, drawn last in the family colour (Claude `#D9A441`, Codex `#6F93D6`, script `#9AA0A6`).
3. Everything inside the field sits in `<g clip-path="url(#inner)">`, with `<clipPath id="inner"><circle cx="256" cy="256" r="222" fill="#FFFFFF"/></clipPath>`.
4. One emblem in a 300x300 box with its mass centroid within about 12px of 256,256: parchment `#F3E6C4` ink, `#C9B48A` shadow (`#A97D2C` on gilt), at most one character accent, 5 to 8 colours in total.
5. Strokes 16px primary and 8 to 9px detail with round caps and joins, nothing under 8px, overlaps separated with the field gradient rather than dark halos, and original iconography only: no crests, logos, faces, film fonts, letters or numbers.
