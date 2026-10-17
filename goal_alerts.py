#!/usr/bin/env python3
"""
Goal alerts: ESPN public scoreboard feed (free, no key)  ->  Telegram.

* Reads today's tracked matches from tracked_matches.csv (export of your sheet).
* Links each match to an ESPN event (per-league fuzzy team-name matching).
* Polls only the leagues that have a tracked match in play (kick-off -10min .. +3h),
  every POLL_SECONDS, and compares scores.
* Telegram message the moment a tracked score goes up; scorer added when ESPN lists it.
* GitHub jobs are capped at 6h, so the script hands its state to a fresh run of the
  same workflow (workflow_dispatch) before the cap - no gaps, no duplicates.

NOTE: ESPN's feed is unofficial/undocumented. The parser is defensive, and the
'tracker started' message reports how many events ESPN returned per league.
"""
import csv, html, json, logging, os, re, sys, threading, time, unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import requests

ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{lg}/scoreboard"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal goal-alert bot)"}
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("FOOTBALL_CHAT_ID", "")
POLL = float(os.environ.get("POLL_SECONDS", "20"))
MAX_RUN = float(os.environ.get("MAX_RUNTIME_MIN", "340")) * 60
CSV_PATH = os.environ.get("CSV_PATH", "tracked_matches.csv")
CSV_UTC_OFFSET = float(os.environ.get("CSV_UTC_OFFSET_HOURS", "0"))
FT_ALERTS = os.environ.get("FT_ALERTS", "1") == "1"
MIN_SIDE, MIN_AVG = 0.78, 0.82

# sheet "League" text  ->  ESPN league code (extend / override as needed)
LEAGUES = {"premier league": "eng.1", "championship": "eng.2", "efl championship": "eng.2",
           "la liga": "esp.1", "bundesliga": "ger.1", "serie a": "ita.1", "ligue 1": "fra.1",
           "eredivisie": "ned.1", "primeira liga": "por.1", "mls": "usa.1"}
try:
    LEAGUES.update({k.lower(): v for k, v in json.loads(os.environ.get("LEAGUE_MAP", "{}")).items()})
except ValueError:
    pass

log = logging.getLogger("goals")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

# ------------------------------------------------------------------ helpers
def fetch_board(lg, day, tries=3):
    for i in range(tries):
        try:
            r = requests.get(ESPN_URL.format(lg=lg), headers=HEADERS, timeout=10,
                             params={"dates": day.strftime("%Y%m%d"), "limit": 100})
            if r.status_code == 429:
                time.sleep(5 * (i + 1)); continue
            r.raise_for_status()
            return r.json().get("events", []) or []
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))

def tg_send(text):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=10)
        return r.json()["result"]["message_id"]
    except Exception as e:
        log.error("telegram send failed: %s", e)

def tg_edit(msg_id, text):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/editMessageText",
                      json={"chat_id": TG_CHAT, "message_id": msg_id, "text": text,
                            "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=10)
    except Exception as e:
        log.error("telegram edit failed: %s", e)

# ------------------------------------------------------- name normalisation
STOP = {"fc", "afc", "cf", "cfc", "sc", "sv", "fsv", "vfb", "vfl", "tsv", "ac", "as", "ssc",
        "fk", "rc", "rcd", "ud", "cd", "ca", "club", "de", "deportivo", "calcio", "bsc"}
TOKEN_MAP = {"utd": "united", "man": "manchester"}
ALIAS = {"internazionale milano": "inter", "internazionale": "inter", "inter milan": "inter",
         "bayern munchen": "bayern munich", "stade brestois": "brest", "brestois": "brest",
         "west bromwich albion": "west brom", "queens park rangers": "qpr",
         "paris saint germain": "psg", "borussia monchengladbach": "gladbach",
         "hamburger": "hamburg", "racing lens": "lens"}

def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s.replace("&", " and "))
    toks = [TOKEN_MAP.get(t, t) for t in s.split() if not t.isdigit()]
    return " ".join(t for t in toks if t not in STOP)

def variants(s):
    n = norm(s)
    return {n, ALIAS.get(n, n)}

def sim(a, b):
    best = 0.0
    for x in variants(a):
        for y in variants(b):
            if not x or not y:
                continue
            tx, ty = set(x.split()), set(y.split())
            best = max(best, len(tx & ty) / min(len(tx), len(ty)),
                       SequenceMatcher(None, x, y).ratio())
    return best

def best_event(home, away, events):
    best, score = None, 0
    for e in events:
        h, a = sim(home, e["home"]), sim(away, e["away"])
        if h >= MIN_SIDE and a >= MIN_SIDE and (h + a) / 2 >= MIN_AVG and (h + a) > score:
            best, score = e, h + a
    return best

# ------------------------------------------------------------ ESPN parsing
def parse(ev):
    try:
        comp = (ev.get("competitions") or [{}])[0]
        side = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        h, a = side["home"], side["away"]
        st = ev.get("status") or comp.get("status") or {}
        typ = st.get("type") or {}
        score = lambda c: int(float(c.get("score") or 0))
        name = lambda c: (c.get("team") or {}).get("displayName") or (c.get("team") or {}).get("name") or ""
        return {"id": str(ev["id"]), "home": name(h), "away": name(a), "hs": score(h), "as": score(a),
                "state": typ.get("state", ""), "tname": typ.get("name", ""),
                "clock": st.get("displayClock") or typ.get("shortDetail") or "",
                "details": comp.get("details") or []}
    except Exception as e:
        log.warning("could not parse event %s: %s", ev.get("id"), e)
        return None

def minute(e):
    if e["tname"] == "STATUS_HALFTIME":
        return "HT"
    return e["clock"]

def last_goal_text(details):
    plays = [d for d in details if d.get("scoringPlay") or "goal" in ((d.get("type") or {}).get("text", "").lower())]
    if not plays:
        return None
    d = plays[-1]
    ath = (d.get("athletesInvolved") or [{}])[0]
    who = ath.get("displayName") or ath.get("shortName")
    if not who:
        return None
    t = ((d.get("type") or {}).get("text") or "").lower()
    tag = " [own goal]" if "own" in t else " [pen]" if "penalty" in t else ""
    return f"👤 {html.escape(who)}{tag}"

# ------------------------------------------------------------ tracked list
def load_tracked(path, day):
    out = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    head = [h.strip().lower() for h in rows[0]]
    ci = lambda key: next(i for i, h in enumerate(head) if key in h)
    c_lg, c_dt, c_h, c_a = ci("league"), ci("date"), ci("home"), ci("away")
    clean = lambda s: re.sub(r"\s*\(.*$", "", s).strip()
    for r in rows[1:]:
        if len(r) <= max(c_lg, c_dt, c_h, c_a) or not r[c_h].strip():
            continue
        ko = datetime.strptime(r[c_dt].strip()[:16], "%Y-%m-%d %H:%M") - timedelta(hours=CSV_UTC_OFFSET)
        ko = ko.replace(tzinfo=timezone.utc)
        if ko.date() == day:
            out.append({"league": r[c_lg].strip(), "kickoff": ko,
                        "home": clean(r[c_h]), "away": clean(r[c_a])})
    return out

def resolve(tracked, day):
    codes = sorted({LEAGUES[m["league"].lower()] for m in tracked if m["league"].lower() in LEAGUES})
    boards, counts = {}, {}
    for lg in codes:
        try:
            boards[lg] = [p for p in (parse(e) for e in fetch_board(lg, day)) if p]
        except Exception as e:
            log.error("scoreboard %s failed: %s", lg, e); boards[lg] = []
        counts[lg] = len(boards[lg])
    matched, missing = {}, []
    for m in tracked:
        lg = LEAGUES.get(m["league"].lower())
        e = best_event(m["home"], m["away"], boards.get(lg, [])) if lg else None
        if e:
            matched[e["id"]] = {**m, "lg": lg, "home": e["home"], "away": e["away"]}
        else:
            missing.append(f'{m["home"]} v {m["away"]}' + ("" if lg else f' (unknown league "{m["league"]}")'))
    return matched, missing, counts

# ----------------------------------------------------------------- alerts
def enrich(mid, base, lg, day, eid):
    for wait in (20, 45):
        time.sleep(wait)
        try:
            for e in fetch_board(lg, day):
                p = parse(e)
                if p and p["id"] == eid:
                    txt = last_goal_text(p["details"])
                    if txt:
                        tg_edit(mid, base + "\n" + txt); return
        except Exception as ex:
            log.warning("enrich failed: %s", ex)

def goal_alert(e, info, prev, cur, day):
    home, away = html.escape(info["home"]), html.escape(info["away"])
    if cur[0] > prev[0] and cur[1] > prev[1]:
        who = "both teams"
    else:
        who = home if cur[0] > prev[0] else away
    text = (f"⚽ <b>GOAL — {who}!</b>\n{html.escape(info['league'])}\n"
            f"{home} <b>{cur[0]} – {cur[1]}</b> {away}   ⏱ {minute(e)}")
    scorer = last_goal_text(e["details"])
    mid = tg_send(text + ("\n" + scorer if scorer else ""))
    log.info("GOAL %s %s-%s %s", info["home"], cur[0], cur[1], info["away"])
    if mid and not scorer:
        threading.Thread(target=enrich, args=(mid, text, info["lg"], day, e["id"]), daemon=True).start()

def correction_alert(info, prev, cur):
    tg_send(f"🚫 <b>Score corrected</b> (goal disallowed?)\n{html.escape(info['home'])} "
            f"<b>{cur[0]} – {cur[1]}</b> {html.escape(info['away'])}   (was {prev[0]} – {prev[1]})")

# ------------------------------------------------------------- handover
def handover(scores, done):
    repo, tok = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not (repo and tok):
        log.error("no GitHub env - cannot hand over"); return
    state = json.dumps({"scores": scores, "done": sorted(done)})
    r = requests.post(
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        f"{os.environ.get('WORKFLOW_FILE', 'goal-alerts.yml')}/dispatches",
        headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
        json={"ref": os.environ.get("GITHUB_REF_NAME", "main"), "inputs": {"state": state}}, timeout=15)
    log.info("handover -> HTTP %s", r.status_code)

# -------------------------------------------------------------------- main
def main():
    start = time.time()
    day = datetime.now(timezone.utc).date()
    if os.environ.get("TRACK_DATE"):
        day = datetime.strptime(os.environ["TRACK_DATE"], "%Y-%m-%d").date()
    state = json.loads(os.environ.get("HANDOVER_STATE") or "{}")
    tracked = load_tracked(CSV_PATH, day)
    if not tracked:
        log.info("No tracked matches for %s - nothing to do.", day); return
    matched, missing, counts = resolve(tracked, day)
    if not state:
        msg = f"✅ <b>Goal tracker started</b> — {len(matched)}/{len(tracked)} matches linked.\n"
        msg += "ESPN events found: " + ", ".join(f"{k}: {v}" for k, v in counts.items())
        if missing:
            msg += "\n⚠️ Not linked:\n" + "\n".join(html.escape(m) for m in missing)
        tg_send(msg)
    if not matched:
        return
    scores = {k: tuple(v) for k, v in state.get("scores", {}).items()}
    done = set(state.get("done", []))
    silent = not state
    first_ko = min(m["kickoff"] for m in matched.values())
    last_ko = max(m["kickoff"] for m in matched.values())

    while not state and datetime.now(timezone.utc) < first_ko - timedelta(minutes=10):
        if time.time() - start > MAX_RUN:
            handover({}, done); return
        time.sleep(30)

    polls, fails = 0, 0
    while True:
        t0 = time.time()
        now = datetime.now(timezone.utc)
        pending = {i for i in matched if i not in done}
        if not pending or now > last_ko + timedelta(hours=3, minutes=30):
            log.info("All tracked matches finished - stopping."); return
        if t0 - start > MAX_RUN:
            handover({k: list(v) for k, v in scores.items()}, done); return
        active = sorted({matched[i]["lg"] for i in pending
                         if matched[i]["kickoff"] - timedelta(minutes=10) <= now
                         <= matched[i]["kickoff"] + timedelta(hours=3)})
        try:
            def grab(lg):
                try:
                    return [p for p in (parse(e) for e in fetch_board(lg, day)) if p]
                except Exception as ex:
                    log.warning("%s fetch failed: %s", lg, ex); return None
            if active:
                with ThreadPoolExecutor(max_workers=len(active)) as ex:
                    results = list(ex.map(grab, active))
                if all(r is None for r in results):
                    raise RuntimeError("all ESPN requests failed")
                fails = 0; polls += 1
                for events in results:
                    for e in events or []:
                        eid = e["id"]
                        if eid not in matched or eid in done:
                            continue
                        info, cur = matched[eid], (e["hs"], e["as"])
                        if e["state"] == "pre":
                            continue
                        if e["state"] == "in" or e["state"] == "post":
                            if eid not in scores:
                                scores[eid] = cur if silent else (0, 0)
                            prev = scores[eid]
                            if cur[0] > prev[0] or cur[1] > prev[1]:
                                goal_alert(e, info, prev, cur, day)
                            elif cur[0] < prev[0] or cur[1] < prev[1]:
                                correction_alert(info, prev, cur)
                            scores[eid] = cur
                        if e["state"] == "post":
                            if e["tname"] in ("STATUS_POSTPONED", "STATUS_CANCELED"):
                                tg_send(f"⏸ {html.escape(info['home'])} v {html.escape(info['away'])}: "
                                        f"{e['tname'].replace('STATUS_', '').title()}")
                            elif FT_ALERTS and not silent:
                                tg_send(f"🏁 <b>FT</b>  {html.escape(info['home'])} <b>{cur[0]} – {cur[1]}</b> {html.escape(info['away'])}")
                            done.add(eid)
                silent = False
                if polls % 100 == 0:
                    log.info("polls=%s active=%s", polls, active)
        except Exception as e:
            fails += 1; log.error("poll failed: %s", e)
            if fails == 10:
                tg_send("⚠️ Goal tracker: 10 failed polls in a row - ESPN feed may be down or changed.")
        time.sleep(max(0.0, POLL - (time.time() - t0)))

if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
Goal alerts: API-Football (live feed)  ->  Telegram.

* Reads today's tracked matches from tracked_matches.csv (export of your sheet).
* Maps each match to an API-Football fixture id (fuzzy team-name matching).
* Polls ONE endpoint (fixtures?live=all) every POLL_SECONDS and compares scores.
* Sends a Telegram message the moment a tracked score goes up, then edits the
  message a few seconds later to add the scorer.
* GitHub jobs are capped at 6h, so the script hands its state to a fresh run of
  the same workflow (workflow_dispatch) before the cap - no gaps, no duplicates.
"""
import csv, html, json, logging, os, re, sys, threading, time, unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import requests

API_BASE = "https://v3.football.api-sports.io"
API_KEY = os.environ.get("API_FOOTBALL_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("FOOTBALL_CHAT_ID", "")
POLL = float(os.environ.get("POLL_SECONDS", "20"))
MAX_RUN = float(os.environ.get("MAX_RUNTIME_MIN", "340")) * 60
CSV_PATH = os.environ.get("CSV_PATH", "tracked_matches.csv")
CSV_UTC_OFFSET = float(os.environ.get("CSV_UTC_OFFSET_HOURS", "0"))  # your sheet's times vs UTC
FT_ALERTS = os.environ.get("FT_ALERTS", "1") == "1"
MIN_SIDE, MIN_AVG = 0.78, 0.82  # fuzzy-match thresholds

log = logging.getLogger("goals")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
_remaining = {"v": None}

# ------------------------------------------------------------------ helpers
def api_get(path, params=None, tries=4):
    for i in range(tries):
        try:
            r = requests.get(f"{API_BASE}/{path}", params=params,
                             headers={"x-apisports-key": API_KEY}, timeout=10)
            _remaining["v"] = r.headers.get("x-ratelimit-requests-remaining", _remaining["v"])
            if r.status_code == 429:
                time.sleep(5 * (i + 1)); continue
            r.raise_for_status()
            data = r.json()
            if data.get("errors"):
                raise RuntimeError(f"API error: {data['errors']}")
            return data
        except Exception as e:
            if i == tries - 1:
                raise
            log.warning("api retry (%s)", e); time.sleep(2 * (i + 1))

def tg_send(text):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=10)
        return r.json()["result"]["message_id"]
    except Exception as e:
        log.error("telegram send failed: %s", e)

def tg_edit(msg_id, text):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/editMessageText",
                      json={"chat_id": TG_CHAT, "message_id": msg_id, "text": text,
                            "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=10)
    except Exception as e:
        log.error("telegram edit failed: %s", e)

# ------------------------------------------------------- name normalisation
STOP = {"fc", "afc", "cf", "cfc", "sc", "sv", "fsv", "vfb", "vfl", "tsv", "ac", "as", "ssc",
        "fk", "rc", "rcd", "ud", "cd", "ca", "club", "de", "deportivo", "calcio", "bsc"}
TOKEN_MAP = {"utd": "united", "man": "manchester"}
ALIAS = {"internazionale milano": "inter", "internazionale": "inter",
         "bayern munchen": "bayern munich", "stade brestois": "brest", "brestois": "brest",
         "west bromwich albion": "west brom", "queens park rangers": "qpr",
         "paris saint germain": "psg", "borussia monchengladbach": "gladbach"}

def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s.replace("&", " and "))
    toks = [TOKEN_MAP.get(t, t) for t in s.split() if not t.isdigit()]
    toks = [t for t in toks if t not in STOP]
    return " ".join(toks)

def variants(s):
    n = norm(s)
    return {n, ALIAS.get(n, n)}

def sim(a, b):
    best = 0.0
    for x in variants(a):
        for y in variants(b):
            if not x or not y:
                continue
            tx, ty = set(x.split()), set(y.split())
            best = max(best, len(tx & ty) / min(len(tx), len(ty)),
                       SequenceMatcher(None, x, y).ratio())
    return best

def best_fixture(home, away, fixtures):
    best, score = None, 0
    for f in fixtures:
        h = sim(home, f["teams"]["home"]["name"]); a = sim(away, f["teams"]["away"]["name"])
        if h >= MIN_SIDE and a >= MIN_SIDE and (h + a) / 2 >= MIN_AVG and (h + a) > score:
            best, score = f, h + a
    return best

# ------------------------------------------------------------ tracked list
def load_tracked(path, day):
    """Rows of the sheet export. Strips the '(Win Home: ..)' odds text from team names."""
    out = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    head = [h.strip().lower() for h in rows[0]]
    ci = lambda key: next(i for i, h in enumerate(head) if key in h)
    c_lg, c_dt, c_h = ci("league"), ci("date"), ci("home")
    c_a = next(i for i, h in enumerate(head) if "away" in h)
    clean = lambda s: re.sub(r"\s*\(.*$", "", s).strip()
    for r in rows[1:]:
        if len(r) <= max(c_lg, c_dt, c_h, c_a) or not r[c_h].strip():
            continue
        ko = datetime.strptime(r[c_dt].strip()[:16], "%Y-%m-%d %H:%M") - timedelta(hours=CSV_UTC_OFFSET)
        ko = ko.replace(tzinfo=timezone.utc)
        if ko.date() == day:
            out.append({"league": r[c_lg].strip(), "kickoff": ko,
                        "home": clean(r[c_h]), "away": clean(r[c_a])})
    return out

def resolve(tracked, day):
    fx = api_get("fixtures", {"date": day.isoformat()})["response"]
    matched, missing = {}, []
    for m in tracked:
        f = best_fixture(m["home"], m["away"], fx)
        if f:
            matched[f["fixture"]["id"]] = {**m, "league": f["league"]["name"],
                                           "home": f["teams"]["home"]["name"],
                                           "away": f["teams"]["away"]["name"]}
        else:
            missing.append(f'{m["home"]} v {m["away"]}')
    return matched, missing

# ----------------------------------------------------------------- alerts
def minute(f):
    st = f["fixture"]["status"]
    if st.get("short") == "HT":
        return "HT"
    el = st.get("elapsed")
    return f"{el}'" if el else ""

def enrich(msg_id, base_text, fid, side_name):
    """Add scorer to the message once the feed publishes the event (best effort)."""
    for wait in (20, 45):
        time.sleep(wait)
        try:
            ev = [e for e in api_get("fixtures/events", {"fixture": fid})["response"]
                  if e.get("type") == "Goal" and "Missed" not in (e.get("detail") or "")]
            if ev:
                e = ev[-1]
                who = e["player"].get("name") or "?"
                extra = f' (assist {e["assist"]["name"]})' if e.get("assist", {}).get("name") else ""
                tag = {"Own Goal": " [own goal]", "Penalty": " [pen]"}.get(e.get("detail"), "")
                tg_edit(msg_id, base_text + f"\n👤 {html.escape(who)}{html.escape(extra)}{tag}")
                return
        except Exception as ex:
            log.warning("enrich failed: %s", ex)

def goal_alert(f, info, prev, cur):
    home, away = html.escape(info["home"]), html.escape(info["away"])
    if cur[0] > prev[0] and cur[1] > prev[1]:
        who = "both teams"
    else:
        who = home if cur[0] > prev[0] else away
    text = (f"⚽ <b>GOAL — {who}!</b>\n{html.escape(info['league'])}\n"
            f"{home} <b>{cur[0]} – {cur[1]}</b> {away}   ⏱ {minute(f)}")
    mid = tg_send(text)
    log.info("GOAL %s %s-%s %s", info["home"], cur[0], cur[1], info["away"])
    if mid:
        threading.Thread(target=enrich, args=(mid, text, f["fixture"]["id"], who), daemon=True).start()

def correction_alert(f, info, prev, cur):
    tg_send(f"🚫 <b>Score corrected</b> (goal disallowed?)\n{html.escape(info['home'])} "
            f"<b>{cur[0]} – {cur[1]}</b> {html.escape(info['away'])}   (was {prev[0]} – {prev[1]})")

# ------------------------------------------------------------- handover
def handover(scores, done, seen):
    repo = os.environ.get("GITHUB_REPOSITORY"); tok = os.environ.get("GITHUB_TOKEN")
    if not (repo and tok):
        log.error("no GitHub env - cannot hand over"); return
    state = json.dumps({"scores": {str(k): v for k, v in scores.items()},
                        "done": sorted(done), "seen": sorted(seen)})
    r = requests.post(
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        f"{os.environ.get('WORKFLOW_FILE', 'goal-alerts.yml')}/dispatches",
        headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
        json={"ref": os.environ.get("GITHUB_REF_NAME", "main"), "inputs": {"state": state}}, timeout=15)
    log.info("handover -> HTTP %s", r.status_code)

# -------------------------------------------------------------------- main
def main():
    start = time.time()
    day = datetime.now(timezone.utc).date()
    if os.environ.get("TRACK_DATE"):
        day = datetime.strptime(os.environ["TRACK_DATE"], "%Y-%m-%d").date()
    state = json.loads(os.environ.get("HANDOVER_STATE") or "{}")
    tracked = load_tracked(CSV_PATH, day)
    if not tracked:
        log.info("No tracked matches for %s - nothing to do.", day); return
    matched, missing = resolve(tracked, day)
    if not state:
        msg = f"✅ <b>Goal tracker started</b> — {len(matched)}/{len(tracked)} matches linked."
        if missing:
            msg += "\n⚠️ Not found in feed:\n" + "\n".join(html.escape(m) for m in missing)
        tg_send(msg)
    if not matched:
        return
    scores = {int(k): tuple(v) for k, v in state.get("scores", {}).items()}
    done, seen = set(state.get("done", [])), set(state.get("seen", []))
    miss = {}
    silent = not state                       # fresh start: baseline whatever is already live
    last_ko = max(m["kickoff"] for m in matched.values())
    first_ko = min(m["kickoff"] for m in matched.values())

    while not state and datetime.now(timezone.utc) < first_ko - timedelta(minutes=5):
        if time.time() - start > MAX_RUN:
            handover(scores, done, seen); return
        time.sleep(30)

    polls, warned, fails = 0, False, 0
    while True:
        t0 = time.time()
        pending = set(matched) - done
        if not pending or datetime.now(timezone.utc) > last_ko + timedelta(hours=3):
            log.info("All tracked matches finished - stopping."); return
        if t0 - start > MAX_RUN:
            handover(scores, done, seen); return
        try:
            live = {f["fixture"]["id"]: f for f in api_get("fixtures", {"live": "all"})["response"]
                    if f["fixture"]["id"] in matched}
            fails = 0; polls += 1
            for fid, f in live.items():
                cur = (f["goals"]["home"] or 0, f["goals"]["away"] or 0)
                seen.add(fid); miss[fid] = 0
                if fid not in scores:
                    scores[fid] = cur if silent else (0, 0)
                prev = scores[fid]
                if cur[0] + cur[1] > prev[0] + prev[1] or cur[0] > prev[0] or cur[1] > prev[1]:
                    goal_alert(f, matched[fid], prev, cur)
                elif cur[0] < prev[0] or cur[1] < prev[1]:
                    correction_alert(f, matched[fid], prev, cur)
                scores[fid] = cur
            silent = False
            for fid in list(seen - done):          # was live, now gone -> check final score
                if fid in live:
                    continue
                miss[fid] = miss.get(fid, 0) + 1
                if miss[fid] >= 3:
                    r = api_get("fixtures", {"id": fid})["response"]
                    if r:
                        f = r[0]; cur = (f["goals"]["home"] or 0, f["goals"]["away"] or 0)
                        prev = scores.get(fid, (0, 0))
                        if cur[0] > prev[0] or cur[1] > prev[1]:
                            goal_alert(f, matched[fid], prev, cur)
                        if FT_ALERTS:
                            i = matched[fid]
                            tg_send(f"🏁 <b>FT</b>  {html.escape(i['home'])} <b>{cur[0]} – {cur[1]}</b> {html.escape(i['away'])}")
                        scores[fid] = cur
                    done.add(fid)
            rem = _remaining["v"]
            if polls % 100 == 0:
                log.info("polls=%s requests_remaining_today=%s", polls, rem)
            if rem and rem.isdigit() and int(rem) < 150 and not warned:
                tg_send(f"⚠️ API-Football requests left today: {rem}"); warned = True
        except Exception as e:
            fails += 1; log.error("poll failed: %s", e)
            if fails == 10:
                tg_send("⚠️ Goal tracker: 10 failed polls in a row - check the feed/API key.")
        time.sleep(max(0.0, POLL - (time.time() - t0)))

if __name__ == "__main__":
    main()
