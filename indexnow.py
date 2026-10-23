#!/usr/bin/env python3
"""Submit new/changed URLs to IndexNow after a deploy.

    python indexnow.py              # submit pages whose content changed since the last run
    python indexnow.py --all        # submit every URL in dist/sitemap.xml
    python indexnow.py --urls URL1 URL2 ...   # submit specific URLs
    options: --wait (poll the live site until this build is published, max ~10 min)
             --dry-run (print what would be sent)

"Changed" = a hash of each page's HTML with build timestamps stripped differs from the hash
saved in data/indexnow_state.json (kept between CI runs by the Actions cache).
Only marks pages as submitted when IndexNow answers 200/202.
"""
import hashlib, json, os, re, sys, time
import requests, yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
STATE = os.path.join(ROOT, "data", "indexnow_state.json")
ENDPOINT = "https://api.indexnow.org/indexnow"

CFG = yaml.safe_load(open(os.path.join(ROOT, "config.yaml")))
SITE = os.environ.get("SITE_URL", CFG["SITE_URL"]).rstrip("/")
KEY = CFG.get("INDEXNOW_KEY")
HOST = re.sub(r"^https?://", "", SITE).split("/")[0]

STRIP = [re.compile(p, re.S) for p in (
    r"<time[^>]*>.*?</time>", r'"dateModified":\s*"[^"]*"', r'datetime="[^"]*"', r'Last updated [^<]*')]


def sitemap_urls():
    return re.findall(r"<loc>(.*?)</loc>", open(os.path.join(DIST, "sitemap.xml")).read())


def page_file(u):
    path = u[len(SITE):].lstrip("/")
    return os.path.join(DIST, path, "index.html") if not path.endswith(".html") else os.path.join(DIST, path)


def page_hash(u):
    try:
        t = open(page_file(u), encoding="utf-8").read()
    except FileNotFoundError:
        return None
    for rx in STRIP:
        t = rx.sub("", t)
    return hashlib.sha256(t.encode()).hexdigest()


def wait_live(timeout=600):
    """Wait until the live homepage shows the same build timestamp as dist/index.html."""
    m = re.search(r'datetime="([^"]+)"', open(os.path.join(DIST, "index.html")).read())
    want = m.group(1) if m else None
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = requests.get(SITE + "/", params={"_": int(time.time())}, timeout=20)
            if want and want in r.text:
                print("live site is on this build")
                return True
        except requests.RequestException:
            pass
        time.sleep(20)
    print("warning: live site not confirmed on this build; submitting anyway")
    return False


def submit(urls, dry=False):
    for i in range(0, len(urls), 10000):
        body = {"host": HOST, "key": KEY, "keyLocation": f"{SITE}/{KEY}.txt", "urlList": urls[i:i + 10000]}
        if dry:
            print(json.dumps(body, indent=1)[:2000]); continue
        r = requests.post(ENDPOINT, json=body, timeout=30,
                          headers={"Content-Type": "application/json; charset=utf-8"})
        print(f"IndexNow {r.status_code} for {len(body['urlList'])} URLs {r.text[:200]}")
        if r.status_code not in (200, 202):
            return False
    return True


def main():
    if not KEY:
        print("INDEXNOW_KEY not set in config.yaml; skipping"); return 0
    a = sys.argv[1:]
    dry = "--dry-run" in a
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    if "--urls" in a:
        urls = [u for u in a[a.index("--urls") + 1:] if u.startswith("http")]
        new_state = {}
    else:
        allu = sitemap_urls()
        new_state = {u: page_hash(u) for u in allu}
        urls = allu if "--all" in a else [u for u in allu if state.get(u) != new_state[u]]
    if not urls:
        print("IndexNow: nothing new or changed"); return 0
    print(f"IndexNow: {len(urls)} URL(s) to submit")
    if "--wait" in a and not dry:
        wait_live()
    if submit(urls, dry) and not dry:
        state.update({k: v for k, v in new_state.items() if k in urls})
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        json.dump(state, open(STATE, "w"), indent=0, sort_keys=True)
    return 0  # never fail the deploy because of IndexNow


if __name__ == "__main__":
    sys.exit(main())
