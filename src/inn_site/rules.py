import ipaddress
import socket
import unicodedata
from urllib.parse import urlsplit

import tldextract

from inn_site.models import Candidate, Evidence, Review

_extract = tldextract.TLDExtract(
    suffix_list_urls=(), include_psl_private_domains=True, extra_suffixes=["tilda.ws"]
)
# Эти источники полезны для поиска, но их домен не является сайтом искомой компании.
DIRECTORIES = {
    "rusprofile.ru",
    "list-org.com",
    "companies.rbc.ru",
    "rbc.ru",
    "saby.ru",
    "sbis.ru",
    "kontur.ru",
    "focus.kontur.ru",
    "checko.ru",
    "zachestnyibiznes.ru",
    "spark-interfax.ru",
    "audit-it.ru",
    "e-ecolog.ru",
    "fbc.ru",
    "b2b.house",
    "synapsenet.ru",
    "reputation.ru",
    "2gis.ru",
    "yell.ru",
    "zoon.ru",
    "business-rating.company",
    "innproverka.ru",
    "buhonline.ru",
    "vbankcenter.ru",
    "companies.data.gov.ru",
}
PLATFORMS = {
    "vk.com",
    "t.me",
    "telegram.org",
    "ok.ru",
    "youtube.com",
    "wikipedia.org",
    "hh.ru",
    "avito.ru",
    "ozon.ru",
    "wildberries.ru",
    "yandex.ru",
    "google.com",
    "bing.com",
    "duckduckgo.com",
    "zakupki.gov.ru",
    "nalog.gov.ru",
}


def normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def valid_inn(inn: str) -> bool:
    if (
        not isinstance(inn, str)
        or len(inn) not in {10, 12}
        or any(c not in "0123456789" for c in inn)
    ):
        return False
    if len(set(inn)) == 1:
        return False
    digits = list(map(int, inn))
    if len(inn) == 10:
        weights = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        return sum(a * b for a, b in zip(digits, weights)) % 11 % 10 == digits[9]
    w11 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    w12 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    return (
        sum(a * b for a, b in zip(digits, w11)) % 11 % 10 == digits[10]
        and sum(a * b for a, b in zip(digits, w12)) % 11 % 10 == digits[11]
    )


def has_inn(text: str, inn: str) -> bool:
    return bool(inn_positions(text, inn))


def inn_positions(text: str, inn: str) -> list[int]:
    positions, start = [], 0
    while (pos := text.find(inn, start)) >= 0:
        end = pos + len(inn)
        if (pos == 0 or not text[pos - 1].isdigit()) and (
            end == len(text) or not text[end].isdigit()
        ):
            positions.append(pos)
        start = end
    return positions


def host_of(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("Нужен HTTP(S) URL")
    if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        raise ValueError("URL с учётными данными или нестандартным портом")
    host = parsed.hostname.rstrip(".").encode("idna").decode().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def site_domain(url: str) -> str:
    host = host_of(url)
    parts = _extract(host)
    return parts.top_domain_under_public_suffix or host


def excluded(url: str) -> bool:
    host = host_of(url)
    path = urlsplit(url).path.casefold()
    card_paths = (
        "/business/contractor/",
        "/kontragent/",
        "/tool/ocompany/",
        "/references/organizations/",
        "/proverka/",
        "/banking_sector/credit/coinfo/",
        "/finorg/foinfo/",
    )
    if path.startswith(card_paths):
        return True
    if host.startswith("kontragent."):
        return True
    if (host.endswith("find-org.net") and urlsplit(url).path.startswith("/cli/")) or (
        host in {"licexpert.ru", "bbnt.ru", "ruinn.ru", "iswork.ru", "blank-kit.ru"}
        and path.startswith(("/company/", "/contractor/"))
    ):
        return True
    if host == "basis.myseldon.com" or host.endswith("jsprav.ru"):
        return True
    if host == "egrul.nalog.ru" or (
        host == "yandex.ru" and urlsplit(url).path.startswith("/maps/")
    ):
        return True
    # kontur.ru сам может быть целью поиска. Исключаем именно карточки компаний.
    if host == "kontur.ru":
        return False
    corporate_platforms = {
        "rbc.ru",
        "saby.ru",
        "sbis.ru",
        "hh.ru",
        "avito.ru",
        "ozon.ru",
        "wildberries.ru",
        "yandex.ru",
        "google.com",
        "bing.com",
        "duckduckgo.com",
        "nalog.gov.ru",
    }
    blocked = (DIRECTORIES - {"kontur.ru"} - corporate_platforms) | (
        PLATFORMS - corporate_platforms
    )
    if host in {"saby.ru", "sbis.ru"} and urlsplit(url).path.startswith("/profile/"):
        return True
    return any(host == d or host.endswith("." + d) for d in blocked)


def public_url(url: str) -> str:
    host_of(url)
    raw_host = urlsplit(url).hostname
    addresses = socket.getaddrinfo(raw_host, None, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Запрещён адрес локальной или служебной сети")
    return url


def ownership_evidence(inn, verdict, candidates, evidence):
    candidate = next((c for c in candidates if c.id == verdict.candidate_id), None)
    source = next((e for e in evidence if e.id == verdict.evidence_id), None)
    if (
        verdict.relation != "owner"
        or not candidate
        or not source
        or source.candidate_id != candidate.id
        or source.kind in {"search", "registry"}
    ):
        return None
    quote = normalize_text(verdict.quote)
    if len(quote) < 16 or quote not in normalize_text(source.text) or not has_inn(quote, inn):
        return None
    if excluded(source.url) or site_domain(source.url) != candidate.domain:
        return None
    return source


def choose_domain(
    inn: str, review: Review, candidates: list[Candidate], evidence: list[Evidence]
) -> tuple[str | None, str]:
    by_candidate = {c.id: c for c in candidates}
    by_evidence = {e.id: e for e in evidence}
    accepted = set()
    owners = set()
    unsupported_main = False
    seen = {}
    for v in review.verdicts:
        signature = (v.relation, v.main_site)
        if v.candidate_id in seen and seen[v.candidate_id] != signature:
            return None, "invalid_model_evidence"
        seen[v.candidate_id] = signature
        if v.relation != "owner":
            continue
        c = by_candidate.get(v.candidate_id)
        if not ownership_evidence(inn, v, candidates, evidence):
            unsupported_main |= v.main_site
            continue
        owners.add(c.domain)
        if v.main_site:
            accepted.add(c.domain)
    if accepted and review.primary_basis == "uncertain":
        return None, "ambiguous"
    if len(owners) > 1:
        compared = set()
        for item in review.comparison_sources:
            source = by_evidence.get(item.evidence_id)
            candidate = by_candidate.get(source.candidate_id) if source else None
            quote = normalize_text(item.quote)
            if (
                source
                and candidate
                and source.kind != "search"
                and candidate.domain in owners
                and site_domain(source.url) == candidate.domain
                and len(quote) >= 16
                and quote in normalize_text(source.text)
            ):
                compared.add(candidate.domain)
        if review.primary_basis != "hierarchy":
            return None, "ambiguous"
        if not owners.issubset(compared):
            return None, "invalid_model_evidence"
    if len(accepted) == 1:
        return accepted.pop(), "found"
    if unsupported_main:
        return None, "invalid_model_evidence"
    return None, "ambiguous" if accepted else "insufficient_evidence"


def verified_name(name: str | None, evidence: list[Evidence], inn: str) -> str | None:
    if not name:
        return None
    plain = normalize_text(name)
    form, _, tail = plain.partition(" ")
    if form.upper() in {"ООО", "ПАО", "АО", "ОАО", "ЗАО", "ИП"}:
        plain = tail
    plain = plain.translate(str.maketrans("", "", '«»"')).strip()
    if len(plain) < 3:
        return None
    for e in evidence:
        source = normalize_text(e.text).translate(str.maketrans("", "", '«»"')).casefold()
        if has_inn(e.text, inn) and plain.casefold() in source:
            return plain
    return None
