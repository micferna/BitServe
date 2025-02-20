# CODEBYGPT

# BitServe

BitServe est une application FastAPI pour gérer les téléchargements de torrents via une interface API.

## Installation

Assurez-vous d'avoir Python installé sur votre machine. Clonez le dépôt et installez les dépendances :

```bash
git clone https://github.com/micferna/BitServe.git
cd BitServe
pip install -r requirements.txt
```

## Démarrage

Pour démarrer le serveur, exécutez :
```bash
uvicorn app:app --reload
```
- Le serveur sera accessible à l'adresse http://0.0.0.0:8000.

## Utilisation
Ajouter des Torrents

Pour ajouter des torrents, envoyez une requête POST avec les fichiers .torrent :
```bash
curl -X POST "http://localhost:8000/add-torrents/" \
    -F "files=@torrent1.torrent" \
    -F "files=@path/to/your/torrent2.torrent"
```

## Lister les Torrents

Pour obtenir la liste des torrents en cours :
```bash
curl -H "Accept: application/json" http://localhost:8000/torrents/ | jq
```

## Supprimer des Torrents

Pour supprimer des torrents, envoyez une requête POST avec les info_hashes des torrents à supprimer :
```bash
curl -X POST "http://localhost:8000/remove-torrents/" \
     -H "Content-Type: application/json" \
     -d '{"info_hashes": ["hash1", "hash2"], "remove_files": true}'
```

## Informations Système

Pour obtenir des informations sur le système (utilisation du disque, de la mémoire, etc.) :
```bash
curl -H "Accept: application/json" http://localhost:8000/system-info/ | jq
```

# Fonctionnalités

 - Gestion des téléchargements de torrents via API.
 - Sauvegarde et restauration de l'état des torrents entre les redémarrages du serveur.
- Informations système détaillées.

---
# EN COURS
## Webhooks

Pour enregistrer un webhook :
```bash
curl -X POST "http://localhost:8000/register-webhook/" \
     -H "Content-Type: application/json" \
     -d '{"event": "your_event", "url": "your_webhook_url"}'
```
