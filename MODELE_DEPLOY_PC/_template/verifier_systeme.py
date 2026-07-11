#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RadarIA - Verification systeme nouveau PC
Client : Slidis Market
Detecte : reseau, cameras, backoffice, config
Genere  : rapport.html (dashboard visuel)
"""

import sys, os, socket, json, subprocess, platform, base64, urllib.request, time
from datetime import datetime
from pathlib import Path

BASE_DIR   = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
RAPPORT_HTML = BASE_DIR / "rapport.html"
BACKOFFICE_URL = "https://backoffice.radaria.fr"

# ─────────────────────────────────────────────
# DETECTION
# ─────────────────────────────────────────────

def get_ip_locale():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "Non detecte"

def get_ip_publique():
    for api in ["https://api.ipify.org", "https://ifconfig.me/ip"]:
        try:
            return urllib.request.urlopen(api, timeout=6).read().decode().strip()
        except:
            continue
    return "Non disponible"

def get_tailscale_ip():
    try:
        r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except:
        pass
    return "Non installe"

def get_wifi_info():
    try:
        r = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                           capture_output=True, text=True, timeout=5, encoding="cp850")
        for line in r.stdout.splitlines():
            if "SSID" in line and "BSSID" not in line:
                return line.split(":")[-1].strip()
    except:
        pass
    return "Ethernet / Non detecte"

def detecter_cameras():
    cameras = []
    print("   Detection cameras (indices 0-9)...")
    try:
        import cv2
        for idx in range(10):
            for backend in [cv2.CAP_DSHOW, cv2.CAP_ANY]:
                cap = cv2.VideoCapture(idx, backend)
                if cap.isOpened():
                    ret, frame = cap.read()
                    w   = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    h   = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    fps = int(cap.get(cv2.CAP_PROP_FPS)) or 30
                    snap_b64 = ""
                    if ret and frame is not None:
                        tmp = BASE_DIR / f"_snap_cam{idx}.jpg"
                        cv2.imwrite(str(tmp), frame)
                        snap_b64 = base64.b64encode(tmp.read_bytes()).decode()
                        tmp.unlink(missing_ok=True)
                    cameras.append({
                        "index": idx, "resolution": f"{w}x{h}",
                        "fps": fps, "ok": bool(ret), "snap": snap_b64
                    })
                    cap.release()
                    print(f"      Camera {idx} : {w}x{h} @ {fps}fps {'OK' if ret else 'ERREUR lecture'}")
                    break
    except ImportError:
        print("   cv2 non installe - veuillez relancer le BAT")
        cameras.append({"index": -1, "erreur": "opencv-python non installe"})
    except Exception as e:
        cameras.append({"index": -1, "erreur": str(e)})
    return cameras

def tester_backoffice(url):
    try:
        start = time.time()
        req   = urllib.request.urlopen(url + "/login", timeout=10)
        ms    = int((time.time() - start) * 1000)
        return {"ok": True, "code": req.getcode(), "ms": ms}
    except urllib.error.HTTPError as e:
        return {"ok": True, "code": e.code, "ms": 0}
    except Exception as e:
        return {"ok": False, "detail": str(e), "ms": -1}

def lire_config():
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    # Config par defaut Slidis Market
    return {
        "nom_magasin":    "Slidis Market",
        "backoffice_url": BACKOFFICE_URL,
        "license_key":    "",
        "username":       "",
    }

# ─────────────────────────────────────────────
# GENERATION HTML DASHBOARD
# ─────────────────────────────────────────────

def badge(ok, texte_ok, texte_ko):
    if ok:
        return f'<span class="badge ok">{texte_ok}</span>'
    return f'<span class="badge ko">{texte_ko}</span>'

def tester_nvr_rtsp(cfg):
    """Teste les flux RTSP du NVR si configuré. Retourne liste de caméras."""
    nvr_ip  = cfg.get("nvr_ip", "")
    nvr_usr = cfg.get("nvr_user", "admin")
    nvr_pwd = cfg.get("nvr_password", "")
    nvr_port = cfg.get("nvr_port", 554)
    nb      = cfg.get("nb_cameras", 4)
    if not nvr_ip:
        return None  # pas de NVR configuré

    print(f"   NVR detecte : {nvr_ip} — test des {nb} flux RTSP...")
    # Formats par ordre de priorité (Dahua d'abord, puis Hikvision, puis générique)
    formats = [
        "rtsp://{u}:{p}@{h}:{port}/cam/realmonitor?channel={ch}&subtype=1",
        "rtsp://{u}:{p}@{h}:{port}/cam/realmonitor?channel={ch}&subtype=0",
        "rtsp://{u}:{p}@{h}:{port}/Streaming/Channels/{ch0}01",
        "rtsp://{u}:{p}@{h}:{port}/ch{ch}/main",
    ]
    cameras = []
    try:
        import cv2
    except ImportError:
        return [{"index": -1, "erreur": "opencv-python non installe"}]

    format_ok = None
    for ch in range(1, nb + 1):
        connectee = False
        for fmt in formats:
            url = fmt.format(u=nvr_usr, p=nvr_pwd, h=nvr_ip,
                             port=nvr_port, ch=ch, ch0=ch)
            try:
                cap = cv2.VideoCapture(url)
                cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 4000)
                if cap.isOpened():
                    ret, frame = cap.read()
                    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    h_px = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    snap_b64 = ""
                    if ret and frame is not None:
                        tmp = BASE_DIR / f"_snap_nvr_ch{ch}.jpg"
                        cv2.imwrite(str(tmp), frame)
                        snap_b64 = base64.b64encode(tmp.read_bytes()).decode()
                        tmp.unlink(missing_ok=True)
                    nom = cfg.get("cameras", [{}])[ch-1].get("nom", f"Canal {ch}") if ch <= len(cfg.get("cameras", [])) else f"Canal {ch}"
                    cameras.append({"index": ch-1, "nom": nom, "resolution": f"{w}x{h_px}",
                                    "fps": 0, "ok": True, "snap": snap_b64, "rtsp": url})
                    print(f"      Canal {ch} [{nom}] : OK {w}x{h_px} (format: {fmt[:40]}...)")
                    if not format_ok:
                        format_ok = fmt
                    connectee = True
                    cap.release()
                    break
                cap.release()
            except Exception:
                pass
        if not connectee:
            nom = cfg.get("cameras", [{}])[ch-1].get("nom", f"Canal {ch}") if ch <= len(cfg.get("cameras", [])) else f"Canal {ch}"
            cameras.append({"index": ch-1, "nom": nom, "ok": False, "snap": "",
                             "resolution": "?", "fps": 0})
            print(f"      Canal {ch} [{nom}] : ECHEC connexion RTSP")
    return cameras

def generer_html(sys_info, reseau, cameras, backoffice, config):
    backoffice_url = config.get("backoffice_url", BACKOFFICE_URL)
    nb_cams_ok = len([c for c in cameras if c.get("ok")])
    tout_ok = (backoffice["ok"] and nb_cams_ok > 0)
    statut_global = ("✅ SYSTEME PRET" if tout_ok
                     else "⚠️ VERIFIER LES POINTS CI-DESSOUS")
    couleur_global = "#4ade80" if tout_ok else "#f59e0b"

    # Snapshots cameras
    cams_html = ""
    for c in cameras:
        if c.get("erreur"):
            cams_html += f'<div class="cam-card ko-card"><div class="cam-label">Erreur</div><div style="color:#f87171;font-size:12px">{c["erreur"]}</div></div>'
        else:
            snap = f'<img src="data:image/jpeg;base64,{c["snap"]}" style="width:100%;border-radius:6px;margin-top:8px">' if c.get("snap") else '<div style="background:#0a0a1a;height:80px;border-radius:6px;display:flex;align-items:center;justify-content:center;color:#333;margin-top:8px">Pas de preview</div>'
            statut_cam = '✅' if c.get("ok") else '❌'
            cams_html += f'''
<div class="cam-card {'ok-card' if c.get('ok') else 'ko-card'}">
  <div class="cam-label">{statut_cam} Caméra {c['index']}</div>
  <div style="color:#888;font-size:11px">{c.get('resolution','?')} · {c.get('fps','?')} fps</div>
  {snap}
</div>'''

    if not cams_html:
        cams_html = '<div style="color:#555;text-align:center;padding:20px">Aucune caméra détectée</div>'

    lkey = config.get("license_key","")
    lkey_display = lkey[:8]+"..." if len(lkey) > 8 else (lkey or '<span style="color:#f59e0b">Non configurée</span>')

    back_statut = f'✅ OK ({backoffice.get("ms",0)} ms)' if backoffice["ok"] else f'❌ {backoffice.get("detail","Erreur")}'
    back_class  = "ok-card" if backoffice["ok"] else "ko-card"

    wifi = reseau.get("wifi","?")
    date_rapport = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    surveillance_py = BASE_DIR.parent / "RadarIA_PC" / "surveillance.py"
    lancer_cmd = str(surveillance_py) if surveillance_py.exists() else ""

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="0">
<title>RadarIA — Setup {config.get('nom_magasin','')}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0 }}
  body {{ background:#050510; color:#e0e0e0; font-family:'Segoe UI',Arial,sans-serif; padding:24px }}
  h1 {{ font-size:22px; color:#7b8cde; margin-bottom:4px }}
  .subtitle {{ color:#555; font-size:13px; margin-bottom:28px }}
  .statut-global {{ background:#0d0d2b; border:2px solid {couleur_global}33;
    border-left:4px solid {couleur_global}; border-radius:12px;
    padding:16px 20px; margin-bottom:28px; display:flex; align-items:center; gap:12px }}
  .statut-global .titre {{ font-size:20px; font-weight:700; color:{couleur_global} }}
  .statut-global .sous {{ color:#888; font-size:13px; margin-top:2px }}
  .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:24px }}
  .card {{ background:#0a0a1a; border:1px solid #1a1a3a; border-radius:12px; padding:18px }}
  .card-title {{ color:#666; font-size:11px; text-transform:uppercase; letter-spacing:.06em; margin-bottom:12px }}
  table {{ width:100%; border-collapse:collapse }}
  td {{ padding:7px 0; border-bottom:1px solid #111; font-size:13px }}
  td:first-child {{ color:#666; width:140px }}
  td:last-child {{ color:#ddd; font-family:monospace }}
  .badge {{ font-size:11px; padding:3px 10px; border-radius:10px; font-weight:600 }}
  .badge.ok {{ background:#1e3a1e; color:#4ade80 }}
  .badge.ko {{ background:#3a1e1e; color:#f87171 }}
  .badge.warn {{ background:#3a3000; color:#fbbf24 }}
  .section-title {{ color:#666; font-size:11px; text-transform:uppercase; letter-spacing:.06em; margin-bottom:14px; margin-top:4px }}
  .ok-card {{ border-left:3px solid #4ade80 }}
  .ko-card {{ border-left:3px solid #f87171 }}
  .cam-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:12px }}
  .cam-card {{ background:#0a0a1a; border:1px solid #1a1a3a; border-radius:10px; padding:12px }}
  .cam-label {{ font-weight:600; font-size:13px; color:#e0e0e0 }}
  .btn-lancer {{ display:block; width:100%; background:linear-gradient(135deg,#4a90e2,#7b8cde);
    color:#fff; font-size:18px; font-weight:700; padding:18px; border:none; border-radius:12px;
    cursor:pointer; margin-top:24px; letter-spacing:.02em; transition:opacity .2s }}
  .btn-lancer:hover {{ opacity:.9 }}
  .btn-lancer:disabled {{ opacity:.4; cursor:not-allowed }}
  .btn-relancer {{ background:#1a1a3a; color:#7b8cde; font-size:13px; padding:8px 16px;
    border:1px solid #2a2a5a; border-radius:8px; cursor:pointer; margin-top:8px }}
  .footer {{ color:#333; font-size:11px; text-align:center; margin-top:28px }}
</style>
</head>
<body>
<h1>🛡️ RadarIA — Configuration PC</h1>
<div class="subtitle">Client : <strong style="color:#7b8cde">{config.get('nom_magasin','Slidis Market')}</strong> · Rapport généré le {date_rapport}</div>

<!-- Statut global -->
<div class="statut-global">
  <div>
    <div class="titre">{statut_global}</div>
    <div class="sous">{nb_cams_ok} caméra(s) détectée(s) · Backoffice {'accessible' if backoffice['ok'] else 'inaccessible'} · {reseau['ip_locale']}</div>
  </div>
</div>

<!-- Système + Réseau -->
<div class="grid">
  <div class="card">
    <div class="card-title">💻 Système</div>
    <table>
      <tr><td>Hostname</td><td>{sys_info['hostname']}</td></tr>
      <tr><td>OS</td><td>{sys_info['os']}</td></tr>
      <tr><td>Python</td><td>{sys_info['python']} {badge(sys_info['python_ok'],'OK','Vérifier version')}</td></tr>
      <tr><td>Utilisateur</td><td>{sys_info['user']}</td></tr>
    </table>
  </div>
  <div class="card">
    <div class="card-title">🌐 Réseau</div>
    <table>
      <tr><td>IP locale</td><td>{reseau['ip_locale']}</td></tr>
      <tr><td>IP publique</td><td>{reseau['ip_publique']}</td></tr>
      <tr><td>Tailscale</td><td>{reseau['ip_tailscale']}</td></tr>
      <tr><td>Wi-Fi / LAN</td><td>{wifi}</td></tr>
    </table>
  </div>
</div>

<!-- Backoffice + Config -->
<div class="grid">
  <div class="card {back_class}">
    <div class="card-title">☁️ Backoffice RadarIA</div>
    <table>
      <tr><td>URL</td><td style="font-size:11px">{backoffice_url}</td></tr>
      <tr><td>Statut</td><td>{back_statut}</td></tr>
    </table>
  </div>
  <div class="card {'ok-card' if lkey else 'ko-card'}">
    <div class="card-title">🔑 Configuration client</div>
    <table>
      <tr><td>Magasin</td><td>{config.get('nom_magasin','—')}</td></tr>
      <tr><td>License Key</td><td>{lkey_display}</td></tr>
      <tr><td>Username</td><td>{config.get('username','—') or '—'}</td></tr>
    </table>
    {'<div style="color:#f59e0b;font-size:12px;margin-top:10px">⚠️ Renseigner la license_key dans config.json</div>' if not lkey else ''}
  </div>
</div>

<!-- Cameras -->
<div class="card" style="margin-bottom:16px">
  <div class="card-title">📷 Caméras détectées ({nb_cams_ok} fonctionnelle(s))</div>
  <div class="cam-grid">{cams_html}</div>
</div>

<!-- Actions -->
<button class="btn-lancer" onclick="lancerSurveillance()" {'disabled' if not tout_ok else ''}>
  {'▶ LANCER LA SURVEILLANCE' if tout_ok else '⚠️ Résoudre les problèmes avant de lancer'}
</button>
<button class="btn-relancer" onclick="location.reload()">🔄 Relancer la vérification</button>

<div class="footer">RadarIA v1.0 · {date_rapport} · <a href="{backoffice_url}" target="_blank" style="color:#333">Backoffice</a></div>

<script>
function lancerSurveillance() {{
  if (!confirm("Lancer la surveillance RadarIA maintenant ?")) return;
  // Ouvrir un lien local au script Python via le protocole file (nécessite le BAT associé)
  window.open('lancer.bat', '_blank');
  document.querySelector('.btn-lancer').textContent = '✅ Surveillance lancée — vérifiez le terminal';
  document.querySelector('.btn-lancer').disabled = true;
}}
</script>
</body>
</html>"""
    return html

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "="*52)
    print("  RadarIA — Verification systeme nouveau PC")
    print("  Client : Slidis Market")
    print("="*52)

    print("\n[1/5] Systeme...")
    py_ok = sys.version_info >= (3, 8)
    sys_info = {
        "hostname":   socket.gethostname(),
        "os":         platform.system() + " " + platform.release(),
        "python":     sys.version.split()[0],
        "python_ok":  py_ok,
        "user":       os.environ.get("USERNAME", os.environ.get("USER", "?")),
    }
    print(f"   Hostname : {sys_info['hostname']}")
    print(f"   OS       : {sys_info['os']}")
    print(f"   Python   : {sys_info['python']} {'OK' if py_ok else 'TROP VIEUX (3.8+ requis)'}")

    print("\n[2/5] Reseau...")
    reseau = {
        "ip_locale":    get_ip_locale(),
        "ip_publique":  get_ip_publique(),
        "ip_tailscale": get_tailscale_ip(),
        "wifi":         get_wifi_info(),
    }
    for k, v in reseau.items():
        print(f"   {k:15s}: {v}")

    print("\n[3/5] Cameras...")
    config_tmp = lire_config()
    cameras_nvr = tester_nvr_rtsp(config_tmp)
    if cameras_nvr is not None:
        cameras = cameras_nvr
    else:
        cameras = detecter_cameras()
    nb_ok = len([c for c in cameras if c.get("ok")])
    print(f"   => {nb_ok} camera(s) fonctionnelle(s)")

    print("\n[4/5] Backoffice...")
    backoffice = tester_backoffice(BACKOFFICE_URL)
    print(f"   => {'OK' if backoffice['ok'] else 'ERREUR : ' + backoffice.get('detail','?')}")

    print("\n[5/5] Config...")
    config = lire_config()
    if not config.get("license_key"):
        print("   ATTENTION : license_key non configuree dans config.json")
    else:
        print(f"   Client : {config.get('nom_magasin','?')}")

    # Generer HTML
    print("\nGeneration du rapport HTML...")
    html = generer_html(sys_info, reseau, cameras, backoffice, config)
    RAPPORT_HTML.write_text(html, encoding="utf-8")
    print(f"   => {RAPPORT_HTML}")

    # Ouvrir dans le navigateur
    import webbrowser
    webbrowser.open(str(RAPPORT_HTML))
    print("\n[OK] Dashboard ouvert dans le navigateur.")
    print("  Verifiez les points en rouge puis lancez la surveillance.")
    print()
