"""PubMed through NCBI's E-utilities: find open-access articles by keyword (PubMed
Central's open-access subset), and fetch an article by DOI (or PMCID, or PMID) as text
for the knowledge base.

- Searching asks PMC for `(WORDS) AND open access[filter]`, by relevance.
- Fetching finds the article in PMC; if it is there, its full text (JATS XML) becomes
  a Markdown document: title, authors, journal, abstract and sections. An article PMC
  does not hold (not open access) falls back to its PubMed abstract.

No key is needed; an NCBI key (keys.py: NCBI_API_KEY) raises the rate limit. Only the
search words or the identifier are sent."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from . import keys
from .errors import HarnessError, Invalid, NotFound

API = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_DOI = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?(10\.\d{4,9}/\S+)$", re.I)
_PMC = re.compile(r"^PMC(\d+)$", re.I)
_SKIP = {"table-wrap", "fig", "ref-list", "fn-group", "supplementary-material", "inline-formula",
         "disp-formula", "table", "graphic", "media"}


class PubMedError(HarnessError):
    status = 502


@dataclass
class Article:
    title: str
    pmcid: str = ""
    pmid: str = ""
    doi: str = ""
    journal: str = ""
    year: str = ""
    authors: list[str] = field(default_factory=list)
    text: str = ""                 # the document, once fetched
    full_text: bool = False        # False: the abstract only

    @property
    def ident(self) -> str:
        return self.pmcid or (f"PMID{self.pmid}" if self.pmid else self.doi)

    def cite(self) -> str:
        who = (self.authors[0] + (" et al." if len(self.authors) > 1 else "")) if self.authors else ""
        return " · ".join(x for x in (who, self.journal, self.year, f"doi:{self.doi}" if self.doi else "", self.pmcid) if x)

    def filename(self) -> str:
        return "pubmed-" + re.sub(r"[^\w.-]+", "_", self.ident) + ".md"

    def to_dict(self) -> dict:
        return {"title": self.title, "pmcid": self.pmcid, "pmid": self.pmid, "doi": self.doi, "journal": self.journal,
                "year": self.year, "authors": self.authors, "cite": self.cite(), "full_text": self.full_text}


class Client:
    def __init__(self, cfg: dict | None = None, opener=None):
        self.cfg = cfg or {}
        self.opener = opener
        self._last = 0.0

    def _get(self, endpoint: str, **params) -> bytes:
        key = keys.get("ncbi")
        params = {**params, "tool": "smallharness", **({"api_key": key} if key else {})}
        wait = (0.11 if key else 0.34) - (time.monotonic() - self._last)    # NCBI: 3 a second, 10 with a key
        if wait > 0:
            time.sleep(wait)
        url = f"{API}/{endpoint}.fcgi?" + urllib.parse.urlencode(params)
        try:
            with (self.opener or urllib.request.urlopen)(urllib.request.Request(url, headers={"User-Agent": "smallharness"}),
                             timeout=float(self.cfg.get("timeout", 30))) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raise PubMedError(f"NCBI error {e.code}: {e.read().decode('utf-8', 'replace')[:200]}") from None
        except (urllib.error.URLError, OSError) as e:
            raise PubMedError(f"NCBI could not be reached: {getattr(e, 'reason', e)}") from None
        finally:
            self._last = time.monotonic()

    def _json(self, endpoint: str, **params) -> dict:
        try:
            return json.loads(self._get(endpoint, retmode="json", **params))
        except ValueError:
            raise PubMedError("NCBI's reply is not JSON") from None

    def _ids(self, db: str, term: str, n: int) -> list[str]:
        return self._json("esearch", db=db, term=term, retmax=n, sort="relevance")["esearchresult"].get("idlist", [])

    # ------------------------------------------------------------ search
    def search(self, words: str, n: int | None = None) -> list[Article]:
        """Open-access articles in PMC for these words, most relevant first."""
        words = words.strip()
        if not words:
            raise Invalid("what should PubMed look for?")
        ids = self._ids("pmc", f"({words}) AND open access[filter]", int(n or self.cfg.get("max_results", 10)))
        return self._summaries(ids)

    def _summaries(self, ids: list[str]) -> list[Article]:
        if not ids:
            return []
        res = self._json("esummary", db="pmc", id=",".join(ids))["result"]
        out = []
        for uid in res.get("uids", ids):
            r = res.get(uid) or {}
            arts = {a.get("idtype"): a.get("value", "") for a in r.get("articleids", [])}
            out.append(Article(title=(r.get("title") or "").strip(), pmcid=arts.get("pmcid") or f"PMC{uid}",
                               pmid=arts.get("pmid", ""), doi=arts.get("doi", ""), journal=r.get("fulljournalname", ""),
                               year=(r.get("pubdate") or "")[:4],
                               authors=[a["name"] for a in r.get("authors", []) if a.get("name")]))
        return out

    # ------------------------------------------------------------- fetch
    def fetch(self, ident: str) -> Article:
        """An article as a document: full text from PMC, else the PubMed abstract.
        `ident`: a DOI (also as a doi.org link), a PMCID (PMC123) or a PMID."""
        ident = ident.strip()
        doi, pmc = _DOI.match(ident), _PMC.match(ident)
        if pmc:
            pmc_id = pmc.group(1)
        elif doi:
            pmc_id = self._pmc_for("doi", doi.group(1))
        elif ident.isdigit():
            pmc_id = self._pmc_for("pmid", ident)
        else:
            raise Invalid(f"{ident!r} is not a DOI (10.…/…), PMCID (PMC…) or PMID")
        if pmc_id:
            art = self._full_text(pmc_id)
            if art:
                return art
        return self._abstract(doi.group(1) if doi else "", ident if ident.isdigit() else "", ident)

    def _pmc_for(self, kind: str, value: str) -> str:
        """The PMC id of the article whose own DOI or PMID is `value`. The search also
        matches articles that merely cite it, so each candidate is checked."""
        ids = self._ids("pmc", f'"{value}"[doi]' if kind == "doi" else f"{value}[pmid]", 5)   # a quoted PMID finds nothing
        for a in self._summaries(ids):
            if getattr(a, kind).lower() == value.lower():
                return a.pmcid[3:]
        return ""

    def _full_text(self, pmc_id: str) -> Article | None:
        raw = self._get("efetch", db="pmc", id=pmc_id, retmode="xml")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            raise PubMedError("PMC's article could not be read") from None
        art = root.find(".//article")
        if art is None:
            return None
        meta = art.find("front/article-meta")
        ids = {e.get("pub-id-type"): (e.text or "").strip() for e in (meta.findall("article-id") if meta is not None else [])}
        a = Article(title=_text(meta.find("title-group/article-title")) if meta is not None else "",
                    pmcid=ids.get("pmcid") or f"PMC{pmc_id}", pmid=ids.get("pmid", ""), doi=ids.get("doi", ""),
                    journal=_text(art.find("front/journal-meta//journal-title")),
                    year=_text(meta.find("pub-date/year")) if meta is not None else "",
                    authors=_authors(meta))
        parts = [f"# {a.title}", "", a.cite(), ""]
        abstract = meta.find("abstract") if meta is not None else None
        if abstract is not None:
            parts += ["## Abstract", ""] + _blocks(abstract, 3)
        body = art.find("body")
        if body is not None:
            parts += _blocks(body, 2)
            a.full_text = True
        lic = art.find(".//permissions/license")
        if lic is not None:
            parts += ["", "_License: " + (lic.get("{http://www.w3.org/1999/xlink}href") or _text(lic))[:300] + "_"]
        a.text = "\n".join(parts).strip() + "\n"
        return a if (body is not None or abstract is not None) else None

    def _abstract(self, doi: str, pmid: str, ident: str) -> Article:
        if not pmid:
            found = self._ids("pubmed", f'"{doi}"[doi]', 1) if doi else []
            if not found:
                raise NotFound(f"PubMed has no article {ident}")
            pmid = found[0]
        text = self._get("efetch", db="pubmed", id=pmid, rettype="abstract", retmode="text").decode("utf-8", "replace")
        summary = self._json("esummary", db="pubmed", id=pmid)["result"].get(pmid, {})
        arts = {x.get("idtype"): x.get("value", "") for x in summary.get("articleids", [])}
        a = Article(title=(summary.get("title") or ident).strip(), pmid=pmid, doi=doi or arts.get("doi", ""),
                    journal=summary.get("fulljournalname", ""), year=(summary.get("pubdate") or "")[:4],
                    authors=[x["name"] for x in summary.get("authors", []) if x.get("name")])
        a.text = f"# {a.title}\n\n{a.cite()}\n\n_Abstract only: the full text is not open access in PMC._\n\n{text.strip()}\n"
        return a


def _text(e) -> str:
    return " ".join("".join(e.itertext()).split()) if e is not None else ""


def _authors(meta) -> list[str]:
    if meta is None:
        return []
    out = []
    for c in meta.findall("contrib-group/contrib[@contrib-type='author']"):
        sur, given = _text(c.find("name/surname")), _text(c.find("name/given-names"))
        if sur:
            out.append(f"{sur} {''.join(w[0] for w in given.split() if w)}".strip())
        elif c.find("collab") is not None:
            out.append(_text(c.find("collab")))
    return out


def _blocks(node, level: int) -> list[str]:
    """Sections as headings and paragraphs; tables, figures and formulas left out."""
    out = []
    for child in node:
        tag = child.tag
        if tag in _SKIP:
            continue
        if tag == "sec":
            title = _text(child.find("title"))
            if title:
                out += ["", "#" * min(level, 6) + " " + title, ""]
            out += _blocks(child, level + 1)
        elif tag == "p":
            # a paragraph's own text, without the tables or figures nested in it
            kept = [t for t in _flat(child)]
            para = " ".join("".join(kept).split())
            if para:
                out += [para, ""]
        elif tag in ("list", "list-item", "boxed-text", "def-list", "statement"):
            out += _blocks(child, level)
    return out


def _flat(e):
    if e.text:
        yield e.text
    for c in e:
        if c.tag not in _SKIP:
            yield from _flat(c)
        if c.tail:
            yield c.tail
