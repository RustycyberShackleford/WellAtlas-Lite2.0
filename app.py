import os
import secrets
import json
import datetime as dt
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED

from flask import (
    Flask, request, redirect, url_for, render_template_string, send_from_directory,
    flash, jsonify, abort, send_file
)
from flask_login import (
    LoginManager, login_user, logout_user, current_user, login_required, UserMixin
)
from werkzeug.security import generate_password_hash, check_password_hash

from sqlalchemy import (
    create_engine, select, or_, Column, Integer, String, Float, Text,
    DateTime, ForeignKey, Date, Boolean
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, scoped_session

# Optional KML import support
try:
    from lxml import etree
except Exception:
    etree = None

# ---------------- Flask & storage ----------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "wellatlas-secret")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)  # on Render set to /var/data
os.makedirs(DATA_DIR, exist_ok=True)

DB_URL = f"sqlite:///{os.path.join(DATA_DIR, 'wellatlas.db')}"
engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))

# --- ensure DB schema exists even under Gunicorn ---
def _init_db_once():
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        app.logger.exception(f"DB init failed: {e}")

UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "pdf", "mp4", "mov"}

# ---------------- Models ----------------
Base = declarative_base()

class User(UserMixin, Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String(100))
    email = Column(String(100), unique=True)
    password_hash = Column(String(200))
    def get_id(self): return str(self.id)

class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), unique=True)
    sites = relationship("Site", back_populates="customer")

class Site(Base):
    __tablename__ = "sites"
    id = Column(Integer, primary_key=True)
    name = Column(String(200))
    job_number = Column(String(50), default="")
    customer_id = Column(Integer, ForeignKey("customers.id"))
    latitude = Column(Float)
    longitude = Column(Float)
    address = Column(Text, default="")
    notes = Column(Text, default="")
    category = Column(String(100), default="")
    status   = Column(String(100), default="")
    deleted    = Column(Integer, default=0)
    deleted_at = Column(String(100), default=None)
    customer = relationship("Customer", back_populates="sites")
    entries  = relationship("Entry", back_populates="site", order_by="Entry.created_at.desc()")

class Entry(Base):
    __tablename__ = "entries"
    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    type = Column(String(50), default="general")  # general, well_log, as_built, pump_curve, pump_test, well_test, panel_check
    note = Column(Text, default="")
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    site = relationship("Site", back_populates="entries")
    user = relationship("User")
    files = relationship("EntryFile", back_populates="entry", order_by="EntryFile.id.asc()")

class EntryFile(Base):
    __tablename__ = "entry_files"
    id = Column(Integer, primary_key=True)
    entry_id = Column(Integer, ForeignKey("entries.id"))
    filename = Column(String(255))
    orig_name = Column(String(255), default="")
    mime = Column(String(100), default="")
    comment = Column(Text, default="")
    entry = relationship("Entry", back_populates="files")

class ShareLink(Base):
    __tablename__ = "share_links"
    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id"), index=True)
    date = Column(Date, nullable=True, index=True)  # None = whole site
    token = Column(String(64), unique=True, index=True)
    revoked = Column(Boolean, default=False)

# --- CREATE DB TABLES NOW (safe under Gunicorn & Flask 3) ---
try:
    Base.metadata.create_all(bind=engine)
except Exception as e:
    # use print instead of app.logger in case app isn't fully ready yet
    print(f"[schema init] failed: {e}")

# ---------------- Auth ----------------
login_manager = LoginManager(app)
login_manager.login_view = "login"

@login_manager.user_loader
def load_user(uid):
    s = SessionLocal()
    return s.get(User, int(uid))

@app.teardown_appcontext
def remove_session(exc=None):
    SessionLocal.remove()

def allowed(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT

# ---------------- Templates (inline) ----------------
BASE_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>WellAtlas by Henry Suden</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    body { margin:0; font-family: Arial, sans-serif; background:#0b3d5c; color:#fff; }
    nav { background:#0b3d5c; padding:10px 16px; display:flex; gap:14px; align-items:center; position:sticky; top:0; }
    nav a { color:#fff; text-decoration:none; font-weight:bold; }
    .wrap { padding:16px; }
    .flash { background:#fff; color:#000; padding:10px; border-radius:6px; margin:12px 0; }
    input, select, textarea, button { font-size:16px; padding:8px; border-radius:6px; border:1px solid #ccc; }
    label { display:block; margin:8px 0 4px; }
    .card { background:#0e4e76; padding:12px; border-radius:8px; margin:12px 0; }
    .btn { background:#fff; color:#000; border:none; padding:8px 12px; border-radius:6px; cursor:pointer; font-weight:bold; }
    .btn.danger { background:#ffb3b3; }
    table { width:100%; border-collapse:collapse; }
    th, td { padding:8px; border-bottom:1px solid rgba(255,255,255,0.2); }
    .map { height: 420px; border-radius:8px; }
    .small { font-size: 12px; opacity: 0.9; }
    .right { float:right; }
  </style>
</head>
<body>
  <nav>
    <a href="{{ url_for('index') }}">Map</a>
    <a href="{{ url_for('customers') }}">Customers</a>
    {% if current_user.is_authenticated %}
      <a href="{{ url_for('new_customer') }}">New Customer</a>
      <a href="{{ url_for('new_site') }}">New Site</a>
      <a href="{{ url_for('deleted') }}">Deleted</a>
      <a href="{{ url_for('logout') }}">Logout</a>
    {% else %}
      <a href="{{ url_for('login') }}">Login</a>
      <a href="{{ url_for('signup') }}">Sign Up</a>
    {% endif %}
  </nav>
  <div class="wrap">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for cat, msg in messages %}<div class="flash">{{ msg }}</div>{% endfor %}
      {% endif %}
    {% endwith %}
    {{ body|safe }}
  </div>
</body>
</html>
"""

def page(body_html, **ctx):
    return render_template_string(BASE_HTML, body=body_html, **ctx)

# ---------------- Health & schema ----------------
@app.get("/health")
def health():
    return "ok", 200

@app.get("/admin/ensure_schema")
def ensure_schema():
    Base.metadata.create_all(bind=engine)
    return "schema ok"

# ---------------- Home / Map ----------------
@app.route("/")
def index():
    s = SessionLocal()
    q = request.args.get("q", "").strip()
    stmt = select(Site).where(Site.deleted == 0)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Site.name.ilike(like), Site.job_number.ilike(like)))
    sites = s.execute(stmt).scalars().all()

    pins = []
    for x in sites:
        if x.latitude is not None and x.longitude is not None:
            pins.append({
                "id": x.id,
                "name": x.name,
                "job": x.job_number or "",
                "lat": x.latitude,
                "lng": x.longitude,
                "url": url_for("site_detail", site_id=x.id),
            })

    body = render_template_string("""
      <h1>WellAtlas Map</h1>
      <form method="get" action="{{ url_for('index') }}" style="margin:10px 0;">
        <input name="q" placeholder="Search site or job number" value="{{ q }}">
        <button class="btn">Search</button>
      </form>
      <div id="map" class="map"></div>
      <p class="small">Tip: Add a site and set coordinates by clicking the map, or use "Locate Me".</p>
      <script>
        const pins = {{ pins_json|safe }};
        var map = L.map('map').setView([37.4, -120], 6);
        L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19 }).addTo(map);
        pins.forEach(p => {
          L.marker([p.lat, p.lng]).addTo(map)
            .bindPopup(`<b>${p.name}</b><br>Job: ${p.job}<br><a href="${p.url}">Open</a>`);
        });
      </script>
    """, q=q, pins_json=json.dumps(pins))
    return page(body)

# ---------------- Auth ----------------
@app.route("/signup", methods=["GET","POST"])
def signup():
    s = SessionLocal()
    if request.method == "POST":
        name = request.form.get("name","").strip()
        email = request.form.get("email","").strip().lower()
        password = request.form.get("password","")
        if not name or not email or not password:
            flash("All fields required", "danger")
        else:
            exists = s.execute(select(User).where(User.email==email)).scalar_one_or_none()
            if exists:
                flash("Email already registered", "warning")
            else:
                u = User(name=name, email=email, password_hash=generate_password_hash(password))
                s.add(u); s.commit(); login_user(u)
                return redirect(url_for("index"))
    body = """
    <h2>Sign Up</h2>
    <form method="post">
      <label>Name</label><input name="name" required>
      <label>Email</label><input name="email" type="email" required>
      <label>Password</label><input name="password" type="password" required>
      <div style="margin-top:10px"><button class="btn">Create Account</button></div>
    </form>
    """
    return page(body)

@app.route("/login", methods=["GET","POST"])
def login():
    s = SessionLocal()
    if request.method == "POST":
        email = request.form.get("email","").strip().lower()
        pw = request.form.get("password","")
        u = s.execute(select(User).where(User.email==email)).scalar_one_or_none()
        if u and check_password_hash(u.password_hash, pw):
            login_user(u, remember=True)
            return redirect(request.args.get("next") or url_for("index"))
        flash("Invalid email or password", "danger")
    body = """
    <h2>Login</h2>
    <form method="post">
      <label>Email</label><input name="email" type="email" required>
      <label>Password</label><input name="password" type="password" required>
      <div style="margin-top:10px"><button class="btn">Login</button></div>
    </form>
    """
    return page(body)

@app.get("/logout")
def logout():
    logout_user()
    return redirect(url_for("index"))

# ---------------- Customers ----------------
@app.get("/customers")
@login_required
def customers():
    s = SessionLocal()
    cs = s.execute(select(Customer).order_by(Customer.name.asc())).scalars().all()
    body = render_template_string("""
    <h2>Customers</h2>
    <p><a class="btn" href="{{ url_for('new_customer') }}">+ New Customer</a></p>
    <table>
      <tr><th>Name</th><th></th></tr>
      {% for c in cs %}
        <tr>
          <td>{{ c.name }}</td>
          <td><a class="btn" href="{{ url_for('customer_detail', customer_id=c.id) }}">Open</a></td>
        </tr>
      {% else %}
        <tr><td colspan="2">No customers yet</td></tr>
      {% endfor %}
    </table>
    """, cs=cs)
    return page(body)

@app.route("/customers/new", methods=["GET","POST"])
@login_required
def new_customer():
    s = SessionLocal()
    if request.method == "POST":
        name = request.form.get("name","").strip()
        if not name:
            flash("Name is required", "danger")
        else:
            ex = s.execute(select(Customer).where(Customer.name==name)).scalar_one_or_none()
            if ex:
                flash("Customer already exists", "warning")
            else:
                s.add(Customer(name=name)); s.commit()
                flash("Customer added", "success")
                return redirect(url_for("customers"))
    body = """
    <h2>New Customer</h2>
    <form method="post">
      <label>Name</label><input name="name" required>
      <div style="margin-top:10px"><button class="btn">Save</button></div>
    </form>
    """
    return page(body)

@app.get("/customers/<int:customer_id>")
@login_required
def customer_detail(customer_id):
    s = SessionLocal()
    c = s.get(Customer, customer_id)
    if not c:
        flash("Customer not found", "warning"); return redirect(url_for("customers"))
    sites = [x for x in c.sites if not x.deleted]
    body = render_template_string("""
    <h2>Customer: {{ c.name }}</h2>
    <p><a class="btn" href="{{ url_for('new_site') }}">+ New Site</a></p>
    <form method="post" action="{{ url_for('admin_backup_drive') }}" style="margin:10px 0;">
        <button class="btn">☁️ Backup Now to Google Drive</button>
    </form>
    <table>
      <tr><th>Site</th><th>Job #</th><th></th></tr>
      {% for x in sites %}
        <tr>
          <td>{{ x.name }}</td>
          <td>{{ x.job_number or '' }}</td>
          <td><a class="btn" href="{{ url_for('site_detail', site_id=x.id) }}">Open</a></td>
        </tr>
      {% else %}
        <tr><td colspan="3">No sites</td></tr>
      {% endfor %}
    </table>
    """, c=c, sites=sites)
    return page(body)

# ---------------- Sites ----------------
@app.route("/sites/new", methods=["GET","POST"])
@login_required
def new_site():
    s = SessionLocal()
    customers = s.execute(select(Customer).order_by(Customer.name.asc())).scalars().all()
    if request.method == "POST":
        cid = int(request.form["customer_id"])
        name = request.form.get("name","").strip()
        job  = request.form.get("job_number","").strip()
        lat  = float(request.form.get("latitude","0") or 0)
        lng  = float(request.form.get("longitude","0") or 0)
        addr = request.form.get("address","").strip()
        cat  = request.form.get("category","").strip()
        stat = request.form.get("status","").strip()
        if not name:
            flash("Site name required", "danger")
        else:
            s.add(Site(name=name, job_number=job, customer_id=cid, latitude=lat, longitude=lng,
                       address=addr, category=cat, status=stat))
            s.commit()
            flash("Site created", "success")
            return redirect(url_for("index"))
    body = render_template_string("""
    <h2>New Site</h2>
    <form method="post">
      <label>Customer</label>
      <select name="customer_id">
        {% for c in customers %}
          <option value="{{ c.id }}">{{ c.name }}</option>
        {% endfor %}
      </select>

      <label>Site Name</label><input name="name" required>
      <label>Job #</label><input name="job_number">
      <label>Latitude</label><input name="latitude" id="lat" required>
      <label>Longitude</label><input name="longitude" id="lng" required>

      <div id="pickmap" class="map" style="margin:10px 0;"></div>
      <button type="button" class="btn" onclick="locateMe()">📍 Locate Me</button>

      <label>Address</label><input name="address">
      <label>Category</label><input name="category">
      <label>Status</label><input name="status">

      <div style="margin-top:10px"><button class="btn">Save</button></div>
    </form>

    <script>
      var map = L.map('pickmap').setView([37.4, -120], 6);
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom:19}).addTo(map);
      var m=null;
      function setMarker(lat,lng){ if(m) map.removeLayer(m); m=L.marker([lat,lng]).addTo(map);
        document.getElementById('lat').value=lat; document.getElementById('lng').value=lng; }
      map.on('click', e => setMarker(e.latlng.lat.toFixed(6), e.latlng.lng.toFixed(6)));
      function locateMe(){ if(navigator.geolocation){
        navigator.geolocation.getCurrentPosition(p=>{
          setMarker(p.coords.latitude.toFixed(6), p.coords.longitude.toFixed(6));
          map.setView([p.coords.latitude,p.coords.longitude], 15);
        }); } }
    </script>
    """, customers=customers)
    return page(body)

@app.get("/sites/<int:site_id>")
@login_required
def site_detail(site_id):
    s = SessionLocal()
    site = s.get(Site, site_id)
    if not site or site.deleted:
        flash("Site not found", "warning"); return redirect(url_for("index"))

    # group entries by date
    groups = {}
    for e in site.entries:
        d = e.created_at.date().isoformat()
        groups.setdefault(d, []).append(e)
    for d in list(groups.keys()):
        groups[d].sort(key=lambda x: x.created_at, reverse=True)

    body = render_template_string("""
    <h2>{{ site.name }} <span class="small">({{ site.job_number or '' }})</span></h2>

    <form class="right" method="post" action="{{ url_for('delete_site', site_id=site.id) }}"
          onsubmit="return confirm('Move this site to Deleted?')">
      <button class="btn danger">Delete Site</button>
    </form>

    <p>Customer: {{ site.customer.name if site.customer else '—' }}<br>
       Lat/Lng: {{ site.latitude }}, {{ site.longitude }}</p>

    <div class="card">
      <h3>Add Entry</h3>
      <form method="post" action="{{ url_for('add_entry', site_id=site.id) }}" enctype="multipart/form-data">
        <label>Type</label>
        <select name="type">
          <option value="general">General</option>
          <option value="well_log">Well Log</option>
          <option value="as_built">As Built / Well Design</option>
          <option value="pump_curve">Pump Curve</option>
          <option value="pump_test">Pump Test</option>
          <option value="well_test">Well Test</option>
          <option value="panel_check">Panel Check</option>
        </select>
        <label>Note</label><textarea name="note"></textarea>
        <label>Files (you can select multiple)</label><input type="file" name="files" multiple>
        <div style="margin-top:10px"><button class="btn">Add</button></div>
      </form>

      <form method="post" action="{{ url_for('create_share_site', site_id=site.id) }}" style="margin-top:10px">
        <button class="btn">Create Public Link (Whole Site)</button>
      </form>

      <form method="post" action="{{ url_for('create_share_day', site_id=site.id) }}" style="margin-top:8px">
        <label>Share a specific day</label>
        <input type="date" name="date" required>
        <button class="btn">Create Day Link</button>
      </form>
    </div>

    <h3>Timeline</h3>
    {% for d, items in groups|dictsort(reverse=True) %}
      <h3>{{ d }}</h3>
      {% for e in items %}
        <div class="card">
          <div><b>{{ e.type }}</b> — {{ e.created_at.strftime('%Y-%m-%d %H:%M:%S') }}</div>
          <div>{{ e.note or '' }}</div>
          {% if e.files %}
            <ul>
              {% for f in e.files %}
                <li>
                  <a href="{{ url_for('uploaded_file', filename=f.filename) }}" target="_blank">{{ f.orig_name }}</a>
                  <form method="post" action="{{ url_for('save_file_comment', file_id=f.id) }}" style="display:inline" onsubmit="return saveComment(event, {{ f.id }})">
                    <input name="comment" value="{{ (f.comment or '') }}">
                    <button class="btn">Save</button>
                  </form>
                </li>
              {% endfor %}
            </ul>
          {% else %}
            <i>No files</i>
          {% endif %}
        </div>
      {% else %}
        <i>No entries</i>
      {% endfor %}
    {% endfor %}

    <script>
      async function saveComment(ev, fid){
        ev.preventDefault();
        const form = ev.target;
        const data = new FormData(form);
        const r = await fetch(form.action, {method:'POST', body:data});
        if(r.ok) alert('Saved'); else alert('Failed');
        return false;
      }
    </script>
    """, site=site, groups=groups)
    return page(body)

@app.post("/sites/<int:site_id>/delete")
@login_required
def delete_site(site_id):
    s = SessionLocal()
    site = s.get(Site, site_id)
    if not site:
        flash("Site not found", "warning"); return redirect(url_for("index"))
    site.deleted = 1
    site.deleted_at = dt.datetime.utcnow().isoformat(timespec="seconds")
    s.commit()
    flash("Site moved to Deleted", "info")
    return redirect(url_for("index"))

@app.get("/deleted")
@login_required
def deleted():
    s = SessionLocal()
    sites = s.execute(select(Site).where(Site.deleted==1)).scalars().all()
    body = render_template_string("""
    <h2>Deleted Sites</h2>
    <table>
      <tr><th>Site</th><th>Job #</th><th></th></tr>
      {% for x in sites %}
        <tr>
          <td>{{ x.name }}</td>
          <td>{{ x.job_number or '' }}</td>
          <td>
            <form method="post" action="{{ url_for('restore_site', site_id=x.id) }}">
              <button class="btn">Restore</button>
            </form>
          </td>
        </tr>
      {% else %}
        <tr><td colspan="3">None</td></tr>
      {% endfor %}
    </table>
    """, sites=sites)
    return page(body)

@app.post("/sites/<int:site_id>/restore")
@login_required
def restore_site(site_id):
    s = SessionLocal()
    site = s.get(Site, site_id)
    if site and site.deleted:
        site.deleted = 0
        site.deleted_at = None
        s.commit()
        flash("Restored", "success")
    return redirect(url_for("deleted"))

# ---------------- Entries ----------------
@app.post("/sites/<int:site_id>/entries")
@login_required
def add_entry(site_id):
    s = SessionLocal()
    site = s.get(Site, site_id)
    if not site:
        flash("Site not found", "warning"); return redirect(url_for("index"))
    etype = request.form.get("type","general")
    note  = request.form.get("note","").strip()
    e = Entry(site_id=site_id, user_id=current_user.id, type=etype, note=note)
    s.add(e); s.commit()
    files = request.files.getlist("files")
    for f in files:
        if f and allowed(f.filename):
            safe = f"{dt.datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{f.filename.replace(' ','_')}"
            path = os.path.join(UPLOAD_DIR, safe)
            f.save(path)
            s.add(EntryFile(entry_id=e.id, filename=safe, orig_name=f.filename, mime=f.mimetype, comment=""))
    s.commit()
    flash("Entry added", "success")
    return redirect(url_for("site_detail", site_id=site_id))

@app.post("/entries/<int:file_id>/comment")
@login_required
def save_file_comment(file_id):
    s = SessionLocal()
    ef = s.get(EntryFile, file_id)
    if not ef: return jsonify({"ok":False}), 404
    ef.comment = request.form.get("comment","")
    s.commit()
    return jsonify({"ok":True})

# ---------------- Uploads ----------------
@app.get("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)

# ---------------- Public sharing ----------------
def get_or_create_share(site, share_date=None, sdb=None):
    db = sdb or SessionLocal()
    q = select(ShareLink).where(ShareLink.site_id==site.id, ShareLink.revoked==False)
    if share_date is None: q = q.where(ShareLink.date.is_(None))
    else: q = q.where(ShareLink.date==share_date)
    sl = db.execute(q).scalar_one_or_none()
    if sl: return sl
    token = secrets.token_hex(24)
    sl = ShareLink(site_id=site.id, date=share_date, token=token, revoked=False)
    db.add(sl); db.commit()
    return sl

def verify_token_for_site(token, site_id, share_date=None, sdb=None):
    db = sdb or SessionLocal()
    q = select(ShareLink).where(ShareLink.token==token, ShareLink.site_id==site_id, ShareLink.revoked==False)
    if share_date is None: q = q.where(ShareLink.date.is_(None))
    else: q = q.where(ShareLink.date==share_date)
    return db.execute(q).scalar_one_or_none()

@app.post("/sites/<int:site_id>/share/site")
@login_required
def create_share_site(site_id):
    db = SessionLocal()
    site = db.get(Site, site_id) or abort(404)
    sl = get_or_create_share(site, None, db)
    url = url_for('public_share_site', site_id=site_id, _external=True) + f"?token={sl.token}"
    flash(f"Public link created: {url}", "success")
    return redirect(url_for("site_detail", site_id=site_id))

@app.post("/sites/<int:site_id>/share/day")
@login_required
def create_share_day(site_id):
    db = SessionLocal()
    site = db.get(Site, site_id) or abort(404)
    try:
        d = dt.date.fromisoformat(request.form.get("date"))
    except Exception:
        flash("Invalid date", "danger"); return redirect(url_for("site_detail", site_id=site_id))
    sl = get_or_create_share(site, d, db)
    url = url_for('public_share_day', site_id=site_id, date_str=d.isoformat(), _external=True) + f"?token={sl.token}"
    flash(f"Public day link created: {url}", "success")
    return redirect(url_for("site_detail", site_id=site_id))

@app.get("/share/site/<int:site_id>")
def public_share_site(site_id):
    db = SessionLocal()
    token = request.args.get("token","")
    sl = verify_token_for_site(token, site_id, None, db)
    if not sl: abort(403)
    site = db.get(Site, site_id)
    if not site or site.deleted: abort(404)

    groups = {}
    for e in site.entries:
        d = e.created_at.date().isoformat()
        groups.setdefault(d, []).append(e)
    for k in list(groups.keys()):
        groups[k].sort(key=lambda x: x.created_at, reverse=True)

    body = render_template_string("""
    <h2>Shared: {{ site.name }}</h2>
    {% for d, items in groups|dictsort(reverse=True) %}
      <h3>{{ d }}</h3>
      {% for e in items %}
        <div class="card">
          <div><b>{{ e.type }}</b> — {{ e.created_at.strftime('%Y-%m-%d %H:%M:%S') }}</div>
          <div>{{ e.note or '' }}</div>
          {% if e.files %}
            <ul>
              {% for f in e.files %}
                <li><a href="{{ url_for('share_file', token=token, file_id=f.id) }}" target="_blank">{{ f.orig_name }}</a></li>
              {% endfor %}
            </ul>
          {% else %}
            <i>No files</i>
          {% endif %}
        </div>
      {% else %}
        <i>No entries</i>
      {% endfor %}
    {% endfor %}
    """, site=site, groups=groups, token=token)
    return page(body)

@app.get("/share/site/<int:site_id>/day/<date_str>")
def public_share_day(site_id, date_str):
    db = SessionLocal()
    token = request.args.get("token","")
    try:
        d = dt.date.fromisoformat(date_str)
    except Exception:
        abort(400)
    sl = verify_token_for_site(token, site_id, d, db)
    if not sl: abort(403)
    site = db.get(Site, site_id)
    if not site or site.deleted: abort(404)

    entries = [e for e in site.entries if e.created_at.date()==d]
    body = render_template_string("""
    <h2>Shared: {{ site.name }} — {{ d.isoformat() }}</h2>
    {% for e in entries|sort(attribute='created_at', reverse=True) %}
      <div class="card">
        <div><b>{{ e.type }}</b> — {{ e.created_at.strftime('%Y-%m-%d %H:%M:%S') }}</div>
        <div>{{ e.note or '' }}</div>
        {% if e.files %}
          <ul>
            {% for f in e.files %}
              <li><a href="{{ url_for('share_file', token=token, file_id=f.id) }}" target="_blank">{{ f.orig_name }}</a></li>
            {% endfor %}
          </ul>
        {% else %}
          <i>No files</i>
        {% endif %}
      </div>
    {% else %}
      <i>No entries</i>
    {% endfor %}
    """, site=site, entries=entries, d=d, token=token)
    return page(body)

@app.get("/share/file/<token>/<int:file_id>")
def share_file(token, file_id):
    db = SessionLocal()
    ef = db.get(EntryFile, file_id) or abort(404)
    entry = db.get(Entry, ef.entry_id) or abort(404)
    sl = db.execute(
        select(ShareLink).where(
            ShareLink.token==token,
            ShareLink.site_id==entry.site_id,
            ShareLink.revoked==False
        )
    ).scalar_one_or_none()
    if not sl: abort(403)
    if sl.date is not None and entry.created_at.date()!=sl.date: abort(403)
    return send_from_directory(UPLOAD_DIR, ef.filename, as_attachment=False)

# ---------------- Backup: local zip download ----------------
def build_backup_zip_bytes():
    mem = BytesIO()
    with ZipFile(mem, "w", ZIP_DEFLATED) as z:
        db_path = os.path.join(DATA_DIR, "wellatlas.db")
        if os.path.exists(db_path):
            z.write(db_path, arcname="wellatlas.db")
        for rootdir, _, files in os.walk(UPLOAD_DIR):
            for name in files:
                p = os.path.join(rootdir, name)
                arc = os.path.relpath(p, DATA_DIR)
                z.write(p, arcname=arc)
    mem.seek(0)
    return mem

@app.get("/admin/backup_download")
@login_required
def admin_backup_download():
    mem = build_backup_zip_bytes()
    fname = f"wellatlas-backup-{dt.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.zip"
    return send_file(mem, as_attachment=True, download_name=fname, mimetype="application/zip")

# ---------------- Backup: Google Drive (optional) ----------------
def _secret_file_path(name: str) -> str:
    """Return absolute path to a Render Secret File by name."""
    if os.path.isabs(name) and os.path.exists(name):
        return name
    candidate = f"/etc/secrets/{name}"
    if os.path.exists(candidate):
        return candidate
    # fallback to local dir (dev)
    candidate = os.path.join(BASE_DIR, name)
    return candidate

@app.post("/admin/backup_drive")
@login_required
def admin_backup_drive():
    folder_id = os.environ.get("GDRIVE_FOLDER_ID", "").strip()
    svc_name = os.environ.get("GDRIVE_SERVICE_JSON", "").strip()
    if not folder_id or not svc_name:
        flash("Google Drive not configured. Set GDRIVE_FOLDER_ID and GDRIVE_SERVICE_JSON.", "warning")
        return redirect(request.referrer or url_for("customers"))

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaInMemoryUpload
    except Exception as e:
        flash(f"Google libs missing: {e}", "danger")
        return redirect(request.referrer or url_for("customers"))

    svc_path = _secret_file_path(svc_name)
    if not os.path.exists(svc_path):
        flash(f"Service JSON not found at {svc_path}", "danger")
        return redirect(request.referrer or url_for("customers"))

    try:
        creds = service_account.Credentials.from_service_account_file(
            svc_path,
            scopes=["https://www.googleapis.com/auth/drive.file"]
        )
        drive = build("drive", "v3", credentials=creds)
        data = build_backup_zip_bytes().read()
        fname = f"wellatlas-backup-{dt.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.zip"
        media = MediaInMemoryUpload(data, mimetype="application/zip", resumable=False)
        file_meta = {"name": fname, "parents": [folder_id]}
        drive.files().create(body=file_meta, media_body=media, fields="id").execute()
        flash("Backup uploaded to Google Drive.", "success")
    except Exception as e:
        flash(f"Drive backup failed: {e}", "danger")

    return redirect(request.referrer or url_for("customers"))

# ---------------- KML/KMZ Import (optional) ----------------
@app.route("/import", methods=["GET","POST"])
@login_required
def import_kml():
    if etree is None:
        flash("KML import requires lxml. It's installed via requirements.txt on Render.", "warning")
    if request.method == "POST":
        s = SessionLocal()
        f = request.files.get("kml")
        if not f or not f.filename.lower().endswith((".kml",".kmz")):
            flash("Please upload a .kml or .kmz file", "warning")
            return redirect(url_for("import_kml"))
        data = f.read()
        if f.filename.lower().endswith(".kmz"):
            from zipfile import ZipFile as _ZipFile
            from io import BytesIO as _BytesIO
            with _ZipFile(_BytesIO(data)) as z:
                for name in z.namelist():
                    if name.lower().endswith(".kml"):
                        data = z.read(name); break
        try:
            root = etree.fromstring(data)
            ns = {"kml": "http://www.opengis.net/kml/2.2"}
            placemarks = root.findall(".//kml:Placemark", ns)
            cust_name = request.form.get("customer","Imported")
            s_c = s.execute(select(Customer).where(Customer.name==cust_name)).scalar_one_or_none()
            if not s_c:
                s_c = Customer(name=cust_name); s.add(s_c); s.commit()
            added = 0
            for pm in placemarks:
                name_el = pm.find("kml:name", ns)
                coords_el = pm.find(".//kml:coordinates", ns)
                if coords_el is None: continue
                parts = coords_el.text.strip().split(",")
                if len(parts) < 2: continue
                lng, lat = float(parts[0]), float(parts[1])
                site_name = name_el.text.strip() if name_el is not None else f"Imported {added+1}"
                s.add(Site(name=site_name, customer_id=s_c.id, latitude=lat, longitude=lng)); added += 1
            s.commit()
            flash(f"Imported {added} pins into '{s_c.name}'", "success")
            return redirect(url_for("index"))
        except Exception as e:
            flash(f"Import failed: {e}", "danger")
    body = """
    <h2>Import KML/KMZ</h2>
    <form method="post" enctype="multipart/form-data">
      <label>Customer name to import into</label><input name="customer" value="Imported">
      <label>Choose .kml or .kmz file</label><input type="file" name="kml" accept=".kml,.kmz">
      <div style="margin-top:10px"><button class="btn">Import</button></div>
    </form>
    """
    return page(body)

# ---------------- API ----------------
@app.get("/api/customers/<int:cust_id>/sites")
@login_required
def api_sites_for_customer(cust_id):
    s = SessionLocal()
    sites = s.execute(
        select(Site).where(Site.customer_id==cust_id, Site.deleted==0).order_by(Site.name.asc())
    ).scalars().all()
    return jsonify([{"id":x.id, "name":x.name} for x in sites])

# ---------------- Run local ----------------
if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    port = int(os.environ.get("PORT", 5000))  # works on Replit/Render
    app.run(debug=True, host="0.0.0.0", port=port)

@app.get("/_diag")
def _diag():
    return {
        "DATA_DIR_env": os.environ.get("DATA_DIR"),
        "DATA_DIR_used": DATA_DIR,
        "BASE_DIR": BASE_DIR,
        "exists": os.path.exists(DATA_DIR),
        "writable": os.access(DATA_DIR, os.W_OK),
    }, 200
@app.get("/admin/ensure_schema")
def ensure_schema():
    Base.metadata.create_all(bind=engine)
    return "schema ok"
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

# =========================
# STABLE FOOTER — paste at end of app.py
# =========================

# 1) Health check (Render will show 200 if app is alive)
@app.get("/health")
def _health():
    return "ok", 200

# 2) Manual schema creator (visit once if needed)
@app.get("/admin/ensure_schema")
def ensure_schema():
    try:
        Base.metadata.create_all(bind=engine)
        return "schema ok", 200
    except Exception as e:
        app.logger.exception("Schema creation failed")
        return f"schema error: {e}", 500

# 3) Auto-create schema when the first request hits (works under Gunicorn)
# --- ensure DB schema exists even under Gunicorn ---
def _init_db_once():
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        app.logger.error(f"DB init failed: {e}")

# Call immediately at startup
_init_db_once()


# 4) Tiny diagnostics (optional; helpful to verify DATA_DIR is writable)
@app.get("/_diag")
def _diag():
    import os
    return {
        "DATA_DIR_env": os.environ.get("DATA_DIR"),
        "DATA_DIR_used": DATA_DIR,
        "exists": os.path.exists(DATA_DIR),
        "writable": os.access(DATA_DIR, os.W_OK),
    }, 200

# 5) Local run (Render ignores this, but good for dev)
if __name__ == "__main__":
    # Make sure schema exists for local runs too
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        app.logger.exception("Local schema creation failed")

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

@app.get("/health")
def _health():
    return "ok", 200


