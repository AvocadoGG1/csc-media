"""Cloud poster for the CSUN Citizen Science Club, run by GitHub Actions every 15 minutes.
Posts anything in schedule.json that is due (and not more than 12 h late) to Instagram (Instagram API) or Discord
(webhook), then records it in state.json so nothing posts twice.

Media in queue/ is Fernet-encrypted (key: MEDIA_KEY secret). At post time a file is decrypted and pushed to the
'media' branch so Instagram can fetch it from a raw URL; the branch is wiped (force-push) right after posting.
Secrets: IG_ACCESS_TOKEN, DISCORD_WEBHOOK_URL, MEDIA_KEY.   Flags: --dry-run (build containers, don't publish)."""
import datetime, json, os, subprocess, sys, time, urllib.parse, urllib.request
from cryptography.fernet import Fernet

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.join(ROOT, "autopost")
REPO_SLUG = os.environ.get("GITHUB_REPOSITORY", "AvocadoGG1/csc-media")
RAW = f"https://raw.githubusercontent.com/{REPO_SLUG}/media/"
API = "https://graph.instagram.com/v21.0"
DRY = "--dry-run" in sys.argv
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")

def log(msg):
    print(f"{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d %H:%M:%SZ}  {msg}", flush=True)

def load(name, default):
    p = os.path.join(HERE, name)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else default

def save_state(state):
    with open(os.path.join(HERE, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def decrypt(rel):
    with open(os.path.join(ROOT, rel), "rb") as f:
        return Fernet(os.environ["MEDIA_KEY"].encode()).decrypt(f.read())

# ---------- temporary public hosting on the 'media' branch ----------
WORK = os.path.join(ROOT, "_media_branch")

def git(*args, cwd=WORK):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

def push_media(files):
    """files: list of (name, bytes). Returns raw URLs once GitHub serves them."""
    os.makedirs(WORK, exist_ok=True)
    git("init", "-q")
    git("config", "user.name", "csc-autopost"); git("config", "user.email", "csc-autopost@users.noreply.github.com")
    for name, data in files:
        with open(os.path.join(WORK, name), "wb") as f:
            f.write(data)
    git("add", "-A"); git("commit", "-q", "-m", "temporary post media")
    remote = f"https://x-access-token:{os.environ['GITHUB_TOKEN']}@github.com/{REPO_SLUG}.git"
    git("push", "-q", "-f", remote, "HEAD:refs/heads/media")
    urls = [RAW + n for n, _ in files]
    for u in urls:
        for _ in range(40):
            try:
                with urllib.request.urlopen(u, timeout=20) as r:
                    if r.status == 200:
                        break
            except Exception:
                pass
            time.sleep(3)
    return urls

def wipe_media():
    try:
        subprocess.run(["rm", "-rf", WORK], check=False)
        os.makedirs(WORK, exist_ok=True)
        git("init", "-q")
        git("config", "user.name", "csc-autopost"); git("config", "user.email", "csc-autopost@users.noreply.github.com")
        with open(os.path.join(WORK, "README.md"), "w") as f:
            f.write("Temporary media branch; emptied after each post.\n")
        git("add", "-A"); git("commit", "-q", "-m", "empty")
        remote = f"https://x-access-token:{os.environ['GITHUB_TOKEN']}@github.com/{REPO_SLUG}.git"
        git("push", "-q", "-f", remote, "HEAD:refs/heads/media")
    finally:
        subprocess.run(["rm", "-rf", WORK], check=False)

# ---------- Instagram ----------
def ig(method, path, params=None):
    params = dict(params or {}); params["access_token"] = TOKEN
    url = f"{API}/{path}"
    req = (urllib.request.Request(url + "?" + urllib.parse.urlencode(params)) if method == "GET"
           else urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), method="POST"))
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path}: HTTP {e.code} {e.read().decode(errors='replace')[:400]}")

def wait_ready(cid):
    for _ in range(60):
        s = ig("GET", cid, {"fields": "status_code,status"})
        if s.get("status_code") == "FINISHED":
            return
        if s.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"container {cid}: {s}")
        time.sleep(10)
    raise RuntimeError(f"container {cid} never finished")

def post_instagram(p):
    me = ig("GET", "me", {"fields": "user_id"})["user_id"]
    collab = {"collaborators": json.dumps(p["collaborators"])} if p.get("collaborators") else {}
    stamp = str(int(time.time()))
    try:
        if p["type"] == "reel":
            url = push_media([(f"{stamp}.mp4", decrypt(p["video"]))])[0]
            c = ig("POST", f"{me}/media", {"media_type": "REELS", "video_url": url, "caption": p["caption"], "share_to_feed": "true",
                                            "thumb_offset": str(p.get("thumb_offset_ms", 0)), **collab})
            wait_ready(c["id"])
        else:  # carousel
            urls = push_media([(f"{stamp}_{i}.jpg", decrypt(f)) for i, f in enumerate(p["images"], 1)])
            kids = []
            for u in urls:
                ch = ig("POST", f"{me}/media", {"image_url": u, "is_carousel_item": "true"}); wait_ready(ch["id"]); kids.append(ch["id"])
            c = ig("POST", f"{me}/media", {"media_type": "CAROUSEL", "children": ",".join(kids), "caption": p["caption"], **collab})
            wait_ready(c["id"])
        if DRY:
            return f"dry run ok (container {c['id']})"
        m = ig("POST", f"{me}/media_publish", {"creation_id": c["id"]})
        return ig("GET", m["id"], {"fields": "permalink"}).get("permalink", m["id"])
    finally:
        wipe_media()

# ---------- Discord ----------
def post_discord(p):
    if DRY:
        return "dry run ok (not sent)"
    payload = json.dumps({"content": p["content"], "allowed_mentions": {"parse": ["everyone"]}})
    headers = {"User-Agent": "CSUN-CSC-announcer/1.0"}
    if p.get("file"):
        data, b = decrypt(p["file"]), "----csc" + os.urandom(8).hex()
        body = (f"--{b}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\nContent-Type: application/json\r\n\r\n{payload}\r\n"
                f"--{b}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"{p.get('filename', 'clip.mp4')}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={b}"
    else:
        body, headers["Content-Type"] = payload.encode(), "application/json"
    req = urllib.request.Request(os.environ["DISCORD_WEBHOOK_URL"], data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=180) as r:
        return f"HTTP {r.status}"

def main():
    only = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")), None)
    sched, state = load("schedule.json", []), load("state.json", {})
    now = datetime.datetime.now(datetime.timezone.utc)
    failed = False
    for p in sched:
        if only and p["id"] != only:
            continue
        due = datetime.datetime.fromisoformat(p["send_at"])
        if p["id"] in state and not DRY:
            continue
        if not only and (now < due or now - due > datetime.timedelta(hours=12)):
            if now - due > datetime.timedelta(hours=12) and p["id"] not in state:
                log(f"{p['id']}: too late ({now - due}), skipping"); state[p["id"]] = {"skipped": now.isoformat()}
            continue
        try:
            result = post_instagram(p) if p["platform"] == "instagram" else post_discord(p)
            log(f"{p['id']}: {result}")
            if not DRY:
                state[p["id"]] = {"at": now.isoformat(), "result": result}
        except Exception as e:
            failed = True
            log(f"{p['id']}: FAILED {e}")
    if not DRY:
        save_state(state)
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
