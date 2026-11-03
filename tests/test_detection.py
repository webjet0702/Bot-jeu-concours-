from concours_assistant.detection import CAPTCHA_FORMULAIRE, DEFI_ACCES, detecter_cadres, detecter_defi


def test_challenge_cloudflare():
    html = "<html><title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/h/b'></script></html>"
    d = detecter_defi(403, {"server": "cloudflare"}, html)
    assert d.type == DEFI_ACCES and d.fournisseur == "Cloudflare"


def test_entete_cf_mitigated():
    assert detecter_defi(200, {"cf-mitigated": "challenge"}, "").type == DEFI_ACCES


def test_datadome():
    html = "<iframe src='https://geo.captcha-delivery.com/captcha/?initialCid=x'></iframe>"
    assert detecter_defi(403, {}, html).fournisseur == "DataDome"


def test_blocage_403_serveur_protege():
    assert detecter_defi(403, {"server": "AkamaiGHost"}, "<h1>Forbidden</h1>").type == DEFI_ACCES


def test_page_de_verification_en_200():
    html = "<p>Vérifiez que vous êtes humain</p><div class='captcha'></div>"
    assert detecter_defi(200, {}, html).type == DEFI_ACCES


def test_recaptcha_dans_formulaire():
    html = "<form><input name=email><div class='g-recaptcha' data-sitekey='x'></div></form>"
    d = detecter_defi(200, {}, html)
    assert d.type == CAPTCHA_FORMULAIRE and d.fournisseur == "reCAPTCHA"


def test_turnstile_et_hcaptcha():
    assert detecter_defi(200, {}, "<div class='cf-turnstile'></div>").fournisseur == "Cloudflare Turnstile"
    assert detecter_defi(200, {}, "<script src='https://hcaptcha.com/1/api.js'></script>").fournisseur == "hCaptcha"


def test_page_normale():
    assert detecter_defi(200, {}, "<form><input type=email name=email><button>Participer</button></form>") is None


def test_iframes():
    assert detecter_cadres(["https://www.google.com/recaptcha/api2/anchor?k=x"]) is not None
    assert detecter_cadres(["https://jeu.marque.fr/form", "about:blank"]) is None
