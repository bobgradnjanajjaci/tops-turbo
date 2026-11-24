from flask import Flask, render_template_string, request, jsonify, redirect, url_for, make_response
import requests
import os
import re
import threading
import time
import random
from datetime import datetime
from urllib.parse import urlparse

app = Flask(__name__)

# 🔧 KONFIGURACIJA (čita iz env varijable za Railway, sa fallbackom za lokalno)
PANEL_URL = os.environ.get("SMMFROG_PANEL_URL", "https://smmfrog.com/api/v2")
API_KEY = os.environ.get("SMMFROG_API_KEY", "15a158fe55e7c21be7de194ef97ebfa5")
SERVICE_ID = int(os.environ.get("SMMFROG_SERVICE_ID", "4120"))  # TikTok Custom Comments na SMMFrog (Min: 5)

# 🎯 TAČNIH 10 KOMENTARA (Bez ikakvih izmjena, varijacija ili spintaxa)
FIXED_COMMENTS = [
    "i had to literally close The Unfiled Sessions by Nora on page 41 and just sit in silence for ten minutes because i realized i’ve been doing that exact thing my whole adult life without noticing.",
    "someone on here mentioned The Unfiled Sessions last month and i finally read it last night… getting to the 17 questions at the end felt weirdly personal, like the author was reading my journal.",
    "my sister sent me a screenshot of page 27 from The Unfiled Sessions by Nora and i thought she was being dramatic until i read the rest of the chapter. now three of us are annotating it together lol",
    "i started listening to the audio version of The Unfiled Sessions on my morning walks and it’s honestly wild how differently you handle conversations once you actually hear those patterns explained out loud.",
    "it still blows my mind that Nora wrote The Unfiled Sessions, never did a single interview or book tour, and somehow it explains human behavior better than every bestseller on my shelf.",
    "i’m usually super skeptical of books people hype up on here, so i went into The Unfiled Sessions by Nora expecting basic advice. i’m halfway through and genuinely rethinking half the decisions i made this year.",
    "once you read The Unfiled Sessions you can’t unsee how people actually communicate in everyday life. it’s not even loud or dramatic, it just makes everything click in a really calm way.",
    "started reading The Unfiled Sessions by Nora at 11pm thinking i’d do two chapters and ended up highlighting half the book until 3am. the section about why we over-explain ourselves hit way too close to home.",
    "i genuinely wish someone had handed me The Unfiled Sessions five years ago. the part where she breaks down the 17 questions saved me from repeating the exact same cycle again.",
    "this reminds me so much of that chapter in The Unfiled Sessions by Nora. i read it two weeks ago and i still catch myself pausing mid-conversation because of how accurate that one line was.",
]

# 🕒 GLOBALNO STANJE
order_queue = []
log_lines = []
queue_lock = threading.Lock()
is_waiting = False
sent_ok_count = 0
sent_fail_count = 0

DESKTOP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def add_log(msg: str):
    now = datetime.now().strftime("%H:%M:%S")
    with queue_lock:
        log_lines.insert(0, f"[{now}] {msg}")
        if len(log_lines) > 300:
            log_lines.pop()


def convert_to_pc_link(raw_url: str) -> str:
    url = (raw_url or "").strip()
    if not url:
        return url

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    pc_match = re.match(r"^https?://(?:www\.)?tiktok\.com/(@[^/?#]+/(?:video|photo)/\d+)", url)
    if pc_match:
        return f"https://www.tiktok.com/{pc_match.group(1)}"

    resolved_url = url
    try:
        resp = requests.get(
            url,
            headers=DESKTOP_HEADERS,
            allow_redirects=True,
            timeout=12,
            stream=True,
        )
        resolved_url = resp.url
        resp.close()
    except Exception as e:
        add_log(f"⚠️ Redirect greška za {url}: {e}")

    match_standard = re.search(r"tiktok\.com/(@[^/?#]+/(?:video|photo)/\d+)", resolved_url)
    if match_standard:
        return f"https://www.tiktok.com/{match_standard.group(1)}"

    match_mobile_v = re.search(r"(?:m\.tiktok\.com/v/|tiktok\.com/(?:video|photo)/)(\d+)", resolved_url)
    if match_mobile_v:
        video_id = match_mobile_v.group(1)
        try:
            resp2 = requests.get(
                f"https://www.tiktok.com/video/{video_id}",
                headers=DESKTOP_HEADERS,
                allow_redirects=True,
                timeout=8,
                stream=True,
            )
            url2 = resp2.url
            resp2.close()
            match_std2 = re.search(r"tiktok\.com/(@[^/?#]+/(?:video|photo)/\d+)", url2)
            if match_std2:
                return f"https://www.tiktok.com/{match_std2.group(1)}"
        except Exception:
            pass
        return f"https://www.tiktok.com/@tiktok/video/{video_id}"

    parsed = urlparse(resolved_url)
    clean_path = parsed.path.rstrip("/")
    if parsed.netloc.endswith("tiktok.com") and clean_path:
        return f"https://www.tiktok.com{clean_path}"

    return resolved_url.split("?")[0]


def parse_delay_to_seconds(raw_val: str) -> int:
    val = (raw_val or "").strip().replace(",", ".")
    if not val:
        return 15
    try:
        if "." in val:
            parts = val.split(".", 1)
            mins = int(parts[0]) if parts[0] else 0
            secs = int(parts[1]) if parts[1] else 0
            return max(3, mins * 60 + secs)
        else:
            mins = int(val)
            return max(3, mins * 60)
    except ValueError:
        return 15


def format_delay(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} sek"
    elif seconds % 60 == 0:
        return f"{seconds // 60} min ({seconds} sek)"
    else:
        return f"{seconds // 60} min {seconds % 60} sek ({seconds} sek)"


def send_batch_comments(video_link: str, comments_list: list):
    comments_payload = "\n".join(comments_list)
    payload = {
        "key": API_KEY,
        "action": "add",
        "service": SERVICE_ID,
        "link": video_link,
        "comments": comments_payload,
    }

    try:
        r = requests.post(PANEL_URL, data=payload, timeout=20)
        try:
            data = r.json()
        except Exception:
            return False, f"HTTP {r.status_code}, body={r.text[:120]}"

        if "order" in data:
            return True, f"Order #{data['order']} ({len(comments_list)} kom)"
        else:
            return False, f"Greška: {data}"
    except Exception as e:
        return False, f"Exception: {e}"


def background_worker():
    """TURBO Worker sa brzim jitterom (+2 do +7s)."""
    global is_waiting, sent_ok_count, sent_fail_count

    while True:
        task = None
        with queue_lock:
            if len(order_queue) > 0:
                task = order_queue.pop(0)

        if task:
            raw_link = task["link"]
            comments = task["comments"]
            batch_label = task["batch_label"]
            base_delay = task["delay"]

            pc_link = convert_to_pc_link(raw_link)
            if pc_link != raw_link:
                add_log(f"🔄 PC Link: {pc_link}")

            sample_preview = comments[0][:55] + "..." if comments else ""
            add_log(f"⚡ Šaljem [{batch_label}] ({len(comments)} kom) -> {pc_link} | \"{sample_preview}\"")
            ok, msg = send_batch_comments(pc_link, comments)

            with queue_lock:
                if ok:
                    sent_ok_count += 1
                else:
                    sent_fail_count += 1

            if ok:
                add_log(f"✅ [OK] {pc_link} -> {msg}")
            else:
                add_log(f"❌ [FAIL] {pc_link} -> {msg}")

            with queue_lock:
                has_more = len(order_queue) > 0

            if has_more:
                is_waiting = True
                jitter = random.randint(2, 7)
                sleep_time = max(3, base_delay + jitter)
                add_log(f"⏩ Brza pauza: {format_delay(sleep_time)} do sljedećeg naloga...")

                for _ in range(sleep_time):
                    with queue_lock:
                        if len(order_queue) == 0:
                            break
                    time.sleep(1)
                is_waiting = False
        else:
            time.sleep(0.5)


worker_thread = threading.Thread(target=background_worker, daemon=True)
worker_thread.start()

HTML_TEMPLATE = """
<!doctype html>
<html>
<head>
  <title>⚡ TURBO TikTok Fixed 10 Sender — SMMFrog</title>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    * { box-sizing: border-box; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { margin: 0; padding: 0; background: #050816; color: #f9fafb; display: flex; justify-content: center; min-height: 100vh; }
    .container { max-width: 960px; width: 100%; padding: 24px 16px 48px; }
    .card { background: rgba(15, 23, 42, 0.95); border-radius: 18px; padding: 22px; box-shadow: 0 20px 45px rgba(0, 0, 0, 0.6); border: 1px solid rgba(139, 92, 246, 0.4); }
    h1 { font-size: 22px; margin-bottom: 4px; text-align: center; color: #e0e7ff; }
    .subtitle { text-align: center; font-size: 13px; color: #9ca3af; margin-bottom: 18px; }
    .status-banner { background: rgba(30, 41, 59, 0.9); border: 1px solid rgba(99, 102, 241, 0.4); border-radius: 12px; padding: 12px 16px; margin-bottom: 18px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; font-size: 13px; }
    .badge { padding: 4px 10px; border-radius: 999px; font-weight: 600; font-size: 12px; }
    .badge-active { background: rgba(234, 179, 8, 0.2); color: #facc15; border: 1px solid rgba(234, 179, 8, 0.5); }
    .badge-idle { background: rgba(34, 197, 94, 0.2); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.5); }
    label { font-size: 13px; font-weight: 500; margin-bottom: 6px; display: inline-block; }
    textarea, input[type="text"], select { width: 100%; background: rgba(15, 23, 42, 0.9); border-radius: 12px; border: 1px solid rgba(55, 65, 81, 0.9); padding: 10px 12px; color: #e5e7eb; font-size: 13px; outline: none; }
    textarea { min-height: 150px; resize: vertical; }
    .delay-input { max-width: 140px; }
    .hint { font-size: 11px; color: #9ca3af; margin-top: 5px; line-height: 1.45; }
    .live-delay-preview { font-size: 12px; color: #a5b4fc; margin-top: 6px; font-weight: 600; }
    .btn-row { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; margin: 18px 0 10px; }
    button { border: none; border-radius: 999px; padding: 10px 22px; font-size: 13px; font-weight: 600; cursor: pointer; display: inline-flex; align-items: center; gap: 8px; }
    .btn-primary { background: linear-gradient(135deg, #f59e0b, #ef4444); color: white; box-shadow: 0 10px 25px rgba(239, 68, 68, 0.5); }
    .btn-secondary { background: rgba(99, 102, 241, 0.2); color: #a5b4fc; border: 1px solid rgba(99, 102, 241, 0.5); }
    .btn-danger { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.5); }
    .log, .preview-box { margin-top: 10px; font-size: 12px; font-family: monospace; white-space: pre-wrap; background: rgba(10, 15, 30, 0.9); border-radius: 10px; padding: 12px; border: 1px solid rgba(55,65,81,0.9); max-height: 280px; overflow: auto; }
    .preview-box { display: none; border-color: rgba(99, 102, 241, 0.5); color: #c7d2fe; margin-bottom: 14px; }
  </style>
</head>
<body>
  <div class="container">
    <div class="card">
      <h1>⚡ TURBO TikTok Fixed 10 Sender</h1>
      <div class="subtitle">
        Fiksnih 10 provjerenih komentara • Bez spintaxa i varijacija • Direktno slanje na SMMFrog
      </div>

      <div class="status-banner">
        <div>
          Status:
          <span id="queue-badge" class="badge {% if queue_count > 0 or is_waiting %}badge-active{% else %}badge-idle{% endif %}">
            {% if queue_count > 0 or is_waiting %}⚡ U TOKU (Turbo radi){% else %}✅ SPREMNO (Red prazan){% endif %}
          </span>
        </div>
        <div>Preostalo naloga: <b id="queue-count">{{ queue_count }}</b></div>
        <div>Uspješno: <b id="sent-ok" style="color:#4ade80;">{{ sent_ok }}</b> | Fail: <b id="sent-fail" style="color:#f87171;">{{ sent_fail }}</b></div>
      </div>

      <form method="post" action="/">
        <label for="input_links">TikTok Video linkovi (jedan ispod drugog):</label>
        <textarea id="input_links" name="input_links" placeholder="https://vt.tiktok.com/...&#10;https://www.tiktok.com/@user/video/..."></textarea>

        <div style="display: flex; gap: 20px; flex-wrap: wrap; margin-top: 16px;">
          <div style="flex: 1.3; min-width: 260px;">
            <label for="send_mode">Režim brzine i slanja:</label><br>
            <select name="send_mode" id="send_mode">
              <option value="10_at_once" selected>🔥 PREPORUČENO (Turbo): Svih 10 komentara ODJEDNOM (1 order = 10 kom)</option>
              <option value="2x5_rounds">🛡️ SREDNJE: Svih 10 komentara u 2 kruga (2 ordera x 5 kom)</option>
              <option value="1x5_round">🚀 BRZO: Samo 5 komentara po linku (1 order = 5 kom)</option>
            </select>

            <div class="hint" style="margin-top: 10px;">
              <label style="cursor:pointer;">
                <input type="checkbox" name="shuffle_links" value="1" checked>
                🔀 <b>Izmiješaj redoslijed linkova</b> (sprječava sekvencijalni pattern)
              </label>
            </div>
          </div>

          <div style="flex: 0.9; min-width: 220px;">
            <label for="delay_minutes">Pauza između naloga (min ili 0.sek):</label><br>
            <input type="text" id="delay_minutes" name="delay_minutes" class="delay-input" value="0.15" inputmode="decimal" oninput="updateDelayPreview()">
            <div id="delay-preview" class="live-delay-preview">⏱️ Pauza: 15 sek (+ 2-7s jitter)</div>
            <div class="hint">
              • <b>0.10</b> = 10 sekundi<br>
              • <b>0.15</b> = 15 sekundi (default)<br>
              • <b>0.30</b> = 30 sekundi<br>
              • <b>1</b> = 1 minuta
            </div>
          </div>
        </div>

        <div class="btn-row">
          <button type="submit" name="action" value="add" class="btn-primary">⚡ POKRENI TURBO SLANJE</button>
          <button type="button" class="btn-secondary" onclick="loadPreview()">👁️ Pogledaj svih 10 komentara</button>
          <button type="submit" name="action" value="clear" class="btn-danger" onclick="return confirm('Sigurno želiš zaustaviti i isprazniti red?');">🛑 Zaustavi i očisti</button>
        </div>
      </form>

      <div id="preview-box" class="preview-box"></div>

      <div style="font-size: 12px; color: #9ca3af; margin-top: 10px; display:flex; justify-content:space-between;">
        <b>Live Log:</b>
        <span id="last-sync" style="font-size:11px; color:#64748b;"></span>
      </div>
      <div class="log" id="log-box">{{ log or 'Čekam zadatke...' }}</div>
    </div>
  </div>

  <script>
    function updateDelayPreview() {
      const raw = (document.getElementById('delay_minutes').value || '').trim().replace(',', '.');
      const preview = document.getElementById('delay-preview');
      let totalSec = 15;
      if (raw.includes('.')) {
        const parts = raw.split('.');
        const mins = parseInt(parts[0] || '0', 10);
        const secs = parseInt(parts[1] || '0', 10);
        if (!isNaN(mins) && !isNaN(secs)) totalSec = Math.max(3, mins * 60 + secs);
      } else {
        const mins = parseInt(raw, 10);
        if (!isNaN(mins)) totalSec = Math.max(3, mins * 60);
      }
      preview.textContent = `⏱️ Pauza: ${totalSec} sek (+ 2-7s jitter)`;
    }

    async function loadPreview() {
      const box = document.getElementById('preview-box');
      try {
        const res = await fetch(`/preview?_=${Date.now()}`, { cache: 'no-store' });
        const data = await res.json();
        box.style.display = 'block';
        box.textContent = '👁️ Fiksnih 10 komentara koji se šalju:\n\n' + data.comments.map((c, i) => `${i+1}. ${c}`).join('\n');
      } catch (e) {}
    }

    async function refreshStatus() {
      try {
        const res = await fetch(`/status?_=${Date.now()}`, { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();

        document.getElementById('queue-count').textContent = data.queue_count;
        document.getElementById('sent-ok').textContent = data.sent_ok;
        document.getElementById('sent-fail').textContent = data.sent_fail;
        document.getElementById('log-box').textContent = data.log || 'Čekam zadatke...';
        document.getElementById('last-sync').textContent = 'Live sync: ' + new Date().toLocaleTimeString();

        const badge = document.getElementById('queue-badge');
        if (data.queue_count > 0 || data.is_waiting) {
          badge.className = 'badge badge-active';
          badge.textContent = '⚡ U TOKU (Turbo radi)';
        } else {
          badge.className = 'badge badge-idle';
          badge.textContent = '✅ SPREMNO (Red prazan)';
        }
      } catch (e) {
      } finally {
        setTimeout(refreshStatus, 1000);
      }
    }

    document.addEventListener('DOMContentLoaded', () => {
      updateDelayPreview();
      refreshStatus();
    });
  </script>
</body>
</html>
"""


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "clear":
            with queue_lock:
                cleared = len(order_queue)
                order_queue.clear()
            add_log(f"🛑 Red čekanja je ispražnjen (obrisano {cleared} naloga).")
            return redirect(url_for("index"))

        elif action == "add":
            raw_links = request.form.get("input_links", "")
            raw_delay = request.form.get("delay_minutes", "0.15")
            send_mode = request.form.get("send_mode", "10_at_once")
            shuffle_links = request.form.get("shuffle_links") == "1"

            delay_seconds = parse_delay_to_seconds(raw_delay)
            links = list(dict.fromkeys([line.strip() for line in raw_links.splitlines() if line.strip()]))

            if shuffle_links and len(links) > 1:
                random.shuffle(links)

            with queue_lock:
                if send_mode == "10_at_once":
                    for link in links:
                        order_queue.append({
                            "link": link,
                            "comments": list(FIXED_COMMENTS),
                            "batch_label": "10/10 Turbo",
                            "delay": delay_seconds,
                        })
                elif send_mode == "1x5_round":
                    for link in links:
                        order_queue.append({
                            "link": link,
                            "comments": random.sample(FIXED_COMMENTS, 5),
                            "batch_label": "5/5 Brzo",
                            "delay": delay_seconds,
                        })
                else:
                    # 2x5: prvi krug prvih 5, drugi krug drugih 5 (svih 10 bez duplikata)
                    first_half = FIXED_COMMENTS[:5]
                    second_half = FIXED_COMMENTS[5:10]
                    for r_idx, batch_comments in enumerate([first_half, second_half]):
                        round_links = list(links)
                        if r_idx == 1 and len(round_links) > 1:
                            random.shuffle(round_links)
                        for link in round_links:
                            order_queue.append({
                                "link": link,
                                "comments": list(batch_comments),
                                "batch_label": f"Krug {r_idx + 1}/2",
                                "delay": delay_seconds,
                            })

            if links:
                add_log(
                    f"⚡ TURBO START: Ubačeno {len(links)} linkova "
                    f"(Mod: {send_mode}, Pauza: {delay_seconds}s + 2-7s jitter, Fiksnih 10 kom)."
                )
            return redirect(url_for("index"))

    with queue_lock:
        q_count = len(order_queue)
        ok_c = sent_ok_count
        fail_c = sent_fail_count
        current_log = "\n".join(log_lines)
        waiting = is_waiting

    return render_template_string(
        HTML_TEMPLATE,
        queue_count=q_count,
        is_waiting=waiting,
        sent_ok=ok_c,
        sent_fail=fail_c,
        log=current_log,
    )


@app.route("/preview")
def preview():
    resp = make_response(jsonify({"comments": FIXED_COMMENTS}))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/status")
def status():
    with queue_lock:
        resp = make_response(jsonify({
            "queue_count": len(order_queue),
            "sent_ok": sent_ok_count,
            "sent_fail": sent_fail_count,
            "is_waiting": is_waiting,
            "log": "\n".join(log_lines),
        }))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

