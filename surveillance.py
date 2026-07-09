#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import cv2, json, os, time, threading, logging, urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path
from flask import Flask, Response, render_template, render_template_string, jsonify, send_file, request
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
                if remote.get("camera_config"):
                    cfg["camera_config"] = remote["camera_config"]
                # Cache pour mode hors ligne
                with open(cache_path, "w", encoding="utf-8") as fc:
                    json.dump(cfg, fc, ensure_ascii=False, indent=2)
                logger.info(f"Config backoffice OK — {len(cfg.get('cameras', []))} caméra(s)")
        except Exception as e:
            logger.warning(f"Backoffice injoignable ({e}) — utilisation du cache")
            if cache_path.exists():
                with open(cache_path, encoding="utf-8") as fc:
                    cached = json.load(fc)
                for key in ("cameras", "seuils", "delai_entre_alertes_sec", "dashboard_port", "camera_config"):
                    if cached.get(key):
                        cfg[key] = cached[key]
    return cfg

CFG = charger_config()

LICENSE_KEY    = CFG.get("license_key", "")
BACKOFFICE_URL = CFG.get("backoffice_url", "")
SEUILS        = CFG["seuils"]
DELAI_ALERTE  = CFG.get("delai_entre_alertes_sec", 30)
DELAI_ALERTE_RAPIDE = CFG.get("delai_alertes_rapides_sec", 15)
PORT          = CFG.get("dashboard_port", 5000)
CAMERAS_CAISSE     = set(CFG.get("cameras_caisse", []))
DUREE_VOL_CAISSE   = CFG.get("duree_vol_caisse_sec", 12)

# ── Config gestes par caméra (mise à jour dynamique toutes les 5 min) ──
_gestes_lock = threading.Lock()
CAMERA_GESTES_CONFIG = dict(CFG.get("camera_config", {}))

def _rafraichir_config_gestes():
    """Recharge la config gestes depuis le backoffice toutes les 5 min."""
    while True:
        time.sleep(300)
        if not (LICENSE_KEY and BACKOFFICE_URL):
            continue
        try:
            import urllib.request as _req2
            payload = json.dumps({"license_key": LICENSE_KEY}).encode()
            req = _req2.Request(
                f"{BACKOFFICE_URL}/api/config",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with _req2.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            if data.get("ok") and data.get("config", {}).get("camera_config"):
                with _gestes_lock:
                    CAMERA_GESTES_CONFIG.clear()
                    CAMERA_GESTES_CONFIG.update(data["config"]["camera_config"])
                logger.info("Config gestes rafraîchie depuis le backoffice")
        except Exception as e:
            logger.warning(f"Rafraîchissement config gestes: {e}")

threading.Thread(target=_rafraichir_config_gestes, daemon=True).start()

ALERTES_DIR   = BASE_DIR / "alertes"
ALERTES_DIR.mkdir(exist_ok=True)
VISITEURS_DIR = BASE_DIR / "visiteurs"
VISITEURS_DIR.mkdir(exist_ok=True)
VIDEOS_DIR    = BASE_DIR / "videos"
VIDEOS_DIR.mkdir(exist_ok=True)
TRAINING_DIR  = BASE_DIR / "training"
TRAINING_VOL  = TRAINING_DIR / "vols_confirmes"
TRAINING_FAUX = TRAINING_DIR / "faux_positifs"
TRAINING_VOL.mkdir(parents=True, exist_ok=True)
TRAINING_FAUX.mkdir(parents=True, exist_ok=True)

BUFFER_SECONDES = 12   # secondes de vidéo AVANT l'alerte
POST_ALERTE_SEC = 6    # secondes APRÈS l'alerte
FPS_BUFFER      = 10   # fréquence d'échantillonnage du buffer

# ── Classes YOLO COCO pour détection d'objets suspects ──
# Sacs : backpack(24), handbag(26), suitcase(28)
SAC_CLASSES   = {24: "sac à dos", 26: "sac à main", 28: "valise"}
# Consommation : bottle(39), wine glass(40), cup(41), fork(42), knife(43), spoon(44),
#               bowl(45), banana(46), apple(47), sandwich(48), orange(49),
#               broccoli(50), carrot(51), hot dog(52), pizza(53), donut(54), cake(55)
CONSO_CLASSES = {39: "bouteille", 40: "verre", 41: "tasse", 42: "fourchette",
                 43: "couteau", 44: "cuillère", 45: "bol",
                 46: "banane", 47: "pomme", 48: "sandwich",
                 49: "orange", 50: "brocoli", 51: "carotte", 52: "hot-dog",
                 53: "pizza", 54: "donut", 55: "gâteau"}
DUREE_SAC_SUSPECT    = SEUILS.get("duree_sac_suspect_sec", 20)  # secondes avec sac détecté avant alerte
CONSO_CONF           = 0.25 # seuil confiance détection objets consommation
SAC_CONF             = 0.15 # seuil confiance sacs (plus bas = catch sacs plastique, cabas)

# ── Paramètres visiteurs ─────────────────────────────────────
FENETRE_RETOUR_MIN   = CFG.get("fenetre_retour_visiteur_min", 60)  # si même personne revient dans les X min → passage supplémentaire
DUREE_EMPLOYE_HEURES = CFG.get("duree_employe_heures", 4)          # resté > Xh = employé → exclu
ZONES_EMPLOYES       = CFG.get("zones_employes", [])               # zones employés [[x1,y1,x2,y2]] normalisé 0-1 ex: [[0.7,0,1.0,1.0]]

ALERTES_LOG_FILE    = BASE_DIR / "alertes_log.json"
VISITEURS_LOG_FILE  = BASE_DIR / "visiteurs_log.json"
VISITEURS_CTR_FILE  = BASE_DIR / "visiteurs_compteur.json"
APPRENTISSAGE_FILE  = BASE_DIR / "apprentissage.json"
INCIDENTS_LOG_FILE  = BASE_DIR / "incidents_confirmes.json"  # journal permanent des incidents confirmés

def _charger_json(path, default):
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default

def _sauvegarder_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Sauvegarde JSON échouée ({path}): {e}")

model = YOLO("yolov8n.pt")
app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.config['TEMPLATES_AUTO_RELOAD'] = True  # templates mis à jour sans redémarrer

# Chargement persistant au démarrage
alertes_log      = _charger_json(ALERTES_LOG_FILE, [])
visiteurs_log    = _charger_json(VISITEURS_LOG_FILE, [])
_ctr_data        = _charger_json(VISITEURS_CTR_FILE, {"compteur": 0})
visiteurs_actifs = {}   # track_id → {debut, camera, photo, numero}
cameras          = {}
_save_lock       = threading.Lock()
_ctr_lock        = threading.Lock()

apprentissage = _charger_json(APPRENTISSAGE_FILE, {
    "vol_confirmes": 0,
    "faux_positifs": 0,
    "stats_par_type": {},
    "seuil_confiance": SEUILS["confiance_detection"]
})

# Journal permanent de tous les incidents confirmés (append-only)
incidents_log = _charger_json(INCIDENTS_LOG_FILE, [])

def _apprentissage_feedback(alerte, feedback_val):
    """Met à jour les stats d'apprentissage, copie l'image, et enregistre l'incident confirmé."""
    import shutil
    typ = alerte.get("type", "inconnu")
    img_name = alerte.get("image", "")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ts_iso = datetime.now().isoformat()

    if feedback_val == "ok":
        apprentissage["vol_confirmes"] = apprentissage.get("vol_confirmes", 0) + 1
        if img_name:
            src = ALERTES_DIR / img_name
            dst = TRAINING_VOL / f"{typ}_{ts}_{img_name}"
            if src.exists():
                shutil.copy2(str(src), str(dst))
    elif feedback_val == "faux":
        apprentissage["faux_positifs"] = apprentissage.get("faux_positifs", 0) + 1
        if img_name:
            src = ALERTES_DIR / img_name
            dst = TRAINING_FAUX / f"{typ}_{ts}_{img_name}"
            if src.exists():
                shutil.copy2(str(src), str(dst))

    # Stats par type
    stats = apprentissage.setdefault("stats_par_type", {})
    t = stats.setdefault(typ, {"vol": 0, "faux": 0})
    if feedback_val == "ok":
        t["vol"] += 1
    elif feedback_val == "faux":
        t["faux"] += 1

    # Ajustement automatique du seuil de confiance
    total_v = apprentissage.get("vol_confirmes", 0)
    total_f = apprentissage.get("faux_positifs", 0)
    total = total_v + total_f
    if total >= 10:
        ratio_faux = total_f / total
        if ratio_faux > 0.6:
            nouveau = min(0.85, apprentissage["seuil_confiance"] + 0.02)
        elif ratio_faux < 0.2:
            nouveau = max(0.35, apprentissage["seuil_confiance"] - 0.01)
        else:
            nouveau = apprentissage["seuil_confiance"]
        if nouveau != apprentissage["seuil_confiance"]:
            apprentissage["seuil_confiance"] = round(nouveau, 3)
            for cam in cameras.values():
                cam.seuil_conf = nouveau
            logger.info(f"Seuil IA ajuste: {nouveau:.3f} (ratio faux={ratio_faux:.0%})")

    _sauvegarder_json(APPRENTISSAGE_FILE, apprentissage)

    # ── APPRENTISSAGE PAR TYPE (effet immédiat) ──
    # Après chaque feedback, recalculer le statut de ce type d'alerte
    stat_type = apprentissage["stats_par_type"].get(typ, {"vol": 0, "faux": 0})
    total_type = stat_type["vol"] + stat_type["faux"]
    if total_type >= 5:  # Minimum 5 feedbacks pour décider
        ratio_faux_type = stat_type["faux"] / total_type
        statuts = apprentissage.setdefault("statuts_par_type", {})
        ancien = statuts.get(typ, "actif")
        if ratio_faux_type >= 0.75:
            # ≥75% faux positifs → silencer ce type (plus d'alertes)
            statuts[typ] = "silence"
            if ancien != "silence":
                logger.warning(f"Type '{typ}' SILENCÉ par IA (ratio faux={ratio_faux_type:.0%} sur {total_type} feedbacks)")
        elif ratio_faux_type >= 0.50:
            # 50–75% faux positifs → mode prudent (délai x3)
            statuts[typ] = "prudent"
            if ancien != "prudent":
                logger.info(f"Type '{typ}' en mode PRUDENT (ratio faux={ratio_faux_type:.0%})")
        else:
            # <50% faux positifs → actif normal
            statuts[typ] = "actif"
            if ancien != "actif":
                logger.info(f"Type '{typ}' redevenu ACTIF (ratio faux={ratio_faux_type:.0%})")
        _sauvegarder_json(APPRENTISSAGE_FILE, apprentissage)

    # ── JOURNAL PERMANENT DES INCIDENTS CONFIRMÉS ──
    # Chaque feedback "ok" ou "faux" est enregistré dans incidents_confirmes.json.
    # Ce fichier ne se vide jamais — il constitue l'historique officiel.
    incident = {
        "id_incident": f"INC_{ts}",
        "alerte_id": alerte.get("id", ""),
        "type_incident": "VOL_CONFIRME" if feedback_val == "ok" else "FAUSSE_ALERTE",
        "type_detection": typ,
        "camera": alerte.get("camera", ""),
        "horodatage_alerte": alerte.get("horodatage", ""),
        "horodatage_confirmation": ts_iso,
        "image": img_name,
        "video": alerte.get("video", ""),
        "confiance_ia": alerte.get("confiance", 0),
    }
    incidents_log.insert(0, incident)
    with _save_lock:
        _sauvegarder_json(INCIDENTS_LOG_FILE, incidents_log)
    logger.info(f"Incident enregistre: {incident['type_incident']} — {typ} — {alerte.get('camera','')}")

def _prochain_numero():
    """Retourne le prochain numéro visiteur et l'incrémente."""
    with _ctr_lock:
        _ctr_data["compteur"] += 1
        n = _ctr_data["compteur"]
        _sauvegarder_json(VISITEURS_CTR_FILE, _ctr_data)
    return n

def sync_alertes_backoffice():
    """Charge l'historique complet depuis le backoffice au démarrage."""
    if not LICENSE_KEY or not BACKOFFICE_URL:
        return
    try:
        import urllib.request as _req
        url = f"{BACKOFFICE_URL}/api/mobile/alertes?license_key={LICENSE_KEY}&limit=500"
        req = _req.Request(url, headers={"User-Agent": "RadarIA/1.0"})
        with _req.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        if data.get("ok") and data.get("alertes"):
            remote = data["alertes"]
            ids_locaux = {a.get("id") for a in alertes_log}
            ajouts = 0
            for a in remote:
                rid = a.get("alert_id") or a.get("id", "")
                if rid and rid not in ids_locaux:
                    alertes_log.append({
                        "id": rid,
                        "date": a.get("date", ""),
                        "heure": a.get("heure", ""),
                        "camera": a.get("camera", ""),
                        "type": a.get("type", ""),
                        "image": a.get("image_path", ""),
                        "feedback": a.get("feedback", ""),
                    })
                    ajouts += 1
            if ajouts:
                alertes_log.sort(key=lambda x: (x.get("date",""), x.get("heure","")), reverse=True)
                _sauvegarder_json(ALERTES_LOG_FILE, alertes_log)
                logger.info(f"Sync backoffice: {ajouts} alerte(s) importée(s)")
    except Exception as e:
        logger.warning(f"Sync alertes backoffice: {e}")

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
        # Compteur alertes journalier (reset à minuit)
        self._alertes_today       = {}
        self._alertes_today_date  = datetime.now().strftime("%Y-%m-%d")
        # Buffer circulaire pour clips vidéo
        max_frames = BUFFER_SECONDES * FPS_BUFFER
        self.frame_buffer = deque(maxlen=max_frames)
        self.buffer_lock  = threading.Lock()
        self._enregistrement_actif = False
        self._last_buf_time = 0

    def _nvr_accessible(self):
        """Vérifie en 3s max si le NVR répond sur le port RTSP."""
        import socket, re
        m = re.search(r'@([\d.]+):(\d+)', self.url)
        if not m:
            return True  # URL inhabituelle, on tente quand même
        host, port = m.group(1), int(m.group(2))
        try:
            s = socket.create_connection((host, port), timeout=3)
            s.close()
            return True
        except Exception:
            return False

    def connecter(self):
        # Pré-test rapide (3s) avant d'ouvrir le flux RTSP (évite le blocage 30s)
        if not self._nvr_accessible():
            logger.warning(f"Camera {self.id} ({self.nom}) — NVR inaccessible, reconnexion dans 10s")
            return False
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"
        self.cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        ok = self.cap.isOpened()
        if not ok:
            logger.warning(f"Camera {self.id} ({self.nom}) — flux RTSP refusé, reconnexion dans 10s")
        else:
            logger.info(f"Camera {self.id} ({self.nom}) connectée ✓")
        return ok

    def envoyer_alerte(self, type_alerte, frame):
        maintenant = time.time()

        # ── CONFIG GESTES PAR CAMÉRA (depuis backoffice) ──
        with _gestes_lock:
            cam_cfg = dict(CAMERA_GESTES_CONFIG.get(self.nom, {}))

        # Caméra désactivée globalement
        if cam_cfg and not cam_cfg.get("active", True):
            logger.debug(f"[ALERTE BLOQUÉE] {self.nom} — caméra désactivée (active=False dans config backoffice)")
            return

        # Ce geste est-il activé pour cette caméra ?
        gestes = cam_cfg.get("gestes", {})
        if gestes and not gestes.get(type_alerte, True):
            logger.debug(f"[ALERTE BLOQUÉE] {self.nom}/{type_alerte} — geste désactivé dans config backoffice")
            return

        # Reset compteur journalier si nouveau jour
        today = datetime.now().strftime("%Y-%m-%d")
        if self._alertes_today_date != today:
            self._alertes_today      = {}
            self._alertes_today_date = today

        # Limite alertes par jour par type
        alertes_max = cam_cfg.get("alertes_max_jour", 20) if cam_cfg else 20
        if self._alertes_today.get(type_alerte, 0) >= alertes_max:
            return

        # ── APPRENTISSAGE PAR TYPE ──
        # Si l'IA a appris que ce type génère trop de faux positifs, on l'ignore
        statut = apprentissage.get("statuts_par_type", {}).get(type_alerte, "actif")
        if statut == "silence":
            return  # Type silencé automatiquement par l'IA

        # ── COOLDOWN (depuis config gestes ou valeur globale) ──
        cooldown_min = cam_cfg.get("cooldown_min") if cam_cfg else None
        if cooldown_min is not None:
            delai = cooldown_min * 60
        else:
            TYPES_RAPIDES = {"mouvement_rapide", "posture_basse", "sac_suspect", "consommation", "dissimulation", "vol_caisse"}
            base_delai = DELAI_ALERTE_RAPIDE if type_alerte in TYPES_RAPIDES else DELAI_ALERTE
            delai = base_delai * 3 if statut == "prudent" else base_delai

        if maintenant - self.derniere_alerte.get(type_alerte, 0) < delai:
            return
        self.derniere_alerte[type_alerte] = maintenant
        self._alertes_today[type_alerte]  = self._alertes_today.get(type_alerte, 0) + 1
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        img = ALERTES_DIR / f"{self.id}_{type_alerte}_{ts}.jpg"
        cv2.imwrite(str(img), frame)
        video_name = f"{self.id}_{type_alerte}_{ts}.mp4"
        entree = {
            "id": f"{self.id}_{ts}",
            "date": datetime.now().strftime("%Y-%m-%d"),
            "heure": datetime.now().strftime("%H:%M:%S"),
            "camera": self.nom,
            "type": type_alerte,
            "image": img.name,
            "video": video_name,
            "feedback": "",
        }
        alertes_log.insert(0, entree)
        if len(alertes_log) > 500:
            alertes_log.pop()
        with _save_lock:
            _sauvegarder_json(ALERTES_LOG_FILE, alertes_log)
        # Enregistrer le clip vidéo en arrière-plan (+ upload backoffice pour accès hors WiFi)
        _alert_id = f"{self.id}_{ts}"
        threading.Thread(
            target=self._sauvegarder_clip,
            args=(video_name, frame, _alert_id),
            daemon=True
        ).start()
        # Envoyer au backoffice (lu par l'app mobile)
        if LICENSE_KEY and BACKOFFICE_URL:
            try:
                import urllib.request as _req
                import socket as _sock
                _local_ip = _sock.gethostbyname(_sock.gethostname())
                payload = json.dumps({
                    "license_key": LICENSE_KEY,
                    "alert_id": f"{self.id}_{ts}",
                    "type": type_alerte,
                    "camera": self.nom,
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "heure": datetime.now().strftime("%H:%M:%S"),
                    "video_url": f"http://{_local_ip}:5000/videos/{video_name}",
                }).encode()
                req = _req.Request(f"{BACKOFFICE_URL}/api/alerte", data=payload,
                    headers={"Content-Type": "application/json"}, method="POST")
                _req.urlopen(req, timeout=5)
                logger.info(f"Alerte envoyee au backoffice: {type_alerte} ({self.nom})")
            except Exception as e:
                logger.warning(f"Backoffice alerte error: {e}")

    def _uploader_clip_backoffice(self, video_name, alert_id):
        """Upload le clip vidéo vers le backoffice pour le rendre accessible hors WiFi."""
        try:
            import base64, urllib.request as _req
            video_path = VIDEOS_DIR / video_name
            if not video_path.exists():
                logger.warning(f"Upload clip: fichier introuvable {video_name}")
                return
            size_mb = video_path.stat().st_size / (1024 * 1024)
            if size_mb > 50:
                logger.warning(f"Clip trop lourd ({size_mb:.1f} Mo), upload ignoré")
                return
            video_b64 = base64.b64encode(video_path.read_bytes()).decode()
            payload = json.dumps({
                "license_key": LICENSE_KEY,
                "alert_id": alert_id,
                "video_b64": video_b64,
            }).encode()
            req = _req.Request(
                f"{BACKOFFICE_URL}/api/video-upload",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            resp = _req.urlopen(req, timeout=60)
            result = json.loads(resp.read())
            if result.get("ok"):
                logger.info(f"Clip uploadé vers backoffice: {video_name} → {result.get('video_url')}")
            else:
                logger.warning(f"Upload clip backoffice refusé: {result}")
        except Exception as e:
            logger.warning(f"Upload clip backoffice error: {e}")

    def _sauvegarder_clip(self, video_name, trigger_frame, alert_id=None):
        """Sauvegarde un clip vidéo H264 compatible navigateurs (imageio-ffmpeg)."""
        if self._enregistrement_actif:
            return
        self._enregistrement_actif = True
        try:
            import numpy as np
            video_path = VIDEOS_DIR / video_name
            h, w = trigger_frame.shape[:2]

            # Collecter toutes les frames d'abord
            with self.buffer_lock:
                frames_avant = list(self.frame_buffer)

            # Frames post-alerte
            frames_apres = [trigger_frame]
            deadline = time.time() + POST_ALERTE_SEC
            while time.time() < deadline:
                with self.lock:
                    f = self.frame
                if f is not None:
                    arr = cv2.imdecode(np.frombuffer(f, np.uint8), cv2.IMREAD_COLOR)
                    if arr is not None:
                        frames_apres.append(arr)
                time.sleep(1.0 / FPS_BUFFER)

            toutes_frames = frames_avant + frames_apres

            try:
                # H264 via imageio-ffmpeg (binaire ffmpeg integre, pas de dependance systeme)
                import imageio
                writer = imageio.get_writer(
                    str(video_path),
                    fps=FPS_BUFFER,
                    codec="libx264",
                    quality=5,
                    output_params=["-pix_fmt", "yuv420p", "-movflags", "faststart"]
                )
                for frame in toutes_frames:
                    writer.append_data(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                writer.close()
                logger.info(f"Clip H264 sauvegarde: {video_name}")
                # Upload vers backoffice pour accès hors WiFi
                if alert_id and LICENSE_KEY and BACKOFFICE_URL:
                    self._uploader_clip_backoffice(video_name, alert_id)

            except ImportError:
                # Fallback mp4v si imageio-ffmpeg pas installe
                logger.warning("imageio-ffmpeg non disponible — fallback mp4v (non lisible dans navigateur)")
                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                out = cv2.VideoWriter(str(video_path), fourcc, FPS_BUFFER, (w, h))
                for frame in toutes_frames:
                    out.write(frame)
                out.release()
                # Upload vers backoffice pour accès hors WiFi
                if alert_id and LICENSE_KEY and BACKOFFICE_URL:
                    self._uploader_clip_backoffice(video_name, alert_id)

        except Exception as e:
            logger.error(f"Erreur clip vidéo: {e}")
        finally:
            self._enregistrement_actif = False

    def analyser(self, frame):
        try:
            with _gestes_lock:
                _ccfg = dict(CAMERA_GESTES_CONFIG.get(self.nom, {}))
            conf_min = _ccfg.get("confidence_min", self.seuil_conf) if _ccfg else self.seuil_conf
            res = model.track(frame, classes=[0], conf=conf_min, persist=True, verbose=False)
            if not res or not res[0].boxes:
                # Clore les visiteurs qui ont disparu
                self._clore_visiteurs_absents(set())
                return frame
            boxes = res[0].boxes
            ids = boxes.id.cpu().numpy().astype(int) if boxes.id is not None else []
            ids_vus = set()
            for i, box in enumerate(boxes.xyxy.cpu().numpy()):
                x1, y1, x2, y2 = map(int, box)
                tid = ids[i] if i < len(ids) else -1
                cx, cy = (x1+x2)//2, (y1+y2)//2
                ids_vus.add(tid)
                if tid not in self.historique:
                    self.historique[tid] = {
                        "debut": time.time(), "positions": [], "mvt_count": 0,
                        "sac_depuis": None,   # timestamp 1ère détection sac près de cette personne
                        "conso_frames": 0,    # frames consécutives avec objet consommable
                        "height_max": (y2-y1),# hauteur max observée (debout = référence)
                        "posture_frames": 0,  # frames consécutives de courbure détectée
                        "dissim_phase": 0,    # 0=idle 1=still_confirmé 2=burst
                        "dissim_still_t": None, # timestamp début immobilité
                        "dissim_burst_t": None, # timestamp début burst
                        "dissim_burst_n": 0,  # frames rapides pendant burst
                    }
                    # Nouveau visiteur — numéro séquentiel
                    num = _prochain_numero()
                    visiteurs_actifs[tid] = {
                        "debut": datetime.now().strftime("%H:%M:%S"),
                        "debut_ts": time.time(),
                        "camera": self.nom,
                        "photo": None,
                        "numero": num,
                    }
                hist = self.historique[tid]
                duree = time.time() - hist["debut"]
                # ── Détection zone employé ──────────────────────────────
                if visiteurs_actifs.get(tid) and ZONES_EMPLOYES:
                    va = visiteurs_actifs[tid]
                    va["frames_total"] = va.get("frames_total", 0) + 1
                    pcx_norm = cx / max(frame.shape[1], 1)
                    pcy_norm = cy / max(frame.shape[0], 1)
                    for zone in ZONES_EMPLOYES:
                        if (zone[0] <= pcx_norm <= zone[2] and zone[1] <= pcy_norm <= zone[3]):
                            va["frames_employe"] = va.get("frames_employe", 0) + 1
                            break

                # ── Photo visiteur : une seule par track ID ──────────────
                if visiteurs_actifs.get(tid) and visiteurs_actifs[tid]["photo"] is None:
                    num = visiteurs_actifs[tid].get("numero", 0)
                    photo_name = f"V{num:05d}_{self.id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
                    photo_path = VISITEURS_DIR / photo_name
                    roi = frame[max(0,y1-20):y2+20, max(0,x1-10):x2+10]
                    if roi.size > 0:
                        cv2.imwrite(str(photo_path), roi)
                        visiteurs_actifs[tid]["photo"] = photo_path.name
                if duree > self.duree_suspect:
                    self.envoyer_alerte("presence_longue", frame)
                # ── VOL CAISSE (présence prolongée à la caisse) ──
                if self.nom in CAMERAS_CAISSE and duree > DUREE_VOL_CAISSE:
                    self.envoyer_alerte("vol_caisse", frame)
                if hist["positions"]:
                    px, py = hist["positions"][-1]
                    if abs(cx-px)+abs(cy-py) > self.mvt_pixels:
                        hist["mvt_count"] += 1
                    else:
                        hist["mvt_count"] = 0
                    if hist["mvt_count"] >= self.mvt_frames:
                        self.envoyer_alerte("mouvement_rapide", frame)
                # ── DISSIMULATION (vol sans sac : immobile→geste rapide→immobile) ──
                _spd = 0
                if hist["positions"]:
                    _px, _py = hist["positions"][-1]
                    _spd = abs(cx - _px) + abs(cy - _py)
                _immobile = (_spd <= self.mvt_pixels)
                _rapide   = (_spd > self.mvt_pixels)
                _dp   = hist.get("dissim_phase", 0)
                _tnow = time.time()
                if _dp == 0:                              # Attente d'immobilité
                    if _immobile:
                        if hist.get("dissim_still_t") is None:
                            hist["dissim_still_t"] = _tnow
                        elif _tnow - hist["dissim_still_t"] >= 5.0:
                            hist["dissim_phase"] = 1      # immobilité ≥5s confirmée
                    else:
                        hist["dissim_still_t"] = None
                elif _dp == 1:                            # Immobilité OK, attendre burst
                    if _rapide:
                        hist["dissim_phase"]   = 2
                        hist["dissim_burst_t"] = _tnow
                        hist["dissim_burst_n"] = 1
                        hist["dissim_still_t"] = None
                    elif not _immobile:                   # mouvement modéré → reset
                        hist["dissim_phase"]   = 0
                        hist["dissim_still_t"] = None
                elif _dp == 2:                            # Burst — surveiller retour immobile
                    _be = _tnow - hist.get("dissim_burst_t", _tnow)
                    if _rapide:
                        hist["dissim_burst_n"] = hist.get("dissim_burst_n", 0) + 1
                    if _be > 4.0:                         # burst trop long = déplacement normal
                        hist["dissim_phase"]   = 0
                        hist["dissim_still_t"] = None
                        hist["dissim_burst_t"] = None
                    elif _immobile and hist.get("dissim_burst_n", 0) >= 2:
                        # Burst bref ≥2 frames + retour immobile = DISSIMULATION !
                        self.envoyer_alerte("dissimulation", frame)
                        hist["dissim_phase"]   = 0
                        hist["dissim_still_t"] = None
                        hist["dissim_burst_t"] = None
                # ── POSTURE BASSE (courbure pour ramasser/dissimuler) ──
                h_now = y2 - y1
                h_max = hist.get("height_max", h_now)
                # Warm-up : pas de mesure posture pendant les 2 premières secondes
                if duree >= 2.0:
                    if h_now > h_max:
                        hist["height_max"] = h_now      # mise à jour de la hauteur debout
                    elif h_max > 60 and h_now < h_max * 0.65:
                        # Personne 35%+ plus courte que son max = courbure nette
                        hist["posture_frames"] = hist.get("posture_frames", 0) + 1
                        if hist["posture_frames"] >= 3:
                            self.envoyer_alerte("posture_basse", frame)
                    else:
                        hist["posture_frames"] = max(0, hist.get("posture_frames", 0) - 1)
                else:
                    # Pendant warm-up, mettre à jour h_max seulement
                    if h_now > h_max:
                        hist["height_max"] = h_now
                hist["positions"].append((cx, cy))
                if len(hist["positions"]) > 30:
                    hist["positions"].pop(0)
                couleur = (0,0,255) if any(self.derniere_alerte.get(t,0) > time.time()-5
                    for t in ["presence_longue","mouvement_rapide","posture_basse",
                              "sac_suspect","consommation","dissimulation","vol_caisse"]) else (0,255,0)
                cv2.rectangle(frame, (x1,y1), (x2,y2), couleur, 2)
                cv2.putText(frame, f"ID:{tid} {duree:.0f}s", (x1,y1-10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, couleur, 2)

            # ── DÉTECTION OBJETS SUSPECTS (sacs + consommation) ──
            # Un seul passage YOLO pour tous les objets en même temps
            toutes_classes = list(SAC_CLASSES.keys()) + list(CONSO_CLASSES.keys())
            try:
                # conf = SAC_CONF (le plus bas) pour ne pas manquer les sacs
                # les objets conso seront filtrés à CONSO_CONF dans la boucle
                res_obj = model(frame, classes=toutes_classes, conf=SAC_CONF,
                                verbose=False)
                objs = res_obj[0].boxes if res_obj and res_obj[0].boxes else None
            except Exception:
                objs = None

            # Boîtes des personnes détectées pour test de proximité
            person_boxes = [(i, map(int, b))
                            for i, b in enumerate(boxes.xyxy.cpu().numpy())]

            if objs is not None and len(objs) > 0:
                for obj_box, obj_cls, obj_conf in zip(objs.xyxy.cpu().numpy(),
                                             objs.cls.cpu().numpy().astype(int),
                                             objs.conf.cpu().numpy()):
                    ox1, oy1, ox2, oy2 = map(int, obj_box)
                    ocx, ocy = (ox1+ox2)//2, (oy1+oy2)//2

                    # Chercher quelle personne est proche de cet objet
                    for i, pbox in enumerate(boxes.xyxy.cpu().numpy()):
                        px1,py1,px2,py2 = map(int, pbox)
                        tid = ids[i] if i < len(ids) else -1
                        if tid < 0 or tid not in self.historique:
                            continue
                        # Proximité : objet dans la zone élargie de la personne
                        zone_x1 = px1 - 80; zone_x2 = px2 + 80
                        zone_y1 = py1 - 40; zone_y2 = py2 + 40
                        proche = (zone_x1 < ocx < zone_x2 and
                                  zone_y1 < ocy < zone_y2)
                        if not proche:
                            continue

                        hist = self.historique[tid]

                        # Filtre conf par type : sacs à SAC_CONF, conso à CONSO_CONF
                        if obj_cls in CONSO_CLASSES and obj_conf < CONSO_CONF:
                            continue

                        if obj_cls in SAC_CLASSES:
                            # ── SAC SUSPECT ──
                            if hist["sac_depuis"] is None:
                                hist["sac_depuis"] = time.time()
                            elif time.time() - hist["sac_depuis"] > DUREE_SAC_SUSPECT:
                                self.envoyer_alerte("sac_suspect", frame)
                            # Dessiner le sac détecté
                            cv2.rectangle(frame, (ox1,oy1), (ox2,oy2), (0,165,255), 2)
                            cv2.putText(frame, SAC_CLASSES[obj_cls],
                                        (ox1, oy1-5), cv2.FONT_HERSHEY_SIMPLEX,
                                        0.45, (0,165,255), 1)

                        elif obj_cls in CONSO_CLASSES:
                            # ── CONSOMMATION ──
                            # L'objet doit être proche de la personne (étagère haute ou basse)
                            if ocy < py2 + 40:  # objet dans la zone corps élargie
                                hist["conso_frames"] = hist.get("conso_frames", 0) + 1
                                if hist["conso_frames"] >= 3:  # ~0.3s consécutive (était 8)
                                    self.envoyer_alerte("consommation", frame)
                                    hist["conso_frames"] = 0
                            else:
                                hist["conso_frames"] = max(0, hist.get("conso_frames",0)-1)
                            cv2.rectangle(frame, (ox1,oy1), (ox2,oy2), (255,0,255), 2)
                            cv2.putText(frame, CONSO_CLASSES[obj_cls],
                                        (ox1, oy1-5), cv2.FONT_HERSHEY_SIMPLEX,
                                        0.45, (255,0,255), 1)
                    # Si sac non vu près d'une personne → reset timer
                    if obj_cls in SAC_CLASSES:
                        for i, pbox in enumerate(boxes.xyxy.cpu().numpy()):
                            tid = ids[i] if i < len(ids) else -1
                            if tid in self.historique and not proche:
                                self.historique[tid]["sac_depuis"] = None

            self._clore_visiteurs_absents(ids_vus)
        except Exception as e:
            logger.error(f"Erreur analyse: {e}")
        return frame

    def _clore_visiteurs_absents(self, ids_vus):
        """Enregistre les visiteurs qui ne sont plus visibles — avec déduplication et exclusion employés."""
        partis = [tid for tid in list(visiteurs_actifs.keys()) if tid not in ids_vus]
        for tid in partis:
            v = visiteurs_actifs.pop(tid)
            duree_sec = int(time.time() - v["debut_ts"])
            num = v.get("numero", 0)
            now = datetime.now()
            now_str = now.strftime("%Y-%m-%d")
            heure_sortie = now.strftime("%H:%M:%S")

            # ── EXCLUSION EMPLOYÉS ───────────────────────────────────────
            # 1. Resté plus de X heures = employé
            if duree_sec > DUREE_EMPLOYE_HEURES * 3600:
                logger.info(f"[VISITEURS] ID:{tid} ignoré — employé (durée {duree_sec//3600}h)")
                if v.get("photo"):
                    try: (VISITEURS_DIR / v["photo"]).unlink(missing_ok=True)
                    except: pass
                continue
            # 2. Passé > 70% du temps dans zone employé
            frames_total = v.get("frames_total", 0)
            frames_emp   = v.get("frames_employe", 0)
            if frames_total > 0 and frames_emp / frames_total > 0.70:
                logger.info(f"[VISITEURS] ID:{tid} ignoré — zone employé ({frames_emp}/{frames_total} frames)")
                if v.get("photo"):
                    try: (VISITEURS_DIR / v["photo"]).unlink(missing_ok=True)
                    except: pass
                continue

            # ── DÉDUPLICATION : retour dans la fenêtre de X minutes ─────
            # Si une entrée récente existe pour cette caméra aujourd'hui
            # → c'est probablement la même personne qui est revenue
            fenetre_sec = FENETRE_RETOUR_MIN * 60
            entree_existante = None
            for e in visiteurs_log[:40]:
                if (e.get("date") == now_str
                        and e.get("camera") == v["camera"]
                        and e.get("categorie") not in ("employe",)
                        and e.get("heure_sortie")):
                    try:
                        ts_sortie = datetime.strptime(
                            f"{now_str} {e['heure_sortie']}", "%Y-%m-%d %H:%M:%S"
                        ).timestamp()
                        if time.time() - ts_sortie < fenetre_sec:
                            entree_existante = e
                            break
                    except Exception:
                        pass

            if entree_existante:
                # Même personne revenue → passage supplémentaire, pas de nouvelle entrée
                entree_existante["nb_passages"] = entree_existante.get("nb_passages", 1) + 1
                entree_existante["heure_sortie"]     = heure_sortie
                entree_existante["duree_totale_sec"] = (
                    entree_existante.get("duree_totale_sec", entree_existante.get("duree_sec", 0))
                    + duree_sec
                )
                logger.info(f"[VISITEURS] Passage #{entree_existante['nb_passages']} "
                            f"→ entrée {entree_existante['id']}")
            else:
                # Nouvelle entrée visiteur
                entree = {
                    "id": f"V{num:05d}_{now.strftime('%Y%m%d%H%M%S')}",
                    "numero": num,
                    "date": now_str,
                    "heure_entree": v["debut"],
                    "heure_sortie": heure_sortie,
                    "duree_sec": duree_sec,
                    "duree_totale_sec": duree_sec,
                    "nb_passages": 1,
                    "camera": v["camera"],
                    "photo": v.get("photo"),
                    "categorie": "inconnu",
                }
                visiteurs_log.insert(0, entree)
                if len(visiteurs_log) > 1000:
                    visiteurs_log.pop()
            with _save_lock:
                _sauvegarder_json(VISITEURS_LOG_FILE, visiteurs_log)

    def boucle(self):
        while self.actif:
            if self.cap is None or not self.cap.isOpened():
                if not self.connecter():
                    time.sleep(10)
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
            # Alimenter le buffer vidéo (echantillonnage à FPS_BUFFER)
            now = time.time()
            if now - self._last_buf_time >= 1.0 / FPS_BUFFER:
                self._last_buf_time = now
                with self.buffer_lock:
                    self.frame_buffer.append(frame.copy())

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
    q = request.args.get("q", "").lower()
    cam = request.args.get("camera", "")
    typ = request.args.get("type", "")
    date_f = request.args.get("date", "")
    result = alertes_log
    if q:
        result = [a for a in result if q in a.get("camera","").lower() or q in a.get("type","").lower()]
    if cam:
        result = [a for a in result if a.get("camera","") == cam]
    if typ:
        result = [a for a in result if a.get("type","") == typ]
    if date_f:
        result = [a for a in result if a.get("date","") == date_f]
    return jsonify(result[:200])

@app.route("/api/status")
def api_status():
    """Etat des cameras + compteurs pour diagnostic."""
    cams_status = []
    for cid, cam in cameras.items():
        ok = cam.frame is not None
        cams_status.append({
            "id": cid,
            "nom": cam.nom,
            "connectee": ok,
            "cap_ok": cam.cap is not None and cam.cap.isOpened() if cam.cap else False,
        })
    return jsonify({
        "ok": True,
        "cameras": cams_status,
        "nb_alertes": len(alertes_log),
        "nb_visiteurs": len(visiteurs_log),
        "seuil_ia": apprentissage.get("seuil_confiance", 0),
        "nb_vols_confirmes": apprentissage.get("vol_confirmes", 0),
        "nb_faux_positifs": apprentissage.get("faux_positifs", 0),
    })

@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    data = request.get_json(silent=True) or {}
    alert_id = data.get("id", "")
    fb = data.get("feedback", "")
    if not alert_id or not fb:
        return jsonify({"error": "id et feedback requis"}), 400
    for a in alertes_log:
        if a.get("id") == alert_id:
            a["feedback"] = fb
            with _save_lock:
                _sauvegarder_json(ALERTES_LOG_FILE, alertes_log)
            threading.Thread(target=_apprentissage_feedback, args=(dict(a), fb), daemon=True).start()
            logger.info(f"Feedback '{fb}' enregistre pour alerte {alert_id}")
            return jsonify({"ok": True})
    # Compat ancienne route
    logger.warning(f"Alerte introuvable pour feedback: {alert_id}")
    return jsonify({"error": "Alerte introuvable"}), 404

@app.route("/api/alerte/feedback/<alert_id>", methods=["POST"])
def api_feedback_compat(alert_id):
    data = request.get_json(silent=True) or {}
    fb = data.get("feedback", "")
    for a in alertes_log:
        if a.get("id") == alert_id:
            a["feedback"] = fb
            with _save_lock:
                _sauvegarder_json(ALERTES_LOG_FILE, alertes_log)
            threading.Thread(target=_apprentissage_feedback, args=(dict(a), fb), daemon=True).start()
            return jsonify({"ok": True})
    return jsonify({"error": "Alerte introuvable"}), 404

@app.route("/api/incidents")
def api_incidents():
    """Journal permanent des incidents confirmés (vols + faux positifs)."""
    limit = request.args.get("limit", 100, type=int)
    type_filtre = request.args.get("type", "")
    data = incidents_log
    if type_filtre in ("VOL_CONFIRME", "FAUSSE_ALERTE"):
        data = [i for i in data if i.get("type_incident") == type_filtre]
    return jsonify(data[:limit])

@app.route("/api/apprentissage")
def api_apprentissage():
    return jsonify(apprentissage)

@app.route("/api/types-statuts")
def api_types_statuts():
    """Statut IA de chaque type d'alerte (actif / prudent / silence)."""
    statuts = apprentissage.get("statuts_par_type", {})
    stats   = apprentissage.get("stats_par_type", {})
    TYPES_CONNUS = ["mouvement_rapide", "posture_basse", "presence_longue",
                    "sac_suspect", "consommation", "dissimulation", "objet_cache"]
    result = []
    for t in TYPES_CONNUS:
        s = stats.get(t, {"vol": 0, "faux": 0})
        total = s["vol"] + s["faux"]
        result.append({
            "type": t,
            "statut": statuts.get(t, "actif"),
            "vol": s["vol"],
            "faux": s["faux"],
            "total_feedbacks": total,
            "ratio_faux": round(s["faux"] / total, 2) if total else None,
        })
    return jsonify(result)

@app.route("/api/types-statuts/<type_alerte>", methods=["POST"])
def api_type_statut_set(type_alerte):
    """Permet de forcer manuellement le statut d'un type (actif/silence/prudent)."""
    data = request.get_json(silent=True) or {}
    statut = data.get("statut", "actif")
    if statut not in ("actif", "prudent", "silence"):
        return jsonify({"error": "statut invalide"}), 400
    apprentissage.setdefault("statuts_par_type", {})[type_alerte] = statut
    _sauvegarder_json(APPRENTISSAGE_FILE, apprentissage)
    logger.info(f"Statut type '{type_alerte}' force manuellement → {statut}")
    return jsonify({"ok": True, "type": type_alerte, "statut": statut})

@app.route("/alertes/<nom>")
def image_alerte(nom):
    p = ALERTES_DIR / nom
    return send_file(str(p)) if p.exists() else ("Introuvable", 404)

@app.route("/visiteurs/<nom>")
def image_visiteur(nom):
    p = VISITEURS_DIR / nom
    return send_file(str(p)) if p.exists() else ("Introuvable", 404)

@app.route("/videos/<nom>")
def video_alerte(nom):
    p = VIDEOS_DIR / nom
    if not p.exists():
        return "Introuvable", 404
    # Support Range requests pour la lecture vidéo dans le navigateur
    file_size = p.stat().st_size
    range_header = request.headers.get("Range", None)
    if range_header:
        byte_start, byte_end = 0, file_size - 1
        match = __import__("re").search(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            byte_start = int(match.group(1))
            if match.group(2):
                byte_end = int(match.group(2))
        length = byte_end - byte_start + 1
        with open(str(p), "rb") as f:
            f.seek(byte_start)
            data = f.read(length)
        resp = app.response_class(data, 206, mimetype="video/mp4", direct_passthrough=True)
        resp.headers["Content-Range"] = f"bytes {byte_start}-{byte_end}/{file_size}"
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Content-Length"] = str(length)
    resp = send_file(str(p), mimetype="video/mp4")
    resp.headers["Accept-Ranges"] = "bytes"
    return resp

@app.route("/api/visiteurs")
def api_visiteurs():
    date_f = request.args.get("date", "")
    result = visiteurs_log
    if date_f:
        result = [v for v in result if v.get("date","") == date_f]
    return jsonify(result[:200])

@app.route("/api/visiteurs/<visiteur_id>/categorie", methods=["POST"])
def api_visiteur_categorie(visiteur_id):
    data = request.get_json(silent=True) or {}
    cat = data.get("categorie", "inconnu")
    for v in visiteurs_log:
        if v.get("id") == visiteur_id:
            v["categorie"] = cat
            with _save_lock:
                _sauvegarder_json(VISITEURS_LOG_FILE, visiteurs_log)
            return jsonify({"ok": True})
    return jsonify({"error": "Visiteur introuvable"}), 404

@app.route("/api/visiteurs/actifs")
def api_visiteurs_actifs():
    return jsonify([
        {"id": tid, "camera": v["camera"], "debut": v["debut"]}
        for tid, v in visiteurs_actifs.items()
    ])

@app.route("/historique")
def historique():
    return render_template("historique.html",
        cameras=list({a["camera"] for a in alertes_log}),
        types=["presence_longue", "mouvement_rapide", "posture_basse"])

@app.route("/visiteurs")
def visiteurs_page():
    return render_template("visiteurs.html")

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

def snapshot_push_loop():
    """Envoie un snapshot JPEG de chaque caméra au backoffice toutes les 5 s."""
    if not LICENSE_KEY or not BACKOFFICE_URL:
        return
    import urllib.request as _req, base64
    import numpy as np
    while True:
        time.sleep(5)
        for cam in list(cameras.values()):
            try:
                with cam.lock:
                    f = cam.frame
                if not f:
                    continue
                arr = cv2.imdecode(np.frombuffer(f, np.uint8), cv2.IMREAD_COLOR)
                if arr is None:
                    continue
                # Redimensionner à 640px pour limiter la bande passante
                h, w = arr.shape[:2]
                if w > 640:
                    arr = cv2.resize(arr, (640, int(h * 640 / w)))
                _, buf = cv2.imencode('.jpg', arr, [cv2.IMWRITE_JPEG_QUALITY, 60])
                img_b64 = base64.b64encode(buf.tobytes()).decode()
                payload = json.dumps({
                    "license_key": LICENSE_KEY,
                    "camera": cam.nom,
                    "image_b64": img_b64,
                }).encode()
                req = _req.Request(
                    f"{BACKOFFICE_URL}/api/snapshot",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                _req.urlopen(req, timeout=4)
            except Exception:
                pass

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


# ─── KIOSK HTML ──────────────────────────────────────────────────────────────
KIOSQUE_HTML = """
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="60">
<title>RadarIA &#8212; Kiosque</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Segoe UI', Arial, sans-serif;
    height: 100vh; display: flex; flex-direction: column;
    justify-content: center; align-items: center;
    background: {% if statut == 'suspendu' %}#c0392b{% else %}#0a0a1a{% endif %};
    color: {% if statut == 'suspendu' %}#fff{% else %}#e0e0e0{% endif %};
    text-align: center;
  }
  .logo { font-size: 3em; font-weight: 900; letter-spacing: 0.05em; margin-bottom: 0.2em; }
  .logo span { color: #e74c3c; }
  .subtitle { font-size: 1.1em; opacity: 0.7; margin-bottom: 2em; }
  .status-badge {
    padding: 0.4em 1.2em; border-radius: 999px; font-size: 0.95em;
    font-weight: 600; letter-spacing: 0.08em; margin-bottom: 2.5em;
    background: {% if statut == 'suspendu' %}rgba(255,255,255,0.2){% else %}#1a7a1a{% endif %};
    color: #fff;
  }
  .cameras-info { font-size: 1.4em; margin-bottom: 0.5em; }
  .alertes-info { font-size: 1em; opacity: 0.6; }
  .warning-block {
    margin-top: 2em; padding: 1.5em 2em;
    background: rgba(255,255,255,0.15); border-radius: 12px;
    max-width: 500px;
  }
  .warning-block h2 { font-size: 1.6em; margin-bottom: 0.5em; }
  .warning-block p { opacity: 0.9; line-height: 1.6; }
  footer { position: fixed; bottom: 1em; font-size: 0.75em; opacity: 0.4; }
</style>
</head>
<body>
  <div class="logo">Radar<span>IA</span></div>
  <div class="subtitle">Surveillance intelligente &mdash; Slidis Market</div>

  {% if statut == 'suspendu' %}
  <div class="status-badge">&#9888;&#65039; LICENCE SUSPENDUE</div>
  <div class="warning-block">
    <h2>&#128683; Surveillance inactive</h2>
    <p>La licence de surveillance est suspendue.<br>
    Veuillez contacter votre administrateur RadarIA.</p>
  </div>
  {% else %}
  <div class="status-badge">&#128308; SURVEILLANCE ACTIVE</div>
  <div class="cameras-info">&#128737;&#65039; {{ nb_cameras }} / {{ total_cameras }} cam&eacute;ra(s) actives</div>
  <div class="alertes-info">{{ nb_alertes }} alerte(s) en m&eacute;moire</div>
  {% endif %}

  <footer>RadarIA v4.7 &mdash; Protection en cours</footer>
</body>
</html>
"""

@app.route("/kiosque")
def kiosque():
    statut = "actif"
    try:
        url = f"{BACKOFFICE_URL}/api/pc/statut?license_key={LICENSE_KEY}"
        with urllib.request.urlopen(url, timeout=4) as r:
            statut = json.loads(r.read().decode()).get("statut", "actif")
    except Exception:
        statut = "hors-ligne"
    nb_cam = len([c for c in cameras.values() if c.frame is not None])
    return render_template_string(KIOSQUE_HTML,
        statut=statut,
        nb_cameras=nb_cam,
        total_cameras=len(cameras),
        nb_alertes=len(alertes_log))


def _ping_backoffice_demarrage():
    """Notifie le backoffice que le PC est demarre (push notification client)."""
    if not LICENSE_KEY or not BACKOFFICE_URL:
        return
    try:
        import socket as _sock
        ip = _sock.gethostbyname(_sock.gethostname())
        payload = json.dumps({
            "license_key": LICENSE_KEY,
            "ip": ip,
            "version": "4.7",
            "nb_cameras": len(CFG.get("cameras", []))
        }).encode()
        req = urllib.request.Request(
            f"{BACKOFFICE_URL}/api/pc/heartbeat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=5)
        logger.info("[PING] Backoffice notifie -- PC connecte")
    except Exception as e:
        logger.warning(f"[PING] Echec ping backoffice: {e}")


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
    _ping_backoffice_demarrage()
    threading.Thread(target=heartbeat_loop,      daemon=True).start()
    threading.Thread(target=snapshot_push_loop,  daemon=True).start()
    # Sync historique alertes depuis backoffice
    threading.Thread(target=sync_alertes_backoffice, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
