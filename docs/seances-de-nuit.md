# Séance de traitement

Un seul script : `scripts/nuit.ps1`. Tu le lances quand tu veux, il traite **uniquement les fichiers pas encore traités** (nouveaux, modifiés, ou restants d'une séance précédente), pour tous les dossiers, `/Local` compris, et tu le coupes avec `Ctrl+C` quand tu veux.

```powershell
cd D:\GIT\nextcloud-ai-auto-tag
pwsh -NoProfile -File .\scripts\nuit.ps1
```

## Ce qu'il fait
1. Verrou `logs/nuit.lock` (une seule séance à la fois).
2. Démarre llama-server (d1-Omni) s'il n'est pas déjà prêt ; ne l'arrête à la fin que s'il l'a lancé lui-même.
3. `inventory` : découvre les nouveaux fichiers et ceux qui ont changé.
4. Boucle : par tours de 400 fichiers, en alternant les comptes, `run --apply --no-delete-tag` : analyse par d1 puis pose des tags dans Nextcloud. S'arrête quand plus rien n'est en attente.
5. Empêche la veille Windows pendant la séance, écrit un journal `logs/nuit-AAAAMMJJ.log` et une ligne « Résumé : » par compte.

Chaque tag posé est journalisé : `rollback --yes` retire exactement ce que l'outil a ajouté, sans toucher à tes tags manuels.

## Sauvegarde et maintenance de Nextcloud
Pas d'horaire à surveiller : si Nextcloud passe en **mode maintenance** (par exemple pendant la sauvegarde de 04:00), il répond 503. L'outil se met alors **en pause** et vérifie toutes les minutes ; il reprend tout seul quand Nextcloud revient (jusqu'à 6 h d'attente). La lecture des photos par SFTP n'est pas concernée. Un réglage facultatif `blackout` existe encore dans `config/settings.toml` (liste vide par défaut).

## Le tag « à supprimer »
La séance **ne pose jamais** « à supprimer » (`--no-delete-tag`) : les propositions et leurs raisons restent dans `state.db`. Quand tu veux les traiter :

```powershell
uv run nc-autotag report --delete-candidates --csv reports\a_supprimer.csv   # relire
uv run nc-autotag run --user alice --apply                                 # ajoute « à supprimer » (rapide, ~4 fichiers/s)
```

## Options
```powershell
pwsh -NoProfile -File .\scripts\nuit.ps1 -Until 02:30   # facultatif : s'arrêter à une heure donnée
pwsh -NoProfile -File .\scripts\nuit.ps1 -Chunk 200      # taille d'un tour (400 par défaut)
```
Codes de sortie : 0 ok, 1 erreur, 2 séance déjà en cours.

## Durée
À environ 0,4 à 0,56 fichier par seconde (limité par le GPU), les ~37 000 fichiers de `/Local` représentent 18 à 24 h de GPU. La reprise est automatique : relance simplement le script.
Les vidéos sont plus lentes (6 images analysées par vidéo).

## Précautions
PC allumé et branché (le script empêche la veille). Un second lancement en même temps est refusé.
Pour une exécution automatique chaque soir, `scripts/install-taches.ps1` (`-Install` / `-Uninstall` / `-Status`) enregistre une tâche planifiée Windows ; elle est facultative et n'est pas installée par défaut.
