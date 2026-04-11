"""
models.py — Modèles SQLAlchemy (déclaratif).

Usage coexistant avec SQLite brut :
    from models import db, Appareil
    db.init_app(app)          # une seule fois au démarrage
    # Ensuite : Appareil.query.filter_by(client_id=cid).all()

Les modèles reflètent exactement le schéma créé par init_db().
Les colonnes ajoutées par migration sont marquées nullable=True.
"""
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


# ─── CLIENTS ──────────────────────────────────────────────────────────────────

class Client(db.Model):
    __tablename__ = 'clients'

    id             = db.Column(db.Integer, primary_key=True)
    nom            = db.Column(db.Text, nullable=False, default='')
    contact        = db.Column(db.Text, default='')
    telephone      = db.Column(db.Text, default='')
    email          = db.Column(db.Text, default='')
    adresse        = db.Column(db.Text, default='')
    notes          = db.Column(db.Text, default='')
    couleur        = db.Column(db.Text, default='#00c9ff')
    date_creation  = db.Column(db.Text, default='')
    date_maj       = db.Column(db.Text, default='')
    auth_user_id   = db.Column(db.Integer, db.ForeignKey('auth_users.id'), nullable=True)

    appareils      = db.relationship('Appareil',     back_populates='client', cascade='all, delete-orphan')
    services       = db.relationship('Service',      back_populates='client', cascade='all, delete-orphan')
    utilisateurs   = db.relationship('Utilisateur',  back_populates='client', cascade='all, delete-orphan')
    identifiants   = db.relationship('Identifiant',  back_populates='client', cascade='all, delete-orphan')
    peripheriques  = db.relationship('Peripherique', back_populates='client', cascade='all, delete-orphan')
    contrats       = db.relationship('Contrat',      back_populates='client', cascade='all, delete-orphan')
    partages       = db.relationship('ClientPartage', back_populates='client', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Client {self.id} {self.nom!r}>'


# ─── AUTHENTIFICATION ─────────────────────────────────────────────────────────

class AuthUser(db.Model):
    __tablename__ = 'auth_users'

    id                   = db.Column(db.Integer, primary_key=True)
    login                = db.Column(db.Text, nullable=False, unique=True)
    password_hash        = db.Column(db.Text, nullable=False)
    nom                  = db.Column(db.Text, nullable=False)
    prenom               = db.Column(db.Text, default='')
    email                = db.Column(db.Text, default='')
    role                 = db.Column(db.Text, default='user')
    logo_fichier         = db.Column(db.Text, default='')
    actif                = db.Column(db.Integer, default=1)
    must_change_password = db.Column(db.Integer, default=0)  # migration
    date_creation        = db.Column(db.Text)
    date_maj             = db.Column(db.Text)

    clients  = db.relationship('Client',       back_populates='owner',   foreign_keys=[Client.auth_user_id])
    partages = db.relationship('ClientPartage', back_populates='user',   cascade='all, delete-orphan')

    def __repr__(self):
        return f'<AuthUser {self.login!r} role={self.role!r}>'


# Fix circular ref : Client.owner
Client.owner = db.relationship('AuthUser', back_populates='clients', foreign_keys=[Client.auth_user_id])


class ClientPartage(db.Model):
    __tablename__ = 'client_partages'
    __table_args__ = (db.UniqueConstraint('client_id', 'auth_user_id'),)

    id           = db.Column(db.Integer, primary_key=True)
    client_id    = db.Column(db.Integer, db.ForeignKey('clients.id', ondelete='CASCADE'), nullable=False)
    auth_user_id = db.Column(db.Integer, db.ForeignKey('auth_users.id', ondelete='CASCADE'), nullable=False)
    niveau       = db.Column(db.Text, default='lecture')
    date_partage = db.Column(db.Text)

    client = db.relationship('Client',   back_populates='partages')
    user   = db.relationship('AuthUser', back_populates='partages')


# ─── PARC GÉNÉRAL ─────────────────────────────────────────────────────────────

class ParcGeneral(db.Model):
    __tablename__ = 'parc_general'

    id                  = db.Column(db.Integer, primary_key=True)
    client_id           = db.Column(db.Integer, db.ForeignKey('clients.id', ondelete='CASCADE'), nullable=False)
    nom_site            = db.Column(db.Text, default='')
    adresse             = db.Column(db.Text, default='')
    type_connexion      = db.Column(db.Text, default='')
    debit_montant       = db.Column(db.Text, default='')
    debit_descendant    = db.Column(db.Text, default='')
    fournisseur_internet= db.Column(db.Text, default='')
    ip_publique         = db.Column(db.Text, default='')
    plage_ip_locale     = db.Column(db.Text, default='192.168.1.0/24')
    nb_machines         = db.Column(db.Integer, default=0)
    nb_utilisateurs     = db.Column(db.Integer, default=0)
    domaine             = db.Column(db.Text, default='')
    serveur_dns         = db.Column(db.Text, default='')
    passerelle          = db.Column(db.Text, default='')
    baie_marque         = db.Column(db.Text, default='')
    baie_nb_u           = db.Column(db.Integer, default=0)
    switch_marque       = db.Column(db.Text, default='')
    switch_nb_ports     = db.Column(db.Integer, default=0)
    switch_nb_unites    = db.Column(db.Integer, default=0)
    routeur_marque      = db.Column(db.Text, default='')
    serveur_marque      = db.Column(db.Text, default='')
    serveur_modele      = db.Column(db.Text, default='')
    ups_marque          = db.Column(db.Text, default='')
    ups_capacite        = db.Column(db.Text, default='')
    autres_equipements  = db.Column(db.Text, default='')
    logiciels_metier    = db.Column(db.Text, default='')
    antivirus           = db.Column(db.Text, default='')
    os_principal        = db.Column(db.Text, default='')
    suite_bureautique   = db.Column(db.Text, default='')
    notes               = db.Column(db.Text, default='')
    date_maj            = db.Column(db.Text, default='')
    # Colonnes WiFi (migration)
    wifi_ssid           = db.Column(db.Text, default='')
    wifi_password       = db.Column(db.Text, default='')
    wifi_securite       = db.Column(db.Text, default='WPA2')
    wifi_ssid2          = db.Column(db.Text, default='')
    wifi_password2      = db.Column(db.Text, default='')
    wifi_securite2      = db.Column(db.Text, default='WPA2')
    wifi_notes          = db.Column(db.Text, default='')

    client = db.relationship('Client')


# ─── APPAREILS ────────────────────────────────────────────────────────────────

class Appareil(db.Model):
    __tablename__ = 'appareils'

    id               = db.Column(db.Integer, primary_key=True)
    client_id        = db.Column(db.Integer, db.ForeignKey('clients.id', ondelete='CASCADE'), nullable=False)
    nom_machine      = db.Column(db.Text, default='')
    type_appareil    = db.Column(db.Text, default='')
    marque           = db.Column(db.Text, default='')
    modele           = db.Column(db.Text, default='')
    numero_serie     = db.Column(db.Text, default='')
    adresse_ip       = db.Column(db.Text, default='')
    adresse_mac      = db.Column(db.Text, default='')
    nom_dns          = db.Column(db.Text, default='')
    utilisateur      = db.Column(db.Text, default='')
    service          = db.Column(db.Text, default='')
    localisation     = db.Column(db.Text, default='')
    date_achat       = db.Column(db.Text, default='')
    duree_garantie   = db.Column(db.Integer, default=0)
    date_fin_garantie= db.Column(db.Text, default='')
    fournisseur      = db.Column(db.Text, default='')
    prix_achat       = db.Column(db.Real)
    numero_commande  = db.Column(db.Text, default='')
    os               = db.Column(db.Text, default='')
    version_os       = db.Column(db.Text, default='')
    ram              = db.Column(db.Text, default='')
    cpu              = db.Column(db.Text, default='')
    stockage         = db.Column(db.Text, default='')
    statut           = db.Column(db.Text, default='actif')
    dernier_ping     = db.Column(db.Text, default='')
    en_ligne         = db.Column(db.Integer, default=0)
    decouvert_scan   = db.Column(db.Integer, default=0)
    ports_ouverts    = db.Column(db.Text, default='')
    notes            = db.Column(db.Text, default='')
    date_creation    = db.Column(db.Text, default='')
    date_maj         = db.Column(db.Text, default='')
    user_login       = db.Column(db.Text, default='')
    user_password    = db.Column(db.Text, default='')
    admin_login      = db.Column(db.Text, default='')
    admin_password   = db.Column(db.Text, default='')
    anydesk_id       = db.Column(db.Text, default='')
    anydesk_password = db.Column(db.Text, default='')
    carte_graphique  = db.Column(db.Text, default='')  # migration

    client       = db.relationship('Client',      back_populates='appareils')
    peripheriques= db.relationship('Peripherique', back_populates='appareil')
    documents    = db.relationship('DocumentAppareil', back_populates='appareil', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Appareil {self.id} {self.nom_machine!r}>'


# ─── SERVICES ─────────────────────────────────────────────────────────────────

class Service(db.Model):
    __tablename__ = 'services'

    id            = db.Column(db.Integer, primary_key=True)
    client_id     = db.Column(db.Integer, db.ForeignKey('clients.id', ondelete='CASCADE'), nullable=False)
    nom           = db.Column(db.Text, nullable=False, default='')
    description   = db.Column(db.Text, default='')
    responsable   = db.Column(db.Text, default='')
    couleur       = db.Column(db.Text, default='#6a8aaa')
    ordre         = db.Column(db.Integer, default=0)
    date_creation = db.Column(db.Text, default='')
    date_maj      = db.Column(db.Text, default='')

    client       = db.relationship('Client',      back_populates='services')
    utilisateurs = db.relationship('Utilisateur', back_populates='service')


# ─── UTILISATEURS ─────────────────────────────────────────────────────────────

class Utilisateur(db.Model):
    __tablename__ = 'utilisateurs'

    id            = db.Column(db.Integer, primary_key=True)
    client_id     = db.Column(db.Integer, db.ForeignKey('clients.id', ondelete='CASCADE'), nullable=False)
    service_id    = db.Column(db.Integer, db.ForeignKey('services.id', ondelete='SET NULL'), nullable=True)
    prenom        = db.Column(db.Text, default='')
    nom           = db.Column(db.Text, default='')
    poste         = db.Column(db.Text, default='')
    email         = db.Column(db.Text, default='')
    telephone     = db.Column(db.Text, default='')
    login_windows = db.Column(db.Text, default='')
    login_mail    = db.Column(db.Text, default='')
    statut        = db.Column(db.Text, default='actif')
    notes         = db.Column(db.Text, default='')
    date_creation = db.Column(db.Text, default='')
    date_maj      = db.Column(db.Text, default='')

    client  = db.relationship('Client',  back_populates='utilisateurs')
    service = db.relationship('Service', back_populates='utilisateurs')
    droits  = db.relationship('DroitUtilisateur', back_populates='utilisateur', cascade='all, delete-orphan')


# ─── DROITS UTILISATEURS ──────────────────────────────────────────────────────

class TypeDroit(db.Model):
    __tablename__ = 'types_droits'

    id          = db.Column(db.Integer, primary_key=True)
    client_id   = db.Column(db.Integer, db.ForeignKey('clients.id', ondelete='CASCADE'), nullable=False)
    categorie   = db.Column(db.Text, default='')
    nom         = db.Column(db.Text, nullable=False, default='')
    description = db.Column(db.Text, default='')
    icone       = db.Column(db.Text, default='🔑')
    ordre       = db.Column(db.Integer, default=0)

    droits = db.relationship('DroitUtilisateur', back_populates='type_droit')


class DroitUtilisateur(db.Model):
    __tablename__ = 'droits_utilisateurs'

    id               = db.Column(db.Integer, primary_key=True)
    utilisateur_id   = db.Column(db.Integer, db.ForeignKey('utilisateurs.id', ondelete='CASCADE'), nullable=False)
    client_id        = db.Column(db.Integer, nullable=False)
    categorie        = db.Column(db.Text, default='')
    type_droit_id    = db.Column(db.Integer, db.ForeignKey('types_droits.id', ondelete='SET NULL'), nullable=True)
    nom_droit        = db.Column(db.Text, default='')
    valeur           = db.Column(db.Text, default='')
    niveau           = db.Column(db.Text, default='lecture')
    notes            = db.Column(db.Text, default='')
    date_attribution = db.Column(db.Text, default='')

    utilisateur = db.relationship('Utilisateur', back_populates='droits')
    type_droit  = db.relationship('TypeDroit',   back_populates='droits')


# ─── IDENTIFIANTS ─────────────────────────────────────────────────────────────

class Identifiant(db.Model):
    __tablename__ = 'identifiants'

    id              = db.Column(db.Integer, primary_key=True)
    client_id       = db.Column(db.Integer, db.ForeignKey('clients.id', ondelete='CASCADE'), nullable=False)
    categorie       = db.Column(db.Text, default='')
    nom             = db.Column(db.Text, default='')
    login           = db.Column(db.Text, default='')
    mot_de_passe    = db.Column(db.Text, default='')
    url             = db.Column(db.Text, default='')
    description     = db.Column(db.Text, default='')
    notes           = db.Column(db.Text, default='')
    date_expiration = db.Column(db.Text, default='')
    date_creation   = db.Column(db.Text, default='')
    date_maj        = db.Column(db.Text, default='')
    # Colonnes Wi-Fi (migration)
    wifi_ssid       = db.Column(db.Text, default='')
    wifi_securite   = db.Column(db.Text, default='WPA2')

    client = db.relationship('Client', back_populates='identifiants')


# ─── PÉRIPHÉRIQUES ────────────────────────────────────────────────────────────

class Peripherique(db.Model):
    __tablename__ = 'peripheriques'

    id               = db.Column(db.Integer, primary_key=True)
    client_id        = db.Column(db.Integer, db.ForeignKey('clients.id', ondelete='CASCADE'), nullable=False)
    appareil_id      = db.Column(db.Integer, db.ForeignKey('appareils.id', ondelete='SET NULL'), nullable=True)
    utilisateur_id   = db.Column(db.Integer, db.ForeignKey('utilisateurs.id', ondelete='SET NULL'), nullable=True)
    categorie        = db.Column(db.Text, default='')
    marque           = db.Column(db.Text, default='')
    modele           = db.Column(db.Text, default='')
    numero_serie     = db.Column(db.Text, default='')
    description      = db.Column(db.Text, default='')
    localisation     = db.Column(db.Text, default='')
    statut           = db.Column(db.Text, default='actif')
    date_achat       = db.Column(db.Text, default='')
    duree_garantie   = db.Column(db.Integer, default=0)
    date_fin_garantie= db.Column(db.Text, default='')
    fournisseur      = db.Column(db.Text, default='')
    prix_achat       = db.Column(db.Real)
    numero_commande  = db.Column(db.Text, default='')
    notes            = db.Column(db.Text, default='')
    date_creation    = db.Column(db.Text, default='')
    date_maj         = db.Column(db.Text, default='')

    client     = db.relationship('Client',      back_populates='peripheriques')
    appareil   = db.relationship('Appareil',    back_populates='peripheriques')
    utilisateur= db.relationship('Utilisateur')
    documents  = db.relationship('DocumentPeripherique', back_populates='peripherique', cascade='all, delete-orphan')


# ─── CONTRATS ─────────────────────────────────────────────────────────────────

class Contrat(db.Model):
    __tablename__ = 'contrats'

    id                    = db.Column(db.Integer, primary_key=True)
    client_id             = db.Column(db.Integer, db.ForeignKey('clients.id', ondelete='CASCADE'), nullable=False)
    titre                 = db.Column(db.Text, default='')
    type_contrat          = db.Column(db.Text, default='')
    fournisseur           = db.Column(db.Text, default='')
    contact_fournisseur   = db.Column(db.Text, default='')
    email_fournisseur     = db.Column(db.Text, default='')
    telephone_fournisseur = db.Column(db.Text, default='')
    numero_contrat        = db.Column(db.Text, default='')
    date_debut            = db.Column(db.Text, default='')
    date_fin              = db.Column(db.Text, default='')
    reconduction_auto     = db.Column(db.Integer, default=0)
    preavis_jours         = db.Column(db.Integer, default=30)
    montant_ht            = db.Column(db.Real)
    periodicite           = db.Column(db.Text, default='annuel')
    description           = db.Column(db.Text, default='')
    notes                 = db.Column(db.Text, default='')
    statut                = db.Column(db.Text, default='actif')
    date_creation         = db.Column(db.Text, default='')
    date_maj              = db.Column(db.Text, default='')

    client    = db.relationship('Client', back_populates='contrats')
    documents = db.relationship('DocumentContrat', back_populates='contrat', cascade='all, delete-orphan')

    appareils    = db.relationship('Appareil',     secondary='contrats_appareils',    viewonly=True)
    peripheriques= db.relationship('Peripherique', secondary='contrats_peripheriques', viewonly=True)


# Tables pivot contrats <-> appareils / périphériques
contrats_appareils = db.Table(
    'contrats_appareils',
    db.Column('id',          db.Integer, primary_key=True),
    db.Column('contrat_id',  db.Integer, db.ForeignKey('contrats.id',     ondelete='CASCADE'), nullable=False),
    db.Column('appareil_id', db.Integer, db.ForeignKey('appareils.id',    ondelete='CASCADE'), nullable=False),
)

contrats_peripheriques = db.Table(
    'contrats_peripheriques',
    db.Column('id',              db.Integer, primary_key=True),
    db.Column('contrat_id',      db.Integer, db.ForeignKey('contrats.id',       ondelete='CASCADE'), nullable=False),
    db.Column('peripherique_id', db.Integer, db.ForeignKey('peripheriques.id',  ondelete='CASCADE'), nullable=False),
)


# ─── DOCUMENTS ────────────────────────────────────────────────────────────────

class DocumentContrat(db.Model):
    __tablename__ = 'documents_contrats'

    id           = db.Column(db.Integer, primary_key=True)
    contrat_id   = db.Column(db.Integer, db.ForeignKey('contrats.id',  ondelete='CASCADE'), nullable=False)
    client_id    = db.Column(db.Integer, db.ForeignKey('clients.id',   ondelete='CASCADE'), nullable=False)
    nom          = db.Column(db.Text, default='')
    description  = db.Column(db.Text, default='')
    type_doc     = db.Column(db.Text, default='')
    nom_fichier  = db.Column(db.Text, default='')
    taille       = db.Column(db.Integer, default=0)
    date_upload  = db.Column(db.Text, default='')

    contrat = db.relationship('Contrat', back_populates='documents')


class DocumentAppareil(db.Model):
    __tablename__ = 'documents_appareils'

    id          = db.Column(db.Integer, primary_key=True)
    appareil_id = db.Column(db.Integer, db.ForeignKey('appareils.id', ondelete='CASCADE'), nullable=False)
    client_id   = db.Column(db.Integer, db.ForeignKey('clients.id',   ondelete='CASCADE'), nullable=False)
    nom         = db.Column(db.Text, default='')
    description = db.Column(db.Text, default='')
    type_doc    = db.Column(db.Text, default='')
    nom_fichier = db.Column(db.Text, default='')
    taille      = db.Column(db.Integer, default=0)
    date_upload = db.Column(db.Text, default='')

    appareil = db.relationship('Appareil', back_populates='documents')


class DocumentPeripherique(db.Model):
    __tablename__ = 'documents_peripheriques'

    id              = db.Column(db.Integer, primary_key=True)
    peripherique_id = db.Column(db.Integer, db.ForeignKey('peripheriques.id', ondelete='CASCADE'), nullable=False)
    client_id       = db.Column(db.Integer, db.ForeignKey('clients.id',       ondelete='CASCADE'), nullable=False)
    nom             = db.Column(db.Text, default='')
    description     = db.Column(db.Text, default='')
    type_doc        = db.Column(db.Text, default='')
    nom_fichier     = db.Column(db.Text, default='')
    taille          = db.Column(db.Integer, default=0)
    date_upload     = db.Column(db.Text, default='')

    peripherique = db.relationship('Peripherique', back_populates='documents')


# ─── BASE DE CONNAISSANCES ────────────────────────────────────────────────────

class KbCategorie(db.Model):
    __tablename__ = 'kb_categories'

    id       = db.Column(db.Integer, primary_key=True)
    nom      = db.Column(db.Text, nullable=False)
    icone    = db.Column(db.Text, default='📋')
    ordre    = db.Column(db.Integer, default=0)

    articles = db.relationship('KbArticle', back_populates='categorie', cascade='all, delete-orphan')


class KbArticle(db.Model):
    __tablename__ = 'kb_articles'

    id            = db.Column(db.Integer, primary_key=True)
    categorie_id  = db.Column(db.Integer, db.ForeignKey('kb_categories.id'), nullable=False)
    titre         = db.Column(db.Text, nullable=False)
    contenu       = db.Column(db.Text, nullable=False)
    tags          = db.Column(db.Text, default='')
    date_creation = db.Column(db.Text)
    date_maj      = db.Column(db.Text)

    categorie = db.relationship('KbCategorie', back_populates='articles')


# ─── HISTORIQUE ───────────────────────────────────────────────────────────────

class Historique(db.Model):
    __tablename__ = 'historique'

    id          = db.Column(db.Integer, primary_key=True)
    client_id   = db.Column(db.Integer, nullable=False)
    entite      = db.Column(db.Text, nullable=False)
    entite_id   = db.Column(db.Integer, nullable=False)
    entite_nom  = db.Column(db.Text, default='')
    action      = db.Column(db.Text, nullable=False)
    date_action = db.Column(db.Text, nullable=False)
    details     = db.Column(db.Text, default='')


# ─── OUTILS ───────────────────────────────────────────────────────────────────

class Outil(db.Model):
    __tablename__ = 'outils'

    id          = db.Column(db.Integer, primary_key=True)
    nom         = db.Column(db.Text, nullable=False)
    url         = db.Column(db.Text, nullable=False)
    description = db.Column(db.Text, default='')
    categorie   = db.Column(db.Text, default='Général')
    icone       = db.Column(db.Text, default='🔧')
    ordre       = db.Column(db.Integer, default=0)
    actif       = db.Column(db.Integer, default=1)


# ─── BAIE DE BRASSAGE ─────────────────────────────────────────────────────────

class BaieSlot(db.Model):
    __tablename__ = 'baie_slots'

    id               = db.Column(db.Integer, primary_key=True)
    client_id        = db.Column(db.Integer, db.ForeignKey('clients.id', ondelete='CASCADE'), nullable=False)
    position         = db.Column(db.Integer, nullable=False)
    col_index        = db.Column(db.Integer, default=0)
    hauteur_u        = db.Column(db.Integer, default=1)
    appareil_id      = db.Column(db.Integer, db.ForeignKey('appareils.id', ondelete='SET NULL'), nullable=True)
    nom_custom       = db.Column(db.Text, default='')
    type_equipement  = db.Column(db.Text, default='')
    couleur          = db.Column(db.Text, default='#1e3a5f')
    description      = db.Column(db.Text, default='')
    baie_nom         = db.Column(db.Text, default='Baie principale')

    client   = db.relationship('Client')
    appareil = db.relationship('Appareil')


class BaiePhoto(db.Model):
    __tablename__ = 'baie_photos'

    id          = db.Column(db.Integer, primary_key=True)
    client_id   = db.Column(db.Integer, db.ForeignKey('clients.id', ondelete='CASCADE'), nullable=False)
    nom         = db.Column(db.Text, default='')
    description = db.Column(db.Text, default='')
    nom_fichier = db.Column(db.Text, default='')
    taille      = db.Column(db.Integer, default=0)
    date_upload = db.Column(db.Text, default='')

    client = db.relationship('Client')


# ─── CONFIG ───────────────────────────────────────────────────────────────────

class Config(db.Model):
    __tablename__ = 'config'

    cle      = db.Column(db.Text, primary_key=True)
    valeur   = db.Column(db.Text, default='')
    date_maj = db.Column(db.Text, default='')


class ConfigListe(db.Model):
    __tablename__ = 'config_listes'
    __table_args__ = (db.UniqueConstraint('nom_liste', 'valeur'),)

    id        = db.Column(db.Integer, primary_key=True)
    nom_liste = db.Column(db.Text, nullable=False)
    valeur    = db.Column(db.Text, nullable=False)
    ordre     = db.Column(db.Integer, default=0)
