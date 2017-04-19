# Objectif Technique

Rendre possible l'annonce de services IP publics au sein d'une architecture orientee service (SOA), constituée autour des conteneurs pour l'execution et d'une topologie en Spine-Leaf pour le réseau.

# Problématiques techniques soulevées

Dans le cadre de sa refonte d'architecture debut 2015, BlaBlaCar a souhaité investir dans 
l'industrialisation de sa plate-forme. Buts:

- réduire les couts d'infrastructure physique (hors Cloud)
- permettre le déploiement par vague
- faire que l'infrastructure soit toujours en avance sur les besoins métiers
- permettre une architecture orientée service

## Conséquences

- banaliser la notion de serveur (on n'achète plus de serveur qui soit dimensionné pour un besoin particulier). Ceci permet de réduire les coûts d'achat (mutualisation, achat par lots), et de diminuer le temps de livraison en production d'un nouveau service (ancticipation des besoins)
  
- organiser la mise en réseau de ces serveurs de manière industrielle. Pas de configuration particulière des équipements réseau (switches, routeurs) pour un serveur donné, topologie en Spine-Leaf / L3 avec BGP pour éviter la constitution d'un trop grand domaine de diffusion avec la croissance d'un datacenter.
  
## Choix effectués

Afin d'opérer ces serveurs de manière industrielle, plusieurs options s'offraient a BlaBlaCar.

Un cloud privé autour de technologies comme VMWare ou OpenStack.
Inconvenients:
- prix des licenses (VMWare)
- courbe d'apprentissage (Openstack)
- overhead lié a la virtualisation

A la place, BlaBlaCar a choisi les conteneurs (écosystème type docker), avec la technologie CoreOS/rkt:
- logiciel libre
- soutenu par de grands acteurs (Google, ...)
- meilleure utilisation des resources physiques des serveurs.

## Problématiques

Dans une architcture SOA, utilsant des conteneurs imutables, une des problématiques majeures est liée 
à la decouverte de service.

Les (micro-)services impliquent un graphe de dépendances entre services qui peut rapidement devenir complexe (taille, cycles dans le graphe). Il faut donc un annuaire de services. BlaBlaCar a retenu ZooKeeper pour jouer ce rôle.

De plus, l'utilisation de BGP entre les serveurs et les switches / routeurs implque de savoir comment annoncer des adresses IP publiques depuis de simples conteneurs, sans appliquer de configuration particulière sur ces derniers.

[Spine-Leaf Architecture: Design Overview White Paper (Cisco)](http://www.cisco.com/c/en/us/products/collateral/switches/nexus-7000-series-switches/white-paper-c11-737022.html)

# Etat de l'art, pourquoi c'est insuffisant

[Redondance avec ExaBGP (Vincent Bernat)](https://vincent.bernat.im/fr/blog/2013-exabgp-haute-dispo)

ExaBGP avec le plugin healtcheck. problemes de MED qui nest pas transitif dans eBGP => 
- a minima un patch healthcheck pour utiliser un autre attribut BGP que la MED
- ne plus utiliser eBGP et lui preferer iBGP => implique une connectivité full-mesh entre tous les routeurs => explosion des couts, maintenance complexe.

De plus, le manque de connexion directe a ZooKeeper implique d'utiliser d'autres techniques (confd) pour savoir quel service annoncer.


D'ou la décision de réaliser un dévelopement spécifique: [ExaZK](https://github.com/shtouff/exazk)

[BGP routing to containers in BlaBlaCar (Rémi Paulmier)](http://blablatech.com/blog/bgp-routing-to-containers)


# Travaux réalisés

- exazk

utilisation de la med toujours
mais: connecté a zookeeper, donc sait ce que doit annoncer et ne PAS annoncer (car annoncé par les autres)



