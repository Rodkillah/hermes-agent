# Candidat Amber subscription reconciliation — manifest rollback R8

Status: candidat source-only. Aucun board live, gateway, job, abonnement, runtime ou Brain n'a été muté. Le job `85fcd56ee535` reste disabled/paused jusqu'à revue indépendante et gate Amber.

## Identité contrôlée

- Base runtime: `b20d9f3c7c8a0a709e862f63240eb3d6fe302e53`.
- Candidat R8 (code, vérificateur et manifeste) : à figer avec le SHA unique de la livraison source, sans activation.
- Branche: `ironrod/forge-amber-subscription-transfer-20260907`.
- Fichiers concernés: `hermes_cli/kanban_db.py`, `cron/jobs.py`, `profile-overlay/amber/scripts/kanban_telegram_subscribe_all.py`, `artifacts/verify-amber-subscription-rollback.py`, et les tests associés.

## Protocole de sûreté

1. `kanban_notify_subs.subscription_generation` est une identité d'incarnation native: token SQLite de 32 hex, unique, immuable et généré sur toute insertion. Une suppression/recréation de la même clé reçoit nécessairement une nouvelle génération. La migration additive remplit les lignes legacy NULL; aucun appel public ne choisit le token.
2. Chaque passe mutative relit le plan et les pré-images sous la transaction externe. Les images complètes contiennent la génération et les vrais NULL. L'inverse restaure uniquement owner/mode, accepte uniquement une avance de curseur, et refuse génération/champ stable/cursor incompatibles. Les anciens journaux sans génération sont refusés.
3. Le job crée automatiquement un journal privé par batch sous `~/.hermes/profiles/amber/kanban-subscription-journals/iron-rod/<batch-id>/`: répertoire 0700, `prepared.json` 0600 fsync avant commit SQLite, puis marqueur `committed.json` fsync. Les écritures gardent des handles no-follow pour le parent, la racine et le batch, puis revalident leurs entrées après le fsync réel; si la racine ou le batch a été détaché, elles retirent seulement l'inode publié par cette passe et la transaction SQLite échoue avant COMMIT. Les lectures distinguent une absence ENOENT d'un ELOOP, refus de permission ou fichier spécial : ces derniers bloquent recovery et toute nouvelle passe. Une interruption avant marqueur est relue conservativement avant toute nouvelle passe: post-images compatibles => marqueur récupéré; pré-images compatibles => aborted; tout état mixte est refusé sans mutation. Un no-op ne modifie pas de journal antérieur. `--journal` reste un export de compatibilité append-only, jamais l'autorité de récupération.

## Preuve copies privées

`artifacts/verify-amber-subscription-rollback.py` utilise des DB, journaux, verrou historique, document de job et fichiers runtime jetables sous une même racine privée. Il exécute dans cet ordre réel : verrou exclusif, recovery des batches, apply journalisé, inverse native, restauration administrative guarded du seul job par l'API cron pendant que `cron/jobs.py` candidat est encore sur disque, puis restauration conditionnelle des trois fichiers exacts (`hermes_cli/kanban_db.py`, `cron/jobs.py`, `profiles/amber/scripts/kanban_telegram_subscribe_all.py`). Les pré-images/modes/hash sont contrôlés sur copies; une création concurrente post-garde est conservée sans écrasement. Aucun restore global de DB/jobs ni document de job n'est produit.

## Préconditions de gate (non réalisées)

1. Revue Architect verte du SHA exact, puis retrait de l'override Terra pour le reviewer.
2. Vérifier le job identique (`85fcd56ee535`, no_agent, 1 min, disabled/paused), WIP effectif, fichiers et hash/modes avant tout apply.
3. Backup SQLite ciblé avec `VACUUM INTO` et `quick_check`; journal privé disponible et chemin contrôlé. Sauvegarder les trois fichiers ciblés avec hash/mode et pré-image sur le même système de fichiers.
4. Quiescence prouvée avant tout fichier : job désactivé/pausé, aucun run en vol, gateway arrêtée selon gate Amber, puis relire hash/mode. Toute divergence ou création post-garde refuse le retour et conserve le conflit; ne jamais appliquer le retour à un chemin déjà recréé.
5. Activation native réversible du seul job existant après gate Amber; premier canari limité: événement -> session Amber -> action native, avec probes et lecture de retour.

## Retour sûr

Avant un retour old-code: pause native du job, quiescence gateway prouvée et absence de run/descripteur en vol; sous le verrou historique, résoudre les journaux avec ce code et appliquer l'inverse native sur leurs pré/post-images. Restaurer ensuite l'administration du seul job par l'API cron guarded tant que le nouveau `cron/jobs.py` reste disponible, puis les trois fichiers ciblés par la revendication conditionnelle décrite ci-dessus. Conserver le schéma additif, index et triggers lors du retour au code base: l'ancien code continue de lire les colonnes additionnelles. Ne pas restaurer globalement `kanban.db` ni `jobs.json`; préserver claims, historique et autres jobs.
