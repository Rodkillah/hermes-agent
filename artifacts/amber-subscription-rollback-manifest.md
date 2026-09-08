# Candidat Amber subscription reconciliation — manifeste SQLite et reprise bornée

Statut: candidat source-only. Aucun board live, gateway, job, abonnement, runtime ou Brain n'a été muté. Le job `85fcd56ee535` reste disabled/paused jusqu'à revue indépendante et gate Amber.

## Identité contrôlée

- Base runtime: `b20d9f3c7c8a0a709e862f63240eb3d6fe302e53`.
- Candidat: `0368e1dc2387c3e42c0ae533ac912f6f63c5d6bb`; parent de rework: `d6cdd5facb90db451f6fe4a2c30723f40da3c62b`.
- Fichiers concernés: `hermes_cli/kanban_db.py`, `cron/jobs.py`, `profile-overlay/amber/scripts/kanban_telegram_subscribe_all.py`, `artifacts/verify-amber-subscription-rollback.py`, et les tests associés.

## Autorité et atomicité

1. `kanban_notify_batches` et `kanban_notify_batch_entries` sont l'autorité durable dans le même `kanban.db` que `kanban_notify_subs`. Leur installation, index et triggers se fait dans une transaction `BEGIN IMMEDIATE`; une migration interrompue ne laisse pas de demi-ledger.
2. Le schéma réel est validé: colonnes, `NOT NULL`, clés primaires, FK, `UNIQUE`, `CHECK`, JSON valides et triggers d'immuabilité. Un schéma homonyme affaibli est refusé; aucun import automatique de vieux JSON.
3. Forward et inverse co-committent les abonnements et leurs images complètes. Les images doivent partager la clé `(task_id, platform, chat_id, thread_id)`. L'inverse vérifie le board du forward, restaure via les gardes natives et journalise l'observation réellement présente après restauration; une absence finale est représentée par JSON `null`.
4. Les générations d'abonnement, origines, métadonnées, `created_at` et curseurs sont conservés. Les curseurs ne régressent pas, les conflits humains annulent l'inverse sans écrasement, et le replay lit le ledger SQLite par `batch_id`/digest.

## Paquet durable de rollback

`artifacts/verify-amber-subscription-rollback.py` sépare la préparation et la consommation. La préparation privée lit une fois les trois sources et le job, conserve les trois pré-images/hash/modes et les pré/post-images administratives du job sous une racine jetable. Elle fsync le `package-manifest.json` d'état `prepared` avant l'exécution mutative et conserve un reçu `completed` après succès.

L'entrée opérationnelle consomme ensuite ce paquet existant: DB + `forward_batch_id` explicite, document de job privé et trois fichiers privés. Elle ne reseed pas, ne recopie pas de source live, ne réinstalle pas de fixture et ne reconstruit pas les pré-images après interruption. Les pré-images et post-images sont validées contre le manifeste immuable; une décision job tierce est refusée sans écrasement. Un marqueur de restauration fichier laissé par SIGKILL est récupéré avant toute classification, puis le fichier est restauré par CAS. Un état terminal est rejoué depuis le reçu et ses copies privées, sans lecture live. Une interruption au job ou sur chacun des trois fichiers est rejouable dans un processus neuf sur les mêmes copies.

Ordre exercé: verrou historique → lecture/recovery du ledger SQLite → inverse native → restauration guarded du job → restauration conditionnelle des trois fichiers. Le replay d'un batch déjà `reverted` n'applique aucun second inverse.

## Exports et classification post-COMMIT

Les JSON sont des exports facultatifs non autoritatifs, générés après COMMIT. Le code ne lit plus un export existant sous le verrou; `lstat` refuse FIFO/symlink/non-fichier et une panne d'export conserve le batch SQLite `committed` avec avertissement.

Si une exception survient après un COMMIT possible, le script rouvre la DB de confiance, relit le `batch_id` et classe l'état durable; DB illisible ou incohérente = `unknown` fail-closed, jamais retry implicite ni succès déduit d'un export.

## Préconditions de gate (non réalisées)

1. Revue Architect verte du SHA exact, puis gate Amber explicite.
2. Vérifier le job identique (`85fcd56ee535`, `no_agent`, 1 min, disabled/paused), WIP effectif, fichiers et hash/modes avant tout apply.
3. Backup SQLite ciblé avec `VACUUM INTO` et `quick_check`; sauvegarder les trois fichiers ciblés avec hash/mode et pré-images sur le même système de fichiers.
4. Quiescence prouvée avant tout fichier: job désactivé/pausé, aucun run en vol, gateway arrêtée selon gate Amber, puis relire hash/mode. Toute divergence ou création post-garde refuse le retour.
5. Activation native réversible du seul job existant après gate Amber; canari borné événement → session Amber → action native, avec probes et lecture de retour.

## Retour sûr

Le rollback candidat est un revert Git du SHA exact vers son parent, après revue. Pour un rollback runtime, pause native du job, quiescence et absence de run/descripteur en vol; sous le verrou historique, résoudre les batches SQLite et appliquer l'inverse native. Restaurer ensuite l'administration guarded du seul job, puis les trois fichiers par CAS pré/post-image. Conserver le schéma additif, index et triggers; ne jamais restaurer globalement `kanban.db` ou `jobs.json`, ni écraser claims, historique ou autres jobs.
