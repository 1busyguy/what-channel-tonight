# IndexNow

[IndexNow](https://www.indexnow.org/) is a protocol for telling search engines about new or changed URLs. Bing, Yandex, Seznam, Naver and Yep all share submissions made through it. Google does not use it; Google picks up changes from `sitemap.xml`.

## Setup

- The key is `INDEXNOW_KEY` in `config.yaml` (`3063a41c910eeb3ac22bccf7239260c6`).
- `build.py` writes the key file into every build at `dist/<key>.txt`. It is served at https://whatchanneltonight.com/3063a41c910eeb3ac22bccf7239260c6.txt, so it stays live across nightly builds.
- `indexnow.py` POSTs JSON `{host, key, keyLocation, urlList}` to https://api.indexnow.org/indexnow.

## Automatic submission (every deploy)

The last step of `.github/workflows/build.yml` runs `python indexnow.py --wait`:

1. It hashes every page listed in `dist/sitemap.xml`, ignoring build timestamps such as "Last updated", `<time>` and `dateModified`.
2. It compares those hashes with `data/indexnow_state.json`. The Actions cache keeps that file between runs.
3. It waits until GitHub Pages is serving this build, by checking that the live homepage shows the same build timestamp.
4. It submits only pages that are new or whose content changed, and records them only after a 200/202 response.

A failed submission never fails the deploy, because the step has `continue-on-error`. New guide pages pushed by the nightly publish routine are submitted automatically on the deploy that publishes them.

## Manual use

```bash
python indexnow.py --dry-run          # show what would be sent
python indexnow.py                    # submit new/changed pages (needs a fresh dist/)
python indexnow.py --all              # resubmit every sitemap URL
python indexnow.py --urls https://whatchanneltonight.com/guides/some-new-guide/
```

Response codes:
- 200 or 202: accepted. 202 means the key is still being validated.
- 400: bad request.
- 403: the key file was not found or does not match.
- 422: a URL does not belong to the host.
- 429: too many requests.
