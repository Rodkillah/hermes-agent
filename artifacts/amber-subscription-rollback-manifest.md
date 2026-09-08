# Candidat Amber subscription reconciliation — manifeste SQLite et reprise bornée

Statut: candidat source-only. Aucun board live, gateway, job, abonnement, runtime ou Brain n'a été muté par ce candidat. Des écritures Brain antérieures sur la carte restent un écart de gouvernance séparé, non validé ni réparé ici. Le job `85fcd56ee535` reste disabled/paused jusqu'à revue indépendante et gate Amber.

## Identité contrôlée

- Base runtime: `b20d9f3c7c8a0a709e862f63240eb3d6fe302e53`.
- Branche: `ironrod/forge-amber-subscription-transfer-20260907`.
- Implementation exacte de ce rework: `d436b88f90c15ba5545dad7c01c091204f233314` (parent `7b85a46e09483a28fe0c9b2aef60855c8c0171f1`). Le candidat final est le HEAD de cette branche au gel de revue ; aucun autre SHA ne vaut candidat.
- Fichiers de ce rework: `cron/jobs.py`, `artifacts/verify-amber-subscription-rollback.py`, `tests/cron/test_update_job_expected_cas.py`, `artifacts/amber-subscription-rollback-manifest.md`.
- Le worktree candidat doit être relu propre et le SHA final exact doit être gelé avant revue; aucun autre SHA ne vaut candidat.

## Autorité et atomicité

1. `kanban_notify_batches` et `kanban_notify_batch_entries` sont l'autorité durable dans le même `kanban.db` que `kanban_notify_subs`. Leur installation, index et triggers se fait dans une transaction `BEGIN IMMEDIATE`; une migration interrompue ne laisse pas de demi-ledger.
2. Le schéma réel est validé par `PRAGMA table_info`, clés/FK/index et comparaison de la définition complète des tables et des quatre triggers canoniques. Un schéma homonyme dont un `CHECK`, type, index ou trigger est affaibli est refusé; aucun import automatique de vieux JSON.
3. Forward et inverse co-committent les abonnements et leurs images complètes. Les images doivent partager la clé `(task_id, platform, chat_id, thread_id)`. L'inverse vérifie le board du forward, restaure via les gardes natives et journalise l'observation réellement présente après restauration; une absence finale est représentée par JSON `null`.
4. Les générations d'abonnement, origines, métadonnées, `created_at` et curseurs sont conservés. Les curseurs ne régressent pas, les conflits humains annulent l'inverse sans écrasement, et le replay lit le ledger SQLite par `batch_id`/digest.

## Paquet durable de rollback

`artifacts/verify-amber-subscription-rollback.py` sépare la préparation et la consommation. La préparation privée lit une fois les trois sources et le job, conserve les trois pré-images/hash/modes et les pré/post-images administratives du job sous une racine jetable. Le job post-image persiste la valeur exacte de `paused_at` au format natif; la pause privée applique cette valeur via l'API cron native. Chaque document et image de récupération est fsyncé avant publication du manifeste `prepared` et avant tout effet sur les copies.

La consommation opérationnelle exige ensuite ce paquet existant: DB + `forward_batch_id` explicite, document de job privé et trois fichiers privés. Elle valide et récupère le manifeste complet avant l'inverse, sans jamais préparer selon l'existence d'un répertoire, reseed, recopier une source live, réinstaller une fixture ou reconstruire les pré-images après interruption. Les pré-images et post-images sont validées contre le manifeste immuable; une décision job tierce est refusée sans écrasement. Un marqueur de restauration fichier laissé par SIGKILL est récupéré après cette validation et avant la classification du fichier, puis le fichier est restauré par CAS. Un état terminal est rejoué depuis le reçu et ses copies privées, sans lecture live.

La garde finale de `cron.jobs.update_job(..., expected=...)` compare désormais sous le verrou natif la présence, le type JSON exact et la valeur de chaque champ attendu: `False` ne correspond pas à `0` et une clé absente ne correspond pas à `null`. Le caller de reprise lit l'image brute du document privé et la valide sans passer par la normalisation d'affichage de `get_job()`.

Publication: manifeste temporaire écrit et fsyncé, répertoire fsyncé, remplacement atomique, sans `write_text` tronquant le primaire. Lors d'une récupération, le primaire endommagé n'écrase jamais la seule copie `.previous` valide. Le paquet nominal est préparé et durable avant l'ordre exercé `inverse SQLite -> job CAS -> trois fichiers CAS`; le replay du même paquet ne réapplique aucune inverse.

## Preuves source-only réellement exécutées

- `python -m pytest -q tests/profile_overlay/test_telegram_subscribe.py tests/profile_overlay/test_telegram_subscription_batches.py` → 21 passed, rc 0.
- `python -m pytest -q tests/hermes_cli/test_kanban_notify.py tests/hermes_cli/test_kanban_notify_owner_transfer.py tests/hermes_cli/test_kanban_review_lifecycle_complete.py tests/hermes_cli/test_kanban_db_init.py tests/cron/test_jobs_crossprocess_lock.py` → 65 passed, rc 0.
- Contrat indépendant R14 (`test_review_round14_contract.py`) → 11 passed, rc 0. Il couvre le refus sans effet d'un paquet absent/malformé/incomplet ou en conflit, l'usage exclusif des post-images durables, l'ordre inverse → CAS job → fichiers, le replay sans second effet et la récupération `.previous`.
- Rejeu indépendant R10→R14 sous sandbox isolé avec adaptateurs de fixtures explicites R14 (aucune désélection) → 77 passed, rc 0. Les deux anciens contrôles de préparation implicite ont été réalignés sur le contrat strict « prepare puis consume »; le contrôle de refus d'un paquet absent est conservé.
- `env -u HERMES_KANBAN_TASK -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD python artifacts/verify-amber-subscription-rollback.py` → `targeted Amber rollback verifier: PASS`, `quick_check=ok`, conflit concurrent refusé sans overwrite, rc 0.
- Contrat indépendant R16 (`test_review_round16_contract.py`) → 7 passed, rc 0. Il couvre le refus sans effet d'un marqueur non régulier, d'une identité de job hors périmètre ou dupliquée, et d'un état administratif observé incomplet ou mal typé.
- Contrat indépendant R17 (`test_review_round17_contract.py`) → 6 passed, rc 0 après correction de la garde finale; les deux interventions tardives `False→0` et `null→absent` sont refusées sans restauration des fichiers et avec reçu conservé `prepared`.
- Régressions natives `tests/cron/test_update_job_expected_cas.py tests/cron/test_jobs.py` → 116 passed, rc 0; suite overlay → 21 passed, rc 0; suite Hermes/cron → 65 passed, rc 0.
- `python3 -m py_compile artifacts/verify-amber-subscription-rollback.py cron/jobs.py tests/cron/test_update_job_expected_cas.py` → rc 0; `git diff --check 7b85a46e09483a28fe0c9b2aef60855c8c0171f1..HEAD` → rc 0.

## Préconditions de gate (non réalisées)

1. Revue Architect verte du SHA exact, puis gate Amber explicite.
2. Vérifier le job identique (`85fcd56ee535`, `no_agent`, 1 min, disabled/paused), WIP effectif, fichiers et hash/modes avant tout apply.
3. Backup SQLite ciblé avec `VACUUM INTO` et `quick_check`; sauvegarder les trois fichiers ciblés avec hash/mode et pré-images sur le même système de fichiers.
4. Quiescence prouvée avant tout fichier: job désactivé/pausé, aucun run en vol, gateway arrêtée selon gate Amber, puis relire hash/mode. Toute divergence ou création post-garde refuse le retour.
5. Activation native réversible du seul job existant après gate Amber; canari borné événement → session Amber → action native, avec probes et lecture de retour.

## Retour sûr

Le rollback source-only du candidat exact est le revert du commit `d436b88f90c15ba5545dad7c01c091204f233314` vers `7b85a46e09483a28fe0c9b2aef60855c8c0171f1`, puis, si nécessaire, des commits antérieurs dans l'ordre inverse. Pour un rollback runtime, pause native du job, quiescence et absence de run/descripteur en vol; sous le verrou historique, résoudre les batches SQLite et appliquer l'inverse native. Restaurer ensuite l'administration guarded du seul job, puis les trois fichiers par CAS pré/post-image. Conserver le schéma additif, index et triggers; ne jamais restaurer globalement `kanban.db` ou `jobs.json`, ni écraser claims, historique ou autres jobs.
