#!/usr/bin/env python3
"""
Goal alerts: API-Football -> Telegram. Works on the FREE plan (100 requests/day)
and automatically polls faster on paid plans.

How the free budget is stretched
  * ONE request per check (fixtures?live=all) - never one request per match.
  * Checks only happen while a tracked match can be in play (kick-off -2min .. +2h05).
  * The gap between checks is recalculated every time:
        seconds of match-time still ahead / API requests still left today
    so the whole day's requests are spread across the whole day (~every 5-6 min on 100/day).
  * Scorers come from the same live response (no extra request per goal).
  * A finished match costs one batched lookup for its final score.
"""
import csv, html, io, json, logging, os, re, threading, time, unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import requests

API_BASE = "https://v3.football.api-sports.io"
API_KEY = os.environ.get("API_FOOTBALL_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("FOOTBALL_CHAT_ID", "")
MIN_POLL = float(os.environ.get("MIN_POLL_SECONDS", "20"))      # fastest allowed (paid plans)
MAX_POLL = float(os.environ.get("MAX_POLL_SECONDS", "600"))     # slowest allowed
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "100"))         # used only if the API sends no header
RESERVE = int(os.environ.get("RESERVE_REQUESTS", "10"))         # kept back for final-score lookups
MAX_RUN = float(os.environ.get("MAX_RUNTIME_MIN", "340")) * 60
CSV_PATH = os.environ.get("CSV_PATH", "tracked_matches.csv")
# Times in the CSV / sheet are read as this UTC offset. Kenya (EAT) = UTC+3, so default is 3.
# Override with the CSV_UTC_OFFSET_HOURS environment variable if ever needed.
CSV_UTC_OFFSET = float(os.environ.get("CSV_UTC_OFFSET_HOURS", "3"))
FT_ALERTS = os.environ.get("FT_ALERTS", "1") == "1"
MIN_SIDE, MIN_AVG = 0.78, 0.82
FINAL = {"FT", "AET", "PEN", "AWD", "WO", "PST", "CANC", "ABD"}

log = logging.getLogger("goals")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
_used = {"n": 0}
_remaining = {"v": None}


# ------------------------------------------------------------------ helpers
def api_get(path, params=None, tries=3):
    for i in range(tries):
        _used["n"] += 1
        r = None
        try:
            r = requests.get(f"{API_BASE}/{path}", params=params,
                             headers={"x-apisports-key": API_KEY}, timeout=10)
            _remaining["v"] = r.headers.get("x-ratelimit-requests-remaining", _remaining["v"])
            if r.status_code in (401, 403):
                raise PermissionError(f"HTTP {r.status_code} - check the API_FOOTBALL_KEY secret")
            if r.status_code == 429:
                time.sleep(5 * (i + 1)); continue
            r.raise_for_status()
            data = r.json()
            if data.get("errors"):
                raise RuntimeError(f"API error: {data['errors']}")
            return data
        except (PermissionError, RuntimeError):
            raise
        except Exception as e:
            if i == tries - 1:
                raise
            log.warning("api retry (%s)", e); time.sleep(2 * (i + 1))


def budget_left():
    v = _remaining["v"]
    return int(v) if v is not None and str(v).isdigit() else max(0, DAILY_LIMIT - _used["n"])


def tg_send(text):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=10)
        return r.json()["result"]["message_id"]
    except Exception as e:
        log.error("telegram send failed: %s", e)


# ------------------------------------------------------- name normalisation
STOP = {"fc", "afc", "cf", "cfc", "sc", "sv", "fsv", "vfb", "vfl", "tsv", "ac", "as", "ssc",
        "fk", "rc", "rcd", "ud", "cd", "ca", "club", "de", "deportivo", "calcio", "bsc"}
TOKEN_MAP = {"utd": "united", "man": "manchester"}
ALIAS = {"internazionale milano": "inter", "internazionale": "inter", "inter milan": "inter",
         "bayern munchen": "bayern munich", "stade brestois": "brest", "brestois": "brest",
         "west bromwich albion": "west brom", "queens park rangers": "qpr",
         "paris saint germain": "psg", "borussia monchengladbach": "gladbach",
         "hamburger": "hamburg", "racing lens": "lens",
         # added: French clubs whose official names differ from the API-Football names
         "stade rennais": "rennes", "olympique lyonnais": "lyon",
         "olympique marseille": "marseille", "wolverhampton wanderers": "wolves"}


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


def best_fixture(home, away, fixtures):
    best, score = None, 0
    for f in fixtures:
        h = sim(home, f["teams"]["home"]["name"]); a = sim(away, f["teams"]["away"]["name"])
        if h >= MIN_SIDE and a >= MIN_SIDE and (h + a) / 2 >= MIN_AVG and (h + a) > score:
            best, score = f, h + a
    return best


# ------------------------------------------------------------ tracked list
SHEET_URL = os.environ.get("SHEET_CSV_URL", "").strip()
_sheet = {"rows": 0, "skipped": 0}
DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M")


def read_rows():
    """Rows from the published Google Sheet (SHEET_CSV_URL) or, if unset, the repo CSV file."""
    if SHEET_URL:
        r = requests.get(SHEET_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        text = r.content.decode("utf-8-sig")
        if text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
            raise RuntimeError("Google returned a web page instead of CSV - the sheet link is not "
                               "published/public (File > Share > Publish to web > CSV)")
        return list(csv.reader(io.StringIO(text)))
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as fh:
        return list(csv.reader(fh))


def parse_dt(text):
    text = text.strip()
    for fmt in DT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def load_tracked(day):
    rows = read_rows()
    hi = next((i for i, r in enumerate(rows)
               if any("league" in c.lower() for c in r) and any("home" in c.lower() for c in r)), None)
    if hi is None:
        raise RuntimeError("Could not find the header row (League / Date / Home / Away) in the sheet")
    head = [h.strip().lower() for h in rows[hi]]
    ci = lambda key: next(i for i, h in enumerate(head) if key in h)
    c_lg, c_dt, c_h, c_a = ci("league"), ci("date"), ci("home"), ci("away")
    clean = lambda s: re.sub(r"\s*\(.*$", "", s).strip()
    out = []
    _sheet["rows"] = _sheet["skipped"] = 0
    for r in rows[hi + 1:]:
        if len(r) <= max(c_lg, c_dt, c_h, c_a) or not r[c_h].strip():
            continue
        _sheet["rows"] += 1
        ko = parse_dt(r[c_dt])
        if ko is None:
            _sheet["skipped"] += 1; continue
        ko = (ko - timedelta(hours=CSV_UTC_OFFSET)).replace(tzinfo=timezone.utc)
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


# ------------------------------------------------- budget-aware scheduling
def windows(matched, done):
    w = sorted((m["kickoff"] - timedelta(minutes=2), m["kickoff"] + timedelta(minutes=125))
               for i, m in matched.items() if i not in done)
    merged = []
    for s, e in w:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def active_left(merged, now):
    return sum(max(0.0, (e - max(s, now)).total_seconds()) for s, e in merged)


def next_interval(merged, now):
    budget = max(1, budget_left() - RESERVE)
    return min(MAX_POLL, max(MIN_POLL, active_left(merged, now) / budget))


# ----------------------------------------------------------------- alerts
def minute(f):
    st = f["fixture"]["status"]
    return "HT" if st.get("short") == "HT" else (f"{st['elapsed']}'" if st.get("elapsed") else "")


def goal_events(f):
    return [e for e in (f.get("events") or []) if e.get("type") == "Goal"
            and "Missed" not in (e.get("detail") or "")]


def goal_alert(f, info, prev, cur):
    home, away = html.escape(info["home"]), html.escape(info["away"])
    n = max(1, (cur[0] + cur[1]) - (prev[0] + prev[1]))
    if n > 1:
        title = f"{n} NEW GOALS"
    else:
        title = "GOAL — " + ("both teams" if cur[0] > prev[0] and cur[1] > prev[1]
                             else home if cur[0] > prev[0] else away)
    text = (f"⚽ <b>{title}!</b>\n{html.escape(info['league'])}\n"
            f"{home} <b>{cur[0]} – {cur[1]}</b> {away} ⏱ {minute(f)}")
    for e in goal_events(f)[-n:]:
        tag = {"Own Goal": " [own goal]", "Penalty": " [pen]"}.get(e.get("detail"), "")
        who = (e.get("player") or {}).get("name") or "?"
        text += f"\n👤 {(e.get('time') or {}).get('elapsed', '?')}' {html.escape(who)}{tag} ({html.escape((e.get('team') or {}).get('name', ''))})"
    tg_send(text)
    log.info("GOAL %s %s-%s %s", info["home"], cur[0], cur[1], info["away"])


def correction_alert(info, prev, cur):
    tg_send(f"🚫 <b>Score corrected</b> (goal disallowed?)\n{html.escape(info['home'])} "
            f"<b>{cur[0]} – {cur[1]}</b> {html.escape(info['away'])} (was {prev[0]} – {prev[1]})")


# ------------------------------------------------------------- handover
def handover(scores, done, seen):
    repo, tok = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not (repo and tok):
        log.error("no GitHub env - cannot hand over"); return
    state = json.dumps({"scores": {str(k): v for k, v in scores.items()},
                        "done": sorted(done), "seen": sorted(seen), "used": _used["n"]})
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
    _used["n"] = state.get("used", 0)
    try:
        tracked = load_tracked(day)
    except Exception as e:
        tg_send(f"❌ <b>Goal tracker could not read your fixtures</b>\n{html.escape(str(e))}")
        raise
    if not tracked:
        log.info("No tracked matches for %s - nothing to do.", day)
        if _sheet["skipped"] and _sheet["skipped"] == _sheet["rows"] and not state:
            tg_send("⚠️ Goal tracker: could not read any dates in the sheet - check the Date / Time format.")
        return
    try:
        matched, missing = resolve(tracked, day)
    except Exception as e:
        tg_send(f"❌ <b>Goal tracker could not start</b>\n{html.escape(str(e))}")
        raise
    done = set(state.get("done", [])); seen = set(state.get("seen", []))
    scores = {int(k): tuple(v) for k, v in state.get("scores", {}).items()}
    if not state:
        iv = next_interval(windows(matched, done), datetime.now(timezone.utc))
        msg = (f"✅ <b>Goal tracker started</b> — {len(matched)}/{len(tracked)} matches linked "
               f"({_sheet['rows']} sheet rows read).\n"
               f"API requests left today: {budget_left()} · checking about every {iv/60:.1f} min")
        if missing:
            msg += "\n⚠️ Not found in feed:\n" + "\n".join(html.escape(m) for m in missing)
        tg_send(msg)
    if not matched:
        return
    silent = not state
    miss_strikes, warned = {}, False
    while True:
        t0 = time.time()
        now = datetime.now(timezone.utc)
        pending = {i for i in matched if i not in done}
        merged = windows(matched, done)
        if not pending or not merged:
            log.info("All tracked matches finished - stopping."); return
        if t0 - start > MAX_RUN:
            handover(scores, done, seen); return
        if not any(s <= now <= e for s, e in merged):   # between matches: sleep, no requests
            nxt = min((s for s, _ in merged if s > now), default=None)
            if nxt is None:
                log.info("No more match windows today - stopping."); return
            time.sleep(min(30.0, max(1.0, (nxt - now).total_seconds()))); continue
        if budget_left() <= 2:
            tg_send("⛔ Daily API-Football requests used up - no more alerts today.")
            return
        try:
            live = {f["fixture"]["id"]: f for f in api_get("fixtures", {"live": "all"})["response"]
                    if f["fixture"]["id"] in matched}
            for fid, f in live.items():
                cur = (f["goals"]["home"] or 0, f["goals"]["away"] or 0)
                seen.add(fid); miss_strikes.pop(fid, None)
                if fid not in scores:
                    scores[fid] = cur if silent else (0, 0)
                prev = scores[fid]
                if cur[0] > prev[0] or cur[1] > prev[1]:
                    goal_alert(f, matched[fid], prev, cur)
                elif cur[0] < prev[0] or cur[1] < prev[1]:
                    correction_alert(matched[fid], prev, cur)
                scores[fid] = cur
            silent = False
            gone = [fid for fid in seen - done if fid not in live]
            for i in range(0, len(gone), 20):           # one batched lookup per poll
                chunk = gone[i:i + 20]
                res = {f["fixture"]["id"]: f for f in
                       api_get("fixtures", {"ids": "-".join(map(str, chunk))})["response"]}
                for fid in chunk:
                    f = res.get(fid)
                    if f and f["fixture"]["status"].get("short") in FINAL:
                        cur = (f["goals"]["home"] or 0, f["goals"]["away"] or 0)
                        prev = scores.get(fid, (0, 0)); info = matched[fid]
                        if cur[0] > prev[0] or cur[1] > prev[1]:
                            goal_alert(f, info, prev, cur)
                        if FT_ALERTS:
                            tg_send(f"🏁 <b>FT</b> {html.escape(info['home'])} <b>{cur[0]} – {cur[1]}</b> {html.escape(info['away'])}")
                        scores[fid] = cur; done.add(fid)
                    else:
                        miss_strikes[fid] = miss_strikes.get(fid, 0) + 1
                        if miss_strikes[fid] >= 3:
                            done.add(fid)
        except PermissionError as e:
            tg_send(f"❌ Goal tracker stopped: {html.escape(str(e))}"); raise
        except Exception as e:
            log.error("poll failed: %s", e)
        iv = next_interval(windows(matched, done), datetime.now(timezone.utc))
        log.info("requests left=%s next check in %.0fs", budget_left(), iv)
        time.sleep(max(0.0, iv - (time.time() - t0)))


if __name__ == "__main__":
    main()
