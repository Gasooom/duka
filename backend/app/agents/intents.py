"""Deterministic intent checks that must not depend on the LLM.

* classify_confirmation: is this message an explicit YES / NO to an order summary? Deliberately strict: a
  short, unambiguous affirmative only. Anything else ("yes but change the size", "maybe") is not a
  confirmation and goes back to the conversation.
* wants_human: the customer asks for a person. English, Kinyarwanda, French, Swahili.
"""
import re
import unicodedata

_AFFIRMATIVE = {
    # English
    "yes", "y", "yeah", "yep", "yup", "ok", "okay", "k", "confirm", "confirmed", "i confirm", "sure", "go ahead",
    "yes confirm", "confirm order", "confirm the order", "place it", "place the order", "proceed",
    # Kinyarwanda
    "yego", "yee", "ego", "nibyo", "ni byo", "ndabyemeje", "ndemeje", "ndemeza", "emeza", "byemejwe",
    # French
    "oui", "d accord", "dac", "je confirme", "confirme", "confirmer", "c est bon", "valide", "je valide",
    # Swahili
    "ndiyo", "ndio", "sawa", "naam",
}
_FILLERS = {"please", "pls", "plz", "thanks", "thank", "you", "murakoze", "merci", "asante", "sir", "madam",
            "now", "it", "s", "il", "vous", "plait", "te", "rwose", "cyane"}
_NEGATIVE = {
    "no", "n", "nope", "nah", "cancel", "stop", "dont", "don", "not", "wait", "change", "but",
    "oya", "hoya", "reka", "non", "annuler", "attends", "mais", "hapana", "subiri",
}
_EMOJI_YES = {"👍", "✅", "👌", "🙆", "👍🏽", "👍🏾", "👍🏿", "👍🏻", "👍🏼"}
_EMOJI_NO = {"👎", "❌", "🙅"}


def _normalize(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def classify_confirmation(text: str | None) -> str | None:
    """'yes' | 'no' | None."""
    raw = (text or "").strip()
    if not raw:
        return None
    if raw in _EMOJI_YES:
        return "yes"
    if raw in _EMOJI_NO:
        return "no"
    t = _normalize(raw)
    if not t or len(t.split()) > 6:
        return None
    words = t.split()
    if t in _NEGATIVE or words[0] in _NEGATIVE:
        return "no"
    if any(w in _NEGATIVE for w in words):
        return None  # mixed ("yes but ..."): not an explicit confirmation
    core = " ".join(w for w in words if w not in _FILLERS)
    return "yes" if core in _AFFIRMATIVE else None


# Request phrasing, not bare nouns: "human hair wigs" or "a person-sized bag" must stay product searches.
_WHO = r"(a |an |the |some |your |a real )?(person|someone|somebody|human|agent|staff|manager|owner|people|representative)"
_HUMAN_PATTERNS = [
    # English
    rf"\b(talk|speak|chat)\s+(to|with)\s+{_WHO}\b",
    rf"\b(i want|i need|i d like|get me|connect me( to| with)?|let me talk to|put me through to)\s+{_WHO}\b",
    r"\b(real person|live agent|human agent|customer (service|care|support))\b",
    r"\bcall me\b",
    # Kinyarwanda: kuvugana na (talk with), nshaka umuntu (I want a person), umukozi (staff), nyiri iduka (owner)
    r"\bkuvugana n", r"\bnshaka umuntu\b", r"\bumukozi\b", r"\bnyir(i)?\s?iduka\b",
    # French
    r"\bparler (a|avec)\b", r"\b(un humain|un conseiller|service client|le responsable|le gerant)\b",
    # Swahili
    r"\bkuongea na\b", r"\b(nataka|naomba)\s+(mtu|kuongea)\b", r"\bmhudumu\b",
]
_HUMAN_RE = re.compile("|".join(_HUMAN_PATTERNS))


def wants_human(text: str | None) -> bool:
    return bool(_HUMAN_RE.search(_normalize(text or "")))
