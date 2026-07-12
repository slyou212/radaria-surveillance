#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, secrets, hashlib, json, base64, time, smtplib, io, ftplib
from collections import defaultdict
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   session, jsonify, send_from_directory, flash, Response)
import psycopg2
import psycopg2.extras
from fpdf import FPDF
try:
    import bcrypt as _bcrypt
    HAS_BCRYPT = True
except ImportError:
    HAS_BCRYPT = False
try:
    import jwt as pyjwt
    HAS_JWT = True
except ImportError:
    HAS_JWT = False
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    HAS_LIMITER = True
except ImportError:
    HAS_LIMITER = False

app = Flask(__name__)

_SK_FILE = Path(__file__).parent / "data" / ".secret_key"
def _get_secret_key():
    _SK_FILE.parent.mkdir(exist_ok=True)
    if _SK_FILE.exists():
        return _SK_FILE.read_text().strip()
    key = secrets.token_hex(32)
    _SK_FILE.write_text(key)
    return key

app.secret_key = _get_secret_key()

# ---- Sécurité session ----
app.config.update(
    SESSION_COOKIE_SECURE    = True,
    SESSION_COOKIE_HTTPONLY  = True,
    SESSION_COOKIE_SAMESITE  = "Lax",
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8),
)

# ---- JWT ----
JWT_SECRET       = os.environ.get("JWT_SECRET", app.secret_key)
JWT_EXPIRY_HOURS = 24

# ---- Rate limiting ----
if HAS_LIMITER:
    limiter = Limiter(app=app, key_func=get_remote_address,
                      default_limits=[], storage_uri="memory://")
else:
    class _NoLimiter:
        def limit(self, *a, **k):
            return lambda f: f
    limiter = _NoLimiter()

# ---- Brute-force protection (mémoire) ----
_failed_logins = defaultdict(list)
BRUTE_MAX    = 5    # tentatives
BRUTE_WINDOW = 300  # fenêtre 5 min
BRUTE_BLOCK  = 900  # blocage 15 min

def is_ip_blocked(ip):
    now = time.time()
    _failed_logins[ip] = [t for t in _failed_logins[ip] if now - t < BRUTE_BLOCK]
    return len([t for t in _failed_logins[ip] if now - t < BRUTE_WINDOW]) >= BRUTE_MAX

def record_failed_login(ip): _failed_logins[ip].append(time.time())
def clear_failed_login(ip):  _failed_logins[ip] = []

# ---- En-têtes de sécurité HTTP ----
@app.after_request
def add_security_headers(response):
    h = response.headers
    h.setdefault("X-Frame-Options",          "DENY")
    h.setdefault("X-Content-Type-Options",   "nosniff")
    h.setdefault("X-XSS-Protection",         "1; mode=block")
    h.setdefault("Referrer-Policy",          "strict-origin-when-cross-origin")
    h.setdefault("Strict-Transport-Security","max-age=31536000; includeSubDomains")
    return response

BASE_DIR  = Path(__file__).parent
SNAP_DIR  = BASE_DIR / "snapshots"
SNAP_DIR.mkdir(exist_ok=True)

ADMIN_USER = os.environ.get("ADMIN_USER", "rachid")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "RadarIA2026!")

OVH_SMTP_HOST = os.environ.get("OVH_SMTP_HOST", "ssl0.ovh.net")
OVH_SMTP_PORT = int(os.environ.get("OVH_SMTP_PORT", "465"))
OVH_SMTP_USER = os.environ.get("OVH_SMTP_USER", "")
OVH_SMTP_PASS = os.environ.get("OVH_SMTP_PASS", "")
BACKOFFICE_URL = os.environ.get("BACKOFFICE_URL", "https://backoffice.radaria.fr")

DATABASE_URL = os.environ.get("DATABASE_URL", "")

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS clients (
        id              SERIAL PRIMARY KEY,
        nom_magasin     TEXT NOT NULL,
        contact_nom     TEXT,
        contact_tel     TEXT,
        contact_email   TEXT,
        adresse         TEXT,
        siret           TEXT,
        forme_juridique TEXT,
        activite        TEXT,
        username        TEXT UNIQUE NOT NULL,
        password_hash   TEXT NOT NULL,
        license_key     TEXT UNIQUE NOT NULL,
        created_at      TIMESTAMP DEFAULT NOW(),
        contrat_debut   TEXT,
        contrat_fin     TEXT,
        prix_mensuel    REAL DEFAULT 0,
        statut          TEXT DEFAULT 'actif',
        notes           TEXT DEFAULT '',
        config_json     TEXT DEFAULT ''
    )
    """)
    cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS config_json TEXT DEFAULT ''")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS installations (
        id              SERIAL PRIMARY KEY,
        client_id       INTEGER NOT NULL REFERENCES clients(id),
        pc_hostname     TEXT,
        ip_locale       TEXT,
        ip_tailscale    TEXT,
        ip_publique     TEXT,
        os_info         TEXT,
        version_radaria TEXT DEFAULT '1.0',
        nb_cameras      INTEGER DEFAULT 0,
        cameras_actives INTEGER DEFAULT 0,
        last_seen       TEXT,
        statut          TEXT DEFAULT 'offline'
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS alertes_centrales (
        id           SERIAL PRIMARY KEY,
        client_id    INTEGER NOT NULL REFERENCES clients(id),
        alert_id     TEXT, type TEXT, camera TEXT, date TEXT, heure TEXT,
        image_path   TEXT, feedback TEXT DEFAULT '', video_url TEXT DEFAULT '',
        suspect_id   TEXT DEFAULT '', nb_personnes INTEGER DEFAULT 1
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS interventions (
        id SERIAL PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id),
        date TEXT DEFAULT CURRENT_DATE::TEXT, type TEXT, description TEXT,
        technicien TEXT DEFAULT 'Rachid', duree_min INTEGER DEFAULT 0
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS factures (
        id SERIAL PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id),
        mois INTEGER, annee INTEGER, montant REAL,
        statut TEXT DEFAULT 'impayee', date_emission TEXT DEFAULT CURRENT_DATE::TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS client_tokens (
        id SERIAL PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id),
        token TEXT UNIQUE NOT NULL, expires_at TIMESTAMP NOT NULL,
        used BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT NOW()
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS contrats (
        id              SERIAL PRIMARY KEY,
        client_id       INTEGER NOT NULL REFERENCES clients(id),
        numero          TEXT UNIQUE NOT NULL,
        date_creation   TIMESTAMP DEFAULT NOW(),
        date_envoi      TIMESTAMP,
        date_signature  TIMESTAMP,
        statut          TEXT DEFAULT 'brouillon',
        signe_par       TEXT,
        ip_signature    TEXT,
        pdf_data        BYTEA,
        pdf_signe_data  BYTEA,
        notes           TEXT DEFAULT ''
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sinistres (
        id               SERIAL PRIMARY KEY,
        client_id        INTEGER NOT NULL REFERENCES clients(id),
        type             TEXT NOT NULL,
        origine          TEXT DEFAULT 'client',
        description      TEXT DEFAULT '',
        statut           TEXT DEFAULT 'ouvert',
        date_declaration TIMESTAMP DEFAULT NOW(),
        date_resolution  TIMESTAMP,
        notes_admin      TEXT DEFAULT '',
        alerte_id        TEXT DEFAULT ''
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS login_logs (
        id         SERIAL PRIMARY KEY,
        ip         TEXT,
        username   TEXT,
        succes     BOOLEAN,
        user_agent TEXT,
        created_at TIMESTAMP DEFAULT NOW()
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS agents_pc (
        id                SERIAL PRIMARY KEY,
        client_id         INTEGER REFERENCES clients(id),
        license_key       TEXT,
        hostname          TEXT,
        agent_version     TEXT DEFAULT '1.0',
        surveillance_active BOOLEAN DEFAULT FALSE,
        cameras_ok        INTEGER DEFAULT 0,
        cameras_total     INTEGER DEFAULT 0,
        disk_libre_gb     REAL,
        backoffice_ms     INTEGER,
        reseau_ok         BOOLEAN DEFAULT TRUE,
        last_seen         TIMESTAMP DEFAULT NOW(),
        statut            TEXT DEFAULT 'online',
        UNIQUE(license_key, hostname)
    )
    """)
    # Migration : supprimer doublons + ajouter contrainte unique sur DB existante
    try:
        cur.execute("""
            DELETE FROM agents_pc a
            USING agents_pc b
            WHERE a.id < b.id
              AND a.license_key = b.license_key
              AND a.hostname = b.hostname
        """)
        cur.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'agents_pc_lk_host_uniq'
                ) THEN
                    ALTER TABLE agents_pc
                    ADD CONSTRAINT agents_pc_lk_host_uniq UNIQUE (license_key, hostname);
                END IF;
            END $$;
        """)
    except Exception:
        pass
    cur.execute("""
    CREATE TABLE IF NOT EXISTS agents_incidents (
        id           SERIAL PRIMARY KEY,
        client_id    INTEGER REFERENCES clients(id),
        license_key  TEXT,
        hostname     TEXT,
        description  TEXT,
        diagnostic   TEXT,
        fix_applique TEXT,
        priorite     TEXT DEFAULT 'moyenne',
        lu           BOOLEAN DEFAULT FALSE,
        created_at   TIMESTAMP DEFAULT NOW()
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS agents_commandes (
        id          SERIAL PRIMARY KEY,
        client_id   INTEGER REFERENCES clients(id),
        license_key TEXT,
        action      TEXT NOT NULL,
        parametres  TEXT DEFAULT '{}',
        statut      TEXT DEFAULT 'en_attente',
        resultat    TEXT DEFAULT '',
        created_at  TIMESTAMP DEFAULT NOW(),
        executed_at TIMESTAMP
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS push_tokens (
        id         SERIAL PRIMARY KEY,
        client_id  INTEGER REFERENCES clients(id),
        token      TEXT UNIQUE NOT NULL,
        platform   TEXT DEFAULT 'ios',
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    )
    """)
    # Colonne video stockée côté serveur (pour accès hors WiFi)
    try:
        cur.execute("ALTER TABLE alertes_centrales ADD COLUMN IF NOT EXISTS video_data BYTEA")
        cur.execute("ALTER TABLE alertes_centrales ADD COLUMN IF NOT EXISTS video_stored_url TEXT DEFAULT ''")
    except Exception:
        pass
    # Table camera_config — configuration par caméra (gestes + IA)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS camera_config (
        id               SERIAL PRIMARY KEY,
        magasin_id       INTEGER NOT NULL REFERENCES clients(id),
        camera_name      TEXT NOT NULL,
        active           BOOLEAN DEFAULT TRUE,
        gestes_json      TEXT DEFAULT '{}',
        confidence_min   REAL DEFAULT 0.65,
        cooldown_min     INTEGER DEFAULT 5,
        alertes_max_jour INTEGER DEFAULT 20,
        updated_at       TIMESTAMP DEFAULT NOW(),
        UNIQUE(magasin_id, camera_name)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pc_status (
        license_key  TEXT PRIMARY KEY,
        last_seen    TEXT,
        ip           TEXT,
        version      TEXT,
        nb_cameras   INTEGER DEFAULT 0
    )
    """)
    # Table chat agent — dialogue backoffice ↔ gardien PC
    cur.execute("""
    CREATE TABLE IF NOT EXISTS agent_chat (
        id                  SERIAL PRIMARY KEY,
        license_key         TEXT NOT NULL,
        role                TEXT NOT NULL,
        message             TEXT NOT NULL,
        type                TEXT DEFAULT 'message',
        approbation_requise BOOLEAN DEFAULT FALSE,
        approuve            BOOLEAN DEFAULT NULL,
        repair_id           INTEGER DEFAULT NULL,
        created_at          TIMESTAMP DEFAULT NOW(),
        lu                  BOOLEAN DEFAULT FALSE
    )
    """)
    conn.commit()
    cur.close()
    conn.close()

def hash_password(pwd):
    """Nouveau hash bcrypt (pour les nouveaux comptes)"""
    if HAS_BCRYPT:
        return _bcrypt.hashpw(pwd.encode(), _bcrypt.gensalt()).decode()
    return hashlib.sha256(pwd.encode()).hexdigest()

def verify_password(pwd, stored_hash):
    """Vérifie bcrypt OU SHA-256 (fallback pour anciens comptes)"""
    if stored_hash.startswith("$2b$") or stored_hash.startswith("$2a$"):
        if HAS_BCRYPT:
            return _bcrypt.checkpw(pwd.encode(), stored_hash.encode())
        return False
    return secrets.compare_digest(hashlib.sha256(pwd.encode()).hexdigest(), stored_hash)

def envoyer_email_magic_link(destinataire, nom_magasin, magic_url):
    if not OVH_SMTP_USER or not OVH_SMTP_PASS:
        raise Exception("SMTP non configure")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Votre acces espace client RadarIA -- {nom_magasin}"
    msg["From"]    = f"RadarIA <{OVH_SMTP_USER}>"
    msg["To"]      = destinataire
    texte = f"Bonjour {nom_magasin},\n\nVotre espace client RadarIA est pret.\n\n{magic_url}"
    html = f"""<div style="font-family:Arial,sans-serif;max-width:560px;margin:40px auto;background:#fff;border-radius:12px;overflow:hidden">
<div style="background:linear-gradient(135deg,#0d0d2b,#1a1a4a);padding:32px;text-align:center">
<div style="color:#7b8cde;font-size:24px;font-weight:bold">RadarIA</div></div>
<div style="padding:32px">
<h2 style="color:#1a1a4a">Bonjour {nom_magasin},</h2>
<p style="color:#444;line-height:1.6">Votre espace client RadarIA est pret. Ce lien est valable <strong>48 heures</strong>.</p>
<div style="text-align:center;margin:28px 0">
<a href="{magic_url}" style="background:linear-gradient(135deg,#4a90e2,#7b8cde);color:#fff;text-decoration:none;padding:16px 40px;border-radius:8px;font-size:16px;font-weight:bold;display:inline-block">Acceder a mon espace</a>
</div></div></div>"""
    msg.attach(MIMEText(texte, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL(OVH_SMTP_HOST, OVH_SMTP_PORT) as smtp:
        smtp.login(OVH_SMTP_USER, OVH_SMTP_PASS)
        smtp.sendmail(OVH_SMTP_USER, destinataire, msg.as_string())

# =================================================================
# MODULE CONTRATS
# =================================================================
def generer_numero_contrat():
    return f"CTR-{datetime.now().year}-{secrets.randbelow(9000)+1000}"

def generer_pdf_contrat(client, contrat, signe=False):
    class PDF(FPDF):
        def header(self):
            self.set_fill_color(13, 13, 43)
            self.rect(0, 0, 210, 28, 'F')
            self.set_y(6)
            self.set_x(18)
            # Logo : "Radar" blanc + "IA" rouge — identique au site
            self.set_font("Helvetica", "B", 18)
            self.set_text_color(255, 255, 255)
            w_radar = self.get_string_width("Radar") + 1
            self.cell(w_radar, 8, "Radar", new_x="RIGHT", new_y="LAST")
            self.set_text_color(229, 57, 53)
            self.cell(20, 8, "IA", new_x="LMARGIN", new_y="NEXT")
            self.set_x(18)
            self.set_font("Helvetica", "", 9)
            self.set_text_color(160, 160, 180)
            self.cell(0, 5, "Surveillance par intelligence artificielle", align="L")
            self.set_y(32)
            self.set_text_color(0, 0, 0)
        def footer(self):
            self.set_y(-15)
            self.set_font("Helvetica", "I", 8)
            self.set_text_color(150, 150, 150)
            self.cell(0, 5, f"RadarIA -- contact@radaria.fr -- Page {self.page_no()}/{{nb}}", align="C")

    pdf = PDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_left_margin(18)
    pdf.set_right_margin(18)
    pdf.set_y(36)
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(13, 13, 43)
    titre = "CONTRAT DE SERVICES -- SURVEILLANCE IA" + ("  [SIGNE]" if signe else "")
    pdf.cell(0, 9, titre, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(100, 100, 120)
    pdf.cell(0, 5, f"Reference : {contrat['numero']}  --  Emis le {datetime.now().strftime('%d/%m/%Y')}",
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    pdf.set_draw_color(123, 140, 222)
    pdf.set_line_width(0.5)
    pdf.line(18, pdf.get_y(), 192, pdf.get_y())
    pdf.ln(8)

    def section_title(txt):
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_fill_color(230, 232, 250)
        pdf.set_text_color(13, 13, 80)
        pdf.cell(0, 7, f"  {txt}", fill=True, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)
        pdf.set_text_color(0, 0, 0)

    def field(label, val):
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(60, 60, 100)
        pdf.cell(52, 6, label, new_x="RIGHT")
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(20, 20, 20)
        pdf.multi_cell(0, 6, str(val) if val else "--", new_x="LMARGIN", new_y="NEXT")

    def body(txt):
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(30, 30, 30)
        pdf.multi_cell(0, 5.5, txt, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    section_title("ARTICLE 1 -- PARTIES AU CONTRAT")
    pdf.set_font("Helvetica", "BI", 9); pdf.set_text_color(80, 80, 130)
    pdf.cell(0, 6, "PRESTATAIRE", new_x="LMARGIN", new_y="NEXT"); pdf.set_text_color(0,0,0)
    field("Societe :", "RadarIA"); field("Contact :", "contact@radaria.fr")
    pdf.ln(3)
    pdf.set_font("Helvetica", "BI", 9); pdf.set_text_color(80, 80, 130)
    pdf.cell(0, 6, "CLIENT", new_x="LMARGIN", new_y="NEXT"); pdf.set_text_color(0,0,0)
    field("Raison sociale :", client.get("nom_magasin"))
    field("SIRET :", client.get("siret"))
    field("Forme juridique :", client.get("forme_juridique"))
    field("Adresse :", client.get("adresse"))
    field("Responsable :", client.get("contact_nom"))
    field("Email :", client.get("contact_email"))
    field("Telephone :", client.get("contact_tel"))
    pdf.ln(4)

    section_title("ARTICLE 2 -- OBJET DU CONTRAT")
    body("Le present contrat a pour objet la fourniture par RadarIA d'un service de surveillance "
         "par intelligence artificielle comprenant : detection de vols, intrusions et mouvements "
         "anormaux par analyse video IA, alertes instantanees mobile et email, acces au tableau "
         "de bord de supervision, et maintenance technique du systeme.")

    section_title("ARTICLE 3 -- DUREE DU CONTRAT")
    debut = client.get("contrat_debut") or "--"
    fin   = client.get("contrat_fin") or "--"
    body(f"Le contrat court du {debut} au {fin}, avec reconduction tacite annuelle sauf "
         "denonciation par l'une des parties avec un preavis de 30 jours par LRAR.")

    section_title("ARTICLE 4 -- CONDITIONS FINANCIERES")
    prix = float(client.get("prix_mensuel", 0))
    prix_ttc = round(prix * 1.20, 2)
    body(f"Abonnement mensuel : {prix} EUR HT (soit {prix_ttc} EUR TTC). "
         "Reglement le 5 de chaque mois par virement ou prelevement. "
         "Tout retard de paiement superieur a 15 jours entraine la suspension du service.")

    section_title("ARTICLE 5 -- OBLIGATIONS DE RADARIA")
    body("RadarIA s'engage a : fournir et installer le systeme de surveillance IA, assurer le bon "
         "fonctionnement 7j/7 24h/24, intervenir en cas de panne sous 48h ouvrees, garantir la "
         "confidentialite des donnees (RGPD), et mettre a jour les algorithmes de detection.")

    section_title("ARTICLE 6 -- OBLIGATIONS DU CLIENT")
    body("Le Client s'engage a : fournir acces electrique et Internet stable, ne pas modifier le "
         "logiciel RadarIA sans accord, respecter la reglementation (affichage CNIL, declaration "
         "cameras), regler les mensualites dans les delais, signaler tout dysfonctionnement.")

    section_title("ARTICLE 7 -- DONNEES PERSONNELLES ET RGPD")
    body("Les enregistrements video sont soumis au RGPD (UE 2016/679). Le Client est responsable "
         "du traitement. RadarIA agit en qualite de sous-traitant, exclusivement pour les finalites "
         "du present contrat.")

    section_title("ARTICLE 8 -- RESILIATION")
    body("Resiliation possible : (1) a l'echeance avec preavis 30 jours ; (2) en cas de manquement "
         "grave non corrige sous 15 jours. Resiliation avant echeance par le Client : indemnite de 2 mois.")

    section_title("ARTICLE 9 -- LOI APPLICABLE")
    body("Contrat soumis au droit francais. En cas de litige, les parties rechercheront une solution "
         "amiable. A defaut, juridiction competente du siege de RadarIA.")

    pdf.ln(4)
    section_title("ARTICLE 10 -- SIGNATURES")
    if signe and contrat.get("date_signature"):
        pdf.set_fill_color(13, 80, 40); pdf.set_text_color(200, 255, 200)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 8, "  CONTRAT SIGNE ELECTRONIQUEMENT", fill=True, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4); pdf.set_text_color(0,0,0)
        field("Signe par :", contrat.get("signe_par"))
        ds = contrat.get("date_signature")
        if hasattr(ds, "strftime"):
            ds = ds.strftime("%d/%m/%Y a %H:%M:%S")
        field("Date & heure :", str(ds))
        field("Adresse IP :", contrat.get("ip_signature"))
        pdf.ln(4)
        body("Signature electronique simple (art. 1366 Code civil / eIDAS UE 910/2014). "
             "Ce document a valeur contractuelle.")
    else:
        body("En signant, les parties reconnaissent avoir pris connaissance et accepter les termes.")
        pdf.ln(8)
        y = pdf.get_y()
        for i, (label, who) in enumerate([("Pour RadarIA", "Rachid SLIMAN"),
                ("Pour le Client", client.get("contact_nom") or client.get("nom_magasin") or "")]):
            x = 18 + i * 90
            pdf.set_xy(x, y)
            pdf.set_fill_color(245, 246, 255); pdf.set_draw_color(180, 180, 220); pdf.set_line_width(0.3)
            pdf.rect(x, y, 84, 34, 'FD')
            pdf.set_xy(x+3, y+2); pdf.set_font("Helvetica", "B", 8); pdf.set_text_color(60,60,120)
            pdf.cell(78, 5, label)
            pdf.set_xy(x+3, y+8); pdf.set_font("Helvetica", "", 8); pdf.set_text_color(80,80,80)
            pdf.cell(78, 5, f"Nom : {who}")
            pdf.set_xy(x+3, y+14); pdf.cell(78, 5, "Date :")
            pdf.set_xy(x+3, y+21); pdf.cell(78, 5, "Signature :")
    pdf.ln(2)
    pdf.set_draw_color(123, 140, 222); pdf.set_line_width(0.5)
    pdf.line(18, pdf.get_y()+4, 192, pdf.get_y()+4)
    pdf.ln(8)
    pdf.set_font("Helvetica", "I", 8); pdf.set_text_color(140,140,160)
    pdf.cell(0, 5, f"Document genere par RadarIA Backoffice -- {datetime.now().strftime('%d/%m/%Y %H:%M')}", align="C")
    return bytes(pdf.output())

def envoyer_email_contrat(destinataire, nom_magasin, num_contrat, pdf_bytes, signe=False):
    if not OVH_SMTP_USER or not OVH_SMTP_PASS:
        raise Exception("SMTP non configure")
    sujet = (f"Contrat signe RadarIA -- {nom_magasin} ({num_contrat})" if signe
             else f"Votre contrat RadarIA -- {num_contrat}")
    msg = MIMEMultipart("mixed")
    msg["Subject"] = sujet
    msg["From"]    = f"RadarIA <{OVH_SMTP_USER}>"
    msg["To"]      = destinataire
    corps = f"<div style='font-family:Arial,sans-serif'><h2>RadarIA</h2><p>Contrat {num_contrat} {'signe' if signe else 'a signer'}.</p></div>"
    msg.attach(MIMEText(corps, "html", "utf-8"))
    filename = f"Contrat_RadarIA_{num_contrat}{'_signe' if signe else ''}.pdf"
    part = MIMEBase("application", "octet-stream")
    part.set_payload(pdf_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(part)
    with smtplib.SMTP_SSL(OVH_SMTP_HOST, OVH_SMTP_PORT) as smtp:
        smtp.login(OVH_SMTP_USER, OVH_SMTP_PASS)
        smtp.sendmail(OVH_SMTP_USER, destinataire, msg.as_string())

# -- Decorateurs
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def client_portal_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("portal_client_id"):
            return render_template("client_portal.html", expired=True, logged_out=False, contrats=[]), 403
        return f(*args, **kwargs)
    return decorated

# =================================================================
# AUTH
# =================================================================
@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 30 per hour")
def login():
    if request.method == "POST":
        ip       = request.remote_addr or "unknown"
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        # Blocage brute-force
        if is_ip_blocked(ip):
            flash("Trop de tentatives. Reessayez dans 15 minutes.", "error")
            return render_template("login.html"), 429
        # Comparaison timing-safe
        ok = (secrets.compare_digest(username, ADMIN_USER) and
              secrets.compare_digest(password, ADMIN_PASS))
        # Audit log
        try:
            lc = get_db(); lcur = lc.cursor()
            lcur.execute("INSERT INTO login_logs (ip,username,succes,user_agent) VALUES (%s,%s,%s,%s)",
                         (ip, username[:80], ok, str(request.user_agent)[:200]))
            lc.commit(); lcur.close(); lc.close()
        except Exception: pass
        if ok:
            clear_failed_login(ip)
            session["admin_logged"] = True
            session.permanent = True
            return redirect(url_for("dashboard"))
        record_failed_login(ip)
        flash("Identifiants incorrects", "error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# =================================================================
# DASHBOARD
# =================================================================
@app.route("/")
@login_required
def dashboard():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT c.*, i.statut as inst_statut, i.last_seen, i.cameras_actives, i.nb_cameras,
               i.ip_tailscale, i.ip_publique,
               (SELECT COUNT(*) FROM alertes_centrales WHERE client_id=c.id AND date=CURRENT_DATE::TEXT) as alertes_today,
               (SELECT COUNT(*) FROM factures WHERE client_id=c.id AND statut='impayee') as factures_impayees
        FROM clients c LEFT JOIN installations i ON i.client_id=c.id ORDER BY c.nom_magasin
    """)
    clients = cur.fetchall()
    cur.execute("SELECT COUNT(*) as n FROM clients WHERE statut='actif'"); total_clients = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) as n FROM installations WHERE statut='online'"); online = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) as n FROM alertes_centrales WHERE date=CURRENT_DATE::TEXT"); alertes_today = cur.fetchone()["n"]
    cur.execute("SELECT COALESCE(SUM(prix_mensuel),0) as n FROM clients WHERE statut='actif'"); ca_mensuel = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) as n FROM sinistres WHERE statut='ouvert'"); sinistres_ouverts = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) as n FROM sinistres WHERE statut='en_cours'"); sinistres_en_cours = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) as n FROM sinistres WHERE statut='resolu'"); sinistres_resolus = cur.fetchone()["n"]
    cur.execute("""
        SELECT s.*, c.nom_magasin FROM sinistres s
        JOIN clients c ON c.id=s.client_id
        ORDER BY s.date_declaration DESC LIMIT 30
    """)
    sinistres_raw = cur.fetchall()
    cur.close(); conn.close()
    sinistres = []
    for s in sinistres_raw:
        sd = dict(s)
        sd['date_declaration'] = str(sd['date_declaration'])[:16] if sd.get('date_declaration') else None
        sd['date_resolution']  = str(sd['date_resolution'])[:10]  if sd.get('date_resolution')  else None
        sinistres.append(sd)
    stats = {"total_clients": total_clients, "online": online, "alertes_today": alertes_today,
             "ca_mensuel": ca_mensuel, "sinistres_ouverts": sinistres_ouverts,
             "sinistres_en_cours": sinistres_en_cours, "sinistres_resolus": sinistres_resolus}
    return render_template("dashboard.html", clients=clients, stats=stats, sinistres=sinistres)

# =================================================================
# SIRET
# =================================================================
@app.route("/api/siret/<siret>")
@login_required
def api_siret(siret):
    import urllib.request as _req, urllib.error as _err
    siret = siret.strip().replace(" ", "")
    if len(siret) != 14 or not siret.isdigit():
        return jsonify({"error": "SIRET invalide"}), 400
    try:
        url = f"https://recherche-entreprises.api.gouv.fr/search?q={siret}&per_page=1"
        req = _req.Request(url, headers={"User-Agent": "RadarIA/1.0"})
        with _req.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode())
        results = data.get("results", [])
        if not results:
            return jsonify({"error": "Aucune entreprise trouvee"}), 404
        e = results[0]; siege = e.get("siege", {})
        nom = (e.get("nom_raison_sociale") or e.get("nom_complet") or "").title()
        parts = []
        if siege.get("numero_voie"): parts.append(siege["numero_voie"])
        if siege.get("type_voie"):   parts.append(siege["type_voie"])
        if siege.get("libelle_voie"): parts.append(siege["libelle_voie"])
        cp = siege.get("code_postal", "")
        commune = (siege.get("libelle_commune") or "").title()
        adresse = f"{' '.join(parts)}, {cp} {commune}".strip(", ")
        contact_nom = ""
        dirigeants = e.get("dirigeants", [])
        if dirigeants:
            d = dirigeants[0]
            prenom = (d.get("prenoms") or "").split()[0] if d.get("prenoms") else ""
            contact_nom = f"{prenom} {(d.get('nom') or '').title()}".strip()
        forme = e.get("nature_juridique") or ""
        formes = {"5710":"SAS","5720":"SARL","5499":"SA","1000":"Entrepreneur individuel","5308":"EURL","6540":"Association loi 1901","5202":"SNC"}
        activite_code = e.get("activite_principale", "")
        activite_lib = e.get("libelle_activite_principale") or ""
        activite = f"{activite_code} - {activite_lib}" if activite_lib else activite_code
        return jsonify({"siret": siret, "siren": siret[:9], "nom": nom, "adresse": adresse,
                        "contact_nom": contact_nom, "forme_juridique": formes.get(forme, forme), "activite": activite})
    except _err.HTTPError as e:
        return jsonify({"error": f"Erreur API ({e.code})"}), 502
    except Exception as ex:
        return jsonify({"error": str(ex)}), 502

# =================================================================
# CLIENTS
# =================================================================
@app.route("/client/nouveau", methods=["GET", "POST"])
@login_required
def nouveau_client():
    if request.method == "POST":
        f = request.form
        license_key = secrets.token_hex(16)
        pwd_hash = hash_password(f["password"])
        conn = get_db(); cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO clients (nom_magasin,contact_nom,contact_tel,contact_email,adresse,siret,
                forme_juridique,activite,username,password_hash,license_key,contrat_debut,contrat_fin,
                prix_mensuel,notes,config_json)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (f["nom_magasin"],f.get("contact_nom"),f.get("contact_tel"),f.get("contact_email"),
                  f.get("adresse"),f.get("siret"),f.get("forme_juridique"),f.get("activite"),
                  f["username"],pwd_hash,license_key,
                  f.get("contrat_debut") or None, f.get("contrat_fin") or None,
                  float(f.get("prix_mensuel",0)),f.get("notes",""),f.get("config_json","")))
            conn.commit()
            flash(f"Client cree -- License Key: {license_key}", "success")
            return redirect(url_for("dashboard"))
        except psycopg2.errors.UniqueViolation:
            conn.rollback(); flash("Nom d'utilisateur deja utilise", "error")
        finally:
            cur.close(); conn.close()
    return render_template("client_form.html", client=None, action="nouveau")

@app.route("/client/<int:client_id>")
@login_required
def client_detail(client_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM clients WHERE id=%s", (client_id,))
    client = cur.fetchone()
    cur.execute("SELECT * FROM installations WHERE client_id=%s", (client_id,))
    installation = cur.fetchone()
    cur.execute("SELECT * FROM alertes_centrales WHERE client_id=%s ORDER BY date DESC, heure DESC LIMIT 50", (client_id,))
    alertes_raw = cur.fetchall()
    # Stats feedback — valeurs réelles stockées par l'app : 'confirme' / 'faux_positif'
    fb_vol = fb_faux = fb_aucun = 0
    alertes = []
    for a in alertes_raw:
        ad = dict(a)
        fb = (ad.get("feedback") or "").lower().strip()
        if fb == "confirme":
            ad["_fb_type"] = "vol"; fb_vol += 1
        elif fb == "faux_positif":
            ad["_fb_type"] = "faux"; fb_faux += 1
        else:
            ad["_fb_type"] = "aucun"; fb_aucun += 1
        alertes.append(ad)
    feedback_stats = {"vol": fb_vol, "faux": fb_faux, "aucun": fb_aucun, "total": len(alertes)}
    # Stats globales sur TOUTES les alertes (pas seulement les 50 affichées)
    cur.execute("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN feedback='confirme'     THEN 1 ELSE 0 END) as confirmes,
               SUM(CASE WHEN feedback='faux_positif' THEN 1 ELSE 0 END) as faux_positifs,
               SUM(CASE WHEN feedback IS NULL OR feedback='' THEN 1 ELSE 0 END) as sans_retour
        FROM alertes_centrales WHERE client_id=%s
    """, (client_id,))
    _sg = cur.fetchone()
    stats_globales = dict(_sg) if _sg else {"total":0,"confirmes":0,"faux_positifs":0,"sans_retour":0}
    # Breakdown par type de geste
    cur.execute("""
        SELECT type,
               COUNT(*) as total,
               SUM(CASE WHEN feedback='confirme'     THEN 1 ELSE 0 END) as confirmes,
               SUM(CASE WHEN feedback='faux_positif' THEN 1 ELSE 0 END) as faux_positifs
        FROM alertes_centrales WHERE client_id=%s
        GROUP BY type ORDER BY total DESC
    """, (client_id,))
    stats_par_geste = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT * FROM interventions WHERE client_id=%s ORDER BY date DESC", (client_id,))
    interventions = cur.fetchall()
    cur.execute("SELECT * FROM factures WHERE client_id=%s ORDER BY annee DESC, mois DESC", (client_id,))
    factures = cur.fetchall()
    cur.execute("SELECT id,numero,date_creation,date_envoi,date_signature,statut,signe_par FROM contrats WHERE client_id=%s ORDER BY date_creation DESC", (client_id,))
    contrats_raw = cur.fetchall()
    cur.execute("SELECT * FROM sinistres WHERE client_id=%s ORDER BY date_declaration DESC", (client_id,))
    sinistres_raw = cur.fetchall()
    cur.close(); conn.close()
    # Convertir les objets datetime en strings pour le template
    contrats = []
    for c in contrats_raw:
        cd = dict(c)
        cd['date_creation'] = str(cd['date_creation'])[:10] if cd.get('date_creation') else None
        cd['date_envoi']    = str(cd['date_envoi'])[:10]    if cd.get('date_envoi')    else None
        cd['date_signature']= str(cd['date_signature'])[:10] if cd.get('date_signature') else None
        contrats.append(cd)
    sinistres = []
    for s in sinistres_raw:
        sd = dict(s)
        sd['date_declaration'] = str(sd['date_declaration'])[:16] if sd.get('date_declaration') else None
        sd['date_resolution']  = str(sd['date_resolution'])[:10]  if sd.get('date_resolution')  else None
        sinistres.append(sd)
    snapshots = []
    snap_dir = SNAP_DIR / str(client_id)
    if snap_dir.exists():
        for f in sorted(snap_dir.iterdir())[:12]: snapshots.append(f.name)
    return render_template("client_detail.html", client=client, installation=installation,
                           alertes=alertes, interventions=interventions, factures=factures,
                           snapshots=snapshots, contrats=contrats, sinistres=sinistres,
                           feedback_stats=feedback_stats, stats_globales=stats_globales,
                           stats_par_geste=stats_par_geste, client_id=client_id)

@app.route("/client/<int:client_id>/edit", methods=["GET", "POST"])
@login_required
def edit_client(client_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM clients WHERE id=%s", (client_id,))
    client = cur.fetchone()
    if request.method == "POST":
        f = request.form
        cur.execute("""UPDATE clients SET nom_magasin=%s,contact_nom=%s,contact_tel=%s,contact_email=%s,
            adresse=%s,contrat_debut=%s,contrat_fin=%s,prix_mensuel=%s,statut=%s,notes=%s,config_json=%s WHERE id=%s""",
            (f["nom_magasin"],f.get("contact_nom"),f.get("contact_tel"),f.get("contact_email"),
             f.get("adresse"),f.get("contrat_debut") or None,f.get("contrat_fin") or None,
             float(f.get("prix_mensuel",0)),f["statut"],f.get("notes",""),f.get("config_json",""),client_id))
        if f.get("new_password"):
            cur.execute("UPDATE clients SET password_hash=%s WHERE id=%s", (hash_password(f["new_password"]),client_id))
        conn.commit(); cur.close(); conn.close()
        flash("Client mis a jour","success"); return redirect(url_for("client_detail",client_id=client_id))
    cur.close(); conn.close()
    return render_template("client_form.html", client=dict(client), action="edit")

@app.route("/client/<int:client_id>/reset-password", methods=["POST"])
@login_required
def reset_password_client(client_id):
    new_pwd = request.form.get("new_password","").strip()
    if not new_pwd:
        flash("Mot de passe vide — aucun changement","warning")
        return redirect(url_for("client_detail", client_id=client_id))
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE clients SET password_hash=%s WHERE id=%s", (hash_password(new_pwd), client_id))
    conn.commit(); cur.close(); conn.close()
    flash("Mot de passe app mobile mis à jour ✓","success")
    return redirect(url_for("client_detail", client_id=client_id))

@app.route("/client/<int:client_id>/suspension", methods=["POST"])
@login_required
def suspendre_client(client_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT statut FROM clients WHERE id=%s", (client_id,))
    client = cur.fetchone()
    new_statut = "suspendu" if client["statut"] == "actif" else "actif"
    cur.execute("UPDATE clients SET statut=%s WHERE id=%s", (new_statut,client_id))
    conn.commit(); cur.close(); conn.close()
    flash(f"Compte {'suspendu' if new_statut=='suspendu' else 'reactive'}","success")
    return redirect(url_for("client_detail",client_id=client_id))

@app.route("/client/<int:client_id>/intervention", methods=["POST"])
@login_required
def ajouter_intervention(client_id):
    f = request.form; conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO interventions (client_id,type,description,duree_min) VALUES (%s,%s,%s,%s)",
                (client_id,f["type"],f["description"],int(f.get("duree_min",0))))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("client_detail",client_id=client_id))

@app.route("/client/<int:client_id>/facture", methods=["POST"])
@login_required
def ajouter_facture(client_id):
    f = request.form; conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO factures (client_id,mois,annee,montant,statut) VALUES (%s,%s,%s,%s,%s)",
                (client_id,int(f["mois"]),int(f["annee"]),float(f["montant"]),f.get("statut","impayee")))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("client_detail",client_id=client_id))

@app.route("/facture/<int:facture_id>/payer", methods=["POST"])
@login_required
def marquer_payee(facture_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT client_id FROM factures WHERE id=%s", (facture_id,))
    f = cur.fetchone()
    cur.execute("UPDATE factures SET statut='payee' WHERE id=%s", (facture_id,))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("client_detail",client_id=f["client_id"]))

# =================================================================
# MODULE CONTRATS ROUTES
# =================================================================
@app.route("/client/<int:client_id>/contrat/generer", methods=["POST"])
@login_required
def generer_contrat(client_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM clients WHERE id=%s", (client_id,))
    client = cur.fetchone()
    if not client:
        cur.close(); conn.close(); flash("Client introuvable","error"); return redirect(url_for("dashboard"))
    numero = generer_numero_contrat()
    for _ in range(5):
        cur.execute("SELECT id FROM contrats WHERE numero=%s", (numero,))
        if not cur.fetchone(): break
        numero = generer_numero_contrat()
    cur.execute("INSERT INTO contrats (client_id,numero,statut) VALUES (%s,%s,'brouillon') RETURNING id",
                (client_id,numero))
    contrat_id = cur.fetchone()["id"]
    conn.commit()
    contrat_row = {"id":contrat_id,"numero":numero,"date_signature":None,"signe_par":None,"ip_signature":None}
    try:
        pdf_bytes = generer_pdf_contrat(dict(client), contrat_row, signe=False)
    except Exception as e:
        cur.execute("DELETE FROM contrats WHERE id=%s", (contrat_id,))
        conn.commit(); cur.close(); conn.close()
        flash(f"Erreur PDF: {str(e)}","error"); return redirect(url_for("client_detail",client_id=client_id))
    cur.execute("UPDATE contrats SET pdf_data=%s, statut='genere' WHERE id=%s",
                (psycopg2.Binary(pdf_bytes),contrat_id))
    conn.commit(); cur.close(); conn.close()
    try: envoyer_email_contrat(OVH_SMTP_USER,client["nom_magasin"],numero,pdf_bytes,signe=False)
    except Exception: pass
    flash(f"Contrat {numero} genere. Backup email envoye a contact@radaria.fr","success")
    return redirect(url_for("client_detail",client_id=client_id))

@app.route("/contrat/<int:contrat_id>/pdf")
@login_required
def telecharger_contrat(contrat_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT co.*,cl.nom_magasin FROM contrats co JOIN clients cl ON cl.id=co.client_id WHERE co.id=%s", (contrat_id,))
    contrat = cur.fetchone(); cur.close(); conn.close()
    if not contrat: flash("Contrat introuvable","error"); return redirect(url_for("dashboard"))
    signe = contrat["statut"] == "signe"
    pdf_data = contrat["pdf_signe_data"] if signe and contrat["pdf_signe_data"] else contrat["pdf_data"]
    if not pdf_data: flash("PDF non disponible","error"); return redirect(url_for("dashboard"))
    suffix = "_signe" if signe else ""
    return Response(bytes(pdf_data), mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename=Contrat_RadarIA_{contrat['numero']}{suffix}.pdf"})

@app.route("/contrat/<int:contrat_id>/envoyer-client", methods=["POST"])
@login_required
def envoyer_contrat_client(contrat_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT co.*,cl.nom_magasin,cl.contact_email,cl.id as cid FROM contrats co JOIN clients cl ON cl.id=co.client_id WHERE co.id=%s", (contrat_id,))
    contrat = cur.fetchone()
    if not contrat or not contrat["contact_email"]:
        cur.close(); conn.close(); flash("Contrat introuvable ou client sans email","error"); return redirect(url_for("dashboard"))
    pdf_data = contrat["pdf_data"]
    if not pdf_data:
        cur.close(); conn.close(); flash("PDF non genere","error"); return redirect(url_for("client_detail",client_id=contrat["cid"]))
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now() + timedelta(hours=72)
    cur.execute("UPDATE client_tokens SET used=TRUE WHERE client_id=%s AND used=FALSE", (contrat["client_id"],))
    cur.execute("INSERT INTO client_tokens (client_id,token,expires_at) VALUES (%s,%s,%s)", (contrat["client_id"],token,expires_at))
    cur.execute("UPDATE contrats SET date_envoi=NOW(),statut='envoye' WHERE id=%s", (contrat_id,))
    conn.commit(); cur.close(); conn.close()
    try:
        envoyer_email_contrat(contrat["contact_email"],contrat["nom_magasin"],contrat["numero"],bytes(pdf_data),signe=False)
        flash(f"Contrat envoye a {contrat['contact_email']}","success")
    except Exception as e:
        flash(f"Erreur envoi: {str(e)}","error")
    return redirect(url_for("client_detail",client_id=contrat["client_id"]))

# =================================================================
# PORTAIL CLIENT
# =================================================================
@app.route("/client/<int:client_id>/envoyer-acces", methods=["POST"])
@login_required
def envoyer_acces_portal(client_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM clients WHERE id=%s", (client_id,))
    client = cur.fetchone()
    if not client: cur.close(); conn.close(); flash("Client introuvable","error"); return redirect(url_for("dashboard"))
    email = client["contact_email"]
    if not email: cur.close(); conn.close(); flash("Aucun email configure","error"); return redirect(url_for("client_detail",client_id=client_id))
    cur.execute("UPDATE client_tokens SET used=TRUE WHERE client_id=%s AND used=FALSE", (client_id,))
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now() + timedelta(hours=48)
    cur.execute("INSERT INTO client_tokens (client_id,token,expires_at) VALUES (%s,%s,%s)", (client_id,token,expires_at))
    conn.commit(); cur.close(); conn.close()
    magic_url = f"{BACKOFFICE_URL}/portal/verify/{token}"
    try:
        envoyer_email_magic_link(email, client["nom_magasin"], magic_url)
        flash(f"Lien envoye a {email} (valable 48h)","success")
    except Exception as e:
        flash(f"Erreur email: {str(e)}","error")
    return redirect(url_for("client_detail",client_id=client_id))

@app.route("/portal/verify/<token>")
def portal_verify(token):
    conn = get_db(); cur = conn.cursor()
    cur.execute("""SELECT ct.id,ct.client_id,c.nom_magasin,c.statut as client_statut FROM client_tokens ct
        JOIN clients c ON c.id=ct.client_id WHERE ct.token=%s AND ct.used=FALSE AND ct.expires_at>NOW()""", (token,))
    tok = cur.fetchone()
    if not tok or tok["client_statut"] != "actif":
        cur.close(); conn.close()
        return render_template("client_portal.html",expired=True,logged_out=False,contrats=[]),403
    cur.execute("UPDATE client_tokens SET used=TRUE WHERE id=%s", (tok["id"],))
    conn.commit(); cur.close(); conn.close()
    session["portal_client_id"] = tok["client_id"]
    session["portal_nom"] = tok["nom_magasin"]
    return redirect(url_for("portal"))

@app.route("/portal")
@client_portal_required
def portal():
    client_id = session["portal_client_id"]
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM clients WHERE id=%s AND statut='actif'", (client_id,))
    client = cur.fetchone()
    if not client:
        cur.close(); conn.close(); session.pop("portal_client_id",None)
        return render_template("client_portal.html",expired=True,logged_out=False,contrats=[]),403
    cur.execute("SELECT * FROM factures WHERE client_id=%s ORDER BY annee DESC,mois DESC LIMIT 24", (client_id,))
    factures = cur.fetchall()
    cur.execute("SELECT type,camera,date,heure FROM alertes_centrales WHERE client_id=%s ORDER BY date DESC,heure DESC LIMIT 10", (client_id,))
    alertes = cur.fetchall()
    cur.execute("SELECT statut,cameras_actives,nb_cameras,last_seen FROM installations WHERE client_id=%s", (client_id,))
    installation = cur.fetchone()
    cur.execute("SELECT id,numero,date_creation,date_signature,statut,signe_par FROM contrats WHERE client_id=%s ORDER BY date_creation DESC", (client_id,))
    contrats_raw = cur.fetchall()
    cur.close(); conn.close()
    contrats = []
    for c in contrats_raw:
        cd = dict(c)
        cd['date_creation'] = str(cd['date_creation'])[:10] if cd.get('date_creation') else None
        cd['date_signature']= str(cd['date_signature'])[:10] if cd.get('date_signature') else None
        contrats.append(cd)
    return render_template("client_portal.html", client=dict(client),
                           factures=[dict(f) for f in factures], alertes=[dict(a) for a in alertes],
                           installation=dict(installation) if installation else None,
                           contrats=[dict(c) for c in contrats], expired=False, logged_out=False)

@app.route("/portal/logout")
def portal_logout():
    session.pop("portal_client_id",None); session.pop("portal_nom",None)
    return render_template("client_portal.html",expired=False,logged_out=True,client=None,
                           factures=[],alertes=[],installation=None,contrats=[]),200

@app.route("/portal/contrat/<int:contrat_id>/signer", methods=["POST"])
@client_portal_required
def portal_signer_contrat(contrat_id):
    client_id = session["portal_client_id"]
    nom_signataire = request.form.get("nom_signataire","").strip()
    if not nom_signataire:
        flash("Veuillez indiquer votre nom complet","error"); return redirect(url_for("portal"))
    conn = get_db(); cur = conn.cursor()
    cur.execute("""SELECT co.*,cl.nom_magasin,cl.contact_email,cl.siret,cl.adresse,cl.forme_juridique,
        cl.contact_nom,cl.contact_tel,cl.prix_mensuel,cl.contrat_debut,cl.contrat_fin
        FROM contrats co JOIN clients cl ON cl.id=co.client_id
        WHERE co.id=%s AND co.client_id=%s AND co.statut!='signe'""", (contrat_id,client_id))
    contrat = cur.fetchone()
    if not contrat:
        cur.close(); conn.close(); flash("Contrat introuvable ou deja signe","error"); return redirect(url_for("portal"))
    ip_sig = request.headers.get("X-Forwarded-For", request.remote_addr)
    now = datetime.now()
    contrat_dict = dict(contrat)
    contrat_dict["date_signature"] = now; contrat_dict["signe_par"] = nom_signataire; contrat_dict["ip_signature"] = ip_sig
    client_dict = {k: contrat[k] for k in ["nom_magasin","siret","adresse","forme_juridique","contact_nom","contact_email","contact_tel","prix_mensuel","contrat_debut","contrat_fin"]}
    pdf_signe = generer_pdf_contrat(client_dict, contrat_dict, signe=True)
    cur.execute("UPDATE contrats SET statut='signe',date_signature=%s,signe_par=%s,ip_signature=%s,pdf_signe_data=%s WHERE id=%s",
                (now,nom_signataire,ip_sig,psycopg2.Binary(pdf_signe),contrat_id))
    conn.commit(); cur.close(); conn.close()
    try: envoyer_email_contrat(OVH_SMTP_USER,contrat["nom_magasin"],contrat["numero"],pdf_signe,signe=True)
    except Exception: pass
    if contrat["contact_email"]:
        try: envoyer_email_contrat(contrat["contact_email"],contrat["nom_magasin"],contrat["numero"],pdf_signe,signe=True)
        except Exception: pass
    flash(f"Contrat {contrat['numero']} signe ! Un exemplaire vous a ete envoye par email.","success")
    return redirect(url_for("portal"))

# =================================================================
# SNAPSHOTS
# =================================================================
@app.route("/snapshots/<int:client_id>/<filename>")
@login_required
def get_snapshot(client_id, filename):
    return send_from_directory(str(SNAP_DIR / str(client_id)), filename)

# =================================================================
# API PC CLIENTS
# =================================================================
@app.route("/api/config", methods=["POST"])
def api_config():
    data = request.get_json(silent=True) or {}; key = data.get("license_key","")
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM clients WHERE license_key=%s AND statut='actif'", (key,))
    client = cur.fetchone(); cur.close(); conn.close()
    if not client: return jsonify({"error":"License invalide"}),403
    config_json = client.get("config_json","") or ""
    try: config = json.loads(config_json) if config_json.strip() else {}
    except Exception: config = {}
    config["nom_magasin"] = client["nom_magasin"]; config["username"] = client["username"]
    # Inclure la config par caméra (gestes + seuils IA)
    conn2 = get_db(); cur2 = conn2.cursor()
    cur2.execute(
        "SELECT camera_name,active,gestes_json,confidence_min,cooldown_min,alertes_max_jour "
        "FROM camera_config WHERE magasin_id=%s", (client["id"],)
    )
    cam_rows = cur2.fetchall(); cur2.close(); conn2.close()
    camera_config = {}
    for cr in cam_rows:
        try: gestes = json.loads(cr["gestes_json"] or "{}")
        except Exception: gestes = {}
        camera_config[cr["camera_name"]] = {
            "active": bool(cr["active"]), "gestes": gestes,
            "confidence_min": cr["confidence_min"],
            "cooldown_min": cr["cooldown_min"],
            "alertes_max_jour": cr["alertes_max_jour"],
        }
    config["camera_config"] = camera_config
    return jsonify({"ok":True,"config":config})

@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json(silent=True) or {}; key = data.get("license_key","")
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM clients WHERE license_key=%s AND statut='actif'", (key,))
    client = cur.fetchone()
    if not client: cur.close(); conn.close(); return jsonify({"error":"License invalide"}),403
    cur.execute("SELECT id FROM installations WHERE client_id=%s", (client["id"],))
    existing = cur.fetchone(); now = datetime.now().isoformat()
    if existing:
        cur.execute("""UPDATE installations SET pc_hostname=%s,ip_locale=%s,ip_tailscale=%s,ip_publique=%s,
            os_info=%s,nb_cameras=%s,last_seen=%s,statut='online' WHERE client_id=%s""",
            (data.get("hostname"),data.get("ip_locale"),data.get("ip_tailscale"),request.remote_addr,
             data.get("os_info"),data.get("nb_cameras",0),now,client["id"]))
    else:
        cur.execute("""INSERT INTO installations (client_id,pc_hostname,ip_locale,ip_tailscale,ip_publique,os_info,nb_cameras,last_seen,statut)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'online')""",
            (client["id"],data.get("hostname"),data.get("ip_locale"),data.get("ip_tailscale"),
             request.remote_addr,data.get("os_info"),data.get("nb_cameras",0),now))
    conn.commit()
    result = {"success":True,"client_id":client["id"],"nom_magasin":client["nom_magasin"],
              "username":client["username"],"password_hash":client["password_hash"]}
    cur.close(); conn.close(); return jsonify(result)

@app.route("/api/heartbeat", methods=["POST"])
@limiter.limit("120 per minute")
def api_heartbeat():
    data = request.get_json(silent=True) or {}; key = data.get("license_key","")
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id FROM clients WHERE license_key=%s AND statut='actif'", (key,))
    client = cur.fetchone()
    if not client: cur.close(); conn.close(); return jsonify({"error":"License invalide"}),403
    cur.execute("UPDATE installations SET last_seen=%s,statut='online',cameras_actives=%s,nb_cameras=%s WHERE client_id=%s",
                (datetime.now().isoformat(),data.get("cameras_actives",0),data.get("nb_cameras",0),client["id"]))
    conn.commit()
    cur.execute("SELECT username,password_hash FROM clients WHERE id=%s", (client["id"],))
    fc = cur.fetchone(); cur.close(); conn.close()
    return jsonify({"ok":True,"username":fc["username"],"password_hash":fc["password_hash"]})

@app.route("/api/alerte", methods=["POST"])
@limiter.limit("60 per minute")
def api_alerte():
    data = request.get_json(silent=True) or {}; key = data.get("license_key","")
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id FROM clients WHERE license_key=%s AND statut='actif'", (key,))
    client = cur.fetchone()
    if not client: cur.close(); conn.close(); return jsonify({"error":"License invalide"}),403
    alert_type = data.get("type","")
    camera_nom = data.get("camera","")
    cur.execute("""INSERT INTO alertes_centrales (client_id,alert_id,type,camera,date,heure,image_path,video_url,suspect_id,nb_personnes)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (client["id"],data.get("alert_id"),alert_type,camera_nom,
         data.get("date",str(date.today())),data.get("heure",""),data.get("image",""),
         data.get("video_url",""),str(data.get("suspect_id","")),data.get("nb_personnes",1)))
    conn.commit(); cur.close(); conn.close()
    # Envoi push notification (async, ne bloque pas la réponse)
    try:
        import threading
        type_labels = {
            "mouvement_rapide":"Mouvement suspect","posture_basse":"Posture suspecte",
            "presence_longue":"Présence longue","sac_suspect":"Sac suspect",
            "consommation":"Consommation sans payer","vol_caisse":"Vol dans la caisse",
            "vol_etalage":"Vol à l'étalage","vol_a_la_tire":"Vol à la tire",
            "agression":"Agression / violence",
        }
        label = type_labels.get(alert_type, alert_type or "Alerte")
        titre = f"🚨 {label}"
        corps = f"Caméra : {camera_nom}" if camera_nom else "Nouvelle alerte détectée"
        threading.Thread(target=envoyer_push_notifications,
                         args=(client["id"], titre, corps, {"alert_id":data.get("alert_id")}),
                         daemon=True).start()
    except Exception: pass
    return jsonify({"ok":True})

# =================================================================
# HELPERS JWT (app mobile)
# =================================================================
def generer_jwt(client_id, nom_magasin):
    payload = {"cid": client_id, "nom": nom_magasin,
               "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRY_HOURS),
               "iat": datetime.utcnow()}
    if HAS_JWT:
        return pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")
    return ""

def verifier_jwt(token):
    if not HAS_JWT or not token:
        return None
    try:
        return pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        return None

def get_mobile_client_id():
    """Retourne client_id depuis JWT (Bearer) ou license_key (legacy)"""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        payload = verifier_jwt(auth[7:])
        if payload:
            return payload.get("cid")
    key = (request.args.get("license_key") or
           (request.get_json(silent=True) or {}).get("license_key", ""))
    if key:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT id FROM clients WHERE license_key=%s AND statut='actif'", (key,))
        row = cur.fetchone(); cur.close(); conn.close()
        return row["id"] if row else None
    return None

@app.route("/api/mobile/login", methods=["POST"])
@limiter.limit("10 per minute")
def api_mobile_login():
    data = request.get_json(silent=True) or {}
    conn = get_db(); cur = conn.cursor()
    cur.execute("""SELECT c.*,i.statut as inst_statut,i.last_seen,i.cameras_actives,i.nb_cameras
        FROM clients c LEFT JOIN installations i ON i.client_id=c.id
        WHERE c.username=%s AND c.statut='actif'""",
        (data.get("username",""),))
    client = cur.fetchone(); cur.close(); conn.close()
    if not client or not verify_password(data.get("password",""), client["password_hash"]):
        return jsonify({"error":"Identifiants invalides"}),401
    token = generer_jwt(client["id"], client["nom_magasin"])
    return jsonify({"ok":True,"token":token,"expires_in":JWT_EXPIRY_HOURS*3600,
                    "client_id":client["id"],"nom_magasin":client["nom_magasin"],
                    "license_key":client["license_key"],
                    "pc_statut":client["inst_statut"] or "offline",
                    "cameras_actives":client["cameras_actives"] or 0,
                    "nb_cameras":client["nb_cameras"] or 0,
                    "last_seen":client["last_seen"] or ""})

@app.route("/api/mobile/alertes", methods=["GET"])
@limiter.limit("60 per minute")
def api_mobile_alertes():
    client_id = get_mobile_client_id()
    if not client_id: return jsonify({"error":"Authentification requise"}),401
    limit = min(int(request.args.get("limit", 50)), 200)
    conn = get_db(); cur = conn.cursor()
    cur.execute("""SELECT alert_id,type,camera,date,heure,image_path,
        COALESCE(NULLIF(video_stored_url,''), NULLIF(video_url,'')) AS video_url,
        feedback,suspect_id,nb_personnes
        FROM alertes_centrales WHERE client_id=%s ORDER BY date DESC,heure DESC LIMIT %s""", (client_id, limit))
    alertes = [dict(a) for a in cur.fetchall()]
    cur.execute("SELECT COUNT(*) as n FROM alertes_centrales WHERE client_id=%s AND date=CURRENT_DATE::TEXT", (client_id,))
    today_count = cur.fetchone()["n"]
    cur.execute("SELECT statut,cameras_actives,nb_cameras,last_seen FROM installations WHERE client_id=%s", (client_id,))
    inst = cur.fetchone(); cur.close(); conn.close()
    return jsonify({"ok":True,"alertes":alertes,"alertes_today":today_count,
                    "pc_statut":inst["statut"] if inst else "offline",
                    "cameras_actives":inst["cameras_actives"] if inst else 0,
                    "nb_cameras":inst["nb_cameras"] if inst else 0,
                    "last_seen":inst["last_seen"] if inst else ""})

@app.route("/api/mobile/status", methods=["GET"])
@limiter.limit("60 per minute")
def api_mobile_status():
    client_id = get_mobile_client_id()
    if not client_id: return jsonify({"error":"Authentification requise"}),401
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT statut,cameras_actives,nb_cameras,last_seen FROM installations WHERE client_id=%s", (client_id,))
    row = cur.fetchone()
    cur.execute("SELECT COUNT(*) as n FROM alertes_centrales WHERE client_id=%s AND date=CURRENT_DATE::TEXT", (client_id,))
    today = cur.fetchone()
    cur.close(); conn.close()
    alertes_today = today["n"] if today else 0
    if not row: return jsonify({"ok":True,"pc_statut":"offline","cameras_actives":0,"nb_cameras":0,"last_seen":"","alertes_today":alertes_today})
    return jsonify({"ok":True,"pc_statut":row["statut"] or "offline","cameras_actives":row["cameras_actives"] or 0,
                    "nb_cameras":row["nb_cameras"] or 0,"last_seen":row["last_seen"] or "",
                    "alertes_today":alertes_today})


@app.route("/api/mobile/video-request/<alert_id>", methods=["GET"])
@limiter.limit("30 per minute")
def api_mobile_video_request(alert_id):
    """Retourne l'URL publique de la vidéo d'une alerte (ou déclenche l'upload depuis le PC)."""
    client_id = get_mobile_client_id()
    if not client_id: return jsonify({"error":"Authentification requise"}),401
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT video_url, video_data FROM alertes_centrales WHERE alert_id=%s AND client_id=%s",
                (alert_id, client_id))
    row = cur.fetchone(); cur.close(); conn.close()
    if not row: return jsonify({"error":"Alerte introuvable"}),404
    # Si la vidéo est déjà stockée côté backoffice, retourner l'URL publique
    video_url = row.get("video_url","")
    if video_url and video_url.startswith("https://backoffice.radaria.fr"):
        return jsonify({"ok":True,"video_url":video_url,"status":"ready"})
    # Sinon : la vidéo est sur le PC local — signaler qu'elle est en attente d'upload
    # Le PC va la push via /api/video-upload dans les prochaines secondes
    return jsonify({"ok":True,"video_url":None,"status":"pending",
                    "message":"Vidéo en cours de chargement depuis le PC..."})

# Credentials OVH FTP (stockage permanent pour les clips vidéo)
OVH_FTP_HOST = "ftp.cluster100.hosting.ovh.net"
OVH_FTP_USER = "radariv"
OVH_FTP_PASS = "RadariaFTP2026"

def _upload_clip_ovh(video_bytes, client_id, fname):
    """Upload un clip vidéo vers OVH via FTP. Retourne l'URL publique ou None."""
    try:
        ftp = ftplib.FTP()
        ftp.connect(OVH_FTP_HOST, 21, timeout=60)
        ftp.login(OVH_FTP_USER, OVH_FTP_PASS)
        ftp.set_pasv(True)
        # Créer les dossiers si nécessaire
        for folder in ["/www/clips", f"/www/clips/{client_id}"]:
            try:
                ftp.mkd(folder)
            except ftplib.error_perm:
                pass  # dossier déjà existant
        ftp.cwd(f"/www/clips/{client_id}")
        ftp.storbinary(f"STOR {fname}", io.BytesIO(video_bytes))
        ftp.quit()
        return f"https://radaria.fr/clips/{client_id}/{fname}"
    except Exception as e:
        app.logger.warning(f"OVH FTP upload error: {e}")
        return None

@app.route("/api/video-upload", methods=["POST"])
def api_video_upload():
    """Le PC surveillance uploade un clip vidéo pour le rendre accessible hors WiFi."""
    data = request.get_json(silent=True) or {}
    key = data.get("license_key","")
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id FROM clients WHERE license_key=%s AND statut='actif'", (key,))
    client = cur.fetchone()
    if not client: cur.close(); conn.close(); return jsonify({"error":"License invalide"}),403
    alert_id = data.get("alert_id","")
    video_b64 = data.get("video_b64","")
    if not alert_id or not video_b64:
        cur.close(); conn.close()
        return jsonify({"error":"alert_id et video_b64 requis"}),400
    try:
        video_bytes = base64.b64decode(video_b64)
        client_id = client["id"]
        fname = f"{alert_id}.mp4"
        # Upload vers OVH (stockage permanent — ne disparaît pas au redémarrage Railway)
        public_url = _upload_clip_ovh(video_bytes, client_id, fname)
        if not public_url:
            # Fallback : stocker localement (temporaire, mais mieux que rien)
            clips_dir = Path("clips") / str(client_id)
            clips_dir.mkdir(parents=True, exist_ok=True)
            (clips_dir / fname).write_bytes(video_bytes)
            public_url = f"https://backoffice.radaria.fr/clips/{client_id}/{fname}"
        cur.execute("UPDATE alertes_centrales SET video_stored_url=%s WHERE alert_id=%s AND client_id=%s",
                    (public_url, alert_id, client_id))
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"ok":True,"video_url":public_url})
    except Exception as e:
        cur.close(); conn.close()
        return jsonify({"error":str(e)}),500

@app.route("/clips/<int:client_id>/<path:filename>")
def servir_clip(client_id, filename):
    """Sert les clips vidéo stockés côté backoffice."""
    clips_dir = Path("clips") / str(client_id)
    return send_from_directory(clips_dir, filename)

@app.route("/api/snapshot", methods=["POST"])
def api_snapshot():
    data = request.get_json(silent=True) or {}; key = data.get("license_key","")
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id FROM clients WHERE license_key=%s", (key,))
    client = cur.fetchone()
    if not client: cur.close(); conn.close(); return jsonify({"error":"License invalide"}),403
    client_id = client["id"]
    camera_nom = data.get("camera","cam").replace(" ","_")
    img_b64 = data.get("image_b64",""); cur.close(); conn.close()
    if img_b64:
        snap_dir = SNAP_DIR / str(client_id)
        snap_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fpath = snap_dir / f"{ts}_{camera_nom}.jpg"
        try:
            fpath.write_bytes(base64.b64decode(img_b64))
            snaps = sorted(snap_dir.iterdir())
            for old in snaps[:-50]: old.unlink()
        except Exception: pass
    return jsonify({"ok":True})

# =================================================================
# MODULE SINISTRES
# =================================================================
TYPES_SINISTRE = ['Vol', 'Vandalisme', 'Intrusion', 'Incendie', 'Degat des eaux', 'Accident', 'Autre']

@app.route("/client/<int:client_id>/sinistre", methods=["POST"])
@login_required
def ajouter_sinistre(client_id):
    f = request.form; conn = get_db(); cur = conn.cursor()
    cur.execute("""INSERT INTO sinistres (client_id,type,origine,description,statut)
                   VALUES (%s,%s,'admin',%s,'ouvert')""",
                (client_id, f.get("type","Autre"), f.get("description","")))
    conn.commit(); cur.close(); conn.close()
    flash("Sinistre enregistre","success")
    return redirect(url_for("client_detail", client_id=client_id))

@app.route("/sinistre/<int:sinistre_id>/statut", methods=["POST"])
@login_required
def maj_statut_sinistre(sinistre_id):
    nouveau_statut = request.form.get("statut","ouvert")
    notes = request.form.get("notes_admin","")
    redirect_url  = request.form.get("redirect","")
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT client_id FROM sinistres WHERE id=%s", (sinistre_id,))
    row = cur.fetchone()
    if nouveau_statut in ("resolu","ferme"):
        cur.execute("UPDATE sinistres SET statut=%s, notes_admin=%s, date_resolution=NOW() WHERE id=%s",
                    (nouveau_statut, notes, sinistre_id))
    else:
        cur.execute("UPDATE sinistres SET statut=%s, notes_admin=%s, date_resolution=NULL WHERE id=%s",
                    (nouveau_statut, notes, sinistre_id))
    conn.commit(); cur.close(); conn.close()
    if redirect_url:
        return redirect(redirect_url)
    if row:
        return redirect(url_for("client_detail", client_id=row["client_id"]))
    return redirect(url_for("dashboard"))

@app.route("/sinistres")
@login_required
def liste_sinistres():
    filtre_statut  = request.args.get("statut","")
    filtre_origine = request.args.get("origine","")
    filtre_type    = request.args.get("type","")
    client_id_f    = request.args.get("client_id","")
    conn = get_db(); cur = conn.cursor()
    conditions = []; params = []
    if filtre_statut:  conditions.append("s.statut=%s");    params.append(filtre_statut)
    if filtre_origine: conditions.append("s.origine=%s");   params.append(filtre_origine)
    if filtre_type:    conditions.append("s.type=%s");      params.append(filtre_type)
    if client_id_f:    conditions.append("s.client_id=%s"); params.append(client_id_f)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    cur.execute(f"""SELECT s.*, c.nom_magasin FROM sinistres s
                    JOIN clients c ON c.id=s.client_id {where}
                    ORDER BY s.date_declaration DESC""", params)
    sinistres_raw = cur.fetchall()
    cur.close(); conn.close()
    sinistres = []
    for s in sinistres_raw:
        sd = dict(s)
        sd['date_declaration'] = str(sd['date_declaration'])[:16] if sd.get('date_declaration') else None
        sd['date_resolution']  = str(sd['date_resolution'])[:10]  if sd.get('date_resolution')  else None
        sinistres.append(sd)
    return render_template("sinistres.html", sinistres=sinistres,
                           filtre_statut=filtre_statut, filtre_origine=filtre_origine,
                           filtre_type=filtre_type, types_sinistre=TYPES_SINISTRE)

# -- API mobile sinistres
@app.route("/api/mobile/sinistre", methods=["POST"])
@limiter.limit("30 per minute")
def api_mobile_declarer_sinistre():
    data = request.get_json(silent=True) or {}
    client_id = get_mobile_client_id()
    if not client_id: return jsonify({"error":"Authentification requise"}),401
    type_s = data.get("type","Autre")
    desc   = data.get("description","")
    conn = get_db(); cur = conn.cursor()
    cur.execute("""INSERT INTO sinistres (client_id,type,origine,description,statut)
                   VALUES (%s,%s,'client',%s,'ouvert') RETURNING id""",
                (client_id, type_s, desc))
    sinistre_id = cur.fetchone()["id"]
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok":True,"sinistre_id":sinistre_id})

@app.route("/api/mobile/sinistres", methods=["GET"])
@limiter.limit("60 per minute")
def api_mobile_sinistres():
    client_id = get_mobile_client_id()
    if not client_id: return jsonify({"error":"Authentification requise"}),401
    conn = get_db(); cur = conn.cursor()
    cur.execute("""SELECT id,type,origine,description,statut,
                          date_declaration::TEXT,notes_admin
                   FROM sinistres WHERE client_id=%s ORDER BY date_declaration DESC""", (client_id,))
    sinistres = [dict(s) for s in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify({"ok":True,"sinistres":sinistres})

@app.route("/api/mobile/feedback", methods=["POST"])
@limiter.limit("60 per minute")
def api_mobile_feedback():
    """Enregistre le feedback (confirme / faux_positif) sur une alerte."""
    data = request.get_json(silent=True) or {}
    client_id = get_mobile_client_id()
    if not client_id: return jsonify({"error":"Authentification requise"}),401
    alert_id = data.get("alert_id")
    feedback  = data.get("feedback","")  # "confirme" ou "faux_positif"
    if not alert_id or feedback not in ("confirme","faux_positif"):
        return jsonify({"error":"Paramètres invalides"}),400
    conn = get_db(); cur = conn.cursor()
    cur.execute("""UPDATE alertes_centrales SET feedback=%s
                   WHERE alert_id=%s AND client_id=%s""",
                (feedback, alert_id, client_id))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok":True})

@app.route("/api/mobile/push-token", methods=["POST"])
@limiter.limit("30 per minute")
def api_mobile_push_token():
    """Enregistre le push token Expo pour les notifications."""
    data = request.get_json(silent=True) or {}
    client_id = get_mobile_client_id()
    if not client_id: return jsonify({"error":"Authentification requise"}),401
    token    = data.get("token","").strip()
    platform = data.get("platform","ios")
    if not token: return jsonify({"error":"Token manquant"}),400
    conn = get_db(); cur = conn.cursor()
    cur.execute("""INSERT INTO push_tokens (client_id,token,platform,updated_at)
                   VALUES (%s,%s,%s,NOW())
                   ON CONFLICT (token) DO UPDATE SET client_id=%s, updated_at=NOW()""",
                (client_id, token, platform, client_id))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok":True})

@app.route("/api/mobile/snapshots", methods=["GET"])
@limiter.limit("60 per minute")
def api_mobile_snapshots():
    """Retourne le dernier snapshot (base64) par caméra pour l'app mobile."""
    client_id = get_mobile_client_id()
    if not client_id: return jsonify({"error":"Authentification requise"}),401
    snap_dir = SNAP_DIR / str(client_id)
    if not snap_dir.exists():
        return jsonify({"ok":True,"snapshots":[]})
    # Grouper fichiers par caméra et garder le plus récent
    cam_latest = {}  # cam_name -> (path, mtime)
    for f in snap_dir.iterdir():
        if not f.name.endswith('.jpg'):
            continue
        # Format: YYYYMMDD_HHMMSS_camname.jpg  (ts = 15 chars, puis _)
        if len(f.name) < 17:
            continue
        cam_key = f.name[16:-4].replace('_', ' ')  # nom caméra lisible
        mtime = f.stat().st_mtime
        if cam_key not in cam_latest or mtime > cam_latest[cam_key][1]:
            cam_latest[cam_key] = (f, mtime)
    snapshots = []
    for cam_name, (fpath, mtime) in cam_latest.items():
        try:
            img_b64 = base64.b64encode(fpath.read_bytes()).decode()
            snapshots.append({
                "camera": cam_name,
                "image_b64": img_b64,
                "ts": datetime.fromtimestamp(mtime).strftime("%H:%M:%S"),
            })
        except Exception:
            pass
    return jsonify({"ok":True,"snapshots":snapshots})

@app.route("/api/mobile/clips", methods=["GET"])
@limiter.limit("60 per minute")
def api_mobile_clips():
    """Retourne les alertes ayant une vidéo enregistrée."""
    client_id = get_mobile_client_id()
    if not client_id: return jsonify({"error":"Authentification requise"}),401
    conn = get_db(); cur = conn.cursor()
    cur.execute("""SELECT alert_id,type,camera,date,heure,
                   COALESCE(NULLIF(video_stored_url,''), video_url) AS video_url,
                   feedback
                   FROM alertes_centrales
                   WHERE client_id=%s
                     AND (video_url IS NOT NULL AND video_url != ''
                       OR video_stored_url IS NOT NULL AND video_stored_url != '')
                   ORDER BY date DESC,heure DESC LIMIT 100""", (client_id,))
    clips = [dict(c) for c in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify({"ok":True,"clips":clips})

@app.route("/api/mobile/cameras", methods=["GET"])
@limiter.limit("60 per minute")
def api_mobile_cameras():
    """Retourne la config par caméra (gestes + seuils IA) pour l'app mobile.
    Priorité : vrais noms depuis config_json du client (= noms utilisés par surveillance.py).
    Fallback : ce qui est en BDD camera_config.
    """
    client_id = get_mobile_client_id()
    if not client_id: return jsonify({"error":"Authentification requise"}),401
    conn = get_db(); cur = conn.cursor()

    # 1) Config déjà sauvegardée en BDD (keyed par camera_name)
    cur.execute(
        "SELECT camera_name,active,gestes_json,confidence_min,cooldown_min,alertes_max_jour "
        "FROM camera_config WHERE magasin_id=%s", (client_id,)
    )
    cam_map = {}
    for r in cur.fetchall():
        try: gestes = json.loads(r["gestes_json"] or "{}")
        except Exception: gestes = {}
        cam_map[r["camera_name"]] = {
            "camera_name": r["camera_name"], "active": bool(r["active"]),
            "gestes": gestes, "confidence_min": r["confidence_min"],
            "cooldown_min": r["cooldown_min"], "alertes_max_jour": r["alertes_max_jour"],
        }

    # 2) Liste réelle des caméras depuis config_json du client
    cur.execute("SELECT config_json FROM clients WHERE id=%s", (client_id,))
    row = cur.fetchone(); cur.close(); conn.close()
    config_json_str = (row["config_json"] if row else "") or ""
    try:
        cfg = json.loads(config_json_str) if config_json_str.strip() else {}
        cam_list = cfg.get("cameras", [])
    except Exception:
        cam_list = []

    DEFAULT_GESTES = {
        "mouvement_rapide": True, "posture_basse": True, "presence_longue": True,
        "sac_suspect": True, "consommation": True, "vol_caisse": True,
        "vol_etalage": True, "vol_a_la_tire": True, "agression": True,
    }

    cameras = []
    if cam_list:
        # Utiliser les vrais noms (= noms que surveillance.py utilise via self.nom)
        for cam in cam_list:
            nom = cam.get("nom") or cam.get("name") or f"Camera {cam.get('index', cam.get('id','?'))}"
            if nom in cam_map:
                cameras.append(cam_map[nom])
            else:
                cameras.append({
                    "camera_name": nom, "active": True,
                    "gestes": dict(DEFAULT_GESTES),
                    "confidence_min": 0.65, "cooldown_min": 5, "alertes_max_jour": 20,
                })
    else:
        # Fallback : retourner ce qui est en BDD
        cameras = sorted(cam_map.values(), key=lambda x: x["camera_name"])

    return jsonify({"ok":True,"cameras":cameras,"global_ia":{"confidence_min":0.65,"cooldown_min":5,"alertes_max_jour":20}})

@app.route("/api/mobile/config-gestes", methods=["POST"])
@limiter.limit("30 per minute")
def api_mobile_config_gestes():
    """Sauvegarde la config gestes/IA par caméra depuis l'app mobile."""
    client_id = get_mobile_client_id()
    if not client_id: return jsonify({"error":"Authentification requise"}),401
    data = request.get_json() or {}
    cameras = data.get("cameras", [])
    global_ia = data.get("global_ia", {})
    conn = get_db(); cur = conn.cursor()
    for cam in cameras:
        cam_name = (cam.get("camera_name") or "").strip()
        if not cam_name: continue
        active   = bool(cam.get("active", True))
        gestes_j = json.dumps(cam.get("gestes", {}))
        conf_min = cam.get("confidence_min", global_ia.get("confidence_min", 0.65))
        cooldown = cam.get("cooldown_min",   global_ia.get("cooldown_min",   5))
        max_jour = cam.get("alertes_max_jour", global_ia.get("alertes_max_jour", 20))
        cur.execute("""
            INSERT INTO camera_config
                (magasin_id,camera_name,active,gestes_json,confidence_min,cooldown_min,alertes_max_jour,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT(magasin_id,camera_name) DO UPDATE SET
                active=%s,gestes_json=%s,confidence_min=%s,cooldown_min=%s,alertes_max_jour=%s,updated_at=NOW()
        """, (client_id,cam_name,active,gestes_j,conf_min,cooldown,max_jour,
              active,gestes_j,conf_min,cooldown,max_jour))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok":True,"message":f"{len(cameras)} caméra(s) configurée(s)"})

@app.route("/api/mobile/reset-gestes", methods=["POST"])
@limiter.limit("10 per minute")
def api_mobile_reset_gestes():
    """Réinitialise la config gestes — active tout avec paramètres par défaut."""
    client_id = get_mobile_client_id()
    if not client_id: return jsonify({"error":"Authentification requise"}),401
    conn = get_db(); cur = conn.cursor()
    # Récupérer les noms de caméras depuis config_json
    cur.execute("SELECT config_json FROM clients WHERE id=%s", (client_id,))
    row = cur.fetchone()
    config_json_str = (row["config_json"] if row else "") or ""
    try:
        cfg = json.loads(config_json_str) if config_json_str.strip() else {}
        cam_list = cfg.get("cameras", [])
    except Exception:
        cam_list = []
    DEFAULT_GESTES = json.dumps({
        "mouvement_rapide": True, "posture_basse": True, "presence_longue": True,
        "sac_suspect": True, "consommation": True, "dissimulation": True, "vol_caisse": True,
    })
    for cam in cam_list:
        nom = cam.get("nom") or cam.get("name") or ""
        if not nom: continue
        cur.execute("""
            INSERT INTO camera_config
                (magasin_id,camera_name,active,gestes_json,confidence_min,cooldown_min,alertes_max_jour,updated_at)
            VALUES (%s,%s,TRUE,%s,0.45,1,50,NOW())
            ON CONFLICT(magasin_id,camera_name) DO UPDATE SET
                active=TRUE,gestes_json=%s,confidence_min=0.45,cooldown_min=1,alertes_max_jour=50,updated_at=NOW()
        """, (client_id, nom, DEFAULT_GESTES, DEFAULT_GESTES))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok":True,"message":f"Config gestes réinitialisée ({len(cam_list)} caméra(s)) — tous gestes activés, cooldown 1 min"})

def envoyer_push_notifications(client_id, titre, corps, data_extra=None):
    """Envoie une notification push Expo à tous les tokens du client."""
    try:
        import urllib.request, json as _json
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT token FROM push_tokens WHERE client_id=%s", (client_id,))
        tokens = [r["token"] for r in cur.fetchall()]
        cur.close(); conn.close()
        if not tokens: return
        messages = [{"to":t,"title":titre,"body":corps,"sound":"default",
                     "data":data_extra or {}} for t in tokens]
        payload = _json.dumps(messages).encode()
        req = urllib.request.Request(
            "https://exp.host/--/api/v2/push/send",
            data=payload,
            headers={"Content-Type":"application/json","Accept":"application/json"}
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[PUSH] Erreur envoi notifications: {e}")

# =================================================================
# AGENTS PC — Supervision des gardiens clients
# =================================================================

def _agent_client_id(license_key):
    """Retourne le client_id depuis la license_key."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id FROM clients WHERE license_key=%s", (license_key,))
    row = cur.fetchone(); cur.close(); conn.close()
    return row["id"] if row else None

@app.route("/api/agent/status", methods=["POST"])
def api_agent_status():
    """Reçoit le heartbeat de statut d'un Gardien PC."""
    data = request.get_json(silent=True) or {}
    lk   = data.get("license_key","")
    if not lk:
        return jsonify({"ok": False, "error": "license_key manquante"}), 400
    client_id = _agent_client_id(lk)
    conn = get_db(); cur = conn.cursor()
    hostname = data.get("hostname","")
    # Vrai UPSERT : 1 seule ligne par (license_key, hostname)
    cur.execute("""
        INSERT INTO agents_pc
            (client_id, license_key, hostname, agent_version,
             surveillance_active, cameras_ok, cameras_total,
             disk_libre_gb, backoffice_ms, reseau_ok, last_seen, statut)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),'online')
        ON CONFLICT (license_key, hostname) DO UPDATE SET
            client_id           = EXCLUDED.client_id,
            surveillance_active = EXCLUDED.surveillance_active,
            cameras_ok          = EXCLUDED.cameras_ok,
            cameras_total       = EXCLUDED.cameras_total,
            disk_libre_gb       = EXCLUDED.disk_libre_gb,
            backoffice_ms       = EXCLUDED.backoffice_ms,
            reseau_ok           = EXCLUDED.reseau_ok,
            last_seen           = NOW(),
            statut              = 'online',
            agent_version       = EXCLUDED.agent_version
    """, (client_id, lk, hostname,
          data.get("agent_version","1.0"),
          data.get("surveillance_active", False),
          data.get("cameras_ok", 0),
          data.get("cameras_total", 0),
          data.get("disk_libre_gb"),
          data.get("backoffice_ms"),
          data.get("reseau_ok", True)))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/agent/incident", methods=["POST"])
def api_agent_incident():
    """Reçoit un incident signalé par un Gardien PC."""
    data = request.get_json(silent=True) or {}
    lk   = data.get("license_key","")
    if not lk:
        return jsonify({"ok": False, "error": "license_key manquante"}), 400
    client_id = _agent_client_id(lk)
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO agents_incidents
            (client_id, license_key, hostname, description, diagnostic,
             fix_applique, priorite)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
    """, (client_id, lk,
          data.get("hostname",""),
          data.get("description",""),
          data.get("diagnostic",""),
          data.get("fix_applique",""),
          data.get("priorite","moyenne")))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/agent/commandes", methods=["GET"])
def api_agent_commandes():
    """Retourne les commandes en attente pour un Gardien PC."""
    lk       = request.args.get("license_key","")
    hostname = request.args.get("hostname","")
    if not lk:
        return jsonify([])
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT id, action, parametres FROM agents_commandes
        WHERE license_key=%s AND statut='en_attente'
        ORDER BY created_at ASC
    """, (lk,))
    cmds = cur.fetchall()
    # Marquer comme "envoyees"
    if cmds:
        ids = [c["id"] for c in cmds]
        cur.execute("UPDATE agents_commandes SET statut='envoyee' WHERE id=ANY(%s)", (ids,))
        conn.commit()
    cur.close(); conn.close()
    result = []
    for c in cmds:
        try:
            params = json.loads(c["parametres"] or "{}")
        except Exception:
            params = {}
        params["action"] = c["action"]
        params["_cmd_id"] = c["id"]
        result.append(params)
    return jsonify(result)

@app.route("/api/agent/commande/envoyer", methods=["POST"])
@login_required
def api_agent_envoyer_commande():
    """Envoie une commande à un Gardien PC (depuis le backoffice admin)."""
    lk     = request.form.get("license_key","")
    action = request.form.get("action","")
    params = request.form.get("parametres","{}")
    if not lk or not action:
        return jsonify({"ok": False, "error": "license_key et action requis"}), 400
    client_id = _agent_client_id(lk)
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO agents_commandes (client_id, license_key, action, parametres)
        VALUES (%s,%s,%s,%s)
    """, (client_id, lk, action, params))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True, "message": f"Commande '{action}' mise en file pour {lk[:8]}..."})

@app.route("/api/agent/deployer_tous", methods=["POST"])
@login_required
def api_deployer_tous():
    """Envoie 'mettre_a_jour' à tous les agents actifs (EN LIGNE)."""
    conn = get_db(); cur = conn.cursor()
    # Récupérer tous les agents en ligne (vu < 10 min)
    cur.execute("""
        SELECT a.license_key, c.nom_magasin FROM agents_status a
        JOIN clients c ON c.id = a.client_id
        WHERE a.last_seen > NOW() - INTERVAL '10 minutes'
    """)
    agents = cur.fetchall()
    nb = 0
    for a in agents:
        cur.execute("""
            INSERT INTO agents_commandes (client_id, license_key, action, parametres)
            SELECT c.id, %s, 'mettre_a_jour', '{}'
            FROM clients c WHERE c.license_key = %s
        """, (a["license_key"], a["license_key"]))
        nb += 1
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True, "nb": nb, "message": f"Mise à jour lancée sur {nb} agent(s) en ligne"})

@app.route("/agents")
@login_required
def supervision_agents():
    """Page de supervision de tous les Gardiens PC."""
    conn = get_db(); cur = conn.cursor()
    # Agents avec infos client
    cur.execute("""
        SELECT a.*, c.nom_magasin,
               EXTRACT(EPOCH FROM (NOW()-a.last_seen))/60 AS minutes_inactif
        FROM agents_pc a
        LEFT JOIN clients c ON c.id = a.client_id
        ORDER BY a.last_seen DESC
    """)
    agents = [dict(r) for r in cur.fetchall()]
    # Incidents non lus
    cur.execute("""
        SELECT i.*, c.nom_magasin FROM agents_incidents i
        LEFT JOIN clients c ON c.id=i.client_id
        WHERE i.lu=FALSE ORDER BY i.created_at DESC LIMIT 50
    """)
    incidents = [dict(r) for r in cur.fetchall()]
    # Marquer comme lus
    cur.execute("UPDATE agents_incidents SET lu=TRUE WHERE lu=FALSE")
    cur.execute("SELECT * FROM clients ORDER BY nom_magasin")
    clients_list = [dict(c) for c in cur.fetchall()]
    conn.commit(); cur.close(); conn.close()

    # Calculer statut visuel
    for a in agents:
        mins = float(a.get("minutes_inactif") or 999)
        a["statut_visuel"] = "online" if mins < 10 else ("warn" if mins < 60 else "offline")
        a["last_seen_str"] = str(a.get("last_seen",""))[:16]

    for i in incidents:
        i["created_at_str"] = str(i.get("created_at",""))[:16]

    return render_template("agents.html",
                           agents=agents, incidents=incidents,
                           clients_list=clients_list,
                           nb_incidents=len(incidents))


# =================================================================
# API PC STATUS
# =================================================================
@app.route("/api/pc/heartbeat", methods=["POST"])
def pc_heartbeat():
    """Reçoit un ping de démarrage du PC surveillance — enregistre le statut et envoie une push notif."""
    data = request.json or {}
    lk = data.get("license_key", "")
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM clients WHERE license_key=%s", (lk,))
    client = cur.fetchone()
    if not client:
        cur.close(); conn.close()
        return jsonify({"error": "licence inconnue"}), 403
    now = datetime.now().isoformat()
    cur.execute("""
        INSERT INTO pc_status (license_key, last_seen, ip, version, nb_cameras)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT(license_key) DO UPDATE SET
            last_seen=EXCLUDED.last_seen, ip=EXCLUDED.ip,
            version=EXCLUDED.version, nb_cameras=EXCLUDED.nb_cameras
    """, (lk, now, data.get("ip",""), data.get("version",""), data.get("nb_cameras",0)))
    conn.commit()
    try:
        import threading
        nb_cam = data.get("nb_cameras", 0)
        threading.Thread(
            target=envoyer_push_notifications,
            args=(client["id"], "PC RadarIA connecte",
                  f"Surveillance demarree — {nb_cam} camera(s) active(s)"),
            daemon=True
        ).start()
    except Exception:
        pass
    cur.execute("SELECT statut FROM clients WHERE license_key=%s", (lk,))
    statut_row = cur.fetchone()
    cur.close(); conn.close()
    return jsonify({"ok": True, "statut_licence": statut_row["statut"] if statut_row else "inconnu"})


@app.route("/api/pc/statut")
def pc_statut():
    """Retourne le statut de la licence pour un PC donné."""
    lk = request.args.get("license_key", "")
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT statut FROM clients WHERE license_key=%s", (lk,))
    client = cur.fetchone()
    cur.close(); conn.close()
    if not client:
        return jsonify({"statut": "inconnu"}), 403
    return jsonify({"statut": client["statut"]})

# =================================================================
# AUTO-CALIBRATION IA
# =================================================================
@app.route("/api/calibration/<license_key>")
def api_calibration(license_key):
    """Retourne les statuts IA par geste (actif/prudent/silence) bases sur feedback 30j."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id FROM clients WHERE license_key=%s", (license_key,))
    client = cur.fetchone()
    if not client:
        cur.close(); conn.close()
        return jsonify({"error": "licence inconnue"}), 403
    client_id = client["id"]
    from datetime import timedelta
    date_30j = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    cur.execute("""
        SELECT type,
               COUNT(*) as total,
               SUM(CASE WHEN feedback='confirme'     THEN 1 ELSE 0 END) as confirmes,
               SUM(CASE WHEN feedback='faux_positif' THEN 1 ELSE 0 END) as faux_positifs
        FROM alertes_centrales
        WHERE client_id=%s AND date >= %s
        GROUP BY type
    """, (client_id, date_30j))
    rows = cur.fetchall()
    cur.close(); conn.close()
    statuts = {}
    details = []
    for row in rows:
        r = dict(row)
        geste = r["type"]
        total_eval = (r["confirmes"] or 0) + (r["faux_positifs"] or 0)
        if total_eval < 5:
            statut = "actif"
        else:
            faux_rate = (r["faux_positifs"] or 0) / total_eval
            if faux_rate >= 0.70:
                statut = "silence"
            elif faux_rate >= 0.45:
                statut = "prudent"
            else:
                statut = "actif"
        statuts[geste] = statut
        details.append({
            "type": geste,
            "total": r["total"],
            "confirmes": r["confirmes"] or 0,
            "faux_positifs": r["faux_positifs"] or 0,
            "total_evalues": total_eval,
            "taux_faux": round((r["faux_positifs"] or 0) / total_eval * 100) if total_eval > 0 else 0,
            "statut": statut,
        })
    return jsonify({
        "client_id": client_id,
        "periode": "30j",
        "statuts": statuts,
        "details": details,
        "generated_at": datetime.now().isoformat(),
    })

# =================================================================
# UPDATE PC MAGASIN — /api/update/surveillance/<license_key>
# =================================================================
@app.route("/api/update/surveillance/<license_key>")
def api_update_surveillance(license_key):
    """Sert la derniere version de surveillance.py aux PCs magasins authorises."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id FROM clients WHERE license_key=%s", (license_key,))
    client = cur.fetchone()
    cur.close(); conn.close()
    if not client:
        return jsonify({"error": "licence inconnue"}), 403
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    surv_path = os.path.join(static_dir, "surveillance.py")
    if not os.path.exists(surv_path):
        return jsonify({"error": "pas de mise a jour disponible"}), 404
    return send_from_directory(static_dir, "surveillance.py",
                               as_attachment=True,
                               download_name="surveillance.py",
                               mimetype="text/x-python")

# =================================================================
# AGENT CHAT — Dialogue backoffice ↔ Gardien PC
# =================================================================

@app.route("/api/agent/chat/send", methods=["POST"])
@login_required
def api_agent_chat_send():
    """Backoffice → Agent : envoyer un message ou instruction."""
    data = request.form if request.form else (request.json or {})
    lk  = (data.get("license_key") or "").strip()
    msg = (data.get("message") or "").strip()
    if not lk or not msg:
        return jsonify({"ok": False, "error": "license_key et message requis"}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO agent_chat (license_key, role, message, type, lu)
        VALUES (%s, 'user', %s, 'message', FALSE) RETURNING id
    """, (lk, msg))
    row = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True, "id": row["id"]})


@app.route("/api/agent/chat/messages", methods=["GET"])
def api_agent_chat_messages():
    """Agent → Backoffice : récupérer les messages non lus de l'utilisateur."""
    lk = request.args.get("license_key", "")
    if not lk:
        return jsonify([])
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT id, message, type, created_at FROM agent_chat
        WHERE license_key=%s AND role='user' AND lu=FALSE
        ORDER BY created_at
    """, (lk,))
    msgs = [dict(r) for r in cur.fetchall()]
    ids  = [m["id"] for m in msgs]
    if ids:
        cur.execute("UPDATE agent_chat SET lu=TRUE WHERE id=ANY(%s)", (ids,))
        conn.commit()
    cur.close(); conn.close()
    for m in msgs:
        m["created_at"] = str(m["created_at"])[:16]
    return jsonify(msgs)


@app.route("/api/agent/chat/respond", methods=["POST"])
def api_agent_chat_respond():
    """Agent → Backoffice : envoyer une réponse ou demande d'approbation."""
    data = request.json or {}
    lk              = data.get("license_key", "")
    msg             = data.get("message", "")
    msg_type        = data.get("type", "message")
    approbation     = bool(data.get("approbation_requise", False))
    repair_id       = data.get("repair_id")
    if not lk or not msg:
        return jsonify({"ok": False}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO agent_chat (license_key, role, message, type, approbation_requise, repair_id)
        VALUES (%s, 'agent', %s, %s, %s, %s) RETURNING id
    """, (lk, msg, msg_type, approbation, repair_id))
    row = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True, "id": row["id"]})


@app.route("/api/agent/chat/approve/<int:msg_id>", methods=["POST"])
@login_required
def api_agent_chat_approve(msg_id):
    """Backoffice → Agent : approuver ou refuser une réparation."""
    data     = request.json or {}
    decision = data.get("decision", "approve")
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        UPDATE agent_chat SET approuve=%s WHERE id=%s RETURNING id
    """, (decision == "approve", msg_id))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return jsonify({"ok": False, "error": "message non trouvé"}), 404
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})


@app.route("/api/agent/chat/approval_status", methods=["GET"])
def api_agent_chat_approval_status():
    """Agent → Backoffice : vérifier les approbations reçues."""
    lk = request.args.get("license_key", "")
    if not lk:
        return jsonify([])
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT id, approuve, repair_id, type FROM agent_chat
        WHERE license_key=%s AND role='agent' AND approbation_requise=TRUE
              AND approuve IS NOT NULL
        ORDER BY created_at DESC LIMIT 20
    """, (lk,))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(rows)


@app.route("/api/agent/chat/history", methods=["GET"])
@login_required
def api_agent_chat_history():
    """Backoffice UI : historique du chat pour un agent."""
    lk    = request.args.get("license_key", "")
    limit = min(int(request.args.get("limit", 60)), 200)
    if not lk:
        return jsonify([])
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT id, role, message, type, approbation_requise, approuve, repair_id, created_at
        FROM agent_chat WHERE license_key=%s
        ORDER BY created_at DESC LIMIT %s
    """, (lk, limit))
    msgs = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    for m in msgs:
        m["created_at"] = str(m["created_at"])[:16]
    return jsonify(list(reversed(msgs)))


# =================================================================
# MAIN
# =================================================================
init_db()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=False)
