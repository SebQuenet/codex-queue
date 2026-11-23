# codex-queue

File d'attente commune aux lots `codex exec`, partagée par toutes les sessions et tous les projets d'une machine.

Lancés en parallèle, des lots `codex exec` (notamment ceux qui génèrent des images) saturent la connexion de Codex et
passent leur temps à se reconnecter. `codex-queue` n'en fait tourner que quelques-uns à la fois, les plus prioritaires
d'abord, et laisse réordonner la file à tout moment, en ligne de commande ou depuis une page web locale.

Un script Python 3 sans dépendance (bibliothèque standard seulement) et une page HTML.

## Installation

```sh
git clone https://github.com/SebQuenet/codex-queue.git
ln -s "$PWD/codex-queue/codex_queue.py" ~/.local/bin/codex-queue
```

Il faut `codex` dans le `PATH`. Linux ou macOS (le script utilise `flock` et les groupes de processus).

## Utilisation

```sh
# Déposer un lot : la consigne est lue dans un fichier, les options après -- vont à codex exec
codex-queue add --priority 5 --label portrait-01 --workdir /tmp/lot --prompt-file consigne.txt \
  --skip-if-exists art/portrait-01.png -- -s workspace-write

codex-queue wait <id>                 # attend la fin du lot (code 0 s'il a réussi)
codex-queue list [--since 1h | --all] # lots par session, avec leurs heures de dépôt, début et fin
codex-queue priority 2 <id>...        # nouvelle priorité (1 urgent … 9 fond)
codex-queue hold | release <sélection>  # mettre de côté, remettre en attente
codex-queue cancel <sélection>        # abandonner un lot en attente, arrêter un lot en cours
codex-queue slots [N]                 # nombre de codex exec en parallèle (4 par défaut)
codex-queue pause | resume            # plus aucun lot ne démarre (ceux en cours finissent), ou reprise
codex-queue keep [6h]                 # durée de conservation des lots finis
codex-queue ui [--port 8765]          # page web locale
```

Une sélection combine des identifiants, `--session <nom>` et `--label <préfixe>` ; sans sélection, la commande
refuse d'agir.

## Fonctionnement

- Chaque lot est un fichier JSON dans `~/.local/state/codex-queue/jobs/` (`$CODEX_QUEUE_DIR` pour changer de dossier),
  son journal dans `logs/<id>.log`.
- Un répartiteur unique (verrou `flock`) démarre au premier dépôt, lance les lots par priorité puis par ancienneté, et
  s'arrête seul quand la file est vide. Le nombre de places et la durée de conservation sont relus à chaque tour.
- Chaque `codex exec` tourne dans sa propre session de processus : il survit au terminal qui l'a déposé, et un
  répartiteur relancé le retrouve par son pid.
- Un lot dont tous les fichiers attendus (`--skip-if-exists`) existent déjà est marqué `done` sans être lancé.
- Chaque lot note son origine : le projet (dépôt git principal), le worktree, la branche et l'appelant
  (`--caller`, `$CODEX_QUEUE_CALLER` ou `$CLAUDE_CODE_SESSION_ID`). La session qui regroupe les lots vaut
  `--session`, `$CODEX_QUEUE_SESSION` ou, à défaut, la branche.

## Page web

`codex-queue ui` sert une page sur http://127.0.0.1:8765/, accessible depuis la machine seulement :

- glisser-déposer des lots en attente pour changer leur ordre de passage ;
- mettre de côté, remettre en attente, annuler ;
- régler le nombre de places, filtrer par session ;
- pour chaque lot : consigne, journal en direct et images générées ;
- pour chaque session : toutes les images de ses lots.

Les images sont celles que Codex range dans `~/.codex/generated_images/<session codex>/` (`$CODEX_HOME`), retrouvées
grâce à la ligne `session id:` en tête du journal du lot.

## Licence

MIT, voir [LICENSE](LICENSE).
