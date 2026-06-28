#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import cv2, json, os, time, threading, logging
from datetime import datetime
from pathlib import Path
from flask import Flask, Response, render_template, jsonify, send_file
from twilio.rest import Client as TwilioClient
from ultralytics import YOLO

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent

def charger_config():
    """Charge la config locale, puis la complète depuis le backoffice si license_key présente."""
    cfg_path = BASE_DIR / "config.json"
    cache_path = BASE_DIR / "config_cache.json"

    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    license_key = cfg.get("license_key", "")
    backoffice_url = cfg.get("backoffice_url", "")

    if license_key and backoffice_url:
        try:
            import urllib.request as _req
            payload = json.dumps({"license_key": license_key}).encode()
            req = _req.Request(
                f"{backoffice_url}/api/config",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with _req.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            if data.get("ok") and data.get("config"):
                remote = data["config"]
                if remote.get("cameras"):
                    cfg["cameras"] = remote["cameras"]
                if remote.get("seuils"):
                    cfg["seuils"] = remote["seuils"]
                if remote.get("delai_entre_alertes_sec"):
                    cfg["delai_entre_alertes_sec"] = remote["delai_entre_alertes_sec"]
                if remote.get("dashboard_port"):
                    cfg["dashboard_port"] = remote["dashboard_port"]
                # Cache pour mode hors ligne
                with open(cache_path, "w", encoding="utf-8") as fc:
                    json.dump(cfg, fc, ensure_ascii=False, indent=2)
                logger.info(f"Config backoffice OK — {len(cfg.get('cameras', []))} caméra(s)")
        except Exception as e:
            logger.warning(f"Backoffice injoignable ({e}) — utilisation du cache")
            if cache_path.exists():
                with open(cache_path, encoding="utf-8") as fc:
                    cached = json.load(fc)
                for key in ("cameras", "seuils", "delai_entre_alertes_sec", "dashboard_port"):
                    if cached.get(key):
                        cfg[key] = cached[key]
    return cfg

CFG = charger_config()

LICENSE_KEY    = CFG.get("license_key", "")
BACKOFFICE_URL = CFG.get("backoffice_url", "")
TWILIO_SID    = CFG.get("twilio", {}).get("account_sid", "")
TWILIO_TOKEN  = CFG.get("twilio", {}).get("auth_token", "")
TWILIO_FROM   = CFG.get("twilio", {}).get("from_whatsapp", "whatsapp:+14155238886")
DESTINATAIRES = CFG.get("destinataires_whatsapp", [])
SEUILS        = CFG["seuils"]
DELAI_ALERTE  = CFG.get("delai_entre_alertes_sec", 60)
PORT          = CFG.get("dashboard_port", 5000)

ALERTES_DIR = BASE_DIR / "alertes"
ALERTES_DIR.mkdir(exist_ok=True)

twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) if TWILIO_SID != "VOTRE_ACCOUNT_SID_TWILIO" else None
model = YOLO("yolov8n.pt")
app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
alertes_log = []
cameras = {}

class CameraDetecteur:
    def __init__(self, cam_cfg):
        self.id   = cam_cfg["id"]
        self.nom  = cam_cfg["nom"]
        self.url  = cam_cfg["url"]
        self.seuil_conf    = SEUILS["confiance_detection"]
        self.duree_suspect = SEUILS["duree_presence_suspecte_sec"]
        self.mvt_pixels    = SEUILS["mouvement_rapide_pixels"]
        self.mvt_frames    = SEUILS["frames_mouvement_rapide"]
        self.cap    = None
        self.frame  = None
        self.lock   = threading.Lock()
        self.actif  = False
        self.derniere_alerte = {}
        self.historique = {}

    def connecter(self):
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        self.cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return self.cap.isOpened()

    def envoyer_alerte(self, type_alerte, frame):
        maintenant = time.time()
        if maintenant - self.derniere_alerte.get(type_alerte, 0) < DELAI_ALERTE:
            return
        self.derniere_alerte[type_alerte] = maintenant
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        img = ALERTES_DIR / f"{self.id}_{type_alerte}_{ts}.jpg"
        cv2.imwrite(str(img), frame)
        alertes_log.insert(0, {"heure": datetime.now().strftime("%H:%M:%S"),
                                "camera": self.nom, "type": type_alerte, "image": img.name})
        if len(alertes_log) > 100:
            alertes_log.pop()
        # Envoyer au backoffice (lu par l'app mobile)
        if LICENSE_KEY and BACKOFFICE_URL:
            try:
                import urllib.request as _req
                payload = json.dumps({
                    "license_key": LICENSE_KEY,
                    "alert_id": f"{self.id}_{ts}",
                    "type": type_alerte,
                    "camera": self.nom,
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "heure": datetime.now().strftime("%H:%M:%S"),
                }).encode()
                req = _req.Request(f"{BACKOFFICE_URL}/api/alerte", data=payload,
                    headers={"Content-Type": "application/json"}, method="POST")
                _req.urlopen(req, timeout=5)
                logger.info(f"Alerte envoyee au backoffice: {type_alerte} ({self.nom})")
            except Exception as e:
                logger.warning(f"Backoffice alerte error: {e}")
        # WhatsApp (optionnel, si Twilio configure)
        if twilio_client and DESTINATAIRES:
            messages = {
                "presence_longue":  f"ALERTE MAGASIN\nCamera: {self.nom}\nPresence suspecte longue\n{datetime.now().strftime('%H:%M:%S')}",
                "mouvement_rapide": f"ALERTE MAGASIN\nCamera: {self.nom}\nMouvement rapide suspect\n{datetime.now().strftime('%H:%M:%S')}",
                "posture_basse":    f"ALERTE MAGASIN\nCamera: {self.nom}\nPosture suspecte detectee\n{datetime.now().strftime('%H:%M:%S')}",
            }
            msg = messages.get(type_alerte, f"ALERTE {self.nom}")
            for dest in DESTINATAIRES:
                try:
                    twilio_client.messages.create(from_=TWILIO_FROM, to=dest, body=msg)
                    logger.info(f"WhatsApp envoye -> {dest}")
                except Exception as e:
                    logger.error(f"Erreur WhatsApp: {e}")

    def analyser(self, frame):
        try:
            res = model.track(frame, classes=[0], conf=self.seuil_conf, persist=True, verbose=False)
            if not res or not res[0].boxes:
                return frame
            boxes = res[0].boxes
            ids = boxes.id.cpu().numpy().astype(int) if boxes.id is not None else []
            for i, box in enumerate(boxes.xyxy.cpu().numpy()):
                x1, y1, x2, y2 = map(int, box)
                tid = ids[i] if i < len(ids) else -1
                cx, cy = (x1+x2)//2, (y1+y2)//2
                if tid not in self.historique:
                    self.historique[tid] = {"debut": time.time(), "positions": [], "mvt_count": 0}
                hist = self.historique[tid]
                duree = time.time() - hist["debut"]
                if duree > self.duree_suspect:
                    self.envoyer_alerte("presence_longue", frame)
                if hist["positions"]:
                    px, py = hist["positions"][-1]
                    if abs(cx-px)+abs(cy-py) > self.mvt_pixels:
                        hist["mvt_count"] += 1
                    else:
                        hist["mvt_count"] = 0
                    if hist["mvt_count"] >= self.mvt_frames:
                        self.envoyer_alerte("mouvement_rapide", frame)
                if (x2-x1) > (y2-y1)*1.2 and (y2-y1) > 30:
                    self.envoyer_alerte("posture_basse", frame)
                hist["positions"].append((cx, cy))
                if len(hist["positions"]) > 30:
                    hist["positions"].pop(0)
                couleur = (0,0,255) if any(self.derniere_alerte.get(t,0) > time.time()-5
                    for t in ["presence_longue","mouvement_rapide","posture_basse"]) else (0,255,0)
                cv2.rectangle(frame, (x1,y1), (x2,y2), couleur, 2)
                cv2.putText(frame, f"ID:{tid} {duree:.0f}s", (x1,y1-10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, couleur, 2)
        except Exception as e:
            logger.error(f"Erreur analyse: {e}")
        return frame

    def boucle(self):
        while self.actif:
            if self.cap is None or not self.cap.isOpened():
                if not self.connecter():
                    time.sleep(5)
                    continue
            ok, frame = self.cap.read()
            if not ok:
                self.cap.release()
                self.cap = None
                continue
            frame = self.analyser(frame)
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            with self.lock:
                self.frame = buf.tobytes()

    def demarrer(self):
        self.actif = True
        threading.Thread(target=self.boucle, daemon=True).start()

    def flux_mjpeg(self):
        while True:
            with self.lock:
                f = self.frame
            if f:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + f + b"\r\n"
            time.sleep(0.033)

@app.route("/")
def index():
    return render_template("dashboard.html", cameras=list(cameras.values()))

@app.route("/flux/<cam_id>")
def flux(cam_id):
    cam = cameras.get(cam_id)
    if not cam:
        return "Camera introuvable", 404
    return Response(cam.flux_mjpeg(), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/api/alertes")
def api_alertes():
    return jsonify(alertes_log[:20])

@app.route("/alertes/<nom>")
def image_alerte(nom):
    p = ALERTES_DIR / nom
    return send_file(str(p)) if p.exists() else ("Introuvable", 404)

@app.route("/api/status")
def api_status():
    return jsonify([{"id": c.id, "nom": c.nom, "actif": c.actif} for c in cameras.values()])

def backoffice_register(ip):
    """Enregistre ce PC dans le backoffice au démarrage."""
    if not LICENSE_KEY or not BACKOFFICE_URL:
        return
    try:
        import urllib.request as _req, platform
        payload = json.dumps({
            "license_key": LICENSE_KEY,
            "hostname": socket.gethostname(),
            "ip_locale": ip,
            "os_info": platform.system() + " " + platform.release(),
            "nb_cameras": len(CFG.get("cameras", [])),
        }).encode()
        req = _req.Request(f"{BACKOFFICE_URL}/api/register", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        _req.urlopen(req, timeout=10)
        logger.info("PC enregistre dans le backoffice")
    except Exception as e:
        logger.warning(f"Erreur register backoffice: {e}")

def heartbeat_loop():
    """Envoie un ping au backoffice toutes les 60 secondes."""
    if not LICENSE_KEY or not BACKOFFICE_URL:
        return
    import urllib.request as _req
    while True:
        time.sleep(60)
        try:
            actives = sum(1 for c in cameras.values() if c.frame is not None)
            payload = json.dumps({
                "license_key": LICENSE_KEY,
                "cameras_actives": actives,
                "nb_cameras": len(cameras),
            }).encode()
            req = _req.Request(f"{BACKOFFICE_URL}/api/heartbeat", data=payload,
                headers={"Content-Type": "application/json"}, method="POST")
            _req.urlopen(req, timeout=5)
            logger.info(f"Heartbeat OK ({actives}/{len(cameras)} cameras actives)")
        except Exception as e:
            logger.warning(f"Heartbeat error: {e}")

if __name__ == "__main__":
    import socket
    ip = socket.gethostbyname(socket.gethostname())
    print(f"\n{'='*50}\n  SURVEILLANCE DEMARREE\n  Dashboard: http://{ip}:{PORT}\n{'='*50}\n")
    for c in CFG["cameras"]:
        cam = CameraDetecteur(c)
        cam.demarrer()
        cameras[cam.id] = cam
    # Enregistrement + heartbeat backoffice
    backoffice_register(ip)
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
