# Pizza Empire : Steam vers Discord

Deux workflows GitHub Actions (copie adaptée de `ashen-steam-discord`) :

| Workflow | Script | Salon Discord | Secret GitHub |
|---|---|---|---|
| `sync.yml` (toutes les 15 min) | `sync_steam.py` | `#steam-announcements` | `DISCORD_WEBHOOK_URL` |
| `reviews.yml` (toutes les 15 min) | `sync_reviews.py` | `#steam-reviews` | `DISCORD_REVIEWS_WEBHOOK_URL` |

Les messages sont postés sous le nom « Pizza Empire Steam News », avec l'icône de la pizza pepperoni.
Ce qui a déjà été posté est retenu dans `state.json` et `reviews_state.json`, que le workflow commit après chaque passage.

## Mise en place

1. Discord : dans chaque salon, Paramètres du salon > Intégrations > Webhooks > Nouveau webhook > Copier l'URL.
2. GitHub : Settings > Secrets and variables > Actions > New repository secret, avec les deux secrets du tableau.
3. Actions : lancer chaque workflow une fois à la main (« Run workflow ») pour vérifier.

## Commandes utiles

```
python sync_steam.py --dry-run      # affiche les embeds sans rien envoyer
python sync_steam.py --mark-seen    # marque tout l'historique comme déjà publié
python sync_reviews.py --dry-run
python sync_reviews.py --mark-seen  # idem pour les évaluations actuelles
python -m unittest discover -s tests
```

Sous Windows, ajouter `PYTHONIOENCODING=utf-8` pour le `--dry-run` (emojis dans la console).
