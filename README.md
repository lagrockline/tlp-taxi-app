# LDE Vols — Infos taxis

Web app mobile affichant en temps quasi-réel les prochains départs/arrivées de
l'Aéroport Tarbes-Lourdes-Pyrénées (LDE), à destination des chauffeurs de
taxi. Filtre par compagnie (Ryanair, Volotea, autres), alertes visuelles et
sonores en direct, et **notifications push même téléphone verrouillé/app
fermée**.

## Comment ça marche

- `scripts/scrape.py` récupère la page publique
  [Infos vols du jour](https://www.tlp.aeroport.fr/page/informations-vols-du-jour)
  de l'aéroport, extrait les vols dans `data/flights.json`, et calcule les
  changements de statut (retard/avance) par rapport au scrape précédent.
- `.github/workflows/update-flights.yml` exécute ce script toutes les 5
  minutes via GitHub Actions, commite le JSON mis à jour, puis lance
  `scripts/send_push.py` qui envoie une notification push à tous les
  téléphones abonnés si un changement a été détecté.
- `index.html` est la page de l'app : lit `data/flights.json`, filtre par
  compagnie, alerte visuelle/sonore/vibration quand la page est ouverte, et
  propose d'activer les notifications push.
- `sw.js` (Service Worker) reçoit les notifications push et les affiche au
  niveau du téléphone, même si l'app n'est pas ouverte.
- `worker/subscribe-worker.js` est un petit Cloudflare Worker qui stocke la
  liste des téléphones abonnés — brique nécessaire car GitHub Pages est un
  hébergement 100 % statique et ne peut pas recevoir de requêtes
  d'inscription.

Aucun serveur permanent à maintenir : tout tourne sur les infrastructures
gratuites GitHub Actions + Pages + Cloudflare Workers.

## Installation — partie 1 : l'app elle-même

1. **Créer le dépôt** et pousser ce dossier :

   ```bash
   git init
   git add .
   git commit -m "Première version"
   git branch -M main
   git remote add origin https://github.com/<votre-compte>/<votre-repo>.git
   git push -u origin main
   ```

2. **Activer GitHub Pages.**
   *Settings* → *Pages* → *Build and deployment* → *Source* : **Deploy from a
   branch**, branche `main`, dossier `/ (root)`. L'app sera disponible à
   `https://<votre-compte>.github.io/<votre-repo>/`.

3. **Vérifier le scraping.**
   Onglet *Actions* → job « Mise à jour des vols » → bouton *Run workflow*
   pour un premier test manuel.

⚠️ **Le déclenchement automatique toutes les 5 minutes (`schedule:` dans le
workflow) n'est PAS fiable à cette fréquence chez GitHub** — c'est documenté
par GitHub lui-même, et constaté en usage réel : les écarts entre deux
exécutions automatiques peuvent aller de 20 minutes à plus de 2 heures selon
la charge de leurs serveurs partagés gratuits. Voir la section suivante pour
la corriger.

À ce stade, l'app fonctionne déjà avec les alertes visuelles/sonores tant
qu'elle reste ouverte à l'écran (voir plus haut dans la conversation), mais
les données ne se rafraîchiront pas de façon fiable tant que la section
suivante n'est pas mise en place.

## Installation — partie 1bis : fiabiliser le rafraîchissement toutes les 5 minutes

Le `schedule:` de GitHub Actions étant peu fiable à haute fréquence, on
utilise à la place les **Cron Triggers de Cloudflare Workers** (beaucoup plus
précis) comme horloge externe : toutes les 5 minutes, le Worker appelle
l'API GitHub pour déclencher le workflow — lequel s'exécute alors
quasi instantanément, comme on l'a constaté avec les déclenchements manuels.

C'est le même Worker Cloudflare que celui utilisé plus bas pour les
notifications push (dossier `worker/`) — vous pouvez faire cette partie
**sans** activer les notifications push, les deux fonctionnalités sont
indépendantes.

### Étape A — Créer un token GitHub pour déclencher le workflow

1. github.com → photo de profil (en haut à droite) → **Settings** →
   tout en bas du menu de gauche, **Developer settings**.
2. **Personal access tokens** → **Fine-grained tokens** → **Generate new
   token**.
3. Donnez-lui un nom (ex. `tlp-taxi-cron-trigger`), une expiration (1 an par
   exemple), et sous **Repository access**, choisissez **Only select
   repositories** → sélectionnez votre dépôt `tlp-taxi-app`.
4. Sous **Permissions** → **Repository permissions** → réglez **Actions**
   sur **Read and write**.
5. **Generate token**, puis copiez-le immédiatement (il ne sera plus
   affiché ensuite).

### Étape B — Déployer (ou mettre à jour) le Worker Cloudflare

Si vous n'avez pas encore de compte Cloudflare / Worker déployé, suivez les
étapes A à B de la section suivante (notifications push) pour la création du
compte, l'installation de `wrangler` et la création du namespace KV — c'est
la même base technique. Une fois cette base en place (ou si elle existe
déjà) :

```bash
cd worker
npx wrangler secret put GITHUB_DISPATCH_TOKEN
# collez le token généré à l'étape A quand demandé

npx wrangler deploy
```

Le fichier `wrangler.toml` contient déjà `GITHUB_OWNER`, `GITHUB_REPO` et le
Cron Trigger toutes les 5 minutes — rien d'autre à configurer.

### Étape C — Vérifier que ça tourne

Dans le tableau de bord Cloudflare → Workers & Pages → `tlp-taxi-push` →
onglet **Triggers**, vous devez voir le cron listé et actif. Après 5-10
minutes, retournez dans l'onglet *Actions* de votre dépôt GitHub : de
nouvelles exécutions "Scheduled" doivent apparaître à intervalles réguliers
et rapprochés (proches de 5 minutes, avec une petite marge).

Le `schedule:` resté dans le workflow GitHub sert désormais de filet de
sécurité en cas d'indisponibilité du Worker — inutile de le retirer.

## Installation — partie 2 : les notifications push

### Étape A — Générer les clés VAPID

Les clés VAPID identifient votre app auprès des services de notification
(Apple/Google/Mozilla). Une seule paire à générer, une fois :

```bash
npx web-push generate-vapid-keys
```

Vous obtenez une **clé publique** et une **clé privée**. Gardez les deux de
côté.

### Étape B — Déployer le Cloudflare Worker

1. Créer un compte Cloudflare (gratuit, pas de carte bancaire requise pour
   les Workers/KV en usage standard).
2. Installer Wrangler (CLI Cloudflare) : `npm install -g wrangler`, puis
   `wrangler login`.
3. Depuis le dossier `worker/` :

   ```bash
   cd worker
   npx wrangler kv namespace create SUBSCRIPTIONS_KV
   ```

   Copiez l'`id` retourné dans `wrangler.toml` (remplacez
   `REMPLACER_PAR_ID_KV_NAMESPACE`).

4. Définir la clé d'administration (choisissez une chaîne aléatoire longue,
   ce sera aussi un secret GitHub plus bas) :

   ```bash
   npx wrangler secret put ADMIN_KEY
   ```

5. Déployer :

   ```bash
   npx wrangler deploy
   ```

   Vous obtenez une URL du type
   `https://tlp-taxi-push.<votre-compte>.workers.dev`.

### Étape C — Configurer les secrets GitHub Actions

Dans le dépôt GitHub → *Settings* → *Secrets and variables* → *Actions* →
*New repository secret*, ajouter :

| Nom | Valeur |
|---|---|
| `VAPID_PRIVATE_KEY` | clé privée générée à l'étape A |
| `VAPID_PUBLIC_KEY` | clé publique générée à l'étape A |
| `VAPID_CLAIMS_EMAIL` | ex. `mailto:vous@example.com` |
| `WORKER_BASE_URL` | l'URL du Worker déployé à l'étape B |
| `WORKER_ADMIN_KEY` | la même valeur que `ADMIN_KEY` de l'étape B.4 |

### Étape D — Relier l'app cliente au Worker

Dans `index.html`, remplir la constante `PUSH_CONFIG` (recherchez-la en
début de `<script>`) :

```js
const PUSH_CONFIG = {
  vapidPublicKey: 'COLLER_LA_CLE_PUBLIQUE_VAPID_ICI',
  workerSubscribeUrl: 'https://tlp-taxi-push.<votre-compte>.workers.dev/subscribe',
};
```

Puis commitez et poussez ce changement. Le bouton "Notifs" dans l'app
devient alors actif.

### Étape E — Installer et activer côté chauffeur

1. Ouvrir l'URL GitHub Pages sur le téléphone.
2. **Ajouter à l'écran d'accueil** (obligatoire sur iPhone pour que le push
   fonctionne ; recommandé aussi sur Android).
3. Ouvrir l'app depuis l'icône ajoutée, taper sur **"Notifs off"** dans
   l'en-tête, puis **autoriser** la demande de permission qui apparaît.
4. Le bouton passe à **"🔔 Notifs on"** — c'est prêt.

⚠️ Voir les contraintes détaillées (iOS 16.4+ requis, app installée
obligatoire sur iPhone, gestion batterie) discutées plus haut — testez avec
un vrai téléphone de chaque type (Android + iPhone) avant un déploiement à
toute l'équipe.

## ⚠️ Point important à vérifier avant mise en production

Ce scraper a été écrit à partir du **texte visible** de la page (motif :
ville → date/heure → compagnie + n° de vol → statut optionnel), car je n'ai
pas eu accès au HTML brut ni aux classes CSS réelles du site depuis
l'environnement où ce projet a été généré. La logique a été testée avec
succès sur le contenu réel de la page (récupéré via recherche web), ainsi que
la logique de diff (détection de changement de statut), mais **le pipeline
complet de notification (Worker Cloudflare + envoi push) n'a pas pu être
testé de bout en bout en conditions réelles** — je n'ai pas d'accès réseau
sortant vers Cloudflare ni vers les services de notification (Apple/Google)
depuis mon environnement d'exécution. Testez l'ensemble avec un vol de test
ou en modifiant temporairement un statut dans `data/flights.json` avant de
compter dessus en conditions réelles.

Si le job de scraping échoue ou renvoie 0 vol après un vrai changement de
page, le script n'écrase pas le fichier existant (voir la fonction `main()`
dans `scripts/scrape.py`) — vous aurez juste un `flights.json` qui ne se
rafraîchit plus, avec une alerte visuelle dans l'app (pastille orange
"données anciennes").

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
- **Robustesse du scraping** : si l'aéroport modifie la page (nouveau design,
  nouveaux libellés de statut, formulation différente de "en avance"), le
  parseur texte peut nécessiter une mise à jour dans
  `scripts/scrape.py` (dictionnaire `STATUS_KEYWORDS`).
- **Nettoyage des abonnements expirés** : `send_push.py` retire
  automatiquement un abonnement du Worker si l'envoi échoue avec 404/410
  (app désinstallée, permission retirée), donc le KV Cloudflare reste propre
  sans intervention manuelle.
- **iOS** : nécessite iOS 16.4+ et l'app installée sur l'écran d'accueil pour
  recevoir les push (limite d'Apple, pas de ce projet).
