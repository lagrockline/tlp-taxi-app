# LDE Vols — Infos taxis

Web app mobile affichant en temps quasi-réel les prochains départs/arrivées de
l'Aéroport Tarbes-Lourdes-Pyrénées (LDE), à destination des chauffeurs de
taxi. Filtre par compagnie (Ryanair, Volotea, autres) et mise en évidence des
retards.

## Comment ça marche

- `scripts/scrape.py` récupère la page publique
  [Infos vols du jour](https://www.tlp.aeroport.fr/page/informations-vols-du-jour)
  de l'aéroport et en extrait la liste des vols dans `data/flights.json`.
- `.github/workflows/update-flights.yml` exécute ce script toutes les 5
  minutes via GitHub Actions et commite le fichier JSON mis à jour.
- `index.html` est une page statique qui lit `data/flights.json` et l'affiche,
  avec filtre par compagnie et badges de statut (décollé / prévu / retardé /
  annulé). Elle se recharge elle-même toutes les 60 secondes.

Aucun serveur à maintenir : tout tourne sur l'infrastructure gratuite de
GitHub (Actions + Pages).

## Installation

1. **Créer le dépôt.** Poussez ce dossier sur un nouveau dépôt GitHub (public
   ou privé — GitHub Pages fonctionne avec les deux si vous avez un compte
   payant ; en gratuit, le dépôt doit être public pour que Pages serve le
   site).

   ```bash
   git init
   git add .
   git commit -m "Première version"
   git branch -M main
   git remote add origin https://github.com/<votre-compte>/<votre-repo>.git
   git push -u origin main
   ```

2. **Activer GitHub Pages.**
   Dans le dépôt GitHub → *Settings* → *Pages* → *Build and deployment* →
   *Source* : choisir **Deploy from a branch**, branche `main`, dossier `/
   (root)`. Après quelques minutes, l'app sera disponible à une adresse du
   type `https://<votre-compte>.github.io/<votre-repo>/`.

3. **Vérifier que le workflow tourne.**
   Onglet *Actions* du dépôt → le job « Mise à jour des vols » doit apparaître
   et s'exécuter toutes les 5 minutes. Vous pouvez le lancer manuellement une
   première fois avec le bouton *Run workflow* (déclencheur
   `workflow_dispatch` déjà prévu dans le fichier).
   Il n'y a rien à configurer côté "secrets" : GitHub fournit automatiquement
   les droits d'écriture nécessaires (`permissions: contents: write` dans le
   workflow).

4. **Ajouter au téléphone.**
   Sur Chrome/Safari mobile, ouvrir l'URL GitHub Pages puis « Ajouter à
   l'écran d'accueil » : l'app se comporte alors comme une icône d'appli
   classique.

## ⚠️ Point important à vérifier avant mise en production

Ce scraper a été écrit à partir du **texte visible** de la page (motif :
ville → date/heure → compagnie + n° de vol → statut optionnel), car je n'ai
pas eu accès au HTML brut ni aux classes CSS réelles du site depuis
l'environnement où ce projet a été généré. La logique a été testée avec
succès sur le contenu réel de la page (récupéré via recherche web), mais
**elle doit être vérifiée une fois déployée en conditions réelles** (premier
lancement du workflow), car des détails de structure HTML non visibles dans
le texte pourraient nécessiter un ajustement mineur des expressions
régulières dans `scripts/scrape.py`.

Si le job échoue ou renvoie 0 vol après un vrai changement de page, le script
n'écrase pas le fichier existant (voir la fonction `main()`) — vous aurez
juste un `flights.json` qui ne se rafraîchit plus, avec une alerte visuelle
dans l'app (pastille orange "données anciennes").

## Limites connues / pistes d'amélioration

- **Respect du site source** : avant un usage en production, il est
  recommandé de vérifier les mentions légales du site aéroportuaire ou de
  contacter l'aéroport pour signaler cet usage (voire obtenir un flux dédié)
  — un scraping toutes les 5 minutes reste très raisonnable en charge, mais
  la courtoisie/transparence est préférable pour un outil utilisé par une
  profession locale.
- **Fuseau horaire** : le script suppose l'heure de Paris été (UTC+2) codée en
  dur pour l'horodatage `scraped_at` ; à ajuster si besoin en hiver ou avec la
  librairie `zoneinfo`.
- **Robustesse** : si l'aéroport modifie la page (nouveau design, nouveaux
  libellés de statut), le parseur texte peut nécessiter une mise à jour.
- **Notifications** : une V2 pourrait ajouter des notifications push (ex. via
  un Service Worker) quand un vol Ryanair/Volotea passe en "Retardé".
