"""
main.py
-------
Railway-ready Flask + Gunicorn app (`gunicorn main:app`) for
"The Unfiled Sessions" TikTok Comment Rotation & SMMFrog (/api/v2) Sender.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from flask import Flask, jsonify, render_template_string, request


# 🔧 KONFIGURACIJA (čita iz env varijable za Railway, sa fallbackom za lokalno)
PANEL_URL = os.environ.get("SMMFROG_PANEL_URL", "https://topsfollows.com/api/v2")
API_KEY = os.environ.get("SMMFROG_API_KEY", "94d76a3403276913f117b66a868e5575")
SERVICE_ID = int(os.environ.get("SMMFROG_SERVICE_ID", "4946"))  # TikTok Custom Comments na SMMFrog

app = Flask(__name__)


# ---------------------------------------------------------------------------
# 0. TIKTOK MOBILE-TO-PC URL RESOLVER
# ---------------------------------------------------------------------------
_SHORT_DOMAINS = ("vm.tiktok.com", "vt.tiktok.com", "m.tiktok.com")


def normalize_tiktok_pc_url(raw_url: str, timeout: int = 12) -> str:
    """
    Converts mobile/short TikTok links (vm.tiktok.com, vt.tiktok.com,
    tiktok.com/t/..., m.tiktok.com) into clean canonical PC URLs
    (https://www.tiktok.com/@user/video/ID) without tracking query params.
    """
    url = raw_url.strip()
    if not url:
        return url

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.lower()
    is_short = any(d in host for d in _SHORT_DOMAINS) or "/t/" in parsed.path

    resolved = url
    if is_short:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0.0.0 Safari/537.36"
                )
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resolved = resp.geturl() or url
        except Exception:
            resolved = url

    clean = urllib.parse.urlsplit(resolved)
    netloc = clean.netloc.lower()
    if netloc in ("tiktok.com", "m.tiktok.com"):
        netloc = "www.tiktok.com"

    # Keep only scheme + www.tiktok.com + clean path (strip ?is_from_webapp=... etc.)
    clean_path = clean.path.rstrip("/")
    return urllib.parse.urlunsplit(("https", netloc or "www.tiktok.com", clean_path, "", ""))


# ---------------------------------------------------------------------------
# 1. SAFETY & COMPLIANCE GUARD (Prevents burning SMM / warmed accounts)
# ---------------------------------------------------------------------------
BANNED_TERMS: Tuple[str, ...] = (
    "alversett",
    "alverset",
    "wiped",
    "went ghost",
    "ghost",
    "vanished",
    "disappeared",
    "manipulation",
    "manipulative",
    "toxic",
    "terrifying",
    "scary",
    "exposed",
    "delusions",
    "delusion",
    "illegal",
    "gaslight",
    "psychological warfare",
    "cheat codes",
    "cheat code",
    "scrubbed",
    "mind control",
    "brainwash",
    "cult",
    "stalk",
    "trauma",
)

ALLOWED_EXTRA_CHARS = {"’", "—", "…"}


def validate_comment(text: str) -> Tuple[bool, str]:
    """Validates a rendered comment against TikTok account-safety rules."""
    if not text or not text.strip():
        return False, "Empty comment"

    lowered = text.lower()
    for term in BANNED_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", lowered):
            return False, f"Contains banned trigger term: {term}"

    if "?" in text:
        return False, "Contains question mark (disabled by config)"

    for ch in text:
        if ord(ch) >= 128 and ch not in ALLOWED_EXTRA_CHARS:
            return False, f"Contains non-ASCII / homoglyph character: {repr(ch)}"

    if len(text) < 45 or len(text) > 250:
        return False, f"Length out of safe range ({len(text)} chars)"

    return True, "OK"


# ---------------------------------------------------------------------------
# 2. HIGH-CONVERTING COLD-TRAFFIC TEMPLATES (Categorized by Angle)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CommentTemplate:
    angle: str
    pattern: str


TEMPLATES: List[CommentTemplate] = [
    CommentTemplate(
        "page_41",
        "i had to {literally |actually |genuinely |}close {The Unfiled Sessions|the unfiled sessions}{ by Nora|} on page 41 and just sit in silence for {ten minutes|like ten minutes|a solid ten mins|a few minutes} because i realized i've been doing that exact thing my whole {adult life|life}{ without noticing| without even realizing it|}.",
    ),
    CommentTemplate(
        "page_41",
        "page 41 in {The Unfiled Sessions|the unfiled sessions}{ by Nora|} stopped me {mid-sentence|cold|right in my tracks}{ ngl| tbh|}, i never realized how much energy i {waste|spend} explaining myself to people who already made up their minds.",
    ),
    CommentTemplate(
        "page_41",
        "if you ever {pick up|read|open} {The Unfiled Sessions|the unfiled sessions}{ by Nora|}, pay attention to page 41 because that one {paragraph|section|page} explains {half the patterns|so many habits|the exact loops} we keep repeating{ for no reason| without thinking|}.",
    ),
    CommentTemplate(
        "questions_end",
        "someone on here mentioned {The Unfiled Sessions|the unfiled sessions} {last month|a few weeks ago|the other day} and i finally {read it|opened it} {last night|this week}, getting to the {17|seventeen} questions at the end felt {weirdly|almost too} personal like {the author|Nora} was reading my journal.",
    ),
    CommentTemplate(
        "questions_end",
        "the {17|seventeen} questions at the end of {The Unfiled Sessions|the unfiled sessions}{ by Nora|} {honestly|genuinely|actually} did more for my clarity in {twenty minutes|half an hour} than {months|a whole year|weeks} of overthinking{ ever did|}.",
    ),
    CommentTemplate(
        "questions_end",
        "i {genuinely|really|honestly} wish someone had handed me {The Unfiled Sessions|the unfiled sessions}{ by Nora|} {five|four|three} years ago, the part where she {breaks down|lays out} those {17|twenty} questions saved me from repeating the exact same {cycle|mistake} again.",
    ),
    CommentTemplate(
        "questions_end",
        "page 187 of {The Unfiled Sessions|the unfiled sessions}{ by Nora|} with the twenty questions {literally|quietly|calmly} called out every single pattern i've been {brushing off|ignoring|making excuses for}{ this year| lately|}.",
    ),
    CommentTemplate(
        "page_27",
        "my {sister|best friend|coworker|roommate} sent me a screenshot of page 27 from {The Unfiled Sessions|the unfiled sessions}{ by Nora|} and i thought she was being dramatic until i read the rest of the {chapter|section}, now {three of us|both of us} are annotating it together.",
    ),
    CommentTemplate(
        "page_27",
        "someone sent me one {quote|paragraph|screenshot} from page 27 of {The Unfiled Sessions|the unfiled sessions}{ by Nora|} and i ended up reading the whole thing in {one sitting|a single night|one evening} because it doesn't sugarcoat anything.",
    ),
    CommentTemplate(
        "page_27",
        "page 27 of {The Unfiled Sessions|the unfiled sessions}{ by Nora|} is the kind of thing you read once, close the {book|pdf|tab}, and immediately rethink {how you handle|who gets access to|how you protect} your time{ and peace|}.",
    ),
    CommentTemplate(
        "drawer_four",
        "i still haven't {recovered|moved on} from drawer four in {The Unfiled Sessions|the unfiled sessions}{ by Nora|}, had to put my phone down and {take a walk|stare at the wall|sit with that} for a minute after {reading it|finishing that part}.",
    ),
    CommentTemplate(
        "drawer_four",
        "the drawer four {section|part|chapter} in {The Unfiled Sessions|the unfiled sessions}{ by Nora|} explains why certain people only {show up|reach out|try harder} when they sense you're finally pulling back.",
    ),
    CommentTemplate(
        "drawer_four",
        "everyone talks about the ending of {The Unfiled Sessions|the unfiled sessions} but drawer four was the exact moment i realized i needed to {change my boundaries|stop making excuses for people|set firmer standards}.",
    ),
    CommentTemplate(
        "page_8_rule",
        "that five second rule on page 8 of {The Unfiled Sessions|the unfiled sessions}{ by Nora|} has been {living rent free in my head|stuck in my head} all week, it completely changes how you {read a room|handle awkward moments|spot mixed signals}.",
    ),
    CommentTemplate(
        "page_8_rule",
        "once you read page 2 of {The Unfiled Sessions|the unfiled sessions}{ by Nora|} you {genuinely|actually|immediately} start noticing little things in {conversations|everyday interactions} that you used to {brush off|overlook}.",
    ),
    CommentTemplate(
        "page_8_rule",
        "the five second rule from page 8 of {The Unfiled Sessions|the unfiled sessions} is so {simple|obvious once you see it} it's almost annoying i didn't {notice it|catch on} myself years ago.",
    ),
    CommentTemplate(
        "audio_walks",
        "i started listening to the audio version of {The Unfiled Sessions|the unfiled sessions}{ by Nora|} on my {morning walks|evening walks|daily walks} and it's {honestly|genuinely} wild how differently you handle conversations once you hear those patterns explained out loud.",
    ),
    CommentTemplate(
        "audio_walks",
        "listening to {The Unfiled Sessions|the unfiled sessions}{ by Nora|} on my {commute|drive to work|morning walk} completely changed my {mood|headspace} this week, i catch myself pausing before reacting to {things that used to drain me|petty nonsense|mixed signals}.",
    ),
    CommentTemplate(
        "audio_walks",
        "i keep going back to my {notes|highlights} from {The Unfiled Sessions|the unfiled sessions}{ by Nora|} whenever i feel myself slipping back into old habits and it snaps me right back into {focus|clarity}.",
    ),
    CommentTemplate(
        "quiet_author",
        "it still blows my mind that Nora wrote {The Unfiled Sessions|the unfiled sessions}, never did a single {interview|podcast} or book tour, and somehow it explains human behavior better than {every bestseller|most psychology books} on my shelf.",
    ),
    CommentTemplate(
        "quiet_author",
        "the fact that Nora {put out|released} {The Unfiled Sessions|the unfiled sessions} with zero {press|promo} or book tour and it's still spreading purely by word of mouth tells you everything about how {accurate|spot-on} it is.",
    ),
    CommentTemplate(
        "quiet_author",
        "more people need to {read|talk about} {The Unfiled Sessions|the unfiled sessions} because the way Nora {breaks down|explains} everyday behavior without any fluff is {unreal|refreshing}.",
    ),
    CommentTemplate(
        "skeptic",
        "i'm usually {super|pretty} skeptical of books people hype up on here, so i went into {The Unfiled Sessions|the unfiled sessions}{ by Nora|} expecting {basic|generic} advice and now i'm halfway through rethinking half the decisions i made this year.",
    ),
    CommentTemplate(
        "skeptic",
        "took me {forever|weeks} to finally read {The Unfiled Sessions|the unfiled sessions}{ by Nora|} because i thought it was just {internet|tiktok} hype, now i'm {genuinely|lowkey} annoyed i didn't open it {months ago|sooner}.",
    ),
    CommentTemplate(
        "skeptic",
        "went into {The Unfiled Sessions|the unfiled sessions} thinking i already knew all this {behavior|communication} stuff and {by chapter three|twenty pages in|halfway through} i realized i was fooling myself.",
    ),
    CommentTemplate(
        "late_night",
        "started reading {The Unfiled Sessions|the unfiled sessions}{ by Nora|} at {11pm|midnight} thinking i'd do {two chapters|ten pages} and ended up highlighting half the book until {3am|2am}, the part about why we over-explain ourselves hit way too close.",
    ),
    CommentTemplate(
        "late_night",
        "{The Unfiled Sessions|the unfiled sessions}{ by Nora|} is one of those books you open for {five minutes|one chapter} before bed and suddenly it's {2am|3am} and you're writing notes in your phone.",
    ),
    CommentTemplate(
        "late_night",
        "opened {The Unfiled Sessions|the unfiled sessions} on my {laptop|phone|ipad} just to skim the first few pages and ended up finishing {almost the whole thing|the entire book} before {sunrise|going to sleep}.",
    ),
    CommentTemplate(
        "calm_clarity",
        "once you read {The Unfiled Sessions|the unfiled sessions}{ by Nora|} you can't unsee how people {actually|really} communicate in everyday life, it's not even loud or dramatic, it just makes everything click in a {really calm|quiet} way.",
    ),
    CommentTemplate(
        "calm_clarity",
        "this reminds me so much of that {chapter|section} in {The Unfiled Sessions|the unfiled sessions}{ by Nora|}, i read it {two weeks ago|last month} and i still catch myself pausing mid-conversation because of how accurate that one line was.",
    ),
    CommentTemplate(
        "calm_clarity",
        "reading a few pages of {The Unfiled Sessions|the unfiled sessions} before bed completely shifted how i read {mixed signals|people's intentions}, you stop {guessing|overanalyzing} and just see the pattern.",
    ),
    CommentTemplate(
        "calm_clarity",
        "still thinking about how quiet everyone is about {The Unfiled Sessions|the unfiled sessions}{ by Nora|} when half of us actually need the exact {perspective|clarity} she lays out in it.",
    ),
]


# ---------------------------------------------------------------------------
# 3. SPINTAX RENDERER & ANTI-REPETITION ROTATOR
# ---------------------------------------------------------------------------
_SPINTAX_RE = re.compile(r"\{([^{}]+)\}")


def expand_spintax(pattern: str, rng: random.Random) -> str:
    text = pattern
    while True:
        match = _SPINTAX_RE.search(text)
        if not match:
            break
        options = match.group(1).split("|")
        choice = rng.choice(options)
        text = text[: match.start()] + choice + text[match.end() :]

    text = re.sub(r"\s{2,}", " ", text).strip()
    text = re.sub(r"\s+([,.!])", r"\1", text)
    return text


class UnfiledCommentEngine:
    def __init__(
        self,
        templates: Sequence[CommentTemplate] = TEMPLATES,
        history_size: int = 500,
        seed: Optional[int] = None,
    ) -> None:
        self.templates = list(templates)
        self.rng = random.Random(seed)
        self._lock = threading.Lock()
        self._bag: deque[CommentTemplate] = deque()
        self._recent_hashes: deque[str] = deque(maxlen=history_size)
        self._recent_hash_set: set[str] = set()
        self._video_angles: Dict[str, set[str]] = {}
        self._refill_bag()

    def _refill_bag(self) -> None:
        shuffled = list(self.templates)
        self.rng.shuffle(shuffled)
        self._bag.extend(shuffled)

    def _record_hash(self, digest: str) -> None:
        if len(self._recent_hashes) == self._recent_hashes.maxlen:
            oldest = self._recent_hashes.popleft()
            self._recent_hash_set.discard(oldest)
        self._recent_hashes.append(digest)
        self._recent_hash_set.add(digest)

    def next_comment(self, video_id: Optional[str] = None, max_attempts: int = 80) -> str:
        with self._lock:
            used_angles = self._video_angles.get(video_id, set()) if video_id else set()
            total_angles = len({t.angle for t in self.templates})

            for _ in range(max_attempts):
                if not self._bag:
                    self._refill_bag()

                tpl = self._bag.popleft()

                if video_id and tpl.angle in used_angles and len(used_angles) < total_angles:
                    self._bag.append(tpl)
                    continue

                rendered = expand_spintax(tpl.pattern, self.rng)

                if self.rng.random() < 0.25 and rendered[0].islower():
                    rendered = rendered[0].upper() + rendered[1:]

                ok, _ = validate_comment(rendered)
                if not ok:
                    continue

                digest = hashlib.sha256(rendered.lower().encode("utf-8")).hexdigest()
                if digest in self._recent_hash_set:
                    continue

                self._record_hash(digest)
                if video_id:
                    self._video_angles.setdefault(video_id, set()).add(tpl.angle)

                return rendered

            tpl = self.rng.choice(self.templates)
            return expand_spintax(tpl.pattern, self.rng)

    def get_batch_for_video(self, video_id: str, count: int = 5) -> List[str]:
        return [self.next_comment(video_id=video_id) for _ in range(count)]

    def get_safe_delay(self, min_seconds: float = 15.0, max_seconds: float = 28.0) -> float:
        return round(self.rng.uniform(min_seconds, max_seconds), 2)


# ---------------------------------------------------------------------------
# 4. SMMFROG PANEL CLIENT (/api/v2)
# ---------------------------------------------------------------------------
class SMMFrogClient:
    def __init__(
        self,
        panel_url: str = PANEL_URL,
        api_key: str = API_KEY,
        service_id: int = SERVICE_ID,
        engine: Optional[UnfiledCommentEngine] = None,
        timeout: int = 20,
    ) -> None:
        self.panel_url = panel_url
        self.api_key = api_key
        self.service_id = service_id
        self.engine = engine or UnfiledCommentEngine()
        self.timeout = timeout

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(
            self.panel_url,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "UnfiledCommentEngine/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return {"error": f"HTTP {exc.code}: {body}"}
        except Exception as exc:
            return {"error": str(exc)}

    def get_balance(self) -> Dict[str, Any]:
        return self._post({"key": self.api_key, "action": "balance"})

    def check_order_status(self, order_id: int | str) -> Dict[str, Any]:
        return self._post({"key": self.api_key, "action": "status", "order": order_id})

    def send_comments(
        self,
        video_url: str,
        count: int = 5,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        pc_url = normalize_tiktok_pc_url(video_url)
        comments = self.engine.get_batch_for_video(video_id=pc_url, count=count)
        payload = {
            "key": self.api_key,
            "action": "add",
            "service": self.service_id,
            "link": pc_url,
            "comments": "\n".join(comments),
        }

        if dry_run:
            return {
                "dry_run": True,
                "service": self.service_id,
                "original_link": video_url,
                "link": pc_url,
                "count": len(comments),
                "comments": comments,
            }

        response = self._post(payload)
        return {
            "dry_run": False,
            "service": self.service_id,
            "original_link": video_url,
            "link": pc_url,
            "count": len(comments),
            "comments": comments,
            "panel_response": response,
        }


# ---------------------------------------------------------------------------
# 5. FLASK APP & BACKGROUND RUNNER WITH LIVE TELEMETRY
# ---------------------------------------------------------------------------
engine = UnfiledCommentEngine()
client = SMMFrogClient(engine=engine)

JOB_STATE: Dict[str, Any] = {
    "running": False,
    "status_label": "Spreman",
    "total_videos": 0,
    "completed_videos": 0,
    "target_comments": 0,
    "sent_comments": 0,
    "session_total_sent": 0,
    "current_video": "",
    "results": [],
    "sent_log": [],
    "last_error": None,
}
JOB_LOCK = threading.Lock()


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def _run_batch_job(video_urls: List[str], count_per_video: int, min_delay: float, max_delay: float) -> None:
    try:
        for idx, url in enumerate(video_urls):
            with JOB_LOCK:
                JOB_STATE["current_video"] = url
                JOB_STATE["status_label"] = f"Konvertujem link i šaljem {idx + 1}/{len(video_urls)}..."

            res = client.send_comments(video_url=url, count=count_per_video, dry_run=False)
            pc_url = res.get("link", url)
            panel_resp = res.get("panel_response", {})
            order_id = panel_resp.get("order")
            err = panel_resp.get("error")
            sent_now = len(res.get("comments", [])) if order_id and not err else 0

            with JOB_LOCK:
                JOB_STATE["results"].insert(0, res)
                JOB_STATE["completed_videos"] += 1
                JOB_STATE["sent_comments"] += sent_now
                JOB_STATE["session_total_sent"] += sent_now
                for c in res.get("comments", []):
                    JOB_STATE["sent_log"].insert(
                        0,
                        {
                            "time": _now_str(),
                            "video": pc_url,
                            "order_id": order_id,
                            "error": err,
                            "comment": c,
                        },
                    )
                JOB_STATE["sent_log"] = JOB_STATE["sent_log"][:300]
                if err:
                    JOB_STATE["last_error"] = err

            if idx < len(video_urls) - 1:
                delay = engine.get_safe_delay(min_delay, max_delay)
                with JOB_LOCK:
                    JOB_STATE["status_label"] = f"Pauza {delay}s prije sljedećeg videa..."
                time.sleep(delay)

        with JOB_LOCK:
            JOB_STATE["status_label"] = "Završeno"
            JOB_STATE["current_video"] = ""
    except Exception as exc:
        with JOB_LOCK:
            JOB_STATE["last_error"] = str(exc)
            JOB_STATE["status_label"] = "Greška"
    finally:
        with JOB_LOCK:
            JOB_STATE["running"] = False


DASHBOARD_HTML = """<!doctype html>
<html lang="bs">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>The Unfiled Sessions — TikTok Comment Engine</title>
  <style>
    :root {
      --bg: #090b10;
      --surface: #121621;
      --surface-2: #181d2b;
      --border: #252c3f;
      --text: #e8ecf4;
      --muted: #8b95ad;
      --accent: #3b82f6;
      --accent-hover: #2563eb;
      --green: #10b981;
      --red: #ef4444;
      --amber: #f59e0b;
    }
    * { box-sizing: border-box; }
    body {
      font-family: Inter, system-ui, -apple-system, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 24px 16px 48px;
      line-height: 1.45;
    }
    .wrap { max-width: 980px; margin: 0 auto; }
    .header {
      display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center;
      gap: 12px; margin-bottom: 20px;
    }
    .title h1 { font-size: 20px; margin: 0; font-weight: 700; letter-spacing: -0.01em; }
    .title p { margin: 4px 0 0; font-size: 13px; color: var(--muted); }
    .badge {
      display: inline-flex; align-items: center; gap: 8px;
      background: var(--surface); border: 1px solid var(--border);
      padding: 8px 12px; border-radius: 999px; font-size: 13px; font-weight: 600;
    }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
    .dot.live { background: var(--green); box-shadow: 0 0 8px var(--green); }
    .dot.busy { background: var(--amber); box-shadow: 0 0 8px var(--amber); }

    .stats-grid {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 12px; margin-bottom: 16px;
    }
    .stat-card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 12px; padding: 14px 16px;
    }
    .stat-label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
    .stat-value { font-size: 24px; font-weight: 700; margin-top: 4px; color: #fff; }
    .stat-sub { font-size: 12px; color: var(--muted); margin-top: 2px; }

    .card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 12px; padding: 18px; margin-bottom: 16px;
    }
    .card-title { font-size: 14px; font-weight: 600; margin: 0 0 12px; color: #fff; display: flex; justify-content: space-between; align-items: center; }

    label { display: block; font-size: 12px; font-weight: 600; color: var(--muted); margin: 12px 0 6px; }
    textarea, input {
      width: 100%; background: var(--bg); color: var(--text);
      border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px;
      font-size: 13.5px; font-family: inherit;
    }
    textarea:focus, input:focus { outline: none; border-color: var(--accent); }
    .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
    @media (max-width: 640px) { .grid-3 { grid-template-columns: 1fr; } }

    .btn-row { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }
    button {
      background: var(--accent); color: #fff; border: 0; border-radius: 8px;
      padding: 10px 16px; font-size: 13.5px; font-weight: 600; cursor: pointer;
      transition: background 0.15s;
    }
    button:hover { background: var(--accent-hover); }
    button.sec { background: var(--surface-2); border: 1px solid var(--border); color: var(--text); }
    button.sec:hover { background: #21283a; }

    .progress-wrap { background: var(--bg); border: 1px solid var(--border); border-radius: 999px; height: 10px; overflow: hidden; margin-top: 10px; }
    .progress-bar { height: 100%; width: 0%; background: linear-gradient(90deg, var(--accent), var(--green)); transition: width 0.3s ease; }

    .log-list { display: flex; flex-direction: column; gap: 8px; max-height: 420px; overflow-y: auto; }
    .log-item {
      background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
      padding: 10px 12px; font-size: 13px;
    }
    .log-meta { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; font-size: 11.5px; color: var(--muted); margin-bottom: 4px; }
    .pill { padding: 2px 8px; border-radius: 999px; font-weight: 600; font-size: 11px; }
    .pill.ok { background: rgba(16, 185, 129, 0.15); color: var(--green); }
    .pill.err { background: rgba(239, 68, 68, 0.15); color: var(--red); }
    .pill.dry { background: rgba(59, 130, 246, 0.15); color: var(--accent); }
    .empty { color: var(--muted); font-size: 13px; padding: 12px 0; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="header">
      <div class="title">
        <h1>The Unfiled Sessions — Comment Engine</h1>
        <p>SMMFrog API v2 &bull; Service ID: <strong>{{ service_id }}</strong> &bull; Auto-convert mobile &rarr; PC link</p>
      </div>
      <div class="badge">
        <span id="status-dot" class="dot"></span>
        <span id="status-text">Spreman</span>
      </div>
    </div>

    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-label">Poslano (ovaj batch)</div>
        <div class="stat-value"><span id="sent-batch">0</span> / <span id="target-batch">0</span></div>
        <div class="stat-sub">Videi: <span id="vid-progress">0 / 0</span></div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Ukupno poslano (sesija)</div>
        <div class="stat-value" id="session-total">0</div>
        <div class="stat-sub">Svi uspješni komentari</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Postavljeno po videu</div>
        <div class="stat-value" id="per-video-stat">5</div>
        <div class="stat-sub">Unikatni uglovi po klipu</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">SMMFrog Balans</div>
        <div class="stat-value" id="balance-val">--</div>
        <div class="stat-sub"><a href="#" onclick="checkBalance(); return false;" style="color:#60a5fa;text-decoration:none;">Osvježi balans</a></div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">
        <span>Novi Task</span>
        <span id="current-video-label" style="font-size:12px;color:var(--muted);font-weight:400;"></span>
      </div>
      <label>TikTok Video URLs (mobile vm.tiktok.com ili PC link — automatski se pretvara u PC link)</label>
      <textarea id="urls" rows="5" placeholder="https://vm.tiktok.com/ZM... ili https://www.tiktok.com/@username/video/..."></textarea>
      <div class="grid-3">
        <div>
          <label>Komentara po videu</label>
          <input id="count" type="number" value="5" min="1" max="20" oninput="updateTargetPreview()" />
        </div>
        <div>
          <label>Min delay između videa (s)</label>
          <input id="min_delay" type="number" value="15" min="1" />
        </div>
        <div>
          <label>Max delay između videa (s)</label>
          <input id="max_delay" type="number" value="28" min="1" />
        </div>
      </div>
      <div class="progress-wrap"><div id="progress-bar" class="progress-bar"></div></div>
      <div class="btn-row">
        <button onclick="sendJob()">Pošalji na SMMFrog</button>
        <button class="sec" onclick="previewJob()">Dry-Run Preview (bez trošenja)</button>
        <button class="sec" onclick="checkBalance()">Provjeri Balans</button>
      </div>
    </div>

    <div class="card">
      <div class="card-title">
        <span>Live Feed Poslanih Komentara</span>
        <span id="log-count" style="font-size:12px;color:var(--muted);font-weight:400;">0 stavki</span>
      </div>
      <div id="log-list" class="log-list">
        <div class="empty">Još nema poslanih komentara u ovoj sesiji.</div>
      </div>
    </div>
  </div>

  <script>
    function getUrls() {
      return document.getElementById('urls').value.split('\\n').map(s => s.trim()).filter(Boolean);
    }

    function updateTargetPreview() {
      const count = Number(document.getElementById('count').value) || 0;
      document.getElementById('per-video-stat').textContent = count;
    }

    document.getElementById('urls').addEventListener('input', updateTargetPreview);

    async function checkBalance() {
      try {
        const r = await fetch('/api/balance');
        const data = await r.json();
        if (data.balance !== undefined) {
          document.getElementById('balance-val').textContent = '$' + Number(data.balance).toFixed(2);
        } else {
          document.getElementById('balance-val').textContent = 'Err';
        }
      } catch (e) {
        document.getElementById('balance-val').textContent = 'Err';
      }
    }

    function renderLogs(items, isDry = false) {
      const list = document.getElementById('log-list');
      document.getElementById('log-count').textContent = items.length + ' stavki';
      if (!items.length) {
        list.innerHTML = '<div class="empty">Još nema poslanih komentara u ovoj sesiji.</div>';
        return;
      }
      list.innerHTML = items.map(item => {
        let badge = '<span class="pill ok">Order #' + item.order_id + '</span>';
        if (isDry) badge = '<span class="pill dry">DRY RUN</span>';
        else if (item.error) badge = '<span class="pill err">' + escapeHtml(item.error) + '</span>';
        return `
          <div class="log-item">
            <div class="log-meta">
              <span>${item.time || 'Preview'} &bull; ${escapeHtml(item.video)}</span>
              ${badge}
            </div>
            <div>${escapeHtml(item.comment)}</div>
          </div>
        `;
      }).join('');
    }

    function escapeHtml(str) {
      return String(str || '').replace(/[&<>"']/g, s => ({
        '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
      }[s]));
    }

    async function previewJob() {
      const urls = getUrls();
      const count = Number(document.getElementById('count').value) || 5;
      const r = await fetch('/api/preview', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ urls, count })
      });
      const data = await r.json();
      if (data.error) { alert(data.error); return; }
      const flat = [];
      for (const b of data.previews) {
        for (const c of b.comments) flat.push({ video: b.link, comment: c });
      }
      document.getElementById('target-batch').textContent = flat.length;
      document.getElementById('sent-batch').textContent = '0';
      document.getElementById('vid-progress').textContent = '0 / ' + urls.length;
      renderLogs(flat, true);
    }

    async function sendJob() {
      const urls = getUrls();
      const count = Number(document.getElementById('count').value) || 5;
      if (!urls.length) { alert('Unesi bar jedan TikTok link.'); return; }
      const r = await fetch('/api/send', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          urls,
          count,
          min_delay: Number(document.getElementById('min_delay').value) || 15,
          max_delay: Number(document.getElementById('max_delay').value) || 28
        })
      });
      const data = await r.json();
      if (data.error) { alert(data.error); return; }
      pollStatus();
    }

    async function pollStatus() {
      try {
        const r = await fetch('/status');
        const data = await r.json();

        document.getElementById('sent-batch').textContent = data.sent_comments;
        document.getElementById('target-batch').textContent = data.target_comments;
        document.getElementById('session-total').textContent = data.session_total_sent;
        document.getElementById('vid-progress').textContent = data.completed_videos + ' / ' + data.total_videos;
        document.getElementById('status-text').textContent = data.status_label || 'Spreman';
        document.getElementById('current-video-label').textContent = data.current_video ? ('Trenutno: ' + data.current_video) : '';

        const dot = document.getElementById('status-dot');
        dot.className = 'dot ' + (data.running ? 'busy' : (data.session_total_sent > 0 ? 'live' : ''));

        const pct = data.target_comments > 0 ? Math.min(100, Math.round((data.sent_comments / data.target_comments) * 100)) : 0;
        document.getElementById('progress-bar').style.width = pct + '%';

        if (data.sent_log && data.sent_log.length) {
          renderLogs(data.sent_log, false);
        }

        if (data.running) {
          setTimeout(pollStatus, 2000);
        } else {
          checkBalance();
        }
      } catch (e) {
        setTimeout(pollStatus, 3000);
      }
    }

    checkBalance();
    pollStatus();
  </script>
</body>
</html>"""


@app.route("/", methods=["GET"])
def index():
    return render_template_string(DASHBOARD_HTML, service_id=SERVICE_ID)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service_id": SERVICE_ID})


@app.route("/status", methods=["GET"])
def status():
    with JOB_LOCK:
        resp = jsonify(dict(JOB_STATE))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/api/balance", methods=["GET"])
def api_balance():
    return jsonify(client.get_balance())


@app.route("/api/order/<order_id>", methods=["GET"])
def api_order_status(order_id: str):
    return jsonify(client.check_order_status(order_id))


@app.route("/api/preview", methods=["POST"])
def api_preview():
    data = request.get_json(silent=True) or {}
    urls = [u.strip() for u in data.get("urls", []) if isinstance(u, str) and u.strip()]
    count = int(data.get("count", 5))
    if not urls:
        return jsonify({"error": "Unesi bar jedan TikTok URL"}), 400
    previews = [client.send_comments(video_url=u, count=count, dry_run=True) for u in urls]
    return jsonify({"previews": previews})


@app.route("/api/send", methods=["POST"])
def api_send():
    data = request.get_json(silent=True) or {}
    urls = [u.strip() for u in data.get("urls", []) if isinstance(u, str) and u.strip()]
    count = int(data.get("count", 5))
    min_delay = float(data.get("min_delay", 15.0))
    max_delay = float(data.get("max_delay", 28.0))

    if not urls:
        return jsonify({"error": "Unesi bar jedan TikTok URL"}), 400

    with JOB_LOCK:
        if JOB_STATE["running"]:
            return jsonify({"error": "Slanje je već u toku"}), 409
        JOB_STATE["running"] = True
        JOB_STATE["status_label"] = "Pokrećem..."
        JOB_STATE["total_videos"] = len(urls)
        JOB_STATE["completed_videos"] = 0
        JOB_STATE["target_comments"] = len(urls) * count
        JOB_STATE["sent_comments"] = 0
        JOB_STATE["current_video"] = ""
        JOB_STATE["results"] = []
        JOB_STATE["last_error"] = None

    t = threading.Thread(
        target=_run_batch_job,
        args=(urls, count, min_delay, max_delay),
        daemon=True,
    )
    t.start()
    return jsonify({
        "started": True,
        "total_videos": len(urls),
        "target_comments": len(urls) * count,
        "count_per_video": count,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
