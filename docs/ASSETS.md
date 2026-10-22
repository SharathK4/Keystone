# Assets

Only SIL Open Font License fonts, self-hosted from npm `@fontsource` packages,
are used in deliverables. No third-party marks or logos. Every image is drawn by
code from committed artifacts.

## Fonts

| Face | Package | Copyright (from the font's name table) | Licence | Licence text | Used in |
| --- | --- | --- | --- | --- | --- |
| IBM Plex Mono | `@fontsource/ibm-plex-mono` 5.2.5 | Copyright 2017 IBM Corp. | SIL OFL 1.1 | [`design/fonts/licenses/ibm-plex-mono-OFL.txt`](../design/fonts/licenses/ibm-plex-mono-OFL.txt) | figures on all concept boards |
| Source Serif 4 | `@fontsource/source-serif-4` 5.2.5 | © 2014–2021 Adobe Systems Incorporated, with Reserved Font Name "Source" | SIL OFL 1.1 | [`design/fonts/licenses/source-serif-4-OFL.txt`](../design/fonts/licenses/source-serif-4-OFL.txt) | Board A, specimen |
| Libre Caslon Text | `@fontsource/libre-caslon-text` 5.2.5 | Copyright 2012 The Libre Caslon Text Project Authors | SIL OFL 1.1 | [`design/fonts/licenses/libre-caslon-text-OFL.txt`](../design/fonts/licenses/libre-caslon-text-OFL.txt) | Board B, specimen |
| Spectral | `@fontsource/spectral` 5.2.5 | Copyright 2017 The Spectral Project Authors | SIL OFL 1.1 | [`design/fonts/licenses/spectral-OFL.txt`](../design/fonts/licenses/spectral-OFL.txt) | Board C, specimen |

The licence files are copied from the packages; their first line is fontsource's
generic header, so the copyright column above is read from each font file instead.
The concept boards embed WOFF2 subsets (only the characters each board uses).
Subsetting counts as modification under the OFL, and a modified version may not
carry a Reserved Font Name. Source Serif 4's notice declares one ("Source"), and
IBM Plex's upstream licence is understood to reserve "Plex" (not yet checked
against the upstream file). **Open for Phase 2:** before the product serves fonts,
either use fontsource's unmodified files, or confirm the OFL FAQ's position on
subsets of Reserved-Font-Name fonts and rename the subsets if required.

`@fontsource/jetbrains-mono`, `dm-mono` and `courier-prime` are installed in
`design/` only to measure their glyph coverage for the specimen; they are not
embedded anywhere.

## Not used, and why

| Asset | Where it is | Licence | Status |
| --- | --- | --- | --- |
| Against, Emotions Condensed, Wolfgang (`fonts/*.zip`) | local only; untracked on `paypal` | personal use only, no commercial use | never used by Keystone deliverables |
| Against (`frontend/public/fonts/against-regular.otf`) | current frontend display face | personal use only | to be replaced by the chosen OFL serif in Phase 2 |

Both are present in the public original repository's history (`origin/master`);
the Phase 3 publish copy removes them.
