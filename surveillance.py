#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import cv2, json, os, time, threading, logging
from collections import deque
from datetime import datetime
from pathlib import Path
from flask import Flask, Response, render_template, jsonify, send_file, request
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
SEUILS        = CFG["seuils"]
DELAI_ALERTE  = CFG.get("delai_entre_alertes_sec", 30)
DELAI_ALERTE_RAPIDE = CFG.get("delai_alertes_rapides_sec", 15)
PORT          = CFG.get("dashboard_port", 5000)

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
# Consommation : bottle(39), wine glass(40), cup(41), banana(46),
#               apple(47), sandwich(48), orange(49), pizza(53)
CONSO_CLASSES = {39: "bouteille", 40: "verre", 41: "tasse",
                 46: "banane", 47: "pomme", 48: "sandwich",
                 49: "orange", 53: "pizza"}
DUREE_SAC_SUSPECT    = SEUILS.get("duree_sac_suspect_sec", 20)  # secondes avec sac détecté avant alerte
CONSO_CONF           = 0.45 # seuil confiance détection objet consommation

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
        # ── APPRENTISSAGE PAR TYPE ──
        # Si l'IA a appris que ce type génère trop de faux positifs, on l'ignore
        statut = apprentissage.get("statuts_par_type", {}).get(type_alerte, "actif")
        if statut == "silence":
            return  # Type silencé automatiquement par l'IA
        # Délai adaptatif : si type "prudent", délai x3
        TYPES_RAPIDES = {"mouvement_rapide", "posture_basse", "sac_suspect", "consommation"}
        base_delai = DELAI_ALERTE_RAPIDE if type_alerte in TYPES_RAPIDES else DELAI_ALERTE
        delai = base_delai * 3 if statut == "prudent" else base_delai
        if maintenant - self.derniere_alerte.get(type_alerte, 0) < delai:
            return
        self.derniere_alerte[type_alerte] = maintenant
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
        # Enregistrer le clip vidéo en arrière-plan
        threading.Thread(
            target=self._sauvegarder_clip,
            args=(video_name, frame),
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

    def _sauvegarder_clip(self, video_name, trigger_frame):
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

            except ImportError:
                # Fallback mp4v si imageio-ffmpeg pas installe
                logger.warning("imageio-ffmpeg non disponible — fallback mp4v (non lisible dans navigateur)")
                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                out = cv2.VideoWriter(str(video_path), fourcc, FPS_BUFFER, (w, h))
                for frame in toutes_frames:
                    out.write(frame)
                out.release()

        except Exception as e:
            logger.error(f"Erreur clip vidéo: {e}")
        finally:
            self._enregistrement_actif = False

    def analyser(self, frame):
        try:
            res = model.track(frame, classes=[0], conf=self.seuil_conf, persist=True, verbose=False)
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
                    for t in ["presence_longue","mouvement_rapide","posture_basse",
                              "sac_suspect","consommation"]) else (0,255,0)
                cv2.rectangle(frame, (x1,y1), (x2,y2), couleur, 2)
                cv2.putText(frame, f"ID:{tid} {duree:.0f}s", (x1,y1-10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, couleur, 2)

            # ── DÉTECTION OBJETS SUSPECTS (sacs + consommation) ──
            # Un seul passage YOLO pour tous les objets en même temps
            toutes_classes = list(SAC_CLASSES.keys()) + list(CONSO_CLASSES.keys())
            try:
                res_obj = model(frame, classes=toutes_classes, conf=CONSO_CONF,
                                verbose=False)
                objs = res_obj[0].boxes if res_obj and res_obj[0].boxes else None
            except Exception:
                objs = None

            # Boîtes des personnes détectées pour test de proximité
            person_boxes = [(i, map(int, b))
                            for i, b in enumerate(boxes.xyxy.cpu().numpy())]

            if objs is not None and len(objs) > 0:
                for obj_box, obj_cls in zip(objs.xyxy.cpu().numpy(),
                                             objs.cls.cpu().numpy().astype(int)):
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
                            # L'objet doit être dans la moitié haute du corps (mains/bouche)
                            mi_corps = py1 + (py2-py1)//2
                            if ocy < mi_corps + 60:  # objet au niveau torse/tête
                                hist["conso_frames"] = hist.get("conso_frames", 0) + 1
                                if hist["conso_frames"] >= 8:  # ~0.8s consécutive
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
        return resp
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
    # Sync historique alertes depuis backoffice
    threading.Thread(target=sync_alertes_backoffice, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
