// Assistant concours — petits comportements de l'interface (aucune dépendance externe).
(function () {
  "use strict";

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

  // Suivi d'une tâche de fond (recherche / analyse).
  var bloc = document.getElementById("tache");
  if (bloc && bloc.getAttribute("data-en-cours") === "1") {
    var log = document.getElementById("tache-log");
    var minuteur = setInterval(function () {
      fetch("/tache", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (etat) {
        if (log) { log.textContent = etat.lignes.join("\n"); log.scrollTop = log.scrollHeight; }
        if (!etat.en_cours) { clearInterval(minuteur); window.location.reload(); }
      }).catch(function () {});
    }, 2000);
  }

  // Bandeau « tâche en cours » sur les autres pages.
  var bandeau = document.getElementById("bandeau-tache");
  if (bandeau) {
    var minuteur2 = setInterval(function () {
      fetch("/tache", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (etat) {
        if (!etat.en_cours) { clearInterval(minuteur2); bandeau.textContent = "Tâche terminée : résultats sur le tableau de bord."; }
      }).catch(function () {});
    }, 3000);
  }

  // Suivi du pré-remplissage en direct.
  var suivi = document.getElementById("suivi-direct");
  if (suivi) {
    var liste = document.getElementById("suivi-messages");
    var erreur = document.getElementById("suivi-erreur");
    var url = suivi.getAttribute("data-etat");
    var minuteur3 = setInterval(function () {
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
          clearInterval(minuteur3);
        }
        if (!etat.active) clearInterval(minuteur3);
      }).catch(function () {});
    }, 1500);
  }
})();
