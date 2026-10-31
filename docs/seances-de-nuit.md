# Séances de nuit

Objectif : analyser les ~37 200 fichiers de `/Local` (stockage externe, `/Local/alice` 8 282 + `/Local/bob` 28 927) sur plusieurs nuits, et traiter chaque jour les nouveaux fichiers du reste du Nextcloud. Une tâche planifiée Windows lance `scripts/nuit.ps1` chaque soir.

## Principe
- **/Local : analyse en dry-run.** Les résultats (scores, propositions, « à supprimer » et raisons) sont stockés dans `state.db` ; rien n'est écrit dans Nextcloud. Tu relis le rapport « à supprimer » avant d'appliquer (l'application est rapide, ~4 fichiers/s).
- **Hors /Local : passe incrémentale quotidienne.** `inventory` puis `run --apply --no-delete-tag --exclude-path /Local` : les nouveaux fichiers ou fichiers modifiés reçoivent leurs tags, mais **pas** « à supprimer » : ces propositions restent dans l'état et dans `report --delete-candidates`.
- **Reprise automatique.** Chaque `run` s'arrête proprement à l'heure limite (`--until`) : le fichier en cours est fini, le run est clos, les fichiers non pris restent `pending` et sont repris la nuit suivante.
- **Jamais pendant la coupure du serveur** : 03:00-05:30 (sauvegarde 04:00 en mode maintenance, tâche Google Drive du dimanche 03:00). La séance s'arrête avant, par défaut à 02:30 (jamais après 02:45).
- Si llama-server n'est pas déjà prêt, la séance le démarre (masqué, `-b`/`-ub` 8192, `-ngl 99`, 127.0.0.1, réglages lus dans `config/settings.toml`) et l'arrête à la fin ; un serveur déjà lancé n'est jamais arrêté.

## Déroulé d'une nuit
| Heure | Étape |
|---|---|
| 22:30 | Démarrage de la tâche : verrou `logs/nuit.lock`, llama-server prêt (démarré au besoin, attente max 3 min) |
| 22:30+ | `inventory` (alice, bob) |
| ensuite | Passe incrémentale : pour chaque compte, `run --user U --exclude-path /Local --apply --no-delete-tag` |
| ensuite | Analyse de `/Local` en dry-run, par tours de 400 fichiers en alternant alice et bob, jusqu'à l'heure limite |
| 02:30 | Arrêt propre, arrêt de llama-server (si démarré par la séance), ligne de résumé dans `logs/nuit-AAAAMMJJ.log` |
| 03:00-05:30 | Coupure serveur : aucune activité du projet |

Journal : `logs/nuit-AAAAMMJJ.log` (horodaté, sortie complète de chaque commande). Codes de sortie : 0 ok, 1 erreur, 2 séance déjà en cours.

## Combien de nuits
À 0,56 photo/s : (8 282 + 28 927) / 0,56 ≈ 66 400 s ≈ **18,5 h de GPU**.
- Fenêtre réelle 22:30 → 02:30 = 4 h (moins la passe incrémentale, en général courte) ≈ 8 000 fichiers/nuit : **5 nuits**.
- Si la séance démarre plus tôt (≈ 5 h utiles/nuit, ~10 000 fichiers) : **4 nuits**.
Comme les deux comptes alternent, alice (≈ 8 h de GPU au rythme partagé) est fini après ~2 nuits, puis bob avance seule. Les vidéos sont plus lentes que les photos : prévoir une nuit de marge. Le fichier d'état n'est jamais perdu : on peut interrompre et reprendre à tout moment.

## Routine du matin
```powershell
uv run nc-autotag status                                   # avancement par compte et statut
uv run nc-autotag report --delete-candidates --csv reports\a-supprimer.csv   # propositions « à supprimer » à relire
```
Après relecture (ajuster les seuils dans `config/tags.yaml` et relancer `--reprocess` si besoin), appliquer un compte :
```powershell
uv run nc-autotag run --user alice --path /Local/alice --apply
uv run nc-autotag run --user bob --path /Local/bob --apply
```
`--apply` pose tous les tags, « à supprimer » compris (l'outil ne supprime jamais de fichier). Retour arrière : `uv run nc-autotag rollback` (n'ôte que les tags journalisés par l'outil).

## Installer, suspendre, supprimer
```powershell
pwsh -NoProfile -File scripts\install-taches.ps1 -Install     # tâche « nc-autotag-nuit » : tous les jours à 22:30
pwsh -NoProfile -File scripts\install-taches.ps1 -Status      # état, prochain et dernier lancement
pwsh -NoProfile -File scripts\install-taches.ps1 -Uninstall
```
La tâche ne tourne que session ouverte, réveille le PC (`WakeToRun`), démarre même sur batterie, durée max 6 h, rattrape un lancement manqué (`StartWhenAvailable`) et ignore un second lancement simultané.

- **Pause temporaire** : `Disable-ScheduledTask -TaskName nc-autotag-nuit` ; reprise : `Enable-ScheduledTask -TaskName nc-autotag-nuit`.
- **Arrêter la séance en cours** : `Stop-ScheduledTask -TaskName nc-autotag-nuit` (les fichiers non traités restent `pending`) ; supprimer `logs\nuit.lock` seulement si le processus n'existe plus (un verrou périmé est de toute façon détecté).
- **Lancement manuel** : `pwsh -NoProfile -File scripts\nuit.ps1 -Until 02:30 -Mode local`. Une heure limite à plus de 8 h est refusée (évite de traverser la coupure) sauf `-Force`.

## Précautions
- **Le PC doit être allumé ou en veille** (réveil par la tâche) et **la session Windows ouverte** (verrouillée, c'est bon ; déconnectée, non). Pas d'hibernation ni d'arrêt : vérifier dans Paramètres > Système > Alimentation que la mise en veille n'est pas remplacée par l'hibernation, et que « autoriser les minuteurs de réveil » est activé (Options d'alimentation avancées > Veille). Le script empêche la veille pendant la séance.
- Branche le PC sur secteur (le GPU tourne ~4 h) ; la tâche démarre aussi sur batterie.
- Si le PC était éteint à 22:30, la tâche part au rattrapage mais la séance est ignorée si l'heure limite est à plus de 8 h : elle reprend simplement le soir suivant.
- Les mots de passe d'application restent dans le gestionnaire d'identifiants Windows ; la tâche s'exécute sous ton compte pour y avoir accès.
