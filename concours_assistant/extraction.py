"""Extraction heuristique : valeur des lots, date limite, âge, signaux de règlement.

Les heuristiques sont volontairement prudentes : en cas de doute, la valeur
renvoyée est None et le concours passe « à vérifier » plutôt que d'être accepté.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .models import InfosExtraites
from .urls import domaine

# --------------------------------------------------------------------- texte


def texte_depuis_html(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for balise in soup(["script", "style", "noscript", "template", "svg", "head"]):
        balise.decompose()
    texte = soup.get_text("\n")
    texte = re.sub(r"[ \t\r\f\v ]+", " ", texte)
    return re.sub(r"\n\s*\n+", "\n", texte).strip()


def texte_depuis_pdf(contenu: bytes, pages_max: int = 40) -> str:
    try:
        from pypdf import PdfReader

        lecteur = PdfReader(io.BytesIO(contenu))
        return "\n".join((p.extract_text() or "") for p in lecteur.pages[:pages_max])
    except Exception:  # PDF illisible / chiffré : le règlement sera signalé « non analysé »
        return ""


def liens_reglement(html: str, url_base: str) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    scores: dict[str, int] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith(("mailto:", "javascript:", "tel:", "#")):
            continue
        texte = (a.get_text(" ", strip=True) + " " + href).lower()
        score = 0
        if re.search(r"r[èe]glement", texte):
            score = 3
        elif re.search(r"modalit[ée]s|conditions\s+(?:de\s+participation|du\s+jeu|g[ée]n[ée]rales\s+du\s+jeu)", texte):
            score = 2
        if score:
            url = urljoin(url_base, href)
            scores[url] = max(scores.get(url, 0), score)
    return [u for u, _ in sorted(scores.items(), key=lambda x: -x[1])]


_TEXTE_PARTICIPER = re.compile(
    r"particip|je\s+joue|jouer|acc[ée]d(?:er|ez)\s+au\s+(?:jeu|concours)|tente[rz]?\s+(?:ma|sa|votre)\s+chance|"
    r"lien\s+(?:du|vers\s+le)\s+(?:jeu|concours)|voir\s+le\s+(?:jeu|concours)", re.I)


def lien_participation(html: str, url_base: str, selecteur_css: str | None = None) -> str | None:
    """Sur une page d'agrégateur, trouve le lien sortant vers la vraie page du concours."""
    soup = BeautifulSoup(html or "", "html.parser")
    if selecteur_css:
        el = soup.select_one(selecteur_css)
        if el is not None and el.get("href"):
            return urljoin(url_base, el["href"])
        return None
    for a in soup.find_all("a", href=True):
        url = urljoin(url_base, a["href"])
        if not url.startswith(("http://", "https://")) or domaine(url) == domaine(url_base):
            continue
        if _TEXTE_PARTICIPER.search(a.get_text(" ", strip=True) + " " + (a.get("title") or "")):
            return url
    return None


# -------------------------------------------------------------------- montants

_SEP = r"[   .]"
_NOMBRE = rf"(?:\d{{1,3}}(?:{_SEP}\d{{3}})+|\d+)(?:,\d{{1,2}}|\.\d{{1,2}}(?!\d))?"
_MONTANT = re.compile(
    rf"(?<![\d,.])(?P<n1>{_NOMBRE})\s*(?P<k1>k)?\s*(?:€|eur(?:os?)?\b)"
    rf"|€\s*(?P<n2>{_NOMBRE})(?P<k2>\s*k\b)?",
    re.IGNORECASE,
)
_NEGATIF = re.compile(
    r"capital|frais|co[uû]t|tarif|prix\s+d['’](?:un|une|achat)|timbre|affranchissement|rembours|"
    r"minimum\s+d['’]achat|achat\s+minimum|pour\s+tout\s+achat|d[eè]s\s+\d+\s*€|amende|p[ée]nalit|"
    r"chiffre\s+d['’]affaires|cotisation|abonnement|caution",
    re.I,
)
_NEGATIF_APRES = re.compile(r"^\s*(?:ttc\s*)?(?:/\s*(?:min|mn|appel|sms|mois)|par\s+(?:minute|appel|sms|mois))", re.I)
_POSITIF = re.compile(
    r"valeur|gagn|lots?\b|dotation|cadeau|bons?\s+d['’]achat|ch[eè]ques?|cartes?|cagnotte|remport|offert|"
    r"(?:1er|premier|grand)\s+prix|r[ée]compense|à\s+gagner|voyage|s[ée]jour|\(\s*$",
    re.I,
)
_POSITIF_APRES = re.compile(
    r"^\s*(?:ttc|\)|à\s+gagner|en\s+(?:bons?|cartes?|ch[eè]ques?|cadeaux)|de\s+(?:cadeaux|lots|bons?|dotation)|"
    r"offerts?)", re.I)
_TOTAL = re.compile(
    r"(?:dotation|valeur)\s+(?:totale|globale)|montant\s+(?:total|global)|total\s+des\s+(?:lots|dotations|gains)",
    re.I)
_QUANTITE = re.compile(r"(?<![\d,.])(\d{1,4})\s*(?:x|×)\s|(?<![\d,.])(\d{1,4})\s+(?:lots?|gagnants?|exemplaires?)\b",
                       re.I)
_COUPURE = re.compile(r"[;!?\n]|\.\s")


def parser_nombre(n: str) -> float:
    n = re.sub(r"[   ]", "", n)
    if "," in n:
        n = n.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", n):
        n = n.replace(".", "")
    return float(n)


@dataclass
class Montant:
    valeur: float
    quantite: int
    contexte: str  # "total" | "lot" | "ignore"


def extraire_montants(texte: str) -> list[Montant]:
    resultats: list[Montant] = []
    fin_precedente = 0
    for m in _MONTANT.finditer(texte):
        brut = m.group("n1") or m.group("n2")
        try:
            valeur = parser_nombre(brut)
        except ValueError:
            continue
        if m.group("k1") or m.group("k2"):
            valeur *= 1000
        debut = max(m.start() - 90, fin_precedente)
        avant = texte[debut:m.start()]
        coupures = list(_COUPURE.finditer(avant))
        if coupures:
            avant = avant[coupures[-1].end():]
        apres = texte[m.end():m.end() + 20]
        fin_precedente = m.end()

        if _NEGATIF.search(avant) or _NEGATIF_APRES.search(apres):
            contexte = "ignore"
        elif _TOTAL.search(avant):
            contexte = "total"
        elif _POSITIF.search(avant) or _POSITIF_APRES.search(apres):
            contexte = "lot"
        else:
            contexte = "ignore"
        quantite = 1
        q = list(_QUANTITE.finditer(avant))
        if q:
            quantite = int(q[-1].group(1) or q[-1].group(2))
        resultats.append(Montant(valeur, max(quantite, 1), contexte))
    return resultats


def extraire_valeurs(texte: str) -> tuple[float | None, float | None, bool]:
    """Renvoie (gain_principal, dotation_totale, dotation_estimee)."""
    montants = extraire_montants(texte)
    lots = [m for m in montants if m.contexte == "lot"]
    totaux = [m.valeur for m in montants if m.contexte == "total"]
    principal = max((m.valeur for m in lots), default=None)
    if totaux:
        return principal, max(totaux), False
    if lots:
        uniques = {(m.valeur, m.quantite) for m in lots}
        return principal, sum(v * q for v, q in uniques), True
    return principal, None, False


# ----------------------------------------------------------------------- dates

MOIS = {"janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
        "aout": 8, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12, "décembre": 12}
_DATE_NUM = re.compile(r"(?<![\d/])(?<!\d[.-])(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})(?!\d|[/.-]\d)")
_DATE_TXT = re.compile(r"(?<!\d)(1er|\d{1,2})\s+(" + "|".join(MOIS) + r")(?:\s+(\d{4}))?", re.I)
_FIN_AVANT = re.compile(
    r"(?:jusqu['’]\s?(?:au|à)|\bau|avant\s+le|fin\s+le|se\s+termine(?:ra)?\s+le|termin[ée]e?\s+le|"
    r"cl[ôo]tur[ée]e?\s+le|cl[ôo]ture\s*(?:le|:)|date\s+limite(?:\s+de\s+participation)?\s*"
    r"(?:est\s+(?:fix[ée]e\s+)?(?:au|le)|:|le|au)?|date\s+de\s+fin\s*(?:du\s+(?:jeu|concours))?\s*:?|"
    r"fin\s+du\s+(?:jeu|concours)\s*:?)\s*(?:le\s+)?(?:(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s+)?$",
    re.I,
)
_EXCLU_DATE = re.compile(
    r"tirage|envoi|exp[ée]di|r[ée]clam|livr|r[ée]sultat|d[ée]sign|annonc|naissance|n[ée]e?s?\s+(?:le|avant|apr[èe]s)|"
    r"publi|d[ée]pos|\bloi\b|rgpd|r[èe]glement\s+\(ue\)|version|mis\s+à\s+jour|modifi|valid",
    re.I,
)


def _construire_date(j: int, m: int, a: int | None, aujourd_hui: date) -> date | None:
    try:
        if a is None:
            d = date(aujourd_hui.year, m, j)
            if d < aujourd_hui - timedelta(days=180):
                d = date(aujourd_hui.year + 1, m, j)
            return d
        if a < 100:
            a += 2000
        return date(a, m, j)
    except ValueError:
        return None


def extraire_date_limite(texte: str, aujourd_hui: date) -> date | None:
    candidates: list[date] = []
    trouvees = []
    for m in _DATE_NUM.finditer(texte):
        trouvees.append((m.start(), _construire_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), aujourd_hui)))
    for m in _DATE_TXT.finditer(texte):
        jour = 1 if m.group(1).lower() == "1er" else int(m.group(1))
        annee = int(m.group(3)) if m.group(3) else None
        trouvees.append((m.start(), _construire_date(jour, MOIS[m.group(2).lower()], annee, aujourd_hui)))
    for debut, d in trouvees:
        if d is None:
            continue
        avant = texte[max(0, debut - 60):debut]
        if not _FIN_AVANT.search(avant[-40:]):
            continue
        if _EXCLU_DATE.search(avant):
            continue
        candidates.append(d)
    return max(candidates) if candidates else None


# ------------------------------------------------------------------------- âge

_AGE_MAJEUR = re.compile(
    r"personnes?\s+(?:physiques?\s+)?majeures?|(?:être|etre|soit|sont|soient)\s+majeure?s?|"
    r"r[ée]serv[ée]e?s?\s+aux\s+(?:personnes\s+)?majeure?s?|participants?\s+majeure?s?|"
    r"majeure?s?\s+(?:au\s+jour|à\s+la\s+date|r[ée]sidant|domicili|capables?)|majorit[ée]\s+(?:l[ée]gale|civile)",
    re.I,
)
_AGE_NUM = re.compile(
    r"(?:âg[ée]e?s?|ag[ée]e?s?)\s+(?:de\s+plus\s+de|d['’]\s*au\s+moins|d['’]\s*(?=\d)|de\s+(?:au\s+moins\s+)?|"
    r"au\s+moins|minimum|plus\s+de)?\s*(\d{2})\s+ans|"
    r"(?:à\s+partir\s+de|plus\s+de|minimum)\s+(\d{2})\s+ans|"
    r"(\d{2})\s+ans\s+(?:r[ée]volus|minimum|et\s+plus|ou\s+plus|au\s+moins)",
    re.I,
)
_AUTORISATION = re.compile(
    r"autorisation\s+(?:parentale|(?:pr[ée]alable\s+)?(?:écrite\s+)?(?:de\s+(?:leurs?|ses|son)\s+)?"
    r"(?:parents|repr[ée]sentants?\s+l[ée]gaux|tuteur|titulaire))|"
    r"accord\s+(?:de\s+(?:leurs?|ses|son)\s+)?(?:parents|repr[ée]sentants?\s+l[ée]gaux)",
    re.I,
)


def extraire_age(texte: str) -> tuple[int | None, bool]:
    ages: list[int] = []
    if _AGE_MAJEUR.search(texte):
        ages.append(18)
    for m in _AGE_NUM.finditer(texte):
        n = int(next(g for g in m.groups() if g))
        if 10 <= n <= 21:
            ages.append(n)
    return (max(ages) if ages else None), bool(_AUTORISATION.search(texte))


# ------------------------------------------------------------------- signaux

_PHRASES = re.compile(r"(?<=[.!?;])\s+|\n+")
_AUTOMATISATION = re.compile(
    r"automatis|automatique|robots?\b|\bbots?\b|scripts?\b|logiciels?|programmes?\s+informatiques?|"
    r"proc[ée]d[ée]s?\s+(?:informatiques?|automatiques?)|macros?\b|intelligence\s+artificielle",
    re.I,
)
_INTERDICTION = re.compile(
    r"interdit|prohib|proscrit|exclu|\bnul(?:le)?s?\b|annul|disqualif|refus|frauduleu|fraude|"
    r"ne\s+(?:sera|seront|pourra|pourront)|non\s+(?:autoris|admis|valable|pris)|pas\s+(?:autoris|admis|pris)|"
    r"invalid|[ée]limin|sanction",
    re.I,
)
_PAIEMENT = re.compile(
    r"cartes?\s+(?:bancaires?|de\s+cr[ée]dit)|coordonn[ée]es\s+bancaires|\biban\b|\brib\b|num[ée]ro\s+de\s+carte|"
    r"cryptogramme|\bcvv\b|frais\s+de\s+(?:dossier|traitement|livraison|port|participation|douane|lib[ée]ration)|"
    r"\bpayer\b|paiement|\br[ée]gle[rz]\b|surtax|audiotel|\b08\s?9\d|"
    r"0[,.]\d{2}\s*€\s*(?:ttc\s*)?(?:/|par)\s*(?:min|appel|sms)|western\s+union|paysafecard|transcash|"
    r"mandat\s+cash|bitcoin|crypto[- ]?monnaie",
    re.I,
)
_NEUTRALISANT = re.compile(
    r"rembours|aucun|jamais|gratuit|sans\s+(?:obligation|frais)|ne\s+(?:vous\s+)?(?:sera|seront)\s+(?:jamais\s+)?demand|"
    r"pas\s+(?:de\s+frais|demand)|à\s+la\s+charge\s+(?:de\s+l['’]organisat|de\s+la\s+soci|du\s+partenaire)|"
    r"pris\s+en\s+charge\s+par\s+(?:l['’]organisat|la\s+soci)",
    re.I,
)
_ACHAT = re.compile(
    r"(?<!sans\s)(?<!aucune\s)obligation\s+d['’]achat|preuve\s+d['’]achat|ticket\s+de\s+caisse|"
    r"achat\s+(?:d['’]un|d['’]une|d['’]au\s+moins|minimum|pr[ée]alable)|sur\s+pr[ée]sentation\s+d['’](?:une\s+)?facture",
    re.I,
)
_SANS_ACHAT = re.compile(r"sans\s+(?:obligation\s+d['’])?achat|aucune\s+obligation|gratuit|aucun\s+achat", re.I)
_ORGANISATEUR = re.compile(r"(?:siren|siret|\brcs\b|r\.c\.s\.?|tva\s+intracom\w*)\D{0,40}\d{3}\s?\d{3}\s?\d{3}", re.I)
_COMMISSAIRE = re.compile(
    r"commissaires?\s+de\s+justice|huissiers?(?:\s+de\s+justice)?|d[ée]pos[ée]\s+(?:chez|aupr[èe]s)|\bselarl\b|\bscp\b",
    re.I)


def _phrases(texte: str) -> list[str]:
    return [p.strip() for p in _PHRASES.split(texte) if p.strip()]


def detecter_interdiction_automatisation(texte: str) -> str | None:
    for phrase in _phrases(texte):
        if _AUTOMATISATION.search(phrase) and _INTERDICTION.search(phrase):
            return phrase[:240]
    return None


def detecter_signaux_arnaque(texte: str) -> list[str]:
    signaux = []
    for phrase in _phrases(texte):
        m = _PAIEMENT.search(phrase)
        if m and not _NEUTRALISANT.search(phrase):
            signaux.append(f"« {m.group(0)} » : {phrase[:160]}")
    return signaux[:5]


def detecter_achat_obligatoire(texte: str) -> bool:
    return any(_ACHAT.search(p) and not _SANS_ACHAT.search(p) for p in _phrases(texte))


# ---------------------------------------------------------------- assemblage


def extraire_infos(texte_complet: str, texte_conditions: str, aujourd_hui: date,
                   url_reglement: str | None = None) -> InfosExtraites:
    """`texte_complet` : tout ce qu'on sait (source + page + règlement).
    `texte_conditions` : page + règlement (pour les signaux juridiques)."""
    principal, total, estimee = extraire_valeurs(texte_complet)
    age, autorisation = extraire_age(texte_conditions or texte_complet)
    interdiction = detecter_interdiction_automatisation(texte_conditions)
    return InfosExtraites(
        gain_principal_eur=principal,
        dotation_totale_eur=total,
        dotation_estimee=estimee,
        date_limite=extraire_date_limite(texte_complet, aujourd_hui),
        age_minimum=age,
        autorisation_parentale=autorisation,
        url_reglement=url_reglement,
        organisateur_identifie=bool(_ORGANISATEUR.search(texte_conditions)),
        depot_commissaire=bool(_COMMISSAIRE.search(texte_conditions)),
        interdit_automatisation=interdiction is not None,
        preuve_interdiction=interdiction,
        achat_obligatoire=detecter_achat_obligatoire(texte_conditions or texte_complet),
        signaux_arnaque=detecter_signaux_arnaque(texte_complet),
    )
