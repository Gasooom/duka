"""Server-written customer messages in the conversation language.

Only wording is localised. Facts — product names, prices (always formatted by the same `fmt`/`money`), SKUs,
order numbers, phone numbers, addresses and anything the owner typed (payment instructions, cancel reasons,
business hours) — are inserted unchanged, so localisation can never alter a commerce fact.

English texts are the originals. rw / sw / ar-SD wording should be reviewed by native speakers before the pilot;
the fallback order is: requested language -> ar (for ar-SD) -> en, and a test enforces that every key exists in
every supported language, so nothing silently falls back to English.
"""
from __future__ import annotations

from app.agents.language import SUPPORTED

L = SUPPORTED  # en rw fr sw ar ar-SD


def _row(en, rw, fr, sw, ar, ar_sd) -> dict[str, str]:
    return dict(zip(L, (en, rw, fr, sw, ar, ar_sd)))


MESSAGES: dict[str, dict[str, str]] = {
    # ---------------------------------------------------------------- catalog / cart (tool renders)
    "sorry_error": _row("Sorry — {error}", "Mbabarira — {error}", "Désolé — {error}", "Samahani — {error}",
                        "عذرًا — {error}", "معليش — {error}"),
    "search_none": _row(
        "Sorry, I couldn't find anything matching that in our catalog. Could you describe it differently?",
        "Mbabarira, nta gicuruzwa gihuye n'ibyo wavuze nabonye. Wabisobanura mu bundi buryo?",
        "Désolé, je n'ai rien trouvé de correspondant dans notre catalogue. Pouvez-vous le décrire autrement ?",
        "Samahani, sikupata bidhaa inayolingana na hiyo. Unaweza kuieleza kwa njia nyingine?",
        "عذرًا، لم أجد ما يطابق طلبك في منتجاتنا. هل يمكنك وصفه بطريقة أخرى؟",
        "معليش، ما لقينا حاجة زي دي في منتجاتنا. ممكن توصفها لينا بطريقة تانية؟"),
    "search_found": _row("Here's what I found ({count}):", "Dore ibyo nabonye ({count}):",
                         "Voici ce que j'ai trouvé ({count}) :", "Hivi ndivyo nilivyopata ({count}):",
                         "إليك ما وجدته ({count}):", "ده اللقيناهو ({count}):"),
    "in_stock": _row("in stock", "birahari", "en stock", "kipo", "متوفر", "موجود"),
    "out_of_stock": _row("out of stock", "byashize", "en rupture", "kimeisha", "غير متوفر", "خلص"),
    "in_stock_cap": _row("In stock", "Birahari", "En stock", "Kipo", "متوفر", "موجود"),
    "out_of_stock_cap": _row("Out of stock", "Byashize", "En rupture", "Kimeisha", "غير متوفر", "خلص"),
    "available_count": _row("({qty} available)", "({qty} birahari)", "({qty} disponible(s))", "({qty} vipo)",
                            "({qty} متوفر)", "({qty} موجودين)"),
    "search_hint": _row('Reply e.g. "add 2" to add an item to your cart.',
                        "Mbwira nimero y'icyo ushaka ko nshyira mu gitebo cyawe.",
                        "Dites-moi le numéro de l'article à ajouter à votre panier.",
                        "Niambie namba ya bidhaa unayotaka niweke kwenye kikapu chako.",
                        "أخبرني برقم المنتج الذي تريد إضافته إلى سلتك.",
                        "قول لينا رقم الحاجة الدايرها عشان نضيفها للسلة."),
    "added": _row("✅ Added {name} x{qty} to your cart.", "✅ Nashyize {name} x{qty} mu gitebo cyawe.",
                  "✅ {name} x{qty} ajouté à votre panier.", "✅ Nimeweka {name} x{qty} kwenye kikapu chako.",
                  "✅ تمت إضافة {name} x{qty} إلى سلتك.", "✅ ضفنا {name} x{qty} للسلة بتاعتك."),
    "cart_empty": _row("Your cart is empty. Tell me what you're looking for!",
                       "Igitebo cyawe kirimo ubusa. Mbwira icyo ushaka!",
                       "Votre panier est vide. Dites-moi ce que vous cherchez !",
                       "Kikapu chako ni tupu. Niambie unachotafuta!",
                       "سلتك فارغة. أخبرني بما تبحث عنه!", "السلة فاضية. قول لينا داير شنو!"),
    "cart_title": _row("🛒 Your cart:", "🛒 Igitebo cyawe:", "🛒 Votre panier :", "🛒 Kikapu chako:", "🛒 سلتك:",
                       "🛒 السلة بتاعتك:"),
    "subtotal": _row("Subtotal", "Igiteranyo cy'ibicuruzwa", "Sous-total", "Jumla ndogo", "المجموع الفرعي",
                     "حساب الحاجات"),
    "delivery": _row("Delivery", "Kugemura", "Livraison", "Usafirishaji", "التوصيل", "التوصيل"),
    "discount": _row("Discount", "Igabanywa", "Remise", "Punguzo", "الخصم", "الخصم"),
    "total": _row("Total", "Igiteranyo cyose", "Total", "Jumla", "الإجمالي", "الجملة"),
    "total_before_delivery": _row("Total before delivery", "Igiteranyo utabariyemo kugemura", "Total hors livraison",
                                  "Jumla bila usafirishaji", "الإجمالي قبل التوصيل", "الجملة من غير التوصيل"),
    "delivery_pending": _row(
        "Delivery fee depends on your area. Share your delivery location for the exact total.",
        "Igiciro cyo kugemura giterwa n'aho uherereye. Mbwira aho tuzakugereza ndakubwira igiteranyo nyacyo.",
        "Les frais de livraison dépendent de votre quartier. Indiquez votre adresse pour obtenir le total exact.",
        "Gharama ya usafirishaji inategemea eneo lako. Nitumie mahali pa kukuletea ili nikupe jumla kamili.",
        "رسوم التوصيل تعتمد على منطقتك. شاركنا موقعك لنحسب الإجمالي بدقة.",
        "رسوم التوصيل بتعتمد على منطقتك. رسل لينا مكانك عشان نديك الجملة بالضبط."),
    "stock_issue": _row("{name}: only {qty} in stock", "{name}: hasigaye {qty} gusa", "{name} : seulement {qty} en stock",
                        "{name}: zimebaki {qty} tu", "{name}: المتوفر {qty} فقط", "{name}: الفاضل {qty} بس"),
    "unavailable_issue": _row("{name} is unavailable", "{name} ntikiboneka", "{name} n'est pas disponible",
                              "{name} haipatikani", "{name} غير متاح", "{name} ما متوفر"),
    "removed": _row("Removed. ", "Byakuweho. ", "Retiré. ", "Imeondolewa. ", "تمت الإزالة. ", "شلناها. "),
    "cart_cleared": _row("Your cart is now empty.", "Igitebo cyawe ubu kirimo ubusa.",
                         "Votre panier est maintenant vide.", "Kikapu chako sasa ni tupu.", "سلتك الآن فارغة.",
                         "السلة بقت فاضية."),
    "delivery_quote": _row("Delivery to {zone}: {fee}{eta}.", "Kugemura i {zone}: {fee}{eta}.",
                           "Livraison à {zone} : {fee}{eta}.", "Usafirishaji hadi {zone}: {fee}{eta}.",
                           "التوصيل إلى {zone}: {fee}{eta}.", "التوصيل لـ {zone}: {fee}{eta}."),
    "eta": _row(" (estimated {time})", " (igihe giteganyijwe: {time})", " (délai estimé : {time})",
                " (muda unaokadiriwa: {time})", " (الوقت المتوقع: {time})", " (الزمن المتوقع: {time})"),
    "no_delivery_there": _row("Sorry, we don't deliver there.", "Mbabarira, ntitugemura aho hantu.",
                              "Désolé, nous ne livrons pas à cet endroit.", "Samahani, hatusafirishi huko.",
                              "عذرًا، لا نوصّل إلى هذا المكان.", "معليش، ما بنوصل للمكان ده."),
    "order_status": _row("Order {number} is {status}. Total {total}.{payment}", "Komande {number}: {status}. "
                         "Igiteranyo {total}.{payment}", "La commande {number} est {status}. Total {total}.{payment}",
                         "Oda {number}: {status}. Jumla {total}.{payment}",
                         "الطلب {number}: {status}. الإجمالي {total}.{payment}",
                         "الطلب {number}: {status}. الجملة {total}.{payment}"),
    "payment_part": _row(" Payment: {payment}.", " Kwishyura: {payment}.", " Paiement : {payment}.",
                         " Malipo: {payment}.", " الدفع: {payment}.", " الدفع: {payment}."),
    "no_orders": _row("You don't have any orders yet.", "Nta komande uratanga.", "Vous n'avez pas encore de commande.",
                      "Bado huna oda yoyote.", "ليس لديك أي طلبات حتى الآن.", "لسه ما عندك أي طلب."),
    "recent_orders": _row("Your recent orders:", "Komande zawe ziheruka:", "Vos commandes récentes :",
                          "Oda zako za hivi karibuni:", "طلباتك الأخيرة:", "طلباتك الأخيرة:"),
    "reference_passed": _row(
        "Thanks! I've passed reference {ref} for order {number} to the shop. They'll confirm once they have "
        "checked the payment.",
        "Murakoze! Nashyikirije iduka nimero y'ubwishyu {ref} ya komande {number}. Bazemeza nibamara kugenzura "
        "ubwishyu.",
        "Merci ! J'ai transmis la référence {ref} de la commande {number} à la boutique. Elle confirmera après "
        "avoir vérifié le paiement.",
        "Asante! Nimeipa duka kumbukumbu {ref} ya oda {number}. Watathibitisha baada ya kukagua malipo.",
        "شكرًا! أرسلت المرجع {ref} للطلب {number} إلى المتجر. سيؤكدون بعد التحقق من الدفع.",
        "شكرًا! رسلنا رقم العملية {ref} للطلب {number} للمحل. حيأكدو ليك بعد ما يراجعو الدفع."),
    "manual_pay": _row(
        "Order {number}: {amount}.\nTo pay: {instructions}\nReply with the transaction ID once you have paid.",
        "Komande {number}: {amount}.\nKwishyura: {instructions}\nNumara kwishyura, nyoherereza nimero y'ubwishyu.",
        "Commande {number} : {amount}.\nPour payer : {instructions}\nEnvoyez la référence de la transaction "
        "une fois le paiement effectué.",
        "Oda {number}: {amount}.\nKulipa: {instructions}\nUkishalipa, nitumie namba ya muamala.",
        "الطلب {number}: {amount}.\nللدفع: {instructions}\nأرسل رقم العملية بعد الدفع.",
        "الطلب {number}: {amount}.\nعشان تدفع: {instructions}\nبعد ما تدفع رسل لينا رقم العملية."),
    "momo_sent": _row(
        "📲 I've sent a mobile money request of {amount} to {phone} for order {number}. Please approve it on your "
        "phone — I'll confirm as soon as the payment is received.",
        "📲 Nohereje gusaba kwishyura {amount} kuri {phone} kuri komande {number}. Byemeze kuri telefone yawe — "
        "ndakumenyesha nkimara kubona ubwishyu.",
        "📲 J'ai envoyé une demande de paiement mobile de {amount} au {phone} pour la commande {number}. Validez-la "
        "sur votre téléphone — je confirmerai dès réception du paiement.",
        "📲 Nimetuma ombi la malipo ya simu la {amount} kwa {phone} kwa oda {number}. Tafadhali likubali kwenye "
        "simu yako — nitathibitisha mara malipo yatakapopokelewa.",
        "📲 أرسلت طلب دفع عبر الهاتف بمبلغ {amount} إلى {phone} للطلب {number}. وافق عليه من هاتفك — "
        "سأؤكد فور استلام الدفع.",
        "📲 رسلنا طلب دفع بالموبايل بـ {amount} للرقم {phone} للطلب {number}. وافق عليهو من تلفونك — "
        "حنأكد ليك أول ما الدفع يوصل."),
    "knowledge_none": _row("I'm not sure about that. Would you like me to connect you with our team?",
                           "Ibyo sinabyizeye. Wifuza ko nguhuza n'abakozi bacu?",
                           "Je n'en suis pas sûr. Voulez-vous que je vous mette en contact avec notre équipe ?",
                           "Sina uhakika kuhusu hilo. Ungependa nikuunganishe na timu yetu?",
                           "لست متأكدًا من ذلك. هل تريد أن أوصلك بفريقنا؟",
                           "ما متأكد من الحاجة دي. داير نوصلك بزول من المحل؟"),
    "biz_delivery": _row("🚚 Delivery:", "🚚 Kugemura:", "🚚 Livraison :", "🚚 Usafirishaji:", "🚚 التوصيل:",
                         "🚚 التوصيل:"),
    "done": _row("Done.", "Byakozwe.", "C'est fait.", "Imekamilika.", "تم.", "تم."),
    # ---------------------------------------------------------------- order statuses (customer wording)
    "status_pending": _row("waiting for the shop's review", "itegereje ko iduka riyisuzuma",
                           "en attente de validation par la boutique", "inasubiri ukaguzi wa duka",
                           "بانتظار مراجعة المتجر", "منتظر المحل يراجعو"),
    "status_accepted": _row("accepted", "yemewe", "acceptée", "imekubaliwa", "مقبول", "اتقبل"),
    "status_ready": _row("ready", "iteguye", "prête", "iko tayari", "جاهز", "جاهز"),
    "status_out_for_delivery": _row("out for delivery", "iri mu nzira", "en cours de livraison", "iko njiani",
                                    "في الطريق إليك", "في الطريق ليك"),
    "status_delivered": _row("delivered", "yagejejwe", "livrée", "imefikishwa", "تم التسليم", "اتسلم"),
    "status_cancelled": _row("cancelled", "yahagaritswe", "annulée", "imeghairiwa", "ملغي", "اتلغى"),
    "pay_unpaid": _row("unpaid", "ntiyishyuwe", "non payée", "haijalipwa", "غير مدفوع", "ما مدفوع"),
    "pay_pending": _row("pending", "itegereje kwemezwa", "en attente de confirmation", "inasubiri uthibitisho",
                        "بانتظار التأكيد", "منتظر التأكيد"),
    "pay_paid": _row("paid", "yishyuwe", "payée", "imelipwa", "مدفوع", "مدفوع"),
    # ---------------------------------------------------------------- checkout
    "summary_title": _row("🧾 Order summary — please check:", "🧾 Incamake ya komande — banza urebe:",
                          "🧾 Récapitulatif de la commande — veuillez vérifier :", "🧾 Muhtasari wa oda — tafadhali kagua:",
                          "🧾 ملخص الطلب — يرجى المراجعة:", "🧾 ملخص الطلب — راجعو لو سمحت:"),
    "deliver_to": _row("Deliver to: {address}", "Aho kugemura: {address}", "Livrer à : {address}",
                       "Peleka: {address}", "التوصيل إلى: {address}", "التوصيل لـ: {address}"),
    "pickup": _row("Pickup at the shop", "Kuzafatira ku iduka", "Retrait à la boutique", "Kuchukua dukani",
                   "الاستلام من المتجر", "استلام من المحل"),
    "confirm_prompt": _row("Reply YES to confirm this order, or tell me what to change.",
                           "Subiza YEGO kugira ngo wemeze iyi komande, cyangwa umbwire icyo wahindura.",
                           "Répondez OUI pour confirmer cette commande, ou dites-moi ce qu'il faut changer.",
                           "Jibu NDIYO kuthibitisha oda hii, au niambie cha kubadilisha.",
                           "أرسل «نعم» لتأكيد الطلب، أو أخبرني بما تريد تغييره.",
                           "رد بـ «أيوه» عشان نأكد الطلب، أو قول لينا داير تغير شنو."),
    "updated_summary": _row("{reason} Here is the updated summary:\n\n{summary}",
                            "{reason} Dore incamake nshya:\n\n{summary}",
                            "{reason} Voici le récapitulatif mis à jour :\n\n{summary}",
                            "{reason} Huu hapa muhtasari mpya:\n\n{summary}",
                            "{reason} إليك الملخص المحدّث:\n\n{summary}",
                            "{reason} ده الملخص الجديد:\n\n{summary}"),
    "could_not_place": _row("Sorry — I couldn't place the order: {reason}",
                            "Mbabarira — sinashoboye gutanga komande: {reason}",
                            "Désolé — je n'ai pas pu passer la commande : {reason}",
                            "Samahani — sikuweza kuweka oda: {reason}", "عذرًا — لم أتمكن من تسجيل الطلب: {reason}",
                            "معليش — ما قدرنا نسجل الطلب: {reason}"),
    "declined": _row("No problem — the order was not placed. What would you like to change?",
                     "Nta kibazo — komande ntiyatanzwe. Ni iki wifuza guhindura?",
                     "Pas de problème — la commande n'a pas été passée. Que souhaitez-vous changer ?",
                     "Hakuna shida — oda haijawekwa. Ungependa kubadilisha nini?",
                     "لا مشكلة — لم يتم تسجيل الطلب. ما الذي تريد تغييره؟",
                     "ما في مشكلة — الطلب ما اتسجل. داير تغير شنو؟"),
    # ---------------------------------------------------------------- orders, payments, statuses
    "order_confirmed": _row("✅ Order {number} confirmed!", "✅ Komande {number} yemejwe!",
                            "✅ Commande {number} confirmée !", "✅ Oda {number} imethibitishwa!",
                            "✅ تم تأكيد الطلب {number}!", "✅ الطلب {number} اتأكد!"),
    "review_soon": _row("{shop} will review it and confirm shortly.", "{shop} iraza kuyisuzuma no kuyemeza vuba.",
                        "{shop} va la vérifier et la confirmer sous peu.",
                        "{shop} wataikagua na kuithibitisha hivi karibuni.",
                        "سيراجع {shop} الطلب ويؤكده قريبًا.", "{shop} حيراجعو الطلب ويأكدوهو قريب."),
    "review_closed": _row("{shop} is closed right now and will review it when it opens ({opening}).",
                          "{shop} ubu rifunze; rizayisuzuma nirifungura ({opening}).",
                          "{shop} est fermée pour le moment et la vérifiera à l'ouverture ({opening}).",
                          "{shop} imefungwa kwa sasa na wataikagua watakapofungua ({opening}).",
                          "{shop} مغلق الآن وسيراجع الطلب عند الافتتاح ({opening}).",
                          "{shop} قافل هسع وحيراجعو الطلب لمن يفتح ({opening})."),
    "accepted": _row("✅ {shop} accepted your order {number} ({total}).",
                     "✅ {shop} yemeye komande yawe {number} ({total}).",
                     "✅ {shop} a accepté votre commande {number} ({total}).",
                     "✅ {shop} wamekubali oda yako {number} ({total}).",
                     "✅ قبل {shop} طلبك {number} ({total}).", "✅ {shop} قبلو طلبك {number} ({total})."),
    "to_pay": _row("To pay: {instructions}\nReply with the transaction ID once you have paid.",
                   "Kwishyura: {instructions}\nNumara kwishyura, nyoherereza nimero y'ubwishyu.",
                   "Pour payer : {instructions}\nEnvoyez la référence de la transaction une fois le paiement effectué.",
                   "Kulipa: {instructions}\nUkishalipa, nitumie namba ya muamala.",
                   "للدفع: {instructions}\nأرسل رقم العملية بعد الدفع.",
                   "عشان تدفع: {instructions}\nبعد ما تدفع رسل لينا رقم العملية."),
    "cancelled": _row("❌ Your order {number} was cancelled{reason}.", "❌ Komande yawe {number} yahagaritswe{reason}.",
                      "❌ Votre commande {number} a été annulée{reason}.", "❌ Oda yako {number} imeghairiwa{reason}.",
                      "❌ تم إلغاء طلبك {number}{reason}.", "❌ طلبك {number} اتلغى{reason}."),
    "refund_note": _row(" We'll contact you about your refund.", " Tuzakuvugisha ku bijyanye no gusubizwa amafaranga.",
                        " Nous vous contacterons pour le remboursement.", " Tutawasiliana nawe kuhusu kurejeshewa pesa.",
                        " سنتواصل معك بخصوص استرداد المبلغ.", " حنتواصل معاك عشان نرجع ليك قروشك."),
    "ready": _row("📦 Your order {number} is ready.", "📦 Komande yawe {number} iteguye.",
                  "📦 Votre commande {number} est prête.", "📦 Oda yako {number} iko tayari.",
                  "📦 طلبك {number} جاهز.", "📦 طلبك {number} جاهز."),
    "out_for_delivery": _row("🚚 Your order {number} is on the way.", "🚚 Komande yawe {number} iri mu nzira.",
                             "🚚 Votre commande {number} est en route.", "🚚 Oda yako {number} iko njiani.",
                             "🚚 طلبك {number} في الطريق إليك.", "🚚 طلبك {number} في الطريق ليك."),
    "delivered": _row("✅ Your order {number} was delivered. Thank you for shopping with {shop}!",
                      "✅ Komande yawe {number} yagejejwe. Murakoze guhahira kuri {shop}!",
                      "✅ Votre commande {number} a été livrée. Merci d'avoir acheté chez {shop} !",
                      "✅ Oda yako {number} imefikishwa. Asante kwa kununua kwa {shop}!",
                      "✅ تم تسليم طلبك {number}. شكرًا لتسوقك من {shop}!",
                      "✅ طلبك {number} اتسلم. شكرًا إنك اشتريت من {shop}!"),
    "manual_payment_received": _row("✅ Payment received for order {number} ({amount}). Thank you!",
                                    "✅ Ubwishyu bwa komande {number} bwakiriwe ({amount}). Murakoze!",
                                    "✅ Paiement reçu pour la commande {number} ({amount}). Merci !",
                                    "✅ Malipo ya oda {number} yamepokelewa ({amount}). Asante!",
                                    "✅ تم استلام الدفع للطلب {number} ({amount}). شكرًا!",
                                    "✅ استلمنا دفع الطلب {number} ({amount}). شكرًا!"),
    "provider_paid": _row(
        "✅ Payment received! Order {number} is now PAID.\nAmount: {amount}\nWe'll let you know when it's on the "
        "way. Thank you for shopping with {shop}!",
        "✅ Ubwishyu bwakiriwe! Komande {number} ubu YARISHYUWE.\nAmafaranga: {amount}\nTuzakumenyesha niba iri mu "
        "nzira. Murakoze guhahira kuri {shop}!",
        "✅ Paiement reçu ! La commande {number} est maintenant PAYÉE.\nMontant : {amount}\nNous vous préviendrons "
        "dès qu'elle sera en route. Merci d'avoir acheté chez {shop} !",
        "✅ Malipo yamepokelewa! Oda {number} sasa IMELIPWA.\nKiasi: {amount}\nTutakujulisha ikiwa njiani. Asante "
        "kwa kununua kwa {shop}!",
        "✅ تم استلام الدفع! الطلب {number} مدفوع الآن.\nالمبلغ: {amount}\nسنخبرك عندما يكون في الطريق. شكرًا "
        "لتسوقك من {shop}!",
        "✅ الدفع وصل! الطلب {number} بقى مدفوع.\nالمبلغ: {amount}\nحنكلمك لمن يكون في الطريق. شكرًا إنك اشتريت "
        "من {shop}!"),
    "provider_failed": _row(
        "❌ Payment for order {number} was not completed{reason}. Reply 'pay' to try again.",
        "❌ Kwishyura komande {number} ntibyarangiye{reason}. Andika 'kwishyura' wongere ugerageze.",
        "❌ Le paiement de la commande {number} n'a pas abouti{reason}. Répondez « payer » pour réessayer.",
        "❌ Malipo ya oda {number} hayakukamilika{reason}. Jibu 'lipa' kujaribu tena.",
        "❌ لم يكتمل الدفع للطلب {number}{reason}. أرسل «ادفع» للمحاولة مرة أخرى.",
        "❌ الدفع للطلب {number} ما كمل{reason}. رد بـ «ادفع» عشان تجرب تاني."),
    # ---------------------------------------------------------------- human control
    "handoff": _row("I've passed your conversation to our team. Someone will reply here shortly.",
                    "Nashyikirije ikiganiro cyawe abakozi bacu. Umuntu araza kugusubiza hano vuba.",
                    "J'ai transmis votre conversation à notre équipe. Quelqu'un vous répondra ici sous peu.",
                    "Nimewapa timu yetu mazungumzo yako. Mtu atakujibu hapa hivi karibuni.",
                    "حوّلت محادثتك إلى فريقنا. سيرد عليك أحدهم هنا قريبًا.",
                    "حولنا كلامك لناس المحل. في زول حيرد عليك هنا قريب."),
    "handoff_closed": _row(
        "I've passed your conversation to our team. We're closed right now, so someone will reply here when we "
        "open ({opening}).",
        "Nashyikirije ikiganiro cyawe abakozi bacu. Ubu dufunze, umuntu azagusubiza hano nidufungura ({opening}).",
        "J'ai transmis votre conversation à notre équipe. Nous sommes fermés pour le moment ; quelqu'un vous "
        "répondra ici à l'ouverture ({opening}).",
        "Nimewapa timu yetu mazungumzo yako. Tumefunga kwa sasa, mtu atakujibu hapa tutakapofungua ({opening}).",
        "حوّلت محادثتك إلى فريقنا. نحن مغلقون الآن، وسيرد عليك أحدهم هنا عند الافتتاح ({opening}).",
        "حولنا كلامك لناس المحل. نحنا قافلين هسع، وفي زول حيرد عليك هنا لمن نفتح ({opening})."),
    "handoff_message": _row("I've passed your message to our team. Someone will reply here shortly.",
                            "Nashyikirije ubutumwa bwawe abakozi bacu. Umuntu araza kugusubiza hano vuba.",
                            "J'ai transmis votre message à notre équipe. Quelqu'un vous répondra ici sous peu.",
                            "Nimewapa timu yetu ujumbe wako. Mtu atakujibu hapa hivi karibuni.",
                            "حوّلت رسالتك إلى فريقنا. سيرد عليك أحدهم هنا قريبًا.",
                            "رسلنا رسالتك لناس المحل. في زول حيرد عليك هنا قريب."),
    "handoff_message_closed": _row(
        "I've passed your message to our team. We're closed right now, so someone will reply here when we open "
        "({opening}).",
        "Nashyikirije ubutumwa bwawe abakozi bacu. Ubu dufunze, umuntu azagusubiza hano nidufungura ({opening}).",
        "J'ai transmis votre message à notre équipe. Nous sommes fermés pour le moment ; quelqu'un vous répondra "
        "ici à l'ouverture ({opening}).",
        "Nimewapa timu yetu ujumbe wako. Tumefunga kwa sasa, mtu atakujibu hapa tutakapofungua ({opening}).",
        "حوّلت رسالتك إلى فريقنا. نحن مغلقون الآن، وسيرد عليك أحدهم هنا عند الافتتاح ({opening}).",
        "رسلنا رسالتك لناس المحل. نحنا قافلين هسع، وفي زول حيرد عليك هنا لمن نفتح ({opening})."),
    "media_unsupported": _row("I can't open {label} yet. ", "Sindashobora gufungura {label}. ",
                              "Je ne peux pas encore ouvrir {label}. ", "Bado siwezi kufungua {label}. ",
                              "لا أستطيع فتح {label} حاليًا. ", "لسه ما بقدر أفتح {label}. "),
    "text_only": _row("Sorry, I can only read text messages for now. Please type your request.",
                      "Mbabarira, ubu nshobora gusoma ubutumwa bwanditse gusa. Andika icyo ukeneye.",
                      "Désolé, je ne peux lire que les messages texte pour le moment. Écrivez votre demande.",
                      "Samahani, kwa sasa ninaweza kusoma ujumbe wa maandishi tu. Tafadhali andika ombi lako.",
                      "عذرًا، أستطيع قراءة الرسائل النصية فقط حاليًا. اكتب طلبك من فضلك.",
                      "معليش، هسع بقدر أقرا الرسايل المكتوبة بس. اكتب لينا طلبك."),
    "ai_paused_soon": _row("Thanks for your message! Our team will reply here soon.",
                           "Murakoze ku butumwa bwanyu! Abakozi bacu baraza kubasubiza hano vuba.",
                           "Merci pour votre message ! Notre équipe vous répondra ici bientôt.",
                           "Asante kwa ujumbe wako! Timu yetu itakujibu hapa hivi karibuni.",
                           "شكرًا لرسالتك! سيرد عليك فريقنا هنا قريبًا.",
                           "شكرًا على رسالتك! ناس المحل حيردو عليك هنا قريب."),
    "ai_paused_closed": _row("Thanks for your message! Our team will reply here when we open ({opening}).",
                             "Murakoze ku butumwa bwanyu! Abakozi bacu bazabasubiza hano nidufungura ({opening}).",
                             "Merci pour votre message ! Notre équipe vous répondra ici à l'ouverture ({opening}).",
                             "Asante kwa ujumbe wako! Timu yetu itakujibu hapa tutakapofungua ({opening}).",
                             "شكرًا لرسالتك! سيرد عليك فريقنا هنا عند الافتتاح ({opening}).",
                             "شكرًا على رسالتك! ناس المحل حيردو عليك هنا لمن نفتح ({opening})."),
    "handoff_unavailable": _row("Our team isn't available on this chat right now.",
                                "Abakozi bacu ntibaboneka kuri iki kiganiro ubu.",
                                "Notre équipe n'est pas disponible sur ce chat pour le moment.",
                                "Timu yetu haipatikani kwenye mazungumzo haya kwa sasa.",
                                "فريقنا غير متاح على هذه المحادثة حاليًا.", "ناس المحل ما موجودين في الشات ده هسع."),
    "contact_us": _row(" You can reach us at {phone}.", " Mushobora kutuvugisha kuri {phone}.",
                       " Vous pouvez nous joindre au {phone}.", " Unaweza kutupata kwa {phone}.",
                       " يمكنك التواصل معنا على {phone}.", " ممكن تتصل علينا في {phone}."),
    "unsure": _row(
        "I want to be sure I give you correct information. Could you tell me which product or order you mean? You "
        "can also ask to talk to our team.",
        "Ndashaka kuguha amakuru nyayo. Wambwira igicuruzwa cyangwa komande uvuga? Ushobora no gusaba kuvugana "
        "n'abakozi bacu.",
        "Je veux être sûr de vous donner une information exacte. De quel produit ou de quelle commande parlez-vous ? "
        "Vous pouvez aussi demander à parler à notre équipe.",
        "Nataka kuhakikisha nakupa taarifa sahihi. Unazungumzia bidhaa au oda gani? Unaweza pia kuomba kuongea na "
        "timu yetu.",
        "أريد التأكد من إعطائك معلومات صحيحة. ما المنتج أو الطلب الذي تقصده؟ يمكنك أيضًا طلب التحدث مع فريقنا.",
        "داير أتأكد إني بديك معلومة صاح. قصدك ياتو حاجة أو ياتو طلب؟ وممكن كمان تطلب تتكلم مع زول من المحل."),
    "fallback": _row("Sorry, I'm having trouble processing that right now. Please try again or contact support.",
                     "Mbabarira, ubu mfite ikibazo cyo gusubiza ibyo. Ongera ugerageze cyangwa uvugane n'abakozi.",
                     "Désolé, j'ai du mal à traiter cela pour le moment. Réessayez ou contactez l'assistance.",
                     "Samahani, nina tatizo kushughulikia hilo kwa sasa. Jaribu tena au wasiliana na huduma.",
                     "عذرًا، أواجه مشكلة في معالجة ذلك الآن. حاول مرة أخرى أو تواصل مع الدعم.",
                     "معليش، عندي مشكلة هسع في الرد على ده. جرب تاني أو كلم ناس المحل."),
    "greeting": _row("Hello! Welcome to {shop}. How can I help you today?",
                     "Muraho! Murakaza neza kuri {shop}. Nabafasha iki uyu munsi?",
                     "Bonjour ! Bienvenue chez {shop}. Comment puis-je vous aider aujourd'hui ?",
                     "Habari! Karibu {shop}. Nikusaidie nini leo?",
                     "مرحبًا! أهلًا بك في {shop}. كيف يمكنني مساعدتك اليوم؟",
                     "أهلًا وسهلًا! مرحب بيك في {shop}. نقدر نساعدك بشنو اليوم؟"),
    # ---------------------------------------------------------------- opening times
    "opening": _row("{day} at {time}", "{day} saa {time}", "{day} à {time}", "{day}, saa {time}",
                    "{day} الساعة {time}", "{day} الساعة {time}"),
    "today": _row("today", "uyu munsi", "aujourd'hui", "leo", "اليوم", "اليوم"),
    "tomorrow": _row("tomorrow", "ejo", "demain", "kesho", "غدًا", "بكرة"),
    # ---------------------------------------------------------------- user-facing tool errors (by error code)
    "err_out_of_stock": _row("Only {qty} unit(s) of {name} in stock", "{name}: hasigaye {qty} gusa",
                             "Il ne reste que {qty} unité(s) de {name}", "Zimebaki {qty} tu za {name}",
                             "المتوفر من {name} هو {qty} فقط", "الفاضل من {name} {qty} بس"),
    "err_product_unavailable": _row("{name} is not available", "{name} ntikiboneka", "{name} n'est pas disponible",
                                    "{name} haipatikani", "{name} غير متاح", "{name} ما متوفر"),
    "err_max_quantity": _row("Maximum {max} units per product", "Ntibirenze {max} kuri buri gicuruzwa",
                             "Maximum {max} unités par produit", "Kiwango cha juu ni {max} kwa kila bidhaa",
                             "الحد الأقصى {max} قطعة لكل منتج", "أقصى حاجة {max} قطعة من كل منتج"),
    "err_not_in_cart": _row("That product is not in the cart", "Icyo gicuruzwa ntikiri mu gitebo",
                            "Ce produit n'est pas dans le panier", "Bidhaa hiyo haipo kwenye kikapu",
                            "هذا المنتج غير موجود في السلة", "الحاجة دي ما في السلة"),
    "err_cart_empty": _row("The cart is empty. Add products before checking out.",
                           "Igitebo kirimo ubusa. Banza ushyiremo ibicuruzwa.",
                           "Le panier est vide. Ajoutez des produits avant de commander.",
                           "Kikapu ni tupu. Ongeza bidhaa kabla ya kuagiza.",
                           "السلة فارغة. أضف منتجات قبل الطلب.", "السلة فاضية. ضيف حاجات قبل ما تطلب."),
    "err_address_required": _row(
        "Please share your delivery address (area and street or a landmark) so I can prepare your order.",
        "Mbwira aho tuzakugereza (agace n'umuhanda cyangwa ahantu hazwi) kugira ngo ntegure komande yawe.",
        "Indiquez votre adresse de livraison (quartier et rue ou un repère) pour que je prépare votre commande.",
        "Tafadhali nitumie anwani ya kukuletea (eneo na mtaa au alama) ili niandae oda yako.",
        "يرجى إرسال عنوان التوصيل (المنطقة والشارع أو علامة مميزة) لأجهّز طلبك.",
        "رسل لينا عنوان التوصيل (الحي والشارع أو علامة قريبة) عشان نجهز ليك الطلب."),
    "err_no_delivery_zone": _row("No delivery zone covers '{location}'. Available zones: {zones}.",
                                 "Nta gace tugemuramo kagera kuri '{location}'. Uduce tugemuramo: {zones}.",
                                 "Aucune zone de livraison ne couvre « {location} ». Zones disponibles : {zones}.",
                                 "Hatuna eneo la usafirishaji linalofika '{location}'. Maeneo yaliyopo: {zones}.",
                                 "لا توجد منطقة توصيل تغطي '{location}'. المناطق المتاحة: {zones}.",
                                 "ما بنوصل لـ '{location}'. المناطق البنوصل ليها: {zones}."),
    "err_need_location": _row("Please share your delivery location to calculate the fee.",
                              "Mbwira aho tuzakugereza kugira ngo mbare igiciro cyo kugemura.",
                              "Indiquez votre lieu de livraison pour calculer les frais.",
                              "Tafadhali nitumie mahali pa kukuletea ili nikokotoe gharama.",
                              "يرجى إرسال موقع التوصيل لحساب الرسوم.", "رسل لينا مكان التوصيل عشان نحسب الرسوم."),
    "err_pickup_only": _row("Delivery is not offered; orders are for pickup.",
                            "Ntitugemura; komande zifatirwa ku iduka.",
                            "La livraison n'est pas proposée ; les commandes sont à retirer en boutique.",
                            "Hatusafirishi; oda zinachukuliwa dukani.",
                            "لا نقدم خدمة التوصيل؛ الطلبات تُستلم من المتجر.",
                            "ما عندنا توصيل؛ الطلبات بتتستلم من المحل."),
    "err_no_checkout": _row("There is no order summary waiting for confirmation.",
                            "Nta ncamake ya komande itegereje kwemezwa.",
                            "Aucun récapitulatif de commande n'attend de confirmation.",
                            "Hakuna muhtasari wa oda unaosubiri uthibitisho.",
                            "لا يوجد ملخص طلب بانتظار التأكيد.", "ما في ملخص طلب منتظر التأكيد."),
    "err_summary_not_delivered": _row("The order summary was not delivered yet.",
                                      "Incamake ya komande ntiragera.",
                                      "Le récapitulatif de la commande n'a pas encore été remis.",
                                      "Muhtasari wa oda bado haujafika.", "لم يصل ملخص الطلب بعد.",
                                      "ملخص الطلب لسه ما وصل."),
    "err_checkout_expired": _row("That order summary has expired.", "Iyo ncamake ya komande yarengeje igihe.",
                                 "Ce récapitulatif de commande a expiré.", "Muhtasari huo wa oda umeisha muda.",
                                 "انتهت صلاحية ملخص الطلب.", "ملخص الطلب ده انتهى."),
    "err_checkout_changed": _row("The cart, prices, stock or delivery changed since the summary.",
                                 "Igitebo, ibiciro, ibihari cyangwa kugemura byahindutse nyuma y'incamake.",
                                 "Le panier, les prix, le stock ou la livraison ont changé depuis le récapitulatif.",
                                 "Kikapu, bei, bidhaa zilizopo au usafirishaji vimebadilika tangu muhtasari.",
                                 "تغيرت السلة أو الأسعار أو المخزون أو التوصيل منذ الملخص.",
                                 "السلة أو الأسعار أو الكميات أو التوصيل اتغيرت بعد الملخص."),
    "err_order_not_found": _row("Order {number} not found", "Komande {number} ntiyabonetse",
                                "Commande {number} introuvable", "Oda {number} haikupatikana",
                                "الطلب {number} غير موجود", "الطلب {number} ما لقيناهو"),
    "err_no_orders": _row("You have no orders yet", "Nta komande uratanga", "Vous n'avez pas encore de commande",
                          "Bado huna oda", "ليس لديك طلبات بعد", "لسه ما عندك طلبات"),
    "err_no_unpaid_order": _row("There is no unpaid order. Place an order first.",
                                "Nta komande itarishyurwa. Banza utange komande.",
                                "Aucune commande impayée. Passez d'abord une commande.",
                                "Hakuna oda isiyolipwa. Weka oda kwanza.",
                                "لا يوجد طلب غير مدفوع. قم بالطلب أولًا.", "ما في طلب ما مدفوع. اطلب الأول."),
    "err_order_cancelled": _row("Order {number} is cancelled", "Komande {number} yahagaritswe",
                                "La commande {number} est annulée", "Oda {number} imeghairiwa",
                                "الطلب {number} ملغي", "الطلب {number} اتلغى"),
    "err_order_paid": _row("Order {number} is already paid", "Komande {number} yamaze kwishyurwa",
                           "La commande {number} est déjà payée", "Oda {number} imeshalipwa",
                           "الطلب {number} مدفوع بالفعل", "الطلب {number} مدفوع من قبل"),
    "err_payment_disabled": _row("Online payment is not enabled for this business",
                                 "Iri duka ntiryemera kwishyura kuri murandasi",
                                 "Le paiement en ligne n'est pas activé pour cette boutique",
                                 "Malipo ya mtandaoni hayajawezeshwa kwa duka hili",
                                 "الدفع الإلكتروني غير مفعّل لهذا المتجر", "الدفع أونلاين ما مفعل في المحل ده"),
    "err_reference_too_short": _row("Please send the full transaction reference",
                                    "Ohereza nimero yuzuye y'ubwishyu", "Envoyez la référence complète de la transaction",
                                    "Tafadhali tuma namba kamili ya muamala", "يرجى إرسال رقم العملية كاملًا",
                                    "رسل لينا رقم العملية كامل"),
    "err_product_not_found": _row("Product '{ref}' not found. Search the catalog first.",
                                  "Igicuruzwa '{ref}' ntikibonetse.", "Produit « {ref} » introuvable.",
                                  "Bidhaa '{ref}' haikupatikana.", "المنتج '{ref}' غير موجود.",
                                  "المنتج '{ref}' ما لقيناهو."),
}

WEEKDAYS: dict[str, list[str]] = {
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "rw": ["Ku wa mbere", "Ku wa kabiri", "Ku wa gatatu", "Ku wa kane", "Ku wa gatanu", "Ku wa gatandatu",
           "Ku cyumweru"],
    "fr": ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."],
    "sw": ["Jumatatu", "Jumanne", "Jumatano", "Alhamisi", "Ijumaa", "Jumamosi", "Jumapili"],
    "ar": ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"],
    "ar-SD": ["الاتنين", "التلات", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"],
}

MEDIA_LABELS: dict[str, dict[str, str]] = {
    "audio": _row("voice notes", "ubutumwa bw'amajwi", "les messages vocaux", "ujumbe wa sauti",
                  "الرسائل الصوتية", "الرسايل الصوتية"),
    "image": _row("photos", "amafoto", "les photos", "picha", "الصور", "الصور"),
    "video": _row("videos", "amashusho", "les vidéos", "video", "مقاطع الفيديو", "الفيديوهات"),
    "document": _row("documents", "inyandiko", "les documents", "nyaraka", "المستندات", "الملفات"),
    "sticker": _row("stickers", "stickers", "les autocollants", "stika", "الملصقات", "الاستيكرات"),
    "location": _row("shared locations", "aho wasangije", "les positions partagées", "maeneo yaliyoshirikiwa",
                     "المواقع المشتركة", "اللوكيشن"),
    "contacts": _row("contact cards", "nimero wasangije", "les fiches contact", "kadi za mawasiliano",
                     "جهات الاتصال", "الأرقام المرسلة"),
}
MEDIA_LABELS["voice"] = MEDIA_LABELS["audio"]
OTHER_MEDIA = _row("this kind of message", "ubu bwoko bw'ubutumwa", "ce type de message", "aina hii ya ujumbe",
                   "هذا النوع من الرسائل", "النوع ده من الرسايل")


def _pick(table: dict[str, str], lang: str) -> str:
    return table.get(lang) or (table.get("ar") if lang == "ar-SD" else None) or table["en"]


def t(key: str, lang: str, **params) -> str:
    return _pick(MESSAGES[key], lang).format(**params)


def status_text(status: str, lang: str) -> str:
    return t(f"status_{status}", lang) if f"status_{status}" in MESSAGES else status.replace("_", " ")


def payment_text(status: str, lang: str) -> str:
    return t(f"pay_{status}", lang) if f"pay_{status}" in MESSAGES else status


def media_label(mtype: str, lang: str) -> str:
    return _pick(MEDIA_LABELS.get(mtype, OTHER_MEDIA), lang)


def error_text(code: str | None, params: dict | None, fallback: str, lang: str) -> str:
    """User-facing tool error in the conversation language (falls back to the service's English message)."""
    key = f"err_{code}"
    if code and key in MESSAGES:
        try:
            return t(key, lang, **(params or {}))
        except (KeyError, IndexError):
            return fallback
    return fallback
