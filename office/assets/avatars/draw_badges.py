"""Writes every desk badge in one chibi style: a big outlined head, dot eyes, a small body, the desk's own field and ring.

Run it with the folder to write into, then export the PNGs as the README says:
  /usr/bin/python3 -I -B office/assets/avatars/draw_badges.py office/assets/avatars
"""
import sys

OUT = sys.argv[1]
INK = "#231F20"
SKIN = "#F6E2C4"
SKIN_SHADE = "#E8CBA4"
GOLD = "#D9A441"
CODEX = "#6F93D6"
O = f'stroke="{INK}" stroke-width="10" stroke-linejoin="round" stroke-linecap="round"'


def badge(field, ring, body):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <defs>
    <radialGradient id="field" gradientUnits="userSpaceOnUse" cx="176" cy="156" r="360">
      <stop offset="0" stop-color="{field[0]}"/>
      <stop offset="1" stop-color="{field[1]}"/>
    </radialGradient>
    <clipPath id="inner">
      <circle cx="256" cy="256" r="222" fill="#FFFFFF"/>
    </clipPath>
  </defs>

  <circle cx="256" cy="256" r="240" fill="url(#field)"/>

  <g clip-path="url(#inner)">
{body}
  </g>

  <circle cx="256" cy="256" r="240" fill="none" stroke="{ring}" stroke-width="16"/>
  <circle cx="256" cy="256" r="222" fill="none" stroke="{ring}" stroke-width="3" stroke-opacity="0.45"/>
</svg>
'''


def robe(color=INK, trim=None, wide=0):
    parts = [f'    <path d="M 224 300 L 288 300 L 290 392 L 222 392 Z" fill="{SKIN_SHADE}" {O}/>',
             f'    <path d="M {160 - wide} 500 L {176 - wide} 384 Q 256 358 {336 + wide} 384 L {352 + wide} 500 Z" fill="{color}" {O}/>']
    if trim:
        parts.append(f'    <path d="M 218 372 L 256 420 L 294 372" fill="none" stroke="{trim}" stroke-width="12" stroke-linejoin="round"/>')
    return "\n".join(parts)


def scarf():
    return "\n".join([
        f'    <path d="M 186 360 Q 256 392 326 360 L 330 390 Q 256 420 182 390 Z" fill="#8A2232" {O}/>',
        f'    <path d="M 270 396 L 300 398 L 296 486 L 264 484 Z" fill="#8A2232" {O}/>',
        f'    <g stroke="{GOLD}" stroke-width="10" stroke-linecap="round">',
        '      <path d="M 192 378 Q 256 406 322 378" fill="none"/>',
        '      <line x1="272" y1="428" x2="292" y2="430"/>',
        '      <line x1="270" y1="456" x2="290" y2="458"/>',
        '    </g>',
    ])


def head(cy=236, rx=108, ry=100):
    return "\n".join([
        f'    <ellipse cx="{256 - rx}" cy="{cy + 14}" rx="20" ry="26" fill="{SKIN_SHADE}" {O}/>',
        f'    <ellipse cx="{256 + rx}" cy="{cy + 14}" rx="20" ry="26" fill="{SKIN_SHADE}" {O}/>',
        f'    <ellipse cx="256" cy="{cy}" rx="{rx}" ry="{ry}" fill="{SKIN}" {O}/>',
    ])


def eyes(y=258, color=INK, gap=40, r=11):
    return "\n".join([
        f'    <ellipse cx="{256 - gap}" cy="{y}" rx="{r}" ry="{r + 3}" fill="{color}"/>',
        f'    <ellipse cx="{256 + gap}" cy="{y}" rx="{r}" ry="{r + 3}" fill="{color}"/>',
        f'    <circle cx="{256 - gap + 4}" cy="{y - 5}" r="4" fill="#FFFFFF"/>',
        f'    <circle cx="{256 + gap + 4}" cy="{y - 5}" r="4" fill="#FFFFFF"/>',
    ])


def mouth(y=298, kind="smile"):
    if kind == "flat":
        return f'    <line x1="242" y1="{y}" x2="270" y2="{y}" stroke="{INK}" stroke-width="8" stroke-linecap="round"/>'
    if kind == "frown":
        return f'    <path d="M 240 {y + 4} Q 256 {y - 6} 272 {y + 4}" fill="none" stroke="{INK}" stroke-width="8" stroke-linecap="round"/>'
    return f'    <path d="M 238 {y} Q 256 {y + 14} 274 {y}" fill="none" stroke="{INK}" stroke-width="8" stroke-linecap="round"/>'


def cheeks(y=284):
    return f'    <ellipse cx="196" cy="{y}" rx="14" ry="8" fill="#EBA9A0"/>\n    <ellipse cx="316" cy="{y}" rx="14" ry="8" fill="#EBA9A0"/>'


CURLS = ((256, 128, 52), (204, 136, 48), (308, 136, 48), (160, 168, 46), (352, 168, 46), (130, 220, 44),
         (382, 220, 44), (122, 276, 42), (390, 276, 42), (132, 330, 40), (380, 330, 40), (156, 370, 34),
         (356, 370, 34), (256, 210, 110))


def curls(color):
    edge = "\n".join(f'    <circle cx="{x}" cy="{y}" r="{r}" fill="{color}" {O}/>' for x, y, r in CURLS)
    fill = "\n".join(f'    <circle cx="{x}" cy="{y}" r="{r - 1}" fill="{color}"/>' for x, y, r in CURLS)
    return edge + "\n" + fill


HERMIONE = "\n".join([
    curls("#8A5A33"),
    robe(),
    scarf(),
    head(),
    f'    <path d="M 150 222 C 150 150 206 124 262 128 C 320 130 366 166 362 230 C 340 192 306 176 270 176 C 240 196 200 204 150 222 Z" fill="#8A5A33" {O}/>',
    f'    <g fill="none" stroke="#A8743F" stroke-width="9" stroke-linecap="round"><path d="M 196 120 Q 226 104 256 108"/><path d="M 118 250 Q 112 284 124 312"/><path d="M 394 250 Q 400 284 388 312"/></g>',
    eyes(y=262, color="#4A2C1A"),
    cheeks(),
    mouth(),
])

HARRY = "\n".join([
    robe(),
    scarf(),
    head(),
    f'    <path d="M 148 226 C 140 156 196 120 258 122 C 322 124 370 160 364 226 L 346 206 L 336 222 L 320 196 L 304 214 L 292 188 L 274 212 L 262 184 L 246 210 L 232 186 L 218 214 L 204 192 L 190 218 L 174 198 Z" fill="#2A211C" {O}/>',
    f'    <polyline points="290,196 278,212 294,214 282,232" fill="none" stroke="#C0473A" stroke-width="8" stroke-linecap="round" stroke-linejoin="round"/>',
    eyes(y=260, color="#2F7A45", r=10),
    f'    <g fill="none" stroke="{INK}" stroke-width="9">',
    '      <circle cx="216" cy="260" r="28"/>',
    '      <circle cx="296" cy="260" r="28"/>',
    '      <path d="M 244 258 Q 256 250 268 258" stroke-linecap="round"/>',
    '    </g>',
    cheeks(y=294),
    mouth(y=306),
])

RON = "\n".join([
    robe(),
    scarf(),
    head(),
    f'    <path d="M 146 236 C 132 160 190 118 258 120 C 330 122 378 164 364 236 C 352 206 332 186 304 178 C 300 196 284 206 266 204 C 270 190 262 180 250 178 C 232 196 206 204 184 198 C 168 206 156 220 146 236 Z" fill="#F07A2A" {O}/>',
    f'    <path d="M 234 128 Q 238 100 266 96 Q 256 112 270 124" fill="#F07A2A" {O}/>',
    eyes(y=262, color="#2E5F96"),
    "\n".join(f'    <circle cx="{x}" cy="{y}" r="5" fill="#C98A5A"/>' for x, y in
              ((190, 286), (204, 296), (186, 302), (322, 286), (308, 296), (326, 302))),
    mouth(y=304),
])

MCGONAGALL = "\n".join([
    robe(color="#2E8048", trim="#1C5A33"),
    head(cy=248),
    f'    <path d="M 150 250 C 146 190 196 160 256 160 C 316 160 366 190 362 250 C 340 214 300 202 256 204 C 212 202 172 214 150 250 Z" fill="#3A3A40" {O}/>',
    f'    <ellipse cx="364" cy="214" rx="26" ry="24" fill="#3A3A40" {O}/>',
    f'    <path d="M 108 182 Q 256 136 404 182 Q 380 210 256 206 Q 132 210 108 182 Z" fill="{INK}" {O}/>',
    f'    <path d="M 180 172 L 262 46 Q 300 34 318 66 L 296 70 L 332 172 Z" fill="{INK}" {O}/>',
    f'    <path d="M 184 160 Q 256 146 330 160" fill="none" stroke="#2E8048" stroke-width="12" stroke-linecap="round"/>',
    f'    <g fill="none" stroke="{INK}" stroke-width="8" stroke-linecap="round">',
    '      <path d="M 196 236 L 232 246"/>',
    '      <path d="M 280 246 L 316 236"/>',
    '    </g>',
    eyes(y=272, color="#1F5A3A", r=9),
    f'    <g fill="none" stroke="#6B6458" stroke-width="9" stroke-linejoin="round">',
    '      <rect x="186" y="254" width="56" height="38" rx="6"/>',
    '      <rect x="270" y="254" width="56" height="38" rx="6"/>',
    '      <line x1="242" y1="270" x2="270" y2="270"/>',
    '    </g>',
    mouth(y=318, kind="flat"),
])

MOODY = "\n".join([
    f'    <path d="M 256 104 C 164 100 106 156 104 232 C 92 290 104 342 124 374 L 186 362 L 326 362 L 388 374 C 408 342 420 290 408 232 C 406 156 348 100 256 104 Z" fill="#A9A49A" {O}/>',
    robe(color="#3A3026", wide=46),
    f'    <path d="M 150 386 Q 256 414 362 386" fill="none" stroke="#5A4632" stroke-width="12" stroke-linecap="round"/>',
    head(cy=246, rx=132, ry=116),
    f'    <ellipse cx="166" cy="300" rx="30" ry="24" fill="{SKIN}"/>',
    f'    <ellipse cx="346" cy="300" rx="30" ry="24" fill="{SKIN}"/>',
    f'    <path d="M 126 236 C 122 150 190 114 258 116 C 330 118 392 150 386 236 C 362 200 328 184 296 186 L 286 166 L 266 188 L 246 164 L 228 190 L 206 170 L 190 196 C 162 200 140 216 126 236 Z" fill="#A9A49A" {O}/>',
    f'    <path d="M 214 346 Q 256 366 298 346" fill="none" stroke="{INK}" stroke-width="8" stroke-linecap="round"/>',
    f'    <ellipse cx="180" cy="300" rx="18" ry="10" fill="#E3A99A"/>',
    f'    <path d="M 124 256 L 196 242 M 318 242 L 388 236" fill="none" stroke="#6B4A2E" stroke-width="10" stroke-linecap="round"/>',
    f'    <circle cx="300" cy="258" r="34" fill="#C49A4A" {O}/>',
    '    <circle cx="300" cy="258" r="22" fill="#F4F1E8"/>',
    f'    <circle cx="306" cy="254" r="13" fill="#3F8FE0"/>',
    f'    <circle cx="306" cy="254" r="6" fill="{INK}"/>',
    f'    <ellipse cx="214" cy="260" rx="8" ry="10" fill="{INK}"/>',
    f'    <g fill="none" stroke="#B57A6A" stroke-width="8" stroke-linecap="round">',
    '      <path d="M 196 214 L 222 236"/>',
    '      <path d="M 232 300 L 250 286"/>',
    '      <path d="M 330 300 L 346 284"/>',
    '    </g>',
    mouth(y=314, kind="frown"),
])

SETS = {
    "hermione": (("#5D2C34", "#4F1A22"), GOLD, HERMIONE),
    "harry": (("#852E2E", "#7A1C1C"), CODEX, HARRY),
    "ron": (("#8A3A1E", "#6E2A12"), GOLD, RON),
    "mcgonagall": (("#315B4A", "#1F4D3A"), GOLD, MCGONAGALL),
    "moody": (("#353B43", "#232A33"), CODEX, MOODY),
}
for name, (field, ring, body) in SETS.items():
    with open(f"{OUT}/{name}.svg", "w") as handle:
        handle.write(badge(field, ring, body))

PEWTER = "#9AA0A6"

SNAPE = "\n".join([
    f'    <path d="M 256 108 C 176 106 130 158 132 236 L 128 352 L 186 364 L 326 364 L 384 352 L 380 236 C 382 158 336 106 256 108 Z" fill="#141416" {O}/>',
    robe(color="#141416"),
    '    <g fill="#3E4248">',
    '      <circle cx="256" cy="398" r="6"/>',
    '      <circle cx="256" cy="426" r="6"/>',
    '      <circle cx="256" cy="454" r="6"/>',
    '    </g>',
    head(),
    f'    <path d="M 150 300 C 140 220 168 150 256 146 C 344 150 372 220 362 300 C 352 246 330 208 286 196 L 256 170 L 226 196 C 182 208 160 246 150 300 Z" fill="#141416" {O}/>',
    '    <g fill="none" stroke="#3E4248" stroke-width="8" stroke-linecap="round"><path d="M 220 176 Q 180 196 166 248"/><path d="M 292 176 Q 332 196 346 248"/></g>',
    eyes(y=262, color=INK, r=9),
    f'    <g fill="none" stroke="{INK}" stroke-width="8" stroke-linecap="round"><path d="M 200 240 L 232 248"/><path d="M 280 248 L 312 240"/></g>',
    f'    <path d="M 256 262 Q 266 290 248 296" fill="none" stroke="{INK}" stroke-width="8" stroke-linecap="round" stroke-linejoin="round"/>',
    f'    <path d="M 236 316 L 262 316 Q 272 314 278 308" fill="none" stroke="{INK}" stroke-width="8" stroke-linecap="round"/>',
])

DUMBLEDORE = "\n".join([
    f'    <path d="M 256 132 C 170 130 122 178 124 250 L 118 380 L 394 380 L 388 250 C 390 178 342 130 256 132 Z" fill="#EEF0F4" {O}/>',
    robe(color="#6B4FA0", trim="#D9A441"),
    head(cy=246),
    f'    <path d="M 180 300 C 186 370 220 430 256 470 C 292 430 326 370 332 300 C 310 318 286 322 256 322 C 226 322 202 318 180 300 Z" fill="#EEF0F4" {O}/>',
    f'    <path d="M 226 312 Q 256 300 286 312" fill="none" stroke="{INK}" stroke-width="8" stroke-linecap="round"/>',
    f'    <path d="M 108 192 Q 256 150 404 192 Q 378 216 256 212 Q 134 216 108 192 Z" fill="#4E3A86" {O}/>',
    f'    <path d="M 176 186 L 250 52 Q 292 30 318 64 L 294 70 L 334 186 Z" fill="#4E3A86" {O}/>',
    f'    <g fill="{GOLD}">',
    '      <polygon points="250,120 256,134 270,136 259,145 262,159 250,151 238,159 241,145 230,136 244,134"/>',
    '      <polygon points="292,154 296,164 306,165 298,171 300,181 292,175 284,181 286,171 278,165 288,164"/>',
    '    </g>',
    eyes(y=266, color="#3F78C8", r=9),
    f'    <g fill="none" stroke="#C9B48A" stroke-width="8" stroke-linecap="round">',
    '      <path d="M 192 268 Q 216 292 240 268 Z"/>',
    '      <path d="M 272 268 Q 296 292 320 268 Z"/>',
    '      <line x1="240" y1="268" x2="272" y2="268"/>',
    '    </g>',
    f'    <path d="M 256 268 L 262 282 L 252 294" fill="none" stroke="{INK}" stroke-width="8" stroke-linecap="round" stroke-linejoin="round"/>',
    cheeks(y=292),
])

OLLIVANDER = "\n".join([
    robe(color="#3B3A4A"),
    head(),
    f'    <path d="M 140 236 C 124 196 140 150 176 140 C 196 120 226 116 248 128 C 270 112 306 118 322 138 C 360 146 380 196 370 236 C 356 214 344 200 330 196 C 316 214 300 186 286 200 C 270 182 248 196 236 184 C 220 200 204 186 190 200 C 170 202 154 214 140 236 Z" fill="#E6E6EC" {O}/>',
    f'    <ellipse cx="214" cy="260" rx="22" ry="24" fill="#FFFFFF" {O}/>',
    f'    <ellipse cx="298" cy="260" rx="22" ry="24" fill="#FFFFFF" {O}/>',
    '    <circle cx="216" cy="262" r="10" fill="#B9C3D0"/>',
    '    <circle cx="300" cy="262" r="10" fill="#B9C3D0"/>',
    f'    <circle cx="216" cy="262" r="5" fill="{INK}"/>',
    f'    <circle cx="300" cy="262" r="5" fill="{INK}"/>',
    mouth(y=310, kind="smile"),
    f'    <line x1="330" y1="470" x2="414" y2="330" stroke="#8A5A33" stroke-width="16" stroke-linecap="round"/>',
    f'    <g fill="{GOLD}">',
    '      <polygon points="420,284 426,300 442,302 430,312 434,328 420,320 406,328 410,312 398,302 414,300"/>',
    '      <circle cx="446" cy="340" r="7"/>',
    '      <circle cx="392" cy="296" r="6"/>',
    '    </g>',
])

GOBLIN_SKIN = "#BFC49A"
GRINGOTTS = "\n".join([
    robe(color="#2E2A24", trim=GOLD),
    '    <g fill="#D9A441"><circle cx="240" cy="440" r="6"/><circle cx="272" cy="440" r="6"/></g>',
    f'    <path d="M 154 236 L 84 176 L 132 268 Z" fill="{GOBLIN_SKIN}" {O}/>',
    f'    <path d="M 358 236 L 428 176 L 380 268 Z" fill="{GOBLIN_SKIN}" {O}/>',
    f'    <ellipse cx="256" cy="246" rx="104" ry="100" fill="{GOBLIN_SKIN}" {O}/>',
    f'    <path d="M 200 156 Q 210 136 230 146 M 280 146 Q 300 136 312 156" fill="none" stroke="{INK}" stroke-width="8" stroke-linecap="round"/>',
    f'    <g fill="none" stroke="{INK}" stroke-width="8" stroke-linecap="round"><path d="M 204 236 L 236 246"/><path d="M 276 246 L 308 236"/></g>',
    eyes(y=266, color=INK, r=9),
    f'    <path d="M 256 262 L 272 300 L 250 302" fill="{GOBLIN_SKIN}" {O}/>',
    f'    <path d="M 232 320 Q 256 330 282 318" fill="none" stroke="{INK}" stroke-width="8" stroke-linecap="round"/>',
    f'    <path d="M 236 338 L 256 372 L 276 338 Z" fill="#6B5A3A" {O}/>',
    f'    <circle cx="372" cy="420" r="30" fill="{GOLD}" {O}/>',
    '    <circle cx="372" cy="420" r="16" fill="none" stroke="#A97D2C" stroke-width="8"/>',
])

OWL_WHITE = "#F4F2EC"
OWLPOST = "\n".join([
    f'    <ellipse cx="256" cy="330" rx="118" ry="132" fill="{OWL_WHITE}" {O}/>',
    f'    <path d="M 150 210 L 156 140 L 204 182 Z" fill="{OWL_WHITE}" {O}/>',
    f'    <path d="M 362 210 L 356 140 L 308 182 Z" fill="{OWL_WHITE}" {O}/>',
    f'    <ellipse cx="256" cy="236" rx="112" ry="96" fill="{OWL_WHITE}" {O}/>',
    '    <g fill="#3A3A40">',
    '      <circle cx="210" cy="356" r="6"/><circle cx="250" cy="372" r="6"/><circle cx="292" cy="358" r="6"/>',
    '      <circle cx="228" cy="400" r="6"/><circle cx="276" cy="404" r="6"/><circle cx="200" cy="300" r="5"/>',
    '      <circle cx="318" cy="302" r="5"/>',
    '    </g>',
    f'    <circle cx="212" cy="240" r="30" fill="#F2C14E" {O}/>',
    f'    <circle cx="300" cy="240" r="30" fill="#F2C14E" {O}/>',
    f'    <circle cx="212" cy="242" r="14" fill="{INK}"/>',
    f'    <circle cx="300" cy="242" r="14" fill="{INK}"/>',
    '    <circle cx="218" cy="236" r="5" fill="#FFFFFF"/>',
    '    <circle cx="306" cy="236" r="5" fill="#FFFFFF"/>',
    f'    <path d="M 244 270 L 268 270 L 256 294 Z" fill="#D9A441" {O}/>',
    f'    <rect x="196" y="396" width="120" height="78" rx="6" fill="#F3E6C4" {O}/>',
    f'    <path d="M 196 400 L 256 440 L 316 400" fill="none" {O}/>',
    f'    <circle cx="256" cy="440" r="16" fill="#B23A2E" {O}/>',
])

MAP = "\n".join([
    f'    <path d="M 120 160 L 200 136 L 312 160 L 392 136 L 392 360 L 312 384 L 200 360 L 120 384 Z" fill="#F3E6C4" {O}/>',
    f'    <path d="M 200 136 L 200 360 M 312 160 L 312 384" fill="none" stroke="#C9B48A" stroke-width="9"/>',
    f'    <path d="M 152 330 Q 210 300 238 250 Q 270 196 350 190" fill="none" stroke="#B9A47A" stroke-width="9" stroke-dasharray="4 18" stroke-linecap="round"/>',
    f'    <g fill="{INK}">',
    '      <ellipse cx="160" cy="318" rx="9" ry="14" transform="rotate(-40 160 318)"/>',
    '      <ellipse cx="190" cy="300" rx="9" ry="14" transform="rotate(-40 190 300)"/>',
    '      <ellipse cx="238" cy="238" rx="9" ry="14" transform="rotate(-25 238 238)"/>',
    '      <ellipse cx="262" cy="214" rx="9" ry="14" transform="rotate(-25 262 214)"/>',
    '      <ellipse cx="318" cy="196" rx="9" ry="14" transform="rotate(70 318 196)"/>',
    '      <ellipse cx="346" cy="178" rx="9" ry="14" transform="rotate(70 346 178)"/>',
    '    </g>',
])

MORE = {
    "snape": (("#24463A", "#13261E"), GOLD, SNAPE),
    "portrait": (("#2F2D4E", "#1D1B3F"), GOLD, DUMBLEDORE),
    "ollivander": (("#6B4478", "#55325F"), PEWTER, OLLIVANDER),
    "gringotts": (("#4A3B25", "#3A2A12"), PEWTER, GRINGOTTS),
    "owlpost": (("#335A5F", "#1E4A4F"), PEWTER, OWLPOST),
    "map": (("#E6D9B9", "#E4D6B3"), PEWTER, MAP),
}
for name, (field, ring, body) in MORE.items():
    with open(f"{OUT}/{name}.svg", "w") as handle:
        handle.write(badge(field, ring, body))
