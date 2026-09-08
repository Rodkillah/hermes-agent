# Candidat Amber subscription reconciliation — manifeste SQLite et reprise bornée

Statut: candidat source-only. Aucun board live, gateway, job, abonnement, runtime ou Brain n'a été muté. Le job `85fcd56ee535` reste disabled/paused jusqu'à revue indépendante et gate Amber.

## Identité contrôlée

- Base runtime: `b20d9f3c7c8a0a709e862f63240eb3d6fe302e53`.
- Branche: `ironrod/forge-amber-subscription-transfer-20260907`.
- Implémentation exacte de ce rework: `ae6d32788d9a132bc3a8b327765eb3576a1d8dfc` (parent `14f860d24a0580239c326be3dd15724f8732b020`).
- Fichiers de ce rework: `artifacts/verify-amber-subscription-rollback.py`, `cron/jobs.py`, `hermes_cli/kanban_db.py`.
- Le worktree candidat doit être relu propre et le SHA final exact doit être gelé avant revue; aucun autre SHA ne vaut candidat.

## Autorité et atomicité

1. `kanban_notify_batches` et `kanban_notify_batch_entries` sont l'autorité durable dans le même `kanban.db` que `kanban_notify_subs`. Leur installation, index et triggers se fait dans une transaction `BEGIN IMMEDIATE`; une migration interrompue ne laisse pas de demi-ledger.
2. Le schéma réel est validé par `PRAGMA table_info`, clés/FK/index et comparaison de la définition complète des tables et des quatre triggers canoniques. Un schéma homonyme dont un `CHECK`, type, index ou trigger est affaibli est refusé; aucun import automatique de vieux JSON.
3. Forward et inverse co-committent les abonnements et leurs images complètes. Les images doivent partager la clé `(task_id, platform, chat_id, thread_id)`. L'inverse vérifie le board du forward, restaure via les gardes natives et journalise l'observation réellement présente après restauration; une absence finale est représentée par JSON `null`.
4. Les générations d'abonnement, origines, métadonnées, `created_at` et curseurs sont conservés. Les curseurs ne régressent pas, les conflits humains annulent l'inverse sans écrasement, et le replay lit le ledger SQLite par `batch_id`/digest.

## Paquet durable de rollback

`artifacts/verify-amber-subscription-rollback.py` sépare la préparation et la consommation. La préparation privée lit une fois les trois sources et le job, conserve les trois pré-images/hash/modes et les pré/post-images administratives du job sous une racine jetable. Le job post-image persiste la valeur exacte de `paused_at` au format natif; la pause privée applique cette valeur via l'API cron native. Chaque document et image de récupération est fsyncé avant publication du manifeste `prepared` et avant tout effet sur les copies.

La consommation opérationnelle exige ensuite ce paquet existant: DB + `forward_batch_id` explicite, document de job privé et trois fichiers privés. Elle valide et récupère le manifeste complet avant l'inverse, sans jamais préparer selon l'existence d'un répertoire, reseed, recopier une source live, réinstaller une fixture ou reconstruire les pré-images après interruption. Les pré-images et post-images sont validées contre le manifeste immuable; une décision job tierce est refusée sans écrasement. Un marqueur de restauration fichier laissé par SIGKILL est récupéré après cette validation et avant la classification du fichier, puis le fichier est restauré par CAS. Un état terminal est rejoué depuis le reçu et ses copies privées, sans lecture live.

Publication: manifeste temporaire écrit et fsyncé, répertoire fsyncé, remplacement atomique, sans `write_text` tronquant le primaire. Lors d'une récupération, le primaire endommagé n'écrase jamais la seule copie `.previous` valide. Le paquet nominal est préparé et durable avant l'ordre exercé `inverse SQLite -> job CAS -> trois fichiers CAS`; le replay du même paquet ne réapplique aucune inverse.

## Preuves source-only réellement exécutées

- `python -m pytest -q tests/profile_overlay/test_telegram_subscribe.py tests/profile_overlay/test_telegram_subscription_batches.py` → 21 passed, rc 0.
- `python -m pytest -q tests/hermes_cli/test_kanban_notify.py tests/hermes_cli/test_kanban_notify_owner_transfer.py tests/hermes_cli/test_kanban_review_lifecycle_complete.py tests/hermes_cli/test_kanban_db_init.py tests/cron/test_jobs_crossprocess_lock.py` → 65 passed, rc 0.
- Revue indépendante confinée, `test_review_round10.py test_review_round11.py test_review_round12_supplement.py test_review_round13_contract.py` → 64 passed, 2 failures, rc 1. Les deux échecs sont les anciens seams qui injectent un SIGKILL en interceptant `Path.open/write_text` sur le chemin primaire; ils n'observent plus l'implémentation atomique par fichier temporaire + `os.replace`. Les nouveaux contrôles métier R13 sont 8/8 verts; la réexécution de ces deux seams doit être adaptée à la frontière atomique, sans réintroduire une écriture primaire tronquante.
- `env -u HERMES_KANBAN_TASK -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD python artifacts/verify-amber-subscription-rollback.py` → `targeted Amber rollback verifier: PASS`, `quick_check=ok`, conflit concurrent refusé sans overwrite, rc 0.
- `python3 -m py_compile artifacts/verify-amber-subscription-rollback.py cron/jobs.py hermes_cli/kanban_db.py` → rc 0; `git diff --check` → rc 0 avant commit.

## Préconditions de gate (non réalisées)

1. Revue Architect verte du SHA exact, puis gate Amber explicite.
2. Vérifier le job identique (`85fcd56ee535`, `no_agent`, 1 min, disabled/paused), WIP effectif, fichiers et hash/modes avant tout apply.
3. Backup SQLite ciblé avec `VACUUM INTO` et `quick_check`; sauvegarder les trois fichiers ciblés avec hash/mode et pré-images sur le même système de fichiers.
4. Quiescence prouvée avant tout fichier: job désactivé/pausé, aucun run en vol, gateway arrêtée selon gate Amber, puis relire hash/mode. Toute divergence ou création post-garde refuse le retour.
5. Activation native réversible du seul job existant après gate Amber; canari borné événement → session Amber → action native, avec probes et lecture de retour.

## Retour sûr

Le rollback candidat est un revert Git du SHA exact `ae6d32788d9a132bc3a8b327765eb3576a1d8dfc` vers son parent `14f860d24a0580239c326be3dd15724f8732b020`, après revue. Pour un rollback runtime, pause native du job, quiescence et absence de run/descripteur en vol; sous le verrou historique, résoudre les batches SQLite et appliquer l'inverse native. Restaurer ensuite l'administration guarded du seul job, puis les trois fichiers par CAS pré/post-image. Conserver le schéma additif, index et triggers; ne jamais restaurer globalement `kanban.db` ou `jobs.json`, ni écraser claims, historique ou autres jobs.
