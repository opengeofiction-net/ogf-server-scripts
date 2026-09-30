#!/usr/bin/env python3
"""
OpenGeofiction Territory Application Review - weekly digest for #territory-administration

Lists every open territory application (pages carrying
{{territory application in progress}}), shows how long each has been waiting and
whose turn it is, and pings the relevant regional manager(s) when an application
has been waiting on an admin for STALE_DAYS.

The application's own lifecycle templates are the source of truth:
    {{territory application in progress}} -> [[Category:Territory application]]
    {{territory application approved}}    -> [[Category:Territory application - approved]]
    {{territory application closed}}      -> [[Category:Territory application - closed]]

Read-only: this script makes no wiki edits, so it needs no {{permission|yes}}
gate on User:Brothie. Its only write is a Discord post, delivered by the cron
job's `deliver` target (stdout), plus var/territory_review.json for the daily book.

Usage:
    bin/territoryApplicationReview.py             # print the digest + write var/territory_review.json
    bin/territoryApplicationReview.py --dry-run   # print the digest, write nothing
    bin/territoryApplicationReview.py --verbose   # include the raw per-application rows

Exit codes:
    0  digest printed (or intentionally silent - no open applications)
    1  a data source failed; stdout carries a failure block instead of a digest
"""

import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ─── Configuration ───────────────────────────────────────────────────────────

WIKI_API = "https://wiki.opengeofiction.net/api.php"
WIKI_BASE = "https://wiki.opengeofiction.net/index.php"
PROCESS_PAGE = "OpenGeofiction:Territory application"
ADMIN_PROPERTIES_URL = "https://data.opengeofiction.net/utility/admin_properties.json"

IN_PROGRESS_TEMPLATE = "Template:Territory application in progress"
APPLICATION_NS = "110"                     # Forum:

USER_AGENT = "OGF-TerritoryReview/1.0 (Brothie adminbot)"
REFERER = "https://opengeofiction.net/"

# Pinging
STALE_DAYS = 14        # waiting on an admin this long -> ping the regional managers
UNTRIAGED_DAYS = 7     # no admin has ever responded and the application is this old -> ping
DORMANT_DAYS = 14      # admin answered last, applicant silent this long -> list as dormant
BOT_USER = "Brothie"

# Admin housekeeping edits (link fixes, unsigned markers, page moves) are NOT replies.
# An edit is treated as housekeeping only when the summary matches AND it adds no bytes.
MAINT_SUMMARY = re.compile(
    r"redlink|unsigned|moved page|typo|fix link|broken link|replace bare territory|"
    r"revert|rv\b|spacing|format|categor|uncategor", re.I)

DISCORD_API = "https://discord.com/api/v10"
DISCORD_GUILD_ID = "855507833301893141"    # OGF Admin Channel
DISCORD_CHANNEL_ID = "884939526856921128"  # #territory-administration
DISCORD_ENV_PATH = os.path.expanduser("~/.hermes/.env")

# Known wiki-admin -> Discord user ID (lowercased keys). Verified against the guild;
# TheMayor's handle is minnoniganmayor (confirmed, not inferable from the name).
DISCORD_IDS = {
    "aiki": "711425747427786772",
    "alessa": "897972067067113474",
    "bixelkoven": "424329827211018253",
    "infinatious": "134112378534100993",
    "leowezy": "780163211486822440",
    "paravion": "208384089676447744",
    "portcal": "393748156342599683",
    "themayor": "292843414938976267",
    "wangi": "707978512857956546",
}
# Admins with no Discord presence in the guild: named in the digest, never mentionable.
NO_DISCORD = {"luciano"}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VAR_DIR = os.path.join(SCRIPT_DIR, "..", "var")
JSON_PATH = os.path.join(VAR_DIR, "territory_review.json")
DISCORD_CACHE_PATH = os.path.join(VAR_DIR, "discord_ids_cache.json")

# ─── Helpers ─────────────────────────────────────────────────────────────────

def api(params, retries=3):
    """GET the wiki API as JSON."""
    params = dict(params)
    params.setdefault("format", "json")
    url = WIKI_API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Referer": REFERER, "Accept": "application/json"})
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.load(resp)
        except Exception as exc:                        # noqa: BLE001 - retried below
            last = exc
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"wiki API request failed: {url} ({last})")


def parse_ts(value):
    return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)


def days_since(value):
    return (datetime.datetime.now(datetime.timezone.utc) - parse_ts(value)).days


def wiki_url(title):
    return WIKI_BASE + "/" + urllib.parse.quote(title.replace(" ", "_"), safe="/:")


def load_discord_token():
    """Read the bot token from the Hermes env file without ever logging it."""
    try:
        with open(DISCORD_ENV_PATH) as handle:
            for line in handle:
                if line.startswith("DISCORD_BOT_TOKEN"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return None
    return None

# ─── Data sources ────────────────────────────────────────────────────────────

def region_managers():
    """Continent -> {'managers': [...], 'status': ...} from the process page wikitable.

    Rowspanned Admin(s)/Status cells mean the second continent of a merged row
    (Tarephia under Antarephia, South Archanta under North Archanta) carries no
    cells of its own and inherits the row above.
    """
    data = api({"action": "query", "titles": PROCESS_PAGE,
                "prop": "revisions", "rvprop": "content", "rvslots": "main"})
    page = list(data["query"]["pages"].values())[0]
    text = page["revisions"][0]["slots"]["main"]["*"]
    body = text.split('{| class="wikitable"', 1)[1].split("\n|}", 1)[0]

    out, previous = {}, None
    for block in body.split("\n|-"):
        cells = [line.strip().lstrip("|").strip()
                 for line in block.splitlines() if line.strip().startswith("|")]
        cells = [re.sub(r'^rowspan="?\d+"?\s*\|\s*', "", cell) for cell in cells]
        if not cells:
            continue
        name = re.sub(r"^'''|'''$", "", cells[0]).strip()
        if len(cells) == 1:                       # continuation row
            if previous and name not in ("-", ""):
                out[name] = dict(out[previous])
            continue
        managers = []
        if len(cells) > 1:
            for raw in cells[1].split(","):
                raw = re.sub(r"\[\[User:([^|\]]+)\|.*?\]\]", r"\1", raw).strip()
                if raw:
                    managers.append(raw)
        status = ("not accepting" if "Not accepting" in cells[-1]
                  else "accepting" if "Accepting" in cells[-1] else "")
        if name == "Others":
            out.setdefault("Others", {"managers": [], "status": status})
        else:
            out[name] = {"managers": managers, "status": status}
            previous = name
    return out


def sysops():
    data = api({"action": "query", "list": "allusers", "augroup": "sysop", "aulimit": "500"})
    return {u["name"] for u in data.get("query", {}).get("allusers", [])}


def territory_continents():
    req = urllib.request.Request(ADMIN_PROPERTIES_URL,
                                headers={"User-Agent": USER_AGENT, "Referer": REFERER})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    return {entry["ogf:id"]: entry.get("is_in:continent", "Unknown")
            for entry in data if entry.get("ogf:id")}


def open_applications():
    """Pages transcluding the in-progress template - the authoritative open set."""
    data = api({"action": "query", "list": "embeddedin", "eititle": IN_PROGRESS_TEMPLATE,
                "eilimit": "500", "einamespace": APPLICATION_NS})
    return sorted(item["title"] for item in data.get("query", {}).get("embeddedin", []))


def resolve_territory_id(title, known_ids):
    """Territory code from the page title ('AN125 ...', 'UL05m - Sardia', 'AN 125 ...')."""
    for match in re.finditer(r"\b([A-Z]{2})\s?-?(\d{2,3})\s?([a-z])?\b", title):
        candidate = match.group(1) + match.group(2) + (match.group(3) or "")
        if candidate in known_ids:
            return candidate
    return None


def application_row(title, known_ids, admins):
    """Everything the digest needs about one application."""
    data = api({"action": "query", "titles": title, "prop": "revisions", "rvlimit": "100",
                "rvprop": "timestamp|user|comment|size|ids"})
    page = list(data["query"]["pages"].values())[0]
    revisions = list(reversed(page.get("revisions", [])))    # API returns newest first
    if not revisions:
        return None

    creator, created = revisions[0]["user"], revisions[0]["timestamp"]
    previous_size = 0
    substantive, admin_posts = [], []
    for revision in revisions:
        delta = revision.get("size", 0) - previous_size
        previous_size = revision.get("size", 0)
        if revision["user"] == BOT_USER:
            continue                                   # the link fixer edits every application
        is_admin = revision["user"] in admins
        if is_admin and MAINT_SUMMARY.search(revision.get("comment") or "") and delta <= 0:
            continue                                   # admin housekeeping, not a reply
        substantive.append(dict(revision, delta=delta))
        if is_admin:
            admin_posts.append(dict(revision, delta=delta))

    last = substantive[-1]
    tid = resolve_territory_id(title, known_ids)
    return {
        "title": title,
        "pageid": page["pageid"],
        "tid": tid,
        "continent": known_ids.get(tid, "Unknown") if tid else "Unknown",
        "creator": creator,
        "created": created,
        "age": days_since(created),
        "last_user": last["user"],
        "last_ts": last["timestamp"],
        "last_comment": last.get("comment") or "",
        "court": "applicant" if last["user"] in admins else "admin",
        "days_in_court": days_since(last["timestamp"]),
        "admin_posts": len(admin_posts),
        "days_since_admin": days_since(admin_posts[-1]["timestamp"]) if admin_posts else None,
        "engaged_admins": sorted({p["user"] for p in admin_posts if p["user"] != creator}),
        "last_human": revisions[-1]["user"],
        "last_human_ts": revisions[-1]["timestamp"],
        "url": wiki_url(title),
    }

# ─── Discord identity resolution ─────────────────────────────────────────────

def load_cache():
    try:
        with open(DISCORD_CACHE_PATH) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    try:
        os.makedirs(os.path.dirname(DISCORD_CACHE_PATH), exist_ok=True)
        with open(DISCORD_CACHE_PATH, "w") as handle:
            json.dump(cache, handle, indent=1, sort_keys=True)
    except OSError:
        pass


def discord_id(username, token, cache):
    """Wiki admin -> Discord user id: known map, then cache, then a live member search."""
    key = username.lower()
    if key in DISCORD_IDS:
        return DISCORD_IDS[key]
    if key in NO_DISCORD:
        return None
    if key in cache:
        return cache[key]
    if not token:
        return None
    url = (f"{DISCORD_API}/guilds/{DISCORD_GUILD_ID}/members/search?"
           + urllib.parse.urlencode({"query": username, "limit": 5}))
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bot {token}", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            for member in json.load(resp):
                user = member["user"]
                handle = (user.get("global_name") or "").lower()
                if user.get("username", "").lower() == key or handle == key:
                    cache[key] = user["id"]
                    save_cache(cache)
                    return user["id"]
    except Exception:                                  # noqa: BLE001 - a missing ping is not fatal
        return None
    return None

# ─── Classification ──────────────────────────────────────────────────────────

def classify(rows):
    """Bucket open applications. Order matters - each row appears in exactly one bucket."""
    stalled = [r for r in rows if r["court"] == "admin" and r["days_in_court"] >= STALE_DAYS]
    untriaged = [r for r in rows if r["admin_posts"] == 0 and r["age"] >= UNTRIAGED_DAYS
                 and r not in stalled]
    unroutable = [r for r in rows if r["continent"] in ("Unknown",)
                  and r not in stalled + untriaged]
    dormant = [r for r in rows if r["court"] == "applicant" and r["days_in_court"] >= DORMANT_DAYS
               and r not in stalled + untriaged + unroutable]
    active = [r for r in rows if r not in stalled + untriaged + unroutable + dormant]
    return stalled, untriaged, unroutable, dormant, active

# ─── Rendering ───────────────────────────────────────────────────────────────

def render(rows, managers, pingable):
    stalled, untriaged, unroutable, dormant, active = classify(rows)
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%d %b %Y")
    lines = [f"**Territory application review** - {today} - {len(rows)} open application(s)",
             "<https://wiki.opengeofiction.net/index.php/Forum:Territory_application>"]

    def short_name(row):
        """'AN125 Kabuni Federation' -> 'Kabuni Federation' (the code is shown separately)."""
        rest = row["title"].split("/", 1)[1]
        if row["tid"]:
            rest = re.sub(rf"^{re.escape(row['tid'])}\s*[-–—:]?\s*", "", rest, flags=re.I)
        return rest.strip() or row["tid"]

    def manager_text(row):
        info = managers.get(row["continent"], {})
        names = info.get("managers", [])
        if names:
            return " · ".join(names)
        return "no regional manager listed (region not accepting requests)" \
            if info.get("status") == "not accepting" else "no regional manager listed"

    def head(row):
        return f"• **{row['tid'] or '???'}** {short_name(row)} (_{row['continent']}_) - by {row['creator']}"

    def detail(row):
        return (f"↳ open {row['age']}d · last post {row['last_user']} {row['days_in_court']}d ago · "
                f"admin replies: {row['admin_posts']}"
                + (f" (last {row['days_since_admin']}d ago)" if row["days_since_admin"] is not None else "")
                + f" · responsible: {manager_text(row)}")

    def line(row):
        return f"{head(row)}\n{detail(row)}\n↳ {row['url']}"

    def line_with_ping(row, reason):
        entry = f"{head(row)}\n{detail(row)}\n↳ {row['url']}"
        names = managers.get(row["continent"], {}).get("managers", [])
        if not names:
            return entry + f"\n⚠️ {reason} {row['days_in_court']}d - {manager_text(row)}"
        mentions, plain = [], []
        for name in names:
            uid = pingable.get(name.lower())
            (mentions if uid else plain).append(f"<@{uid}>" if uid else name)
        text = " ".join(mentions + plain)
        if mentions and plain:
            text += " (no Discord account: " + ", ".join(plain) + ")"
        return entry + f"\n⚠️ {reason} {row['days_in_court']}d - {text}"

    if stalled:
        lines.append(f"\n**⚠️ Waiting on an admin more than {STALE_DAYS} days**")
        lines += [line_with_ping(r, "waiting on an admin") for r in stalled]
    if untriaged:
        lines.append(f"\n**⚠️ No admin has responded yet (open {UNTRIAGED_DAYS}+ days)**")
        lines += [line_with_ping(r, "waiting on an admin") for r in untriaged]
    if unroutable:
        lines.append("\n**⚠️ Region could not be determined - needs manual triage**")
        lines += [line(r) for r in unroutable]
    if dormant:
        lines.append(f"\n**😴 Waiting on the applicant more than {DORMANT_DAYS} days - consider closing**")
        lines += [line(r) for r in dormant]
    if active:
        lines.append("\n**In progress**")
        lines += [line(r) for r in active]
    lines.append("\n_Automated weekly review - posted every Sunday._")
    return "\n".join(lines)

# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv
    verbose = "--verbose" in sys.argv

    admins = sysops()
    managers = region_managers()
    known_ids = territory_continents()
    titles = open_applications()

    rows = []
    for title in titles:
        row = application_row(title, known_ids, admins)
        if row:
            rows.append(row)
    rows.sort(key=lambda r: r["days_in_court"], reverse=True)

    token = load_discord_token()
    cache = load_cache()
    handles = {name for info in managers.values() for name in info["managers"]}
    pingable = {name.lower(): discord_id(name, token, cache) for name in handles}
    pingable = {k: v for k, v in pingable.items() if v}

    stalled, untriaged, unroutable, dormant, active = classify(rows)

    if rows:
        print(render(rows, managers, pingable))
    if verbose:
        print("\n--- raw rows ---")
        for row in rows:
            print(json.dumps(row, ensure_ascii=False))

    if not dry_run:
        os.makedirs(VAR_DIR, exist_ok=True)
        state = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "script": "territoryReview",
            "open": len(rows),
            "stalled": [r["tid"] or r["title"] for r in stalled],
            "untriaged": [r["tid"] or r["title"] for r in untriaged],
            "unroutable": [r["tid"] or r["title"] for r in unroutable],
            "dormant": [r["tid"] or r["title"] for r in dormant],
            "active": [r["tid"] or r["title"] for r in active],
            "managers": managers,
            "unpingable": sorted(n for n in handles if n.lower() not in pingable),
            "applications": [
                {k: r[k] for k in ("tid", "title", "continent", "creator", "age",
                                   "court", "days_in_court", "admin_posts",
                                   "days_since_admin", "engaged_admins")}
                for r in rows],
        }
        with open(JSON_PATH, "w") as handle:
            json.dump(state, handle, indent=1, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                            # noqa: BLE001 - reported, not swallowed
        print(f"TERRITORY REVIEW FAILED: {exc}")
        sys.exit(1)
