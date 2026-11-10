// Assistant concours : envoie chaque jour tes Google Alertes (libellé Gmail « Concours ») à ton site.
// Aucun mot de passe : ce script tourne dans TON compte Google. Il lit seulement les emails du
// libellé ci-dessous et envoie leur contenu à ton site, qui en extrait les liens de concours.
//
// Installation : 1) Enregistrer (Ctrl+S)  2) en haut, choisir « installer »  3) ▶ Exécuter  4) Autoriser.

const SITE = '{{ site }}';
const CLE = '{{ cle }}';           // clé d'import de TON site (ne la partage pas)
const LIBELLE = 'Concours';        // le libellé Gmail de tes Google Alertes
const JOURS = 7;                   // premier lancement : les alertes des 7 derniers jours
const PAR_ENVOI = 5;               // emails par envoi
const TAILLE_MAX = 300000;         // un email plus gros est ignoré

function installer() {
  ScriptApp.getProjectTriggers().forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('envoyerAlertes').timeBased().everyDays(1).atHour(5).create();
  envoyerAlertes();
}

function envoyerAlertes() {
  var proprietes = PropertiesService.getScriptProperties();
  var depuis = Number(proprietes.getProperty('dernier_envoi')) || (Date.now() - JOURS * 24 * 3600 * 1000);
  var debut = Date.now();
  var fils = GmailApp.search('label:' + LIBELLE.replace(/ /g, '-') + ' after:' + Math.floor(depuis / 1000), 0, 100);
  var emails = [];
  fils.forEach(function (fil) {
    fil.getMessages().forEach(function (message) {
      if (message.getDate().getTime() < depuis) return;
      var brut = message.getRawContent();
      if (brut.length <= TAILLE_MAX) emails.push(brut);
    });
  });
  var nouveaux = 0;
  for (var i = 0; i < emails.length; i += PAR_ENVOI) {
    var reponse = UrlFetchApp.fetch(SITE + '/api/importer', {
      method: 'post',
      contentType: 'application/json',
      headers: { Authorization: 'Bearer ' + CLE },
      payload: JSON.stringify({ emails: emails.slice(i, i + PAR_ENVOI) }),
      muteHttpExceptions: true
    });
    if (reponse.getResponseCode() !== 200) {
      throw new Error('Le site a répondu ' + reponse.getResponseCode() + ' : ' + reponse.getContentText().slice(0, 300));
    }
    nouveaux += JSON.parse(reponse.getContentText()).nouveaux;
  }
  proprietes.setProperty('dernier_envoi', String(debut));
  Logger.log(emails.length + ' email(s) envoyé(s) au site, ' + nouveaux + ' nouveau(x) concours.');
}
