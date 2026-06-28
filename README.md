# RadarIA Surveillance

Système de surveillance vidéo intelligent pour les clients RadarIA.

## Installation

1. Copier `config.json.exemple` vers `config.json`
2. Remplir les informations caméras et Twilio dans `config.json`
3. Double-cliquer sur `1_INSTALLER.bat`
4. Double-cliquer sur `2_DEMARRER.bat`

## Configuration

Éditer `config.json` :
- `cameras` : URLs RTSP de vos caméras
- `twilio` : Identifiants Twilio pour alertes WhatsApp
- `destinataires_whatsapp` : Numéros qui reçoivent les alertes
