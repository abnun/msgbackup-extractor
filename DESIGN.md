# Design system

Recorded from the built artifact (`website/`), not from intentions. Seed
`8c512068`; the direction is a workshop linocut broadside, chosen by the author
over the assigned roll.

## The world in one sentence

One relief ink printed on cheap manila: a workshop broadside that states a
demand in hand-cut type and then explains itself in letterpress body copy.

## Color

Six values, no more, and **no dark variant** — a broadside is a physical object
and this one is printed on manila. Strategy: committed monochrome, where the ink
owns whole regions rather than appearing as an accent.

| Token | Value | Role |
|---|---|---|
| `--ink` | `#15120e` | the single relief ink, warmed by the paper |
| `--ink-soft` | `#4a4033` | aged fibre — second-rank body copy |
| `--ink-faint` | `#7a6e5a` | ink drag — labels inside dark bars |
| `--paper` | `#d6c4a3` | newsprint beige, the ground |
| `--paper-worn` | `#b79f7a` | worn tan — tile grounds, muted text on ink |
| `--carved` | `#f2e8d6` | carved white, knocked out of a filled block |

Reversal is the only emphasis device: a panel either sits on paper or is filled
with ink and knocks its type out. There is no third state and no accent hue.

## Type

| Role | Face | Setting |
|---|---|---|
| Display (`h1`, `h2`, `.cut`) | Archivo Black | uppercase, `line-height` 0.87–0.9, tracking −0.005em, cut by `#carve` |
| Small headings (`h3`) | Archivo Black | uppercase, **never filtered** — the carve turns to mush below display size |
| Body | Archivo 600 | 1.0625rem / 1.5 |
| Labels, buttons, nav | Archivo Black | uppercase, 0.75–1rem, tracking 0.02–0.06em |
| Commands | Archivo 700 | in a filled bar; no monospaced face exists in this world |

Both faces are self-hosted (`website/fonts/`) under the OFL. Hero lines carry a
sub-degree rotation each, because hand-set lines never sit square on the stone.

**No box-drawing characters.** The folder tree is a nested list stepped by cut
rules; `├──` needs a monospaced face this world does not have.

## Material

- `#carve` — `feTurbulence type="turbulence" baseFrequency="0.21"` into
  `feDisplacementMap scale="3.4"`. Applied to display type and rules.
- `#gouge` — `baseFrequency="0.42"`, `scale="6"`. Applied to the illustration.
- Paper fibre — one `feTurbulence` pass as a data URI, `position: fixed`,
  `mix-blend-mode: multiply`, opacity 0.62, so scrolling does not slide the
  grain across the sheet.

## Structure

- `--rule: 3px` for every internal cut line, `--frame: 4px` where a sheet edge
  is meant. `--col: 1180px` is the sheet width.
- Rules belong to elements **inside** the sheet's padding, never to the padded
  container itself, or they run wider than the panels they should align with.
- Panels butt against each other (`.grid--butt`) so a shared rule reads as one
  cut line rather than two borders.
- Every grid track that can hold a scrollable child is `minmax(0, 1fr)`, and
  `auto-fit` tracks use `minmax(min(Npx, 100%), 1fr)`.

## Components

| Component | Rest | Active |
|---|---|---|
| Primary button | filled ink block, carved-white caps, 3px border | chisel bursts struck at both ends, `transition: transform .12s steps(3)` |
| Secondary button | paper ground, ink caps, 3px border | the same bursts |
| Button on an ink panel | paper ground and paper border — an outlined button would vanish | the same bursts |
| Chip | 3px border; `--on` fills with ink | — |
| Section head | `01` in a ruled box beside a carved `h2` | — |
| Step | filled ink numeral, then heading, note, and a filled command bar | — |

## Motion

One idea only: the chisel burst that appears when a button is struck, in three
steps rather than a smooth ease, because a press either bites or it does not.
`prefers-reduced-motion` removes it and disables smooth scrolling.

## Rules with teeth

1. **No third-party request, ever** — enforced by a CSP meta on every page.
2. **No photographs** — the gallery tiles are stand-ins and the page says so.
3. `.mail-at::before` carries the `@`; the address is split in the markup.
4. The page must print. `@media print` drops the fibre, the nav and the filters.
