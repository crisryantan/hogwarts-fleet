# Hogwarts fleet avatars

Eleven SVG badges, one for each agent desk and script desk, drawn as one matching set. All eleven share one chibi style with thick dark outlines: the eight character desks are portraits drawn from the books' descriptions of each character, never from any actor, Gringotts is a goblin, Owl Post a snowy owl, and the Marauder's Map an outlined folded map. Each has transparent PNG exports at 512, 128 and 64 pixels in `png/<name>-<size>.png`.

Export PNGs with headless Chrome. qlmanage fills the corners with opaque white.

| File | Display name | Job | Emblem | Ring |
|---|---|---|---|---|
| mcgonagall.svg | McGonagall - Chief of Staff | Front desk on the frontier tier: writes TASK.md, routes work | Chibi portrait: tall pointed black hat with an emerald band, grey hair in a side bun, square spectacles, a stern straight mouth, emerald robe, on emerald | Claude gold |
| harry.svg | Harry - Senior Engineer | Builds features and fixes on Codex | Chibi portrait: messy black hair, round spectacles, green eyes, a lightning-bolt scar, black robe and a maroon and gold scarf, on scarlet | Codex steel blue |
| hermione.svg | Hermione - Staff Engineer | Reviews Codex-written code and fact-checks bot review comments | Chibi portrait: big bushy brown curls, brown eyes, rosy cheeks, black robe and a maroon and gold scarf, on oxblood | Claude gold |
| ron.svg | Ron - Release Engineer | Sorts PR and CI changes, calls reds real or flaky, writes the morning lineup and weekly scoreboard | Chibi portrait: bright orange hair with a cowlick, blue eyes, freckles, black robe and a maroon and gold scarf, on Weasley orange | Claude gold |
| moody.svg | Moody - Security Reviewer | Read-only Codex review of Claude-written code, security first | Chibi portrait: long grizzled grey hair, a scarred face, one dark eye and a large blue magical eye in a brass rim on a leather strap, a frown, a brown traveling cloak, on dark slate | Codex steel blue |
| snape.svg | Snape - Data Analyst | Read-only warehouse and observability reads with provenance | Chibi portrait: curtains of black shoulder-length hair parted in the middle, a hooked nose, a thin sneer, buttoned black robes, on deep green | Claude gold |
| portrait.svg | Dumbledore - Knowledge Manager | Weeknight review of the day that proposes memory changes | Chibi portrait: a purple pointed hat with gold stars, long silver hair and a long silver beard, half-moon spectacles, bright blue eyes, purple robes, on starry indigo | Claude gold |
| map.svg | Marauder's Map - PR Watcher | Zero-token script that diffs PR and CI state | Outlined folded parchment map with two creases, a dashed trail and three pairs of footprints walking across it, on parchment | Script pewter |
| gringotts.svg | Gringotts - Backup | Nightly local backup script, with a restore drill | Chibi goblin: tall pointed ears, a clever narrow face with a pointed beard and a long nose, a dark waistcoat with gold buttons, a gold coin, on deep bronze | Script pewter |
| owlpost.svg | Owl Post - Message Router | Script that moves owls between desks and stamps the sender | Chibi snowy owl: round white body flecked dark, big amber eyes, ear tufts and a gold beak, holding a letter sealed in red wax, on deep teal | Script pewter |
| ollivander.svg | Ollivander - Model Keeper | Daily script that keeps each desk on the model its role card asks for | Chibi portrait: wispy white hair, wide pale silvery eyes, a dark robe, a wand held up throwing gold sparks, on plum | Script pewter |

## Style rules

1. 512 viewBox, plain SVG 1.1 only (circle, rect, ellipse, line, polyline, polygon, path, g, gradients, clipPath, and the transform attribute), every shape with an explicit fill, no filters, masks, patterns, images, text, fonts, scripts or `<use>`, under 12KB.
2. Identical chrome: field `r=240` filled by radial gradient `field` (userSpaceOnUse, cx 176, cy 156, r 360, 8% white mix to base), then outer ring `r=240` at 16px and hairline `r=222` at 3px and 0.45, drawn last in the family colour (Claude `#D9A441`, Codex `#6F93D6`, script `#9AA0A6`).
3. Everything inside the field sits in `<g clip-path="url(#inner)">`, with `<clipPath id="inner"><circle cx="256" cy="256" r="222" fill="#FFFFFF"/></clipPath>`.
4. One chibi figure per badge, centred in the field: a big head (about `rx 108`, `ry 100` at 256,236, rounder for a heavier character) with ears, a small robed body cut off by the field's edge, dot eyes with a white glint, and the character's signature details (hair, spectacles, a scar, a magical eye, a hat, a wand, a letter). Skin `#F6E2C4` with `#E8CBA4` ears and neck.
5. Every shape carries a 10px `#231F20` outline with round caps and joins; detail strokes are 8 to 9px, nothing under 4px, and original iconography only: no crests, logos, film fonts, letters or numbers.
6. Characters are drawn from the books' descriptions, never an actor's likeness. The badges are made by one generator, so a change to the shared head, body, eyes or outline lands on every badge at once.

## Making them

`draw_badges.py` writes all eleven SVGs: `/usr/bin/python3 -I -B office/assets/avatars/draw_badges.py office/assets/avatars`. Export each to a transparent 512 PNG with headless Chrome, then scale it to 128 and 64 with `sips -z`:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --hide-scrollbars --default-background-color=00000000 --window-size=512,512 --screenshot=png/NAME-512.png "file://$PWD/NAME.svg"
```
