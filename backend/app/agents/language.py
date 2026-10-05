"""Conversation language detection and persistent language state.

Offline and deterministic: no provider call, so no customer text or phone number leaves the server.

Supported codes: en, rw (Kinyarwanda), fr, sw (Swahili), ar (general Arabic), ar-SD (Sudanese Arabic).

How a message is scored (`detect`)
  * Product, category, business and customer names are removed first, as are numbers, SKUs and order numbers,
    so "Adidas Samba OG Black" says nothing about the customer's language. Emojis carry no letters.
  * Arabic script decides the Arabic *family*. The *variant* is decided by lexical signals, never by script:
    Sudanese markers (داير، متين، شنو، ده، زول، عايز…) against Modern Standard markers (أريد، هل، هذا، لديكم…).
  * Latin-script languages are scored with weighted marker words (distinctive words 2, function words 1).

How the conversation language changes (`next_language`)
  * The last *confident* detection becomes the conversation language; weak ones keep the current language.
  * Switching to another language needs confidence >= SWITCH_CONFIDENCE and >= MIN_SWITCH_EVIDENCE distinct
    words, so an isolated "hello", "merci" or "شكرا" inside another language is code-switching, not a switch.
  * Inside Arabic, the variant changes only on a confident dialect signal; an Arabic message with no dialect
    markers keeps the current variant (neither variant is forced on the customer).
  * The first message needs only INITIAL_CONFIDENCE. With no usable detection the business language applies.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

SUPPORTED = ("en", "rw", "fr", "sw", "ar", "ar-SD")
NAMES = {"en": "English", "rw": "Kinyarwanda", "fr": "French", "sw": "Swahili", "ar": "Arabic",
         "ar-SD": "Sudanese Arabic"}
SWITCH_CONFIDENCE = 0.5
INITIAL_CONFIDENCE = 0.25
MIN_SWITCH_EVIDENCE = 2


def _markers(strong: str, weak: str) -> dict[str, int]:
    return dict.fromkeys(weak.split(), 1) | dict.fromkeys(strong.split(), 2)


_LATIN: dict[str, dict[str, int]] = {
    "en": _markers(
        "hello thanks thank please want need looking would could where when much show send which does how what "
        "talk person",
        "the is are i you your my do have can and with for to of it this that price order deliver delivery "
        "address hi buy pay paid available me yes no any"),
    "rw": _markers(
        "muraho mwaramutse mwiriwe amakuru ndashaka nshaka ndifuza mufite murakoze ese angahe amafaranga igiciro "
        "ibiciro cyane neza ryari hehe inkweto umukara mumbwire mbwira ndabyemeje kugura cyangwa yanjye wanjye "
        "cyanjye kuvugana nyamuneka ndabishaka komande yego oya mwakoze nibyo rwose iyihe ingahe mpa nshobora "
        "telefone",
        "iki ni mu ku kuri iyi uyu ibyo iyo kandi umuntu na he ki ubu"),
    "fr": _markers(
        "bonjour bonsoir je vous les des combien prix merci voudrais veux cherche livraison commande quand avez "
        "plait moins payer parler aujourd est noires noir acheter votre notre",
        "le la du pour avec oui non que qui une un mon ma mes pas il ce cette salut stp svp"),
    "sw": _markers(
        "habari jambo nataka nini gani bei asante ndiyo hapana tafadhali kiatu viatu nyeusi rangi lini wapi ngapi "
        "kununua kuongea karibu kesho pesa shilingi naomba nina mna simu nitapata bidhaa nunua lipa oda sasa",
        "je sana una kwa ya wa na ni mtu sawa bado leo hii hiyo yangu"),
}

# Arabic, after normalize_arabic(). Sudanese markers include the colloquial Nile-valley forms (عايز، ده) that
# Sudanese customers use; MSA markers are formal-register words a Sudanese chat would not use.
_AR_SD = _markers(
    "داير دايره دايرين دايرا متين شنو زول هسع هسه ياخ ساي عديل قروش كدي بوصل بتوصل عايز عاوز عايزه عاوزه "
    "شايف حقتي حقي بتاعي بتاعتي سمح زي ليه لسع ياهو ياها تب كمان",
    "ده دي دا كده كدا عندكم بكم بكام لسه وين اها")
_AR_MSA = _markers(
    "اريد اود ارغب هل لديكم يتوفر متوفر متي ماذا سوف ليس معرفه هذا هذه الذي التي كيف لماذا يمكنني ارجو نود الان",
    "")

_ARABIC_CHAR = re.compile(r"[؀-ۿ]")
_LATIN_CHAR = re.compile(r"[A-Za-z]")
_NOISE = re.compile(r"\b[A-Za-z]{1,6}-\d+\b|\b[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+\b|\d[\d,.٫٬]*")


@dataclass
class Detection:
    code: str | None            # best language, or None when the message carries no usable signal
    confidence: float           # 0..1 that the message is in this language (family level for Arabic)
    evidence: int               # distinct words behind the decision
    dialect_confidence: float = 0.0  # Arabic only: how sure the ar / ar-SD split is (0 = no markers)
    scores: dict[str, int] | None = None  # Latin-language marker scores, to recognise code-switching
    arabic_words: int = 0


def normalize_arabic(text: str) -> str:
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))  # harakat and the hamza on أ/إ/آ
    return t.replace("ة", "ه").replace("ى", "ي").replace("ـ", "")


def _strip_ignored(text: str, ignore_terms: set[str]) -> str:
    low = _NOISE.sub(" ", text).lower()
    for term in sorted(ignore_terms, key=len, reverse=True):
        if len(term) >= 3:
            low = re.sub(rf"(?<!\w){re.escape(term)}(?!\w)", " ", low)
    return low


def _confidence(top: int, second: int) -> float:
    return round(min(1.0, top / 4) * (top - second) / top, 2) if top > 0 else 0.0


def detect(text: str | None, ignore_terms: set[str] | None = None) -> Detection:
    raw = _strip_ignored(text or "", {t.lower() for t in ignore_terms or set() if t})
    arabic, latin = len(_ARABIC_CHAR.findall(raw)), len(_LATIN_CHAR.findall(raw))
    words = re.findall(r"[ء-ي]+", normalize_arabic(raw))
    folded = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode()
    tokens = set(re.findall(r"[a-z]+", folded))
    scores = {lang: sum(m.get(t, 0) for t in tokens) for lang, m in _LATIN.items()}
    if arabic >= 3 and arabic >= latin:
        sd = sum(_AR_SD.get(w, 0) for w in set(words))
        msa = sum(_AR_MSA.get(w, 0) for w in set(words))
        family = round(min(1.0, len(words) / 2), 2)
        if sd == msa:
            return Detection("ar", family, len(words), 0.0, scores, len(words))
        code, top, second = ("ar-SD", sd, msa) if sd > msa else ("ar", msa, sd)
        return Detection(code, family, len(words), _confidence(top, second), scores, len(words))
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    if not top:
        return Detection(None, 0.0, 0, scores=scores, arabic_words=len(words))
    return Detection(best, _confidence(top, second), sum(1 for t in tokens if _LATIN[best].get(t)), 0.0, scores,
                     len(words))


def _still_speaking(current: str, det: Detection) -> bool:
    """The message still carries a distinctive word of the current language: the customer is mixing languages
    (normal code-switching), not leaving theirs."""
    if is_arabic(current):
        return det.arabic_words > 0 and not is_arabic(det.code)
    return (det.scores or {}).get(current, 0) >= 2


def is_arabic(code: str | None) -> bool:
    return code in ("ar", "ar-SD")


def next_language(current: str | None, det: Detection) -> tuple[str | None, bool]:
    """(conversation language after this message, whether this detection set or confirmed it).
    None means "no language known yet" and the caller uses the business language."""
    if det.code is None:
        return current, False
    if current is None:  # first signal in the conversation
        if det.confidence < INITIAL_CONFIDENCE:
            return None, False
        if is_arabic(det.code):
            return (det.code if det.dialect_confidence >= INITIAL_CONFIDENCE else "ar"), True
        return det.code, True
    if is_arabic(current) and is_arabic(det.code):
        if det.code != current and det.dialect_confidence >= SWITCH_CONFIDENCE:
            return det.code, True  # confident change of Arabic variant
        return current, det.code == current and det.dialect_confidence >= SWITCH_CONFIDENCE
    confident = det.confidence >= SWITCH_CONFIDENCE and det.evidence >= MIN_SWITCH_EVIDENCE
    if det.code == current:
        return current, confident
    if not confident or _still_speaking(current, det):
        return current, False  # weak signal, isolated words or code-switching: keep the conversation language
    if is_arabic(det.code):
        return (det.code if det.dialect_confidence >= INITIAL_CONFIDENCE else "ar"), True
    return det.code, True


def effective(code: str | None, default: str | None) -> str:
    """The language to speak: the conversation's, else the business default, else English."""
    for c in (code, default):
        if c in SUPPORTED:
            return c
    return "en"
