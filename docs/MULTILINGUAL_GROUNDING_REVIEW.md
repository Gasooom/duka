# Native-speaker review: grounding claims in Kinyarwanda, French and Swahili

**Status: not reviewed.** Before the pilot, a native speaker of each language must review this list. The
check works as the tests describe (`backend/tests/test_grounding_multilingual.py`), but the repository cannot show
that the phrases are complete or idiomatic. Kinyarwanda and Swahili carry the most risk.

## What this protects

Before an AI reply is sent, `backend/app/agents/grounding.py` checks it against the facts the tools returned in
that turn. If the reply makes one of these claims without the matching fact, it is not sent. The customer gets
the server's own factual message instead:

| Claim | Fact required from the tools |
|---|---|
| an order was placed or confirmed | an order lookup this turn |
| the order is paid / the payment was received | `payment_status = paid` |
| the order is accepted, ready, on the way, delivered or cancelled | that order status |
| an item was added to or removed from the cart | a cart tool ran this turn |

English and Arabic use the original patterns, which are unchanged. Kinyarwanda, French and Swahili use
`LOCAL_CLAIMS`, built from completed-state verb forms. Offers ("shall I add it?"), infinitives and future forms do
not match.

A match is **not** treated as a claim in four cases:

- its own clause contains a negation, condition or deferral word (listed per language below);
- the sentence starts with a question word: *Ese* (rw), *Je,* (sw), *Est-ce que* (fr);
- in French only, a plain present tense is followed by a policy phrase ("est livrée **sous 24 h**", "est payée **à la livraison**").

## Where the wording comes from

- **i18n:** the server's own customer texts in `backend/app/i18n.py`. Those texts are themselves still flagged
  there for native review.
- **added:** variants added in Phase 3 by someone who is not a native speaker. Review these first.

## How to review

For each phrase, answer three questions:

1. Is it something a shop assistant would naturally write to mean *this* claim?
2. Can it mean something else in a shop chat? That would cause a false alarm: a correct reply gets replaced.
3. What common ways of saying the claim are missing? Those would let a false claim through.

Record every correction as a sentence in `CLAIMS` (must be caught) or `NOT_CLAIMS` (must pass) in
`backend/tests/test_grounding_multilingual.py`. Then adjust `LOCAL_CLAIMS` until
`pytest tests/test_grounding_multilingual.py` passes.

## Kinyarwanda (rw): highest risk

Order nouns: *komande*, *order*, *oda*, *ibyo watumije/mwatumije*. Basket: *igitebo* (also *agaseke*, *cart*,
*panier*).

| Claim | Phrases | Source |
|---|---|---|
| placed / confirmed | komande … yemejwe, yamaze kwemezwa | i18n |
| | komande … yatanzwe, yakiriwe, yanditswe; natanze / twatanze / watanze / twemeje / twakiriye / twanditse komande | added |
| paid | ubwishyu … bwakiriwe; yishyuwe, yarishyuwe, yamaze kwishyurwa (and class forms cy-, by-, z-) | i18n |
| | ubwishyu … bwemejwe, bwageze, bwabonetse, bwarangiye, bwagenze neza; twakiriye / twabonye / twemeje ubwishyu *or* amafaranga; amafaranga … yageze, yakiriwe, yabonetse, yinjiye; wamaze / mwamaze kwishyura, warishyuye, mwarishyuye | added |
| delivered | yagejejwe | i18n |
| | yamaze kugezwa, yashyikirijwe; komande … yageze, yagezeyo, yakugezeho, yabagezeho | added |
| on the way | iri mu nzira | i18n |
| | iri mu rugendo; komande … yoherejwe, yahagurutse | added |
| accepted | komande … yemewe; *(shop)* yemeye komande | i18n |
| | twemeye / nemeye / bemeye / ryemeye komande | added |
| cancelled | yahagaritswe | i18n |
| | komande … yasheshwe, yakuweho; twahagaritse / bahagaritse / twasheshe komande | added |
| ready | komande … iteguye | i18n |
| | komande … yateguwe, yamaze gutegurwa | added |
| cart | nashyize … mu gitebo | i18n |
| | nongeyeho / nongereye / nakuyemo / nakuye / nakuyeho … mu gitebo; kiri / biri / iri mu gitebo; cyashyizwe / cyakuwe / cyavanywe / cyongewe mu gitebo | added |

**Not a claim when the clause contains:** ntabwo, nta, ntago, oya, sibyo, niba, numara, nimara, nibamara,
nibimara, nitumara, namara, mumara, nimumara, kugira (ngo), kugirango, nibiba, nuramuka, nimuramuka, ese, the
"after/before" constructions *nyuma yo / nyuma y'…* and *mbere yo / mbere y'…*, or a future auxiliary ending in
*-zaba*.

Some words never exempt a claim:
- bare *nyuma* / *mbere*: "komande yawe ya nyuma / ya mbere" means your last / first order;
- the "want" words *ushaka, urashaka, wifuza, mwifuza*: "ibyo ushaka" means what you want.

Offers never contain the past-tense claim verbs, so they needed no exemption word.

Negative verb forms are separate words (*ntiyishyuwe*, *ntiyatanzwe*, *ntikiri*), so they never match.

**Please check in particular:**
- whether *nyuma yo / nyuma y'…* and *mbere yo / mbere y'…* are the right "after/before" constructions to exempt,
  and which others exist. The tests use *Nyuma y'uko komande yawe yishyuwe, …* and *Mbere y'uko komande yawe
  yemejwe, …*; their wording is unconfirmed;
- whether *yemewe* (accepted) and *yemejwe* (confirmed) are used as distinctly as the server texts assume;
- the payment variants a customer-facing assistant would use;
- whether *yoherejwe* naturally means "shipped" for an order.

## Swahili (sw): high risk

Order nouns: *oda*, *order*, *agizo*, *maagizo*, *mzigo*, *kifurushi*. The verb must agree with the noun's class:
*oda … imelipwa*, *agizo … limethibitishwa*, *mzigo … umefika*. Basket: *kikapu* (also *cart*, *toroli*,
*mkokoteni*).

| Claim | Phrases | Source |
|---|---|---|
| placed / confirmed | oda … imethibitishwa (tayari imethibitishwa) | i18n |
| | oda … imewekwa, imepokelewa, imesajiliwa, imeundwa; nimeweka / tumeweka / umeweka / nimethibitisha / tumepokea / nimesajili oda | added |
| paid | malipo … yamepokelewa; imelipwa, imeshalipwa | i18n |
| | malipo … yamethibitishwa, yamekamilika, yamefanikiwa, yameingia, yamefika; past tense *ililipwa*; tumepokea / tumeona / tumethibitisha malipo *or* pesa; umelipa, umeshalipa, umelipia | added |
| delivered | imefikishwa | i18n |
| | imekabidhiwa; oda … imefika, imewasilishwa, imeletwa; tumefikisha / tumewasilisha / tumekabidhi / tumeleta oda; umepokea oda | added |
| on the way | iko njiani | i18n |
| | imesafirishwa; oda … imetumwa, imeondoka | added |
| accepted | oda … imekubaliwa; *(shop)* wamekubali oda | i18n |
| | tumekubali / amekubali / limekubali oda | added |
| cancelled | imeghairiwa | i18n |
| | imebatilishwa; oda … imefutwa, imesitishwa; tumeghairi / tumefuta / tumesitisha oda | added |
| ready | oda … iko tayari | i18n |
| | oda … imeandaliwa, imetayarishwa | added |
| cart | nimeweka … kwenye kikapu | i18n |
| | nimeongeza / nimeondoa / nimetoa … kwenye kikapu; kiko / iko / viko kwenye kikapu; imewekwa / imeongezwa / imeondolewa kwenye kikapu | added |

**Not a claim when the clause contains:** hapana, hakuna, si, sio, siyo, wala, kama, ikiwa, endapo, iwapo,
baada, kabla, pindi, je, niweke, niongeze, or a *-takapo-* / *-takavyo-* verb.

Some words never exempt a claim:
- bare *mara*: "oda yako ya mara ya kwanza" means your first-time order. The "as soon as" sense (*mara malipo
  yatakapopokelewa*) is still exempt, through its *-takapo-* verb;
- the "want" words *ungependa, unataka, mnataka*.

Negative verb forms are separate words (*haijalipwa*, *haijafika*, *haipo*), so they never match.

**Please check in particular:**
- *imewekwa* for "the order has been placed";
- *umelipa* in polite replies;
- whether *iko tayari* also appears in unrelated phrases. *Timu yetu iko tayari* (our team is ready) passes
  because the order noun is required.

## French (fr): lower risk

Accents are optional (*livrée / livree*), and both ' and ’ apostrophes are accepted.

| Claim | Phrases | Source |
|---|---|---|
| placed / confirmed | Commande … confirmée ; votre commande est déjà confirmée | i18n |
| | commande … a (bien) été / vient d'être / est déjà confirmée, enregistrée, validée, passée ; votre / cette commande est confirmée ; commande prise en compte ; j'ai / nous avons / vous avez passé, confirmé, enregistré, validé, reçu votre commande | added |
| paid | Paiement reçu ; est maintenant PAYÉE ; est déjà payée | i18n |
| | votre paiement a (bien) été / est reçu, confirmé, validé, effectué, enregistré ; paiement réussi ; a été / est déjà payée, réglée ; nous avons (bien) reçu votre paiement ; vous avez (déjà) payé | added |
| delivered | a été livrée | i18n |
| | est (déjà / bien) livrée *(specific subject)* ; livraison effectuée / terminée ; nous avons livré | added |
| on the way | est en route ; en cours de livraison | i18n |
| | en chemin ; a été / vient d'être expédiée ; nous avons expédié ; remise au livreur | added |
| accepted | a accepté votre commande | i18n |
| | commande … a été / est acceptée | added |
| cancelled | a été annulée ; est annulée | i18n |
| | commande annulée ; nous avons annulé votre commande | added |
| ready | est prête | i18n |
| | — | |
| cart | ajouté à votre panier | i18n |
| | j'ai / je viens d'ajouter, mis, placé, retiré, supprimé, enlevé … panier ; j'ajoute … à votre panier ; est (bien) dans votre panier ; retiré(e) de votre panier | added |

**Not a claim when the clause contains:**
- **negation:** ne / n', pas, jamais, aucun(e);
- **condition or time:** si, s'il, une fois, dès (without the accent only as *des que* / *des qu'*), lorsque, quand,
  après, avant, jusqu'à, attente / attend;
- **future and modal verbs:** sera / seront / serait, va / vont / vais, devrait / doit, pourra / peut, pouvez,
  voulez, souhaitez, veuillez;
- **question:** est-ce;
- **generic wording:** the generic subjects *chaque commande*, *toute commande* and *toutes les commandes*;
  généralement, habituellement, normalement, d'habitude, en général.

These never exempt a claim:
- the article *des* ("le paiement des articles a été reçu");
- *tout / toute / tous / chaque* in front of a specific order or item ("toute votre commande", "tous vos
  articles");
- *non / sans / impayé(e)* inside the subject ("votre commande impayée a été annulée").

A plain present tense ("est livrée") counts only with a specific subject: *votre*, *vos*, *cette*, *elle*, or an
order number. Generic policy sentences ("Les commandes sont livrées sous 24 h") pass.

## Known gaps (accepted for now, listed for the reviewers)

- Ambiguous verbs used without the order noun are not detected. Examples: *"Yemejwe."*, *"Imethibitishwa."*. This
  is deliberate: *aderesi yemejwe* and *bei imethibitishwa* (address/price confirmed) must not count as order
  claims.
- One-word server-style cart confirmations are not cart claims: *Byakuweho.*, *Imeondolewa.*, *Retiré.*
- French generic-subject present tense is not detected: *La commande est livrée.*
- "Being processed / in preparation" wording is not treated as a status claim in any language. This matches
  English.
- English verbs inside Kinyarwanda or Swahili sentences ("order yawe ni delivered") are not targeted.
- **Introductory phrase without a comma.** If a sentence opens with a phrase that uses an exemption word in another
  sense and has no comma after it, the claim is still exempted. Examples:
  - *Kama ulivyoomba oda yako imethibitishwa*, *Baada ya siku mbili oda yako imefika*, *Hakuna shida oda yako
    imelipwa*;
  - *Après vérification votre paiement a été confirmé*, *Votre commande d'avant-hier a été livrée*.

  With the usual comma these are caught.
- **French adverb inside the verb.** An adverb inside the verb hides the claim: *a été correctement livrée*, *a
  finalement été livrée*.
- **False alarms (safe: the reply is replaced by the server's facts):** *Votre panier est prêt* (English "is ready"
  behaves the same), *Taarifa yako imefikishwa kwa timu yetu*, *Une commande est acceptée dès que …*.
- **Arabic cart claims are not detected.** Example: *تمت إضافة الحذاء إلى سلتك*. This predates Phase 3 and was left
  unchanged, because the scope required unchanged Arabic behaviour. It needs its own reviewed change.

## Still needed before the pilot

1. A native speaker reviews the rw and sw tables above, then fr, and records name, date and corrections here.
2. Real-model evaluation in the pilot languages: `python -m evals.run --provider openai_compat`. This also runs
   the cases that need a real LLM.
   Watch for two things:
   - correct replies rejected as ungrounded (false alarms);
   - false claims the patterns missed. Read a sample of transcripts; the eval cannot find these automatically.
3. During the pilot, regularly review rejected replies: `agent_runs` with status `ungrounded`, whose rejected
   text is in the step log. See `docs/EXTERNAL_VALIDATION.md`.
