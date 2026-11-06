// Assistant concours — petits comportements de l'interface (aucune dépendance externe).
(function () {
  "use strict";

  var meta = document.querySelector('meta[name="jeton"]');
  var JETON = meta ? meta.getAttribute("content") : "";

  // Confirmation avant les actions sensibles.
  document.querySelectorAll("form[data-confirmer]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirmer"))) e.preventDefault();
    });
  });

  // Boutons « Copier ».
  document.querySelectorAll("[data-copier]").forEach(function (bouton) {
    bouton.addEventListener("click", function () {
      var texte = bouton.getAttribute("data-copier");
      var fini = function () {
        var avant = bouton.textContent;
        bouton.textContent = "Copié ✓";
        setTimeout(function () { bouton.textContent = avant; }, 1500);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(texte).then(fini, function () {});
      }
    });
  });

  // Recherche / analyse par tranches : chaque appel au serveur dure au plus ~30 s
  // (limite des hébergeurs), la page enchaîne les tranches et affiche le journal en direct.
  var panneau = document.getElementById("progression");
  if (panneau) {
    var log = document.getElementById("progression-log");
    var etat = document.getElementById("progression-etat");
    var TRANCHES_MAX = 15;

    var ecrire = function (lignes) {
      if (!lignes || !lignes.length) return;
      log.textContent += (log.textContent ? "\n" : "") + lignes.join("\n");
      log.scrollTop = log.scrollHeight;
    };
    var appeler = function (url, donnees) {
      return fetch(url, { method: "POST", body: donnees, headers: { "X-Jeton": JETON }, credentials: "same-origin" })
        .then(function (r) {
          if (r.status === 401) { window.location.href = "/connexion"; throw new Error("connexion requise"); }
          return r.json();
        });
    };
    var terminer = function (message) {
      etat.textContent = message;
      etat.className = "pastille pastille-ok";
      setTimeout(function () { window.location.href = window.location.pathname; }, 1800);
    };
    var analyser = function (tranche) {
      etat.textContent = "analyse… (" + tranche + ")";
      var donnees = new FormData();
      donnees.append("jeton", JETON);
      return appeler("/api/analyser", donnees).then(function (res) {
        ecrire(res.lignes);
        if (res.occupe) { terminer("déjà en cours"); return; }
        if (res.en_attente > 0 && tranche < TRANCHES_MAX) return analyser(tranche + 1);
        terminer(res.en_attente > 0 ? "pause : " + res.en_attente + " en attente" : "terminé");
      });
    };
    var lancer = function (etape, form) {
      panneau.hidden = false;
      document.querySelectorAll("[data-action]").forEach(function (b) { b.disabled = true; });
      var debut = Promise.resolve();
      if (etape === "chercher") {
        etat.textContent = "collecte…";
        debut = appeler("/api/collecter", new FormData(form)).then(function (res) { ecrire(res.lignes); });
      }
      debut.then(function () { return analyser(1); }).catch(function (e) {
        ecrire(["⚠ " + e.message]);
        etat.textContent = "interrompu";
        etat.className = "pastille pastille-danger";
        document.querySelectorAll("[data-action]").forEach(function (b) { b.disabled = false; });
      });
      panneau.scrollIntoView({ behavior: "smooth", block: "start" });
    };

    document.querySelectorAll("form[data-boucle]").forEach(function (form) {
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        lancer(form.getAttribute("data-boucle"), form);
      });
    });
    if (panneau.getAttribute("data-auto") === "1") lancer("analyser", null);
  }

  // Suivi du pré-remplissage en direct (version PC).
  var suivi = document.getElementById("suivi-direct");
  if (suivi) {
    var liste = document.getElementById("suivi-messages");
    var erreur = document.getElementById("suivi-erreur");
    var url = suivi.getAttribute("data-etat");
    var minuteur = setInterval(function () {
      fetch(url, { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (etat) {
        if (etat.messages.length) {
          liste.innerHTML = "";
          etat.messages.forEach(function (m) {
            var li = document.createElement("li");
            li.textContent = m;
            liste.appendChild(li);
          });
        }
        if (etat.erreur) {
          erreur.hidden = false;
          erreur.querySelector("[data-texte]").textContent = etat.erreur;
          clearInterval(minuteur);
        }
        if (!etat.active) clearInterval(minuteur);
      }).catch(function () {});
    }, 1500);
  }
})();
