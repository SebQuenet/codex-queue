# codex-queue

File d'attente commune aux lots `codex exec` qui **génèrent des images**, pour toutes les sessions Claude et tous les
projets. Les lots qui écrivent du texte ne passent pas par elle : ils se lancent directement par `codex exec`. Lancés en
parallèle, les lots saturent la connexion de Codex et passent leur temps à se reconnecter ; la file en fait tourner
quelques-uns à la fois, les plus prioritaires d'abord, et laisse l'utilisateur réordonner la file quand il veut.

Deux fichiers : `codex_queue.py` (Python 3, bibliothèque standard seulement) et `codex_queue_ui.html`, la page servie
par `codex-queue ui`. Commande installée : `codex-queue`,
lien symbolique `~/.local/bin/codex-queue` vers `codex_queue.py`.

## Fonctionnement

- **Un lot** est un fichier JSON dans `~/.local/state/codex-queue/jobs/<id>.json` (`$CODEX_QUEUE_DIR` pour changer de
  dossier) : identifiant, libellé, session, priorité, état, dossier de travail (`-C`), dossiers ajoutés
  (`--add-dir`), consigne, et fichiers attendus (`skip_if_exists`).
- **L'origine** du lot, relevée au dépôt dans le dossier `--origin` (le dossier courant par défaut) : le **projet**
  (le dépôt git principal, commun à ses worktrees, suivi du sous-dossier quand le projet en est un :
  `mon-jeu`, `monorepo/app`…), le **worktree** et sa **branche**.
- **L'appelant**, facultatif : `--caller`, sinon `$CODEX_QUEUE_CALLER`, sinon `$CLAUDE_CODE_SESSION_ID` (l'identifiant
  de la session Claude Code qui a déposé le lot).
- **La session** regroupe les lots dans la liste : `--session`, sinon `$CODEX_QUEUE_SESSION`, sinon la branche (une
  session Claude travaille dans son propre worktree, donc sa propre branche).
- **La priorité** va de 1 (urgent) à 9 (fond) ; à priorité égale, le plus ancien part d'abord, sauf si la page a
  donné au lot un champ `order` (glisser-déposer) : il remplace alors l'heure de dépôt dans le tri.
- **Les états** : `waiting` (attend son tour), `held` (mis de côté, ignoré par le répartiteur), `running`, puis
  `done`, `failed` ou `cancelled`. Les lots finis restent dans la file le temps fixé par `codex-queue keep` (6 h par défaut,
  réglage relu en direct), puis leur fiche est effacée ; leur journal reste dans `logs/`.
- **Relancer un lot** : un lot `failed` ou `cancelled`, ou `done` sans tous ses fichiers attendus, se remet en attente
  sous le même identifiant, avec sa priorité et sa place (`codex-queue retry <sélection>`, ou le bouton ↻ de sa ligne
  sur la page) ; `codex-queue wait <id>` attend alors sa nouvelle fin. Rien n'est relancé tout seul. Le lot repart avec
  la même consigne entière : pour ne refaire qu'une partie, déposer un nouveau lot avec une consigne réduite. À la fin
  d'un lot relançable, `wait` affiche la commande de relance (`retry: codex-queue retry <id> && codex-queue wait <id>`).
- **Le répartiteur** (`codex-queue dispatch`) est unique, garanti par un verrou `flock` sur
  `dispatcher.lock`. `add`, `release` et `retry` le démarrent s'il ne tourne pas ; il s'arrête seul quand la file
  est restée vide une minute.
  Toutes les 5 secondes il relève les lots finis, puis lance les lots en attente jusqu'au nombre de places
  (`codex-queue slots`, 4 par défaut, rangé dans `settings.json` et relu à chaque tour : le changer agit tout de suite,
  sans relancer le répartiteur ; en le baissant, les lots en cours finissent, rien n'est arrêté).
  `codex-queue pause` (réglage `paused`, relu de même) l'empêche de lancer quoi que ce soit ; `resume` lève la pause et remet en attente les lots mis de côté (sur la page, « tout reprendre »
  s'affiche dès qu'un lot est en pause ou mis de côté).
  `codex-queue fair on` (réglage `fair`, relu de même) sert les sessions à tour de rôle : à chaque place libre, parmi
  les lots de la meilleure priorité, il prend celui de la session qui a le moins de lots en cours. `codex-queue
  interleave`, au contraire, réécrit une fois le champ `order` des lots en attente pour alterner les sessions ; les lots
  déposés ensuite passent après eux.
  Chaque `codex exec` part dans sa propre session de processus (il survit à la session Claude qui l'a déposé), sa
  sortie dans `logs/<id>.log`.
- **Un lot dont tous les fichiers attendus existent** est marqué `done` sans être lancé : on peut redéposer un lot
  sans craindre de repeindre ce qui est déjà fait.
- **Un lot qui déclare des fichiers attendus** n'est marqué `done` que s'ils existent tous à sa fin, sinon `failed` :
  `codex exec` peut sortir avec le code 0 sans rien peindre (quota d'images atteint, référence absente), et un lot
  suivi par son seul pid (lancé par un répartiteur précédent) n'a pas de code de sortie.
- Un répartiteur relancé retrouve les lots en cours par leur pid.
- **La page** (`codex-queue ui`, http://127.0.0.1:8765/, locale seulement) relit la file toutes les 3 secondes. On y
  glisse les lots en attente pour les réordonner (le lot prend la priorité de son voisin du dessus), on les met de côté,
  on les annule, on met toute la file en pause ou on arrête tout ce qui tourne, on relance un lot échoué ou annulé (↻ sur sa ligne) ou tous les lots finis sans leurs fichiers, on sert les sessions à tour de rôle ou on les entrelace une fois
  (la file affiche l'ordre de lancement calculé par le serveur ; en tour de rôle, le glisser-déposer est désactivé) ; on trie les listes par priorité, demande, début, fin, libellé ou
  session (le glisser-déposer ne marche qu'en « ordre de passage ») ; un lot montre son journal, sa consigne et ses images. Les images sont celles du dossier
  `~/.codex/generated_images/<session codex>/` (`$CODEX_HOME`), la session codex étant lue sur la ligne
  `session id:` du journal. Un lot en cours signale le dernier incident lu dans les dernières lignes de son journal
  (quota, disque plein, hors réseau, reconnexion, erreur) ; un lot fini dit s'il a réussi, échoué, été tué par un
  signal, arrêté en cours (annulé après son départ) ou annulé avant. Un clic sur une session montre les images de tous ses lots encore conservés et fixe d'un coup la priorité de
  tous ses lots en attente ou mis de côté.

## Commandes

```sh
codex-queue add --priority 5 --label <lot> [--workdir <dossier>] --prompt-file <consigne.txt> \
  [--add-dir <dossier>]... [--skip-if-exists <fichier>...] [--origin <worktree>] [--session <nom>] [--caller <id>] \
  [-- <options de codex exec : -s workspace-write, -o <fichier>, -m <modèle>…>]
codex-queue wait <id>                           # attend la fin du lot, affiche son état et son journal
codex-queue list [--session <nom>]              # par session, avec projet / worktree et appelant ; pour chaque
                                                # lot : état, priorité, id, heures de demande, de début et de
                                                # fin de génération (null tant qu'elles n'existent pas), libellé
codex-queue list --since 1h                     # ajoute les lots finis dans l'heure
codex-queue list --all                          # ajoute tous les lots finis encore gardés
codex-queue keep [2d]                           # montre ou fixe la durée de conservation des lots finis
codex-queue priority <1-9> <id>... | --session <nom> | --label <préfixe>
codex-queue hold <sélection>                    # met de côté des lots en attente
codex-queue release <sélection>                 # les remet en attente
codex-queue cancel <sélection>                  # abandonne ceux qui attendent, arrête ceux qui tournent
codex-queue slots [N]                           # montre ou fixe le nombre de codex exec en parallèle (1 à 32)
codex-queue pause | resume                      # met toute la file en pause (les lots en cours finissent), ou la relance avec les lots mis de côté
codex-queue fair [on|off]                       # montre ou fixe le tour de rôle entre sessions
codex-queue interleave                          # réécrit une fois l'ordre des lots en attente en alternant les sessions
codex-queue retry <sélection>                   # remet en attente des lots échoués ou annulés, même identifiant
codex-queue retry-missing                       # remet en attente les lots finis dont un fichier attendu manque
codex-queue stop-all                            # met la file en pause et arrête tous les lots en cours
codex-queue ui [--port 8765]                    # page web locale de la file
```

Les sélecteurs (identifiants, `--session`, `--label`) se combinent ; sans sélecteur, la commande refuse d'agir.

`add` affiche l'identifiant du lot. `--workdir` est le `-C` de `codex exec` ; par défaut, la file donne au lot son
propre dossier, `work/<id>`, effacé avec sa fiche : un dossier temporaire de l'appelant (le scratchpad d'une session
Claude) peut disparaître pendant que le lot tourne, et Codex ne peut alors plus lancer aucune commande. Les options
après `--` sont passées telles quelles à `codex exec`. Pour être prévenu de la fin d'un lot, une session lance
`codex-queue wait <id>` en arrière-plan : la commande se termine quand le lot est fini (code 0 s'il a réussi).

## Règles

- Après une modification de `codex_queue.py`, relancer le répartiteur (le tuer par son pid ; `codex-queue release
  --label x` en démarre un neuf) : celui qui tourne garde l'ancien code. Les lots en cours continuent, il les retrouve
  par leur pid.

- Un lot qui génère des images passe toujours par `codex-queue add`, jamais par `codex exec` direct. Un lot qui écrit
  du texte (rédaction, relecture) se lance directement par `codex exec` : la file ne le concerne pas.
- Ne pas tuer un répartiteur avec `pkill -f codex_queue` depuis un shell dont la ligne de commande contient ce motif
  (il se tue lui-même) : viser son pid (`ps -eo pid,args | grep -E "[c]odex.queue(.py)? dispatch"`).
- Le programme reste générique : rien de propre à un projet (les règles de peinture d'un projet vont dans sa consigne
  et dans ses propres skills).
