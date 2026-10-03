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

WAIT_LIMIT = datetime.timedelta(hours=5, minutes=30)  # GitHub jobs may run up to 6 h

def next_due(sched, state, now):
    pending = [datetime.datetime.fromisoformat(p["send_at"]) for p in sched if p["id"] not in state
               and datetime.datetime.fromisoformat(p["send_at"]) - now > datetime.timedelta(minutes=-1)]
    return min(pending) if pending else None

def relay():
    """Start the next run (allowed from GITHUB_TOKEN for workflow_dispatch) so a run is always waiting.
    GitHub's own cron is too sparse on quiet repos (runs were ~6 h apart), so the schedule can't rely on it."""
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO_SLUG}/actions/workflows/autopost.yml/dispatches",
                                 data=json.dumps({"ref": "main"}).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"})
    try:
        urllib.request.urlopen(req, timeout=30); log("relay: next run started")
    except Exception as e:
        log(f"relay failed ({e}); falling back to the cron schedule")

# ---------- state: always read fresh from origin/main, saved right after each post ----------
# (On 10/2 a relayed run checked out a commit from before the previous run saved state, and the end-of-run state
#  commit was then rejected, so Woniya posted twice on Instagram and its Discord post repeated. Never again.)
def repo(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)

def fresh_state():
    repo("fetch", "-q", "origin", "main")
    r = repo("show", "origin/main:autopost/state.json")
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}

def record(pid, info):
    """Write one entry to state.json on origin/main, retrying on push races. Raises if it can't be saved."""
    repo("config", "user.name", "csc-autopost"); repo("config", "user.email", "csc-autopost@users.noreply.github.com")
    for attempt in range(6):
        state = fresh_state()
        state[pid] = info
        repo("reset", "-q", "--hard", "origin/main")
        save_state(state)
        repo("add", "autopost/state.json")
        repo("commit", "-q", "-m", f"autopost: {pid}")
        if repo("push", "-q", "origin", "HEAD:main").returncode == 0:
            return
        time.sleep(3 + attempt * 2)
    raise RuntimeError(f"could not save state for {pid}")

def already_on_instagram(caption):
    """Last-resort duplicate guard: was this exact caption posted in the last 36 h?"""
    try:
        recent = ig("GET", "me/media", {"fields": "caption,timestamp", "limit": "10"}).get("data", [])
    except Exception:
        return False
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=36)
    for m in recent:
        t = datetime.datetime.fromisoformat(m["timestamp"].replace("+0000", "+00:00"))
        if t > cutoff and (m.get("caption") or "").strip() == caption.strip():
            return True
    return False

def main():
    only = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")), None)
    sched = load("schedule.json", [])
    now = datetime.datetime.now(datetime.timezone.utc)
    if not DRY and not only:
        nd = next_due(sched, fresh_state(), now)
        if nd and now < nd:  # wait here so the post goes out on time (or hand off after WAIT_LIMIT)
            until = min(nd, now + WAIT_LIMIT)
            log(f"next post at {nd.isoformat()}; waiting until {until.isoformat()}")
            time.sleep(max(0, (until - now).total_seconds()) + 5)
            now = datetime.datetime.now(datetime.timezone.utc)
    failed = False
    for p in sched:
        if only and p["id"] != only:
            continue
        due = datetime.datetime.fromisoformat(p["send_at"])
        state = fresh_state()  # re-check right before every post
        if p["id"] in state and not DRY:
            continue
        if not only and (now < due or now - due > datetime.timedelta(hours=12)):
            if now - due > datetime.timedelta(hours=12) and not DRY:
                log(f"{p['id']}: too late ({now - due}), skipping"); record(p["id"], {"skipped": now.isoformat()})
            continue
        try:
            if p["platform"] == "instagram" and not DRY and already_on_instagram(p["caption"]):
                log(f"{p['id']}: same caption already on Instagram, not posting again")
                record(p["id"], {"at": now.isoformat(), "result": "already posted (duplicate guard)"})
                continue
            result = post_instagram(p) if p["platform"] == "instagram" else post_discord(p)
            log(f"{p['id']}: {result}")
            if not DRY:
                record(p["id"], {"at": now.isoformat(), "result": result})  # saved before anything else happens
        except Exception as e:
            failed = True
            log(f"{p['id']}: FAILED {e}")
    if not DRY and not only and not failed and next_due(sched, fresh_state(), datetime.datetime.now(datetime.timezone.utc)):
        relay()  # only after state is safely saved
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
