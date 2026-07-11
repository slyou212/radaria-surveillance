# BRIEFING — Claude, nouveau PC de surveillance RadarIA
> Tu es Claude sur un nouveau PC client RadarIA.
> Lis entièrement ce briefing avant de faire quoi que ce soit.
> Ce document contient tout ce que tu dois savoir, y compris les pièges connus.

---

## 1. Contexte du système RadarIA

RadarIA est un système de surveillance antivol pour commerces. Il se compose de :

- **Ce PC** — tourne `surveillance.py`, détecte les vols via caméra(s), envoie les alertes
- **Backoffice web** — `https://backoffice.radaria.fr` — reçoit les alertes, gère les clients
- **App mobile** — les commerçants voient les alertes en temps réel

Le PC envoie des alertes (image + horodatage) au backoffice via HTTP avec la `license_key` du client.
Le backoffice envoie la config des caméras au démarrage via `/api/config` (si license_key valide).

---

## 2. Infos client — lire dans config.json

Le fichier `config.json` dans `RadarIA_PC\` contient toutes les infos du client :
- `nom_magasin`, `license_key`, `username`, `contact_email`, `contact_tel`
- `nvr_ip`, `nvr_user`, `nvr_password`, `nvr_port` — si caméras IP via NVR
- `cameras` — liste des caméras avec URL RTSP

**Toujours vérifier que config.json est un JSON valide** avant de commencer.

---

## 3. Structure des dossiers attendue

```
[Dossier racine]\
  NOUVEAU_PC\          <- outils de setup (tu es probablement ici)
    SETUP_RADARIA.bat
    LANCER_SURVEILLANCE.bat
    DEMARRAGE_AUTO.bat
    verifier_systeme.py
    config.json
    rapport.html           <- généré par verifier_systeme.py
    BRIEFING_CLAUDE.md     <- ce fichier
  RadarIA_PC\          <- programme de surveillance
    surveillance.py        <- code principal
    yolov8n.pt             <- modèle YOLO (détection personnes, ~6 MB)
    config.json            <- config surveillance (NE PAS ECRASER avec celui de NOUVEAU_PC)
    templates\
      dashboard.html
      historique.html
      visiteurs.html
```

---

## 4. Ta mission — étapes dans l'ordre

### Etape 0 — Audit rapide
Avant tout, fais un tour complet :
- `python --version` — Python installé ?
- Lire `RadarIA_PC\config.json` — valide ? license_key présente ?
- `surveillance.py` présent dans `RadarIA_PC\` ?
- `yolov8n.pt` présent dans `RadarIA_PC\` ?

### Etape 1 — Installer les dépendances
```
pip install opencv-python requests psutil flask ultralytics torch --quiet
```
Si erreur réseau/SSL :
```
pip install opencv-python requests psutil flask ultralytics torch --no-cache-dir --trusted-host pypi.org --trusted-host files.pythonhosted.org
```
ultralytics/torch peut prendre 5-10 minutes et télécharger plusieurs centaines de Mo. C'est normal.

### Etape 2 — Lancer la vérification système
```
python NOUVEAU_PC\verifier_systeme.py
```
Ouvre `rapport.html` — vérifie : réseau, NVR/caméras, backoffice.

### Etape 3 — Tester les caméras
**Si caméras USB** : le script les détecte automatiquement (indices 0-9).
**Si caméras IP via NVR** : voir section 5 "Types de NVR et formats RTSP".

### Etape 4 — Lancer la surveillance
```
cd RadarIA_PC
python surveillance.py
```
Vérifie dans les logs :
- `SURVEILLANCE DEMARREE`
- `PC enregistré dans le backoffice`
- `Sync backoffice : X alerte(s)`
- Chaque caméra : `connectee` ou `ERREUR`

### Etape 5 — Vérifier le dashboard local
Ouvrir `http://127.0.0.1:5000/` dans le navigateur — doit répondre HTTP 200.

### Etape 6 — Démarrage automatique au boot
Copier `DEMARRAGE_AUTO.bat` dans :
```
C:\Users\[utilisateur]\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\
```

**Connexion automatique Windows 11 + compte Microsoft :**
- Paramètres -> Comptes -> Options de connexion -> désactiver "Windows Hello uniquement"
- Supprimer le PIN
- Lancer `netplwiz` -> décocher "L'utilisateur doit entrer un nom et un mot de passe"
- Entrer le mot de passe du compte Microsoft quand demandé

---

## 5. Types de NVR et formats RTSP (CRITIQUE)

Les caméras IP passent par un NVR. Le format RTSP dépend de la marque.

### Dahua (le plus courant chez nos clients)
```
rtsp://admin:MotDePasse@192.168.1.x:554/cam/realmonitor?channel=1&subtype=1
```
- `channel` = numéro de canal (1 à N)
- `subtype=1` = sous-flux (performance) / `subtype=0` = flux principal (qualité)

### Hikvision
```
rtsp://admin:MotDePasse@192.168.1.x:554/Streaming/Channels/101
```
- `101` = canal 1 flux principal, `201` = canal 2, etc.

### Generique / autres marques
```
rtsp://admin:MotDePasse@192.168.1.x:554/ch1/main
```

**Pour tester un flux RTSP rapidement :**
```python
import cv2
cap = cv2.VideoCapture("rtsp://admin:MotDePasse@IP:554/cam/realmonitor?channel=1&subtype=1")
print(cap.isOpened())
ret, frame = cap.read()
print(ret, frame.shape if frame is not None else "aucune frame")
cap.release()
```

Si le format ne fonctionne pas, essaie dans l'ordre : Dahua -> Hikvision -> générique.
Note le format qui fonctionne et mets à jour `RadarIA_PC\config.json` avec les URLs RTSP correctes.

---

## 6. Bugs connus et pièges à éviter

Découverts lors de l'installation Slidis Market (29/06/2026) :

1. **config.json tronqué** — vérifie toujours qu'il est valide avec `json.load()` avant de commencer.

2. **`python -c` multiligne dans les .bat** — cmd.exe ne supporte pas les blocs multilignes. Si un .bat plante avec une erreur Python bizarre, condense le code en une seule ligne ou crée un fichier .py séparé.

3. **LANCER_SURVEILLANCE.bat ne doit PAS écraser RadarIA_PC\config.json** — ce fichier contient la liste des caméras RTSP. Si tu modifies un .bat, vérifie l'absence de `copy /Y` qui l'écraserait.

4. **Encodage console Windows** — évite les caractères Unicode (checkmarks, croix) dans les `print()` appelés depuis un .bat. Utilise `[OK]`, `[ERREUR]`, `[AVERT]`.

5. **verifier_systeme.py** — si `nvr_ip` est dans config.json, il teste les flux RTSP. Sinon, il tombe sur la détection USB. Vérifie que `nvr_ip` est bien dans `NOUVEAU_PC\config.json`.

6. **ultralytics/torch** — installation longue (5-10 min, 500+ Mo). Normal. Si erreur CUDA, ignore : le système fonctionne en CPU.

---

## 7. Note de retour à préparer (HANDOVER)

Une fois l'installation terminée (ou si tu es bloqué), crée `HANDOVER_PC_principal.md` :

```markdown
# RAPPORT INSTALLATION — [Nom du client]
Date : [date]
PC : [hostname Windows]

## Etat final
| Element | Statut |
|---|---|
| Python | version X.X.X / OK ou ABSENT |
| Dependencies | OK ou liste des erreurs |
| Reseau | IP locale / IP publique / Tailscale / WiFi ou Ethernet |
| Backoffice | OUI (~Xms) ou NON |
| NVR | marque, IP, format RTSP valide |
| Cameras | X/Y fonctionnelles, liste des canaux |
| Surveillance | lancee OUI/NON + logs de demarrage |
| Dashboard :5000 | OK ou NON |
| Demarrage auto | installe OUI/NON |
| Login auto Windows | OK / en cours / non configure |

## Bugs et corrections appliquees
[liste]

## Ce qui reste a faire
[liste]

## Suggestions pour ameliorer le prochain package
[tout ce qui devrait etre mis a jour dans MODELE_DEPLOY_PC]
```

Transmettre ce fichier a Rachid (rachid_sliman@yahoo.fr) ou a la session Claude du PC principal.

---

## 8. Contact et escalade

- **Responsable** : Rachid — rachid_sliman@yahoo.fr
- **Backoffice** : https://backoffice.radaria.fr
- **En cas de blocage** : documente l'erreur exacte et les commandes testées, prépare le HANDOVER avec "Ce qui reste à faire" rempli.

---

*Dernière mise à jour : 29/06/2026 — retour installation Slidis Market intégré*
