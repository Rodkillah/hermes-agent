# Candidat Amber subscription reconciliation — manifest rollback R3

Status: candidat source-only. Aucun board live, gateway, job, abonnement, runtime ou Brain n'a été muté. Le job `85fcd56ee535` reste disabled/paused jusqu'à revue indépendante et gate Amber.

## Identité contrôlée

- Base runtime: `b20d9f3c7c8a0a709e862f63240eb3d6fe302e53`.
- Correctif R3: `e3f840f2265bb4d1eadc9d5a01cfcecff8d4125c`.
- Branche: `ironrod/forge-amber-subscription-transfer-20260907`.
- Fichiers concernés: `hermes_cli/kanban_db.py`, `profile-overlay/amber/scripts/kanban_telegram_subscribe_all.py`, et le vérificateur/test associés.

## Protocole de sûreté

1. `kanban_notify_subs.subscription_generation` est une identité d'incarnation native: token SQLite de 32 hex, unique, immuable et généré sur toute insertion. Une suppression/recréation de la même clé reçoit nécessairement une nouvelle génération. La migration additive remplit les lignes legacy NULL; aucun appel public ne choisit le token.
2. Chaque passe mutative relit le plan et les pré-images sous la transaction externe. Les images complètes contiennent la génération et les vrais NULL. L'inverse restaure uniquement owner/mode, accepte uniquement une avance de curseur, et refuse génération/champ stable/cursor incompatibles. Les anciens journaux sans génération sont refusés.
3. Le job crée automatiquement un journal privé par batch sous `~/.hermes/profiles/amber/kanban-subscription-journals/iron-rod/<batch-id>/`: répertoire 0700, `prepared.json` 0600 fsync avant commit SQLite, puis marqueur `committed.json` fsync. Une interruption avant marqueur est relue conservativement avant toute nouvelle passe: post-images compatibles => marqueur récupéré; pré-images compatibles => aborted; tout état mixte est refusé sans mutation. Un no-op ne modifie pas de journal antérieur. `--journal` reste un export de compatibilité append-only, jamais l'autorité de récupération.

## Preuve copies privées

`artifacts/verify-amber-subscription-rollback.py` utilise des DB et journaux jetables, et lit seulement le document de job Amber pour en faire une copie privée. Il installe/retire les octets réels du candidat `kanban_db.py` et du script overlay contre la pré-image runtime connue, puis utilise `cron.jobs.update_job`/`pause_job` uniquement avec `JOBS_FILE` redirigé vers la copie. Il vérifie le retour des champs administrés du job ciblé et l'absence de changement sur les autres jobs. Aucun secret ni document de job n'est imprimé.

## Préconditions de gate (non réalisées)

1. Revue Architect verte du SHA exact, puis retrait de l'override Terra pour le reviewer.
2. Vérifier le job identique (`85fcd56ee535`, no_agent, 1 min, disabled/paused), WIP effectif, fichiers et hash/modes avant tout apply.
3. Backup SQLite ciblé avec `VACUUM INTO` et `quick_check`; journal privé disponible et chemin contrôlé.
4. Activation native réversible du seul job existant après gate Amber; premier canari limité: événement -> session Amber -> action native, avec probes et lecture de retour.

## Retour sûr

Avant un retour old-code: pause native du job et absence de run en vol; résoudre les journaux avec ce code, appliquer l'inverse native sur leurs pré/post-images, puis restaurer les seuls fichiers ciblés avec hash/mode guards. Conserver le schéma additif, index et triggers lors du retour au code base: l'ancien code continue de lire les colonnes additionnelles. Ne pas restaurer globalement `kanban.db` ni `jobs.json`; préserver claims, historique et autres jobs.
