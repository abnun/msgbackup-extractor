# website/

The GitHub Pages site: a workshop linocut broadside, one ink on cheap manila.

| File | What it is |
|---|---|
| `index.html` | the landing page, English |
| `de/index.html` | the same page in German |
| `impressum.html` | Impressum under § 5 DDG — German, and it has to stay German |
| `datenschutz.html` | privacy notice under the GDPR — likewise |
| `styles.css` | the whole visual system, shared by all three pages |
| `script.js` | assembles the split email address into a clickable link |
| `fonts/` | Archivo and Archivo Black, self-hosted, with their OFL |

## Two languages, two files

`index.html` is English, `de/index.html` is German. Same world, same structure,
same section ids — and that means **every content change has to be made twice.**
There is no template at serve time and there will not be one: this site loads no
script it does not need. The two pages point at each other through the masthead
and through `hreflang` alternates, and `x-default` points at the English one.

The German legal pages link back to the German landing page, because a page
under § 5 DDG addresses German visitors.

## Rules that must survive any edit

**Nothing loads from a third party.** No CDN, no Google Fonts, no analytics, no
embeds, no remote images. The software promises offline operation; its website
does not get to undercut that. Every page carries a `Content-Security-Policy`
meta that pins this down, and the privacy notice states it as fact.

**`.mail-at::before { content: "@" }`** carries the @ of the email address. The
address is split across spans in the HTML so a harvester finds no
`something@something.de` in the source. Delete that rule and both legal pages
read `abnungmx.de`.

**No photographs, ever.** The tiles in section 01 stand in for a gallery and say
so on the page. A screenshot of a real export would put someone's private
pictures on a public site.

**The carving is real, not an image.** Two SVG filters live in `index.html`:
`#carve` roughens display type and rules, `#gouge` roughens the illustration.
The paper fibre is one `feTurbulence` pass in a data URI. There is no texture
photograph to lose.

## How the illustration was made

The hero SVG and the twelve tiles are generated geometry, not hand-typed paths:
a burst of tapered rays, a phone whose screen is a grid of blank blocks
(nothing in a backup is readable), and four photographs spilling out — one per
category the tool writes. The committed HTML is the artifact; edit it directly.

## Screenshotting it

Headless Chrome clamps `--window-size` to a minimum of 500 CSS pixels and
crops the image rather than scaling it, so `--window-size=390` produces a
picture that looks broken while the page is fine. To test a phone viewport,
load the page in a 390-pixel-wide iframe and screenshot that instead.
