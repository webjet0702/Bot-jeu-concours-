// Assistant concours : envoie chaque jour tes Google Alertes (libellé Gmail « Concours ») à ton site.
// Aucun mot de passe : ce script tourne dans TON compte Google. Il lit seulement les emails du
// libellé ci-dessous et envoie leur contenu à ton site, qui en extrait les liens de concours.
//
// Installation : 1) Enregistrer (Ctrl+S)  2) en haut, choisir « installer »  3) ▶ Exécuter  4) Autoriser.
// Pour renvoyer tout de suite : choisir « envoyerAlertes » puis ▶ Exécuter.

const SITE = '{{ site }}';
const CLE = '{{ cle }}';           // clé d'import de TON site (ne la partage pas)
const LIBELLE = 'Concours';        // le libellé Gmail de tes Google Alertes
const JOURS = 7;                   // renvoie les alertes des 7 derniers jours (le site ignore les doublons)
const PAR_ENVOI = 5;               // emails par envoi
const TAILLE_MAX = 300000;         // un email plus gros est ignoré

function installer() {
  ScriptApp.getProjectTriggers().forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('envoyerAlertes').timeBased().everyDays(1).atHour(5).create();
  envoyerAlertes();
}

function envoyerAlertes() {
  if (!GmailApp.getUserLabelByName(LIBELLE)) {
    throw new Error('Le libellé « ' + LIBELLE + ' » n\'existe pas dans ce compte Gmail. ' +
                    'Es-tu sur ton Gmail dédié ? As-tu créé le filtre des Google Alertes ?');
  }
  var depuis = Date.now() - JOURS * 24 * 3600 * 1000;
  var fils = GmailApp.search('label:' + LIBELLE.replace(/ /g, '-') + ' newer_than:' + JOURS + 'd', 0, 100);
  var emails = [];
  fils.forEach(function (fil) {
    fil.getMessages().forEach(function (message) {
      if (message.getDate().getTime() < depuis) return;
      var brut = message.getRawContent();
      if (brut.length <= TAILLE_MAX) emails.push(brut);
    });
  });
  Logger.log('Libellé « ' + LIBELLE + ' » : ' + emails.length + ' email(s) des ' + JOURS + ' derniers jours.');
  var lots = [];
  for (var i = 0; i < emails.length; i += PAR_ENVOI) lots.push(emails.slice(i, i + PAR_ENVOI));
  if (!lots.length) lots.push([]);  // on prévient quand même le site : il saura que le script marche
  var nouveaux = 0;
  lots.forEach(function (lot) {
    var reponse = UrlFetchApp.fetch(SITE + '/api/importer', {
      method: 'post',
      contentType: 'application/json',
      headers: { Authorization: 'Bearer ' + CLE },
      payload: JSON.stringify({ emails: lot }),
      muteHttpExceptions: true
    });
    if (reponse.getResponseCode() !== 200) {
      throw new Error('Le site a répondu ' + reponse.getResponseCode() + ' : ' + reponse.getContentText().slice(0, 300));
    }
    nouveaux += JSON.parse(reponse.getContentText()).nouveaux;
  });
  Logger.log('Envoyé au site. Nouveaux concours : ' + nouveaux + '.');
}
