import socket

import pytest

from inn_site.models import Candidate, ComparisonSource, Evidence, Review, Verdict
from inn_site.rules import (
    choose_domain,
    has_inn,
    public_url,
    site_domain,
    valid_inn,
    verified_name,
)

INN = "7721581040"
TEXT = "Сервис принадлежит ООО «Дейта Кью». Наши реквизиты: ИНН 7721581040, ОГРН 5077746329876."


def review(**changes):
    v = dict(
        candidate_id="c1",
        relation="owner",
        main_site=True,
        evidence_id="e1",
        quote=TEXT,
        reason="Реквизиты оператора сервиса",
    )
    v.update(changes)
    return Review(
        company_name="Дейта Кью", verdicts=[Verdict(**v)], action="finish", reason="Проверено"
    )


@pytest.mark.parametrize(
    "inn", ["7721581040", "9715225506", "6663003127", "9724061893", "500100732259"]
)
def test_valid_inn(inn):
    assert valid_inn(inn)


@pytest.mark.parametrize(
    "inn", ["7721581041", "0000000000", "123", 7721581040, "７７２１５８１０４０", " 7721581040"]
)
def test_invalid_inn(inn):
    assert not valid_inn(inn)


def test_confirmed_owner():
    result = choose_domain(
        INN,
        review(),
        [Candidate("c1", "dadata.ru")],
        [Evidence("e1", "https://dadata.ru/offer/", "html", TEXT, "c1")],
    )
    assert result == ("dadata.ru", "found")


def test_uncertain_primary_basis_cannot_return_a_domain():
    r = review()
    r.primary_basis = "uncertain"
    assert choose_domain(
        INN,
        r,
        [Candidate("c1", "dadata.ru")],
        [Evidence("e1", "https://dadata.ru/offer/", "html", TEXT, "c1")],
    ) == (None, "ambiguous")


@pytest.mark.parametrize(
    "change",
    [
        {"quote": TEXT + " Сайт официальный."},
        {"evidence_id": "invented"},
        {"candidate_id": "invented"},
        {"relation": "third_party"},
        {"main_site": False},
    ],
)
def test_unsupported_verdict_is_rejected(change):
    domain, _ = choose_domain(
        INN,
        review(**change),
        [Candidate("c1", "dadata.ru")],
        [Evidence("e1", "https://dadata.ru/offer/", "html", TEXT, "c1")],
    )
    assert domain is None


def test_snippet_is_not_enough():
    domain, _ = choose_domain(
        INN,
        review(),
        [Candidate("c1", "dadata.ru")],
        [Evidence("e1", "https://dadata.ru/", "search", TEXT, "c1")],
    )
    assert domain is None


def test_source_from_another_domain_is_rejected():
    domain, _ = choose_domain(
        INN,
        review(),
        [Candidate("c1", "dadata.ru")],
        [Evidence("e1", "https://other.ru/", "html", TEXT, "c1")],
    )
    assert domain is None


def test_directory_cannot_be_returned():
    domain, _ = choose_domain(
        INN,
        review(),
        [Candidate("c1", "rusprofile.ru")],
        [Evidence("e1", "https://rusprofile.ru/id/1", "html", TEXT, "c1")],
    )
    assert domain is None


def test_equal_candidates_require_abstention():
    r = review()
    r.verdicts.append(
        Verdict(
            candidate_id="c2",
            relation="owner",
            main_site=True,
            evidence_id="e2",
            quote=TEXT,
            reason="Другой собственный сайт",
        )
    )
    assert choose_domain(
        INN,
        r,
        [Candidate("c1", "one.ru"), Candidate("c2", "two.ru")],
        [
            Evidence("e1", "https://one.ru/", "html", TEXT, "c1"),
            Evidence("e2", "https://two.ru/", "html", TEXT, "c2"),
        ],
    ) == (None, "ambiguous")


def test_range_of_services_does_not_prove_primary_brand():
    r = review()
    r.verdicts.append(
        Verdict(
            candidate_id="c2",
            relation="owner",
            main_site=False,
            evidence_id="e2",
            quote=TEXT,
            reason="Узкий сервис того же юрлица",
        )
    )
    assert choose_domain(
        INN,
        r,
        [Candidate("c1", "one.ru"), Candidate("c2", "two.ru")],
        [
            Evidence("e1", "https://one.ru/", "html", TEXT, "c1"),
            Evidence("e2", "https://two.ru/", "html", TEXT, "c2"),
        ],
    ) == (None, "ambiguous")


@pytest.mark.parametrize("broken", [False, True])
def test_hierarchy_requires_literal_sources_from_both_owned_sites(broken):
    main = TEXT + " Это головной сайт компании; отдельный продукт размещён на two.ru."
    product = TEXT + " Это отдельный продукт. Головной сайт компании: one.ru."
    r = review(quote=main)
    r.primary_basis = "hierarchy"
    r.verdicts.append(
        Verdict(
            candidate_id="c2",
            relation="owner",
            main_site=False,
            evidence_id="e2",
            quote=product,
            reason="Отдельный продукт",
        )
    )
    r.comparison_sources = [
        ComparisonSource(evidence_id="e1", quote=main),
        ComparisonSource(
            evidence_id="e2", quote="Выдуманная иерархия брендов" if broken else product
        ),
    ]
    assert choose_domain(
        INN,
        r,
        [Candidate("c1", "one.ru"), Candidate("c2", "two.ru")],
        [
            Evidence("e1", "https://one.ru/company", "html", main, "c1"),
            Evidence("e2", "https://two.ru/legal", "html", product, "c2"),
        ],
    ) == ((None, "invalid_model_evidence") if broken else ("one.ru", "found"))


def test_longer_number_does_not_match():
    assert not has_inn("ИНН 177215810400", INN)


def test_private_hosting_domain_is_preserved():
    assert site_domain("https://shop.tilda.ws/legal") == "shop.tilda.ws"
    assert site_domain("https://www.example.co.uk/contact") == "example.co.uk"
    assert site_domain("https://пример.рф/") == "xn--e1afmkfd.xn--p1ai"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/",
        "http://user:pass@example.com/",
        "file:///etc/passwd",
    ],
)
def test_local_or_unsafe_urls(url):
    with pytest.raises(ValueError):
        public_url(url)


def test_private_dns_is_rejected(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 80))],
    )
    with pytest.raises(ValueError):
        public_url("https://external-looking.example/")


def test_company_name_must_be_grounded():
    e = [Evidence("e1", "https://example.ru/", "search", TEXT)]
    assert verified_name("Дейта Кью", e, INN) == "Дейта Кью"
    assert verified_name("Выдуманная компания", e, INN) is None


def test_consistent_duplicate_keeps_verified_evidence():
    r = review()
    r.verdicts.append(r.verdicts[0].model_copy(update={"quote": "Вторичная короткая цитата"}))
    assert choose_domain(
        INN,
        r,
        [Candidate("c1", "dadata.ru")],
        [Evidence("e1", "https://dadata.ru/offer/", "html", TEXT, "c1")],
    )[0] == ("dadata.ru")


def test_conflicting_duplicate_requires_abstention():
    r = review()
    r.verdicts.append(r.verdicts[0].model_copy(update={"relation": "third_party"}))
    assert choose_domain(
        INN,
        r,
        [Candidate("c1", "dadata.ru")],
        [Evidence("e1", "https://dadata.ru/offer/", "html", TEXT, "c1")],
    ) == (None, "invalid_model_evidence")
