# What Channel Tonight

A small static directory that answers "what channel is the game on tonight?" for the NFL, NBA, NHL, MLB, FBS college football and the NASCAR Cup Series. It has a nightly hub, league pages, and one evergreen page per team. Each game shows its start time (ET), national and local broadcasters, and legal streaming options.

## Build

```bash
pip install -r requirements.txt
python build.py            # fetch ESPN public scoreboard JSON + rebuild dist/
python build.py --offline  # rebuild from the last snapshot only
```

Safety rules:
- Each league and day request falls back to the last good cached copy in `data/cache/`.
- The site builds into `.tmp_dist/` and only replaces `dist/` when validation passes: every NFL/NBA/NHL/MLB team page exists, no page is thin, and no `None` values leak into a page.
- If the API is completely down and there's no cache, the build aborts and `dist/` is left untouched.

## Configure

| File | What it holds |
|---|---|
| `config.yaml` | `SITE_NAME`, `SITE_URL`, `GOATCOUNTER_CODE` (analytics slot), leagues, and the date window |
| `affiliates.yaml` | Streaming service → URL. `/go/<service>/` redirects here, so put real affiliate links in this file. |
| `networks.yaml` | Broadcaster name → type (broadcast, cable, regional, OTA, team app) and the legal services that carry it |
| `leagues_notes.yaml` | In-market vs. out-of-market explainer for each league |
| `featured.yaml` | The "Popular" links, title overrides, and extra notes for featured teams |
| `content/guides/*.md` | Markdown guides with YAML front matter, built to `/guides/<slug>/` |

You can also set `SITE_URL` and `GOATCOUNTER_CODE` as GitHub repo variables. The workflow passes them to the build.

## Click tracking

Every affiliate link points at `/go/<service>/` with `rel="sponsored nofollow"`. These are noindex redirect pages. When GoatCounter is enabled they count the visit and then redirect, so each `/go/...` path shows up as a click count.

## Deploy

`.github/workflows/build.yml` runs at 10:00 and 21:00 UTC, on every push to `main`, and on manual dispatch. It builds the site and force-pushes `dist/` to the `gh-pages` branch, which GitHub Pages serves. If the build fails, nothing is pushed and the live site stays on the last good build.

Data source: ESPN's public (undocumented) scoreboard API. Listings can change, so the site tells readers to confirm with their provider. Only legal, licensed services are listed.
