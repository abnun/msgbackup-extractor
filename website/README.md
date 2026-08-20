# website/

The GitHub Pages site. **Incomplete:** the landing page (`index.html`) does not
exist yet — only the legal pages do.

| File | State |
|---|---|
| `impressum.html` | ready, needs its font paragraph filled in once the landing page exists |
| `datenschutz.html` | ready |
| `styles.css` | provisional — the landing page brings its own visual world and replaces this |
| `script.js` | assembles the split email address into a clickable link |
| `index.html` | **missing** — the footer links already point at it |

Both legal pages are German on purpose. An Impressum under § 5 DDG and a privacy
notice under the GDPR address visitors in Germany, and the German text is the
one that has to be legally sound. If the site gets an English translation, the
German version stays authoritative and the translation says so.

Two things any replacement stylesheet must keep:

- `.mail-at::before { content: "@" }` — the address is split across spans in the
  HTML so a harvester finds no `something@something.de` in the source. Without
  this rule the page reads `abnungmx.de`.
- `.container` as the text column.

No external resources anywhere on this site: no CDN, no Google Fonts, no
analytics. The software promises offline operation; its website should not
undercut that.
