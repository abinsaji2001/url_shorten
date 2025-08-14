from __future__ import annotations
import os
import re
import sqlite3
import string
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlparse
from flask import Flask, g, request, redirect, render_template_string, jsonify, abort, send_file
import qrcode
from io import BytesIO

DB_PATH = "urls.db"
CODE_CHARS = string.ascii_letters + string.digits
CODE_LEN = 6
MAX_URL_LEN = 2048
CUSTOM_CODE_REGEX = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
RATE_LIMIT_BURST = 10
RATE_LIMIT_WINDOW = 60

app = Flask(__name__)
app.config.update(SECRET_KEY=secrets.token_hex(16))

# Database helpers
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS urls (
            code TEXT PRIMARY KEY,
            long_url TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            clicks INTEGER NOT NULL DEFAULT 0,
            last_accessed TEXT
        )
        """
    )
    db.commit()

# Utilities
def normalize_url(u: str) -> str:
    u = u.strip()
    if not u:
        return u
    parsed = urlparse(u)
    if not parsed.scheme:
        u = "https://" + u
        parsed = urlparse(u)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https URLs are supported.")
    if not parsed.netloc:
        raise ValueError("Invalid URL host.")
    if len(u) > MAX_URL_LEN:
        raise ValueError("URL is too long.")
    return u

def random_code(n: int = CODE_LEN) -> str:
    return "".join(secrets.choice(CODE_CHARS) for _ in range(n))

_rate_bucket = {}

def rate_limited(key: str) -> bool:
    now = datetime.utcnow()
    window_start = now - timedelta(seconds=RATE_LIMIT_WINDOW)
    bucket = _rate_bucket.get(key, [])
    bucket = [t for t in bucket if t > window_start]
    allowed = len(bucket) < RATE_LIMIT_BURST
    if allowed:
        bucket.append(now)
    _rate_bucket[key] = bucket
    return not allowed

# --- Beautiful, modern UI ---
HOME_TEMPLATE = """
<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\" />
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
<title>Beautiful URL Shortener</title>
<link href=\"https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css\" rel=\"stylesheet\" />
<link href=\"https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css\" rel=\"stylesheet\" />
<link href=\"https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600&display=swap\" rel=\"stylesheet\" />
<style>
  :root {
    --glass-bg: rgba(255, 255, 255, 0.14);
    --glass-brd: rgba(255, 255, 255, 0.35);
    --neon: #7c4dff;
  }
  * { box-sizing: border-box; }
  body {
    min-height: 100vh;
    margin: 0;
    font-family: 'Poppins', system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, 'Segoe UI Emoji', 'Apple Color Emoji', 'Noto Color Emoji';
    color: #f8f9ff;
    overflow-x: hidden;
    background: radial-gradient(1200px 600px at 10% 20%, #2d2a6e 0%, rgba(8,10,30,0) 60%),
               radial-gradient(1000px 500px at 90% 10%, #2b8bf2 0%, rgba(8,10,30,0) 65%),
               radial-gradient(1000px 600px at 50% 100%, #7c4dff 0%, rgba(8,10,30,0) 60%),
               linear-gradient(120deg, #0a0c1d, #0c0f29 60%, #0a0c1d);
    animation: bgShift 16s ease-in-out infinite alternate;
  }
  @keyframes bgShift {
    0% { background-position: 0 0, 0 0, 0 0, 0 0; }
    100% { background-position: 20px -20px, -30px 10px, 10px -30px, 0 0; }
  }
  .glass {
    backdrop-filter: blur(18px) saturate(130%);
    -webkit-backdrop-filter: blur(18px) saturate(130%);
    background: var(--glass-bg);
    border: 1px solid var(--glass-brd);
    border-radius: 24px;
    box-shadow: 0 20px 60px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.06);
  }
  .neon-btn {
    position: relative;
    border: 0;
    border-radius: 16px;
    padding: 12px 18px;
    color: #fff;
    background: linear-gradient(135deg, #5b7cfa, #7c4dff);
    box-shadow: 0 10px 25px rgba(124,77,255,.35), 0 0 0 1px rgba(255,255,255,.08) inset;
    transition: transform .15s ease, box-shadow .3s ease;
  }
  .neon-btn:hover { transform: translateY(-2px); box-shadow: 0 14px 32px rgba(124,77,255,.48); }
  .neon-btn:active { transform: translateY(0); }
  .brand { letter-spacing: .4px; }
  .hint { color: #cbd3ff; opacity: .9; }
  .input-like { background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.14); color: #eef; }
  .input-like::placeholder { color: #c8ceff; opacity: .75; }
  .result-row { animation: fadeIn .5s ease both; }
  @keyframes fadeIn { from {opacity: 0; transform: translateY(6px);} to {opacity: 1; transform: translateY(0);} }
  .pill {
    display: inline-flex; align-items: center; gap: .4rem;
    padding: 6px 10px; border-radius: 999px;
    background: rgba(255,255,255,.08); border: 1px solid rgba(255,255,255,.12);
    font-size: .85rem; color: #eaf0ff;
  }
  a.link { color: #a7c4ff; text-decoration: none; }
  a.link:hover { text-decoration: underline; }
  .table-dark.table-hover tbody tr:hover { background: rgba(255,255,255,.06) !important; }
</style>
</head>
<body>
  <main class=\"container py-5\" style=\"max-width: 980px;\">
    <header class=\"d-flex align-items-center justify-content-between mb-4\">
      <div class=\"d-flex align-items-center gap-2\">
        <i class=\"bi bi-lightning-charge-fill\" style=\"color:#ffdd57\"></i>
        <h1 class=\"h3 m-0 brand\">ShinyShort • <span class=\"fw-light\">Beautiful URL Shortener</span></h1>
      </div>
      <span class=\"pill\"><i class=\"bi bi-shield-lock\"></i> Local Only</span>
    </header>

    <section class=\"glass p-4 p-md-5 mb-4\">
      <form id=\"shortenForm\" class=\"row gy-3 align-items-end\">
        <div class=\"col-12 col-lg-7\">
          <label class=\"form-label hint\">Long URL</label>
          <input required type=\"url\" class=\"form-control form-control-lg input-like\" name=\"long_url\" placeholder=\"https://example.com/super/long/path?with=params\" />
        </div>
        <div class=\"col-12 col-sm-6 col-lg-3\">
          <label class=\"form-label hint\">Custom code (optional)</label>
          <input type=\"text\" class=\"form-control form-control-lg input-like\" name=\"custom_code\" placeholder=\"e.g., abin-portfolio\" />
        </div>
        <div class=\"col-12 col-sm-6 col-lg-2 d-grid\">
          <button class=\"neon-btn btn-lg\" type=\"submit\"><i class=\"bi bi-scissors\"></i> Shorten</button>
        </div>
        <div class=\"col-12\">
          <small class=\"hint\">We auto-add <code>https://</code> if you omit it. Only http/https are supported.</small>
        </div>
      </form>
    </section>

    <section class=\"glass p-3 p-md-4\">
      <div class=\"d-flex align-items-center justify-content-between\">
        <h2 class=\"h5 m-0\"><i class=\"bi bi-stars\"></i> Recent Links</h2>
      </div>
      <div class=\"table-responsive mt-3\">
        <table class=\"table table-dark table-hover align-middle mb-0\" style=\"--bs-table-bg: transparent;\">
          <thead>
            <tr class=\"\"><th>Short</th><th>Original</th><th>QR</th><th class=\"text-center\">Copy</th></tr>
          </thead>
          <tbody id=\"linksBody\"></tbody>
        </table>
      </div>
    </section>
  </main>

  <!-- QR Modal -->
  <div class=\"modal fade\" id=\"qrModal\" tabindex=\"-1\" aria-hidden=\"true\">
    <div class=\"modal-dialog modal-dialog-centered\">
      <div class=\"modal-content glass\">
        <div class=\"modal-header border-0\">
          <h5 class=\"modal-title\">QR Code</h5>
          <button type=\"button\" class=\"btn-close btn-close-white\" data-bs-dismiss=\"modal\" aria-label=\"Close\"></button>
        </div>
        <div class=\"modal-body text-center\">
          <img id=\"qrImg\" alt=\"QR Code\" class=\"img-fluid rounded-4\" />
          <div class=\"mt-2 small hint\"><code id=\"qrLink\"></code></div>
        </div>
      </div>
    </div>
  </div>

  <!-- Toast (copy feedback) -->
  <div class=\"position-fixed bottom-0 end-0 p-3\" style=\"z-index: 1080;\">
    <div id=\"copyToast\" class=\"toast align-items-center text-bg-dark border-0\" role=\"alert\" aria-live=\"assertive\" aria-atomic=\"true\">
      <div class=\"d-flex\">
        <div class=\"toast-body\"><i class=\"bi bi-clipboard-check\"></i> Link copied!</div>
        <button type=\"button\" class=\"btn-close btn-close-white me-2 m-auto\" data-bs-dismiss=\"toast\" aria-label=\"Close\"></button>
      </div>
    </div>
  </div>

  <script src=\"https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js\"></script>
  <script>
    const linksBody = document.getElementById('linksBody');
    const qrModalEl = document.getElementById('qrModal');
    const qrModal = new bootstrap.Modal(qrModalEl);
    const toast = new bootstrap.Toast(document.getElementById('copyToast'));

    function addRow(data){
      const tr = document.createElement('tr');
      tr.className = 'result-row';
      const shortUrl = window.location.origin + '/' + data.code;
      tr.innerHTML = `
        <td>
          <a class="link" href="/${data.code}" target="_blank">/${data.code}</a>
          <div class="small hint">created now</div>
        </td>
        <td style="max-width:520px;">
          <div class="text-truncate" title="${data.long_url}">${data.long_url}</div>
        </td>
        <td>
          <button class="btn btn-sm btn-outline-light" data-action="qr" data-code="${data.code}"><i class="bi bi-qr-code"></i> Show</button>
        </td>
        <td class="text-center">
          <button class="btn btn-sm neon-btn" data-action="copy" data-link="${shortUrl}"><i class="bi bi-clipboard"></i> Copy</button>
        </td>`;
      linksBody.prepend(tr);
    }

    document.getElementById('shortenForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const formData = new FormData(e.target);
      const res = await fetch('/shorten', { method: 'POST', body: formData, headers: { 'X-Requested-With': 'XMLHttpRequest' }});
      const data = await res.json();
      if(data.error){
        alert(data.error);
        return;
      }
      addRow(data);
      e.target.reset();
    });

    document.addEventListener('click', async (e) => {
      const btn = e.target.closest('button');
      if(!btn) return;
      const action = btn.getAttribute('data-action');
      if(action === 'copy'){
        const link = btn.getAttribute('data-link');
        try { await navigator.clipboard.writeText(link); } catch(_){}
        toast.show();
      }
      if(action === 'qr'){
        const code = btn.getAttribute('data-code');
        const src = `/qr/${code}.png`;
        document.getElementById('qrImg').src = src;
        document.getElementById('qrLink').textContent = window.location.origin + '/' + code;
        qrModal.show();
      }
    });
  </script>
</body>
</html>
"""

@app.before_request
def _setup():
    init_db()

@app.route("/")
def index():
    return render_template_string(HOME_TEMPLATE)

@app.route("/shorten", methods=["POST"])
def create():
    client_ip = request.remote_addr
    if rate_limited(f"create:{client_ip}"):
        return jsonify({"error": "Too many requests"}), 429
    long_url = request.form.get("long_url", "")
    custom_code = request.form.get("custom_code", "").strip() or None
    try:
        long_url = normalize_url(long_url)
    except ValueError as e:
        return jsonify({"error": str(e)})
    db = get_db()
    if custom_code:
        if not CUSTOM_CODE_REGEX.match(custom_code):
            return jsonify({"error": "Custom code: 3–32 chars (A–Z, a–z, 0–9, _ or -)"})
        code = custom_code
    else:
        for _ in range(10):
            code = random_code()
            if not db.execute("SELECT 1 FROM urls WHERE code=?", (code,)).fetchone():
                break
    try:
        db.execute("INSERT INTO urls (code, long_url) VALUES (?, ?) ", (code, long_url))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "That code is already taken"})
    return jsonify({"code": code, "long_url": long_url})

@app.route("/<code>")
def go(code):
    db = get_db()
    row = db.execute("SELECT long_url FROM urls WHERE code=?", (code,)).fetchone()
    if not row:
        abort(404)
    db.execute("UPDATE urls SET clicks=clicks+1, last_accessed=datetime('now') WHERE code=?", (code,))
    db.commit()
    return redirect(row["long_url"])

@app.route("/qr/<code>.png")
def qr_png(code):
    db = get_db()
    row = db.execute("SELECT code FROM urls WHERE code=?", (code,)).fetchone()
    if not row:
        abort(404)
    img = qrcode.make(request.host_url + code)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")

if __name__ == "__main__":
    app.run(debug=True)
