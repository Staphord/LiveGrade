# Vendored frontend assets

| Package | Version | Source | License | Verification |
|---|---:|---|---|---|
| Bootstrap | 5.3.0 | `https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/` | MIT; see `bootstrap/5.3.0/LICENSE` | CSS SHA-384: `9ndCyUaIbzAi2FUVXJi0CjmCapSmO7SnpJef0486qhLnuZ2cdeRhO02iuK6FUUVM`; bundle SHA-384: `geWF76RCwLtnZ8qwWowPQNguL3RmwHVBC9FhGdlKrxdiJJigb/j/68SIy3Te4Bkz` |
| Bootstrap Icons | 1.11.0 | `https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/` | MIT; see `bootstrap-icons/1.11.0/LICENSE` | Package manifest reports 1.11.0. CSS SHA-384: `QuGBSgV5Im3DzL2z+8Ko9/hqNy/N0O7zwvXAtfd1MvPKWa/UbeLV65cfm4BV5Wgq`; WOFF and WOFF2 are the font files referenced by that CSS |
| Chart.js | 4.4.0 | `https://cdn.jsdelivr.net/npm/chart.js@4.4.0/` | MIT; see `chartjs/4.4.0/LICENSE.md` | Package manifest reports 4.4.0. Bundle SHA-384: `e6nUZLBkQ86NJ6TVVKAeSaK8jWa3NhkYWZFomE39AvDbQWeie9PlQqM3pmYW5d1g` |

Keep these versions explicit. Update the CSS, scripts, font files, licenses, and SRI verification together when intentionally upgrading a package. Templates should reference the local files through Django's `static` tag; participant first-load transfer size must be measured in a browser with production-equivalent compression before release.
