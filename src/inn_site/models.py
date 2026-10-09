from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    relation: Literal["owner", "third_party", "unclear"]
    main_site: bool
    evidence_id: str | None
    quote: str = Field(max_length=600)
    reason: str = Field(max_length=240)


class ComparisonSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    quote: str = Field(max_length=600)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str | None = Field(max_length=160)
    verdicts: list[Verdict] = Field(max_length=8)
    action: Literal["finish", "search", "read"]
    reason: str = Field(max_length=240)
    investigate_candidate_id: str | None = None
    company_city: str | None = Field(default=None, max_length=80)
    next_query: str | None = Field(default=None, max_length=240)
    read_urls: list[str] = Field(default_factory=list, max_length=3)
    primary_basis: Literal["single", "hierarchy", "uncertain"] = "single"
    comparison_sources: list[ComparisonSource] = Field(default_factory=list, max_length=3)
    dismiss_candidate_ids: list[str] = Field(default_factory=list, max_length=8)
    subject_kind: Literal["organization", "entrepreneur", "private_person", "unknown"] = "unknown"
    subject_evidence_ids: list[str] = Field(default_factory=list, max_length=2)


@dataclass
class SearchHit:
    url: str
    title: str
    text: str


@dataclass
class Evidence:
    id: str
    url: str
    kind: Literal["search", "html", "pdf", "remote", "registry"]
    text: str
    candidate_id: str | None = None
    fetched_at: str = ""
    links: list[dict] = field(default_factory=list)


@dataclass
class Candidate:
    id: str
    domain: str
    urls: list[str] = field(default_factory=list)
    read_urls: list[str] = field(default_factory=list)
    failed_urls: list[str] = field(default_factory=list)


@dataclass
class Resolution:
    domain: str | None
    status: str
    trace: dict

    def public(self) -> dict:
        return {"domain": self.domain}
