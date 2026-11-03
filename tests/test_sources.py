"""Outside sources, only on command: API keys, asking Gemini, PubMed. No
network: the HTTP calls are answered here."""
import io
import json
import os
import stat
import urllib.error
import urllib.parse
from unittest import mock

from smallharness import gemini, keys, pubmed
from smallharness.errors import HarnessError, Invalid, NotFound
from smallharness.research import Research

from helpers import WorkspaceTest

GEMINI_REPLY = {"candidates": [{
    "content": {"parts": [{"text": "BamA is the central component "}, {"text": "of the Bam complex."}]},
    "groundingMetadata": {
        "webSearchQueries": ["what is BamA"],
        "groundingChunks": [{"web": {"uri": "https://example.org/bama", "title": "example.org"}},
                            {"web": {"uri": "https://example.org/bama", "title": "dup"}},
                            {"web": {"uri": "javascript:alert(1)", "title": "bad"}}]}}]}

PMC_XML = b"""<?xml version="1.0"?><pmc-articleset><article><front>
<journal-meta><journal-title-group><journal-title>mBio</journal-title></journal-title-group></journal-meta>
<article-meta><article-id pub-id-type="pmcid">PMC6529637</article-id><article-id pub-id-type="doi">10.1128/mBio.00660-19</article-id>
<article-id pub-id-type="pmid">31113900</article-id>
<title-group><article-title>Improper Coordination of BamA and BamD</article-title></title-group>
<contrib-group><contrib contrib-type="author"><name><surname>Tata</surname><given-names>Muralidhar</given-names></name></contrib>
<contrib contrib-type="author"><name><surname>Konovalova</surname><given-names>Anna</given-names></name></contrib></contrib-group>
<pub-date><year>2019</year></pub-date>
<abstract><p>The Bam complex folds <italic>beta</italic>-barrels.</p></abstract></article-meta></front>
<body><sec><title>Introduction</title><p>BamE coordinates BamA and BamD.<table-wrap><table><tr><td>noise</td></tr></table></table-wrap></p>
<sec><title>Detail</title><p>RcsF jams the complex.</p></sec></sec></body></article></pmc-articleset>"""


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def ncbi(routes: dict):
    """An opener answering E-utilities calls: (endpoint, db) -> bytes, or a function of the params."""
    calls = []

    def opener(req, timeout=0):
        url = urllib.parse.urlparse(req.full_url)
        params = dict(urllib.parse.parse_qsl(url.query))
        endpoint = url.path.rsplit("/", 1)[-1].replace(".fcgi", "")
        calls.append((endpoint, params))
        out = routes[(endpoint, params["db"])]
        out = out(params) if callable(out) else out
        return Reply(out if isinstance(out, bytes) else json.dumps(out).encode())
    opener.calls = calls
    return opener


def summary(uid, pmcid, doi, pmid="1", title="A title"):
    return {"title": title, "fulljournalname": "mBio", "pubdate": "2019 May",
            "authors": [{"name": "Tata M"}, {"name": "Konovalova A"}],
            "articleids": [{"idtype": "pmid", "value": pmid}, {"idtype": "pmcid", "value": pmcid},
                           {"idtype": "doi", "value": doi}]}


class KeyTest(WorkspaceTest):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"SMALLHARNESS_KEYS_FILE": str(self.ws / "home" / "keys.toml")})
        env.start()
        self.addCleanup(env.stop)
        for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "NCBI_API_KEY"):
            os.environ.pop(var, None)


class Keys(KeyTest):
    def test_saved_private_and_outside_the_workspace_config(self):
        self.assertEqual(keys.get("gemini"), "")
        keys.put("gemini", "  AIzaSECRET1234 ")
        path = keys.keys_file()
        self.assertEqual(keys.get("gemini"), "AIzaSECRET1234")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(keys.masked("gemini"), "…1234")
        self.assertNotIn("SECRET", json.dumps(keys.status()))                # never shown in full
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "from-env-key"}):
            self.assertEqual((keys.get("gemini"), keys.where("gemini")), ("from-env-key", "$GEMINI_API_KEY"))
        keys.put("gemini", "")
        self.assertEqual(keys.get("gemini"), "")
        with self.assertRaises(Invalid):
            keys.put("openai", "x")
        with self.assertRaises(Invalid):
            keys.put("gemini", "two\nlines")


class Gemini(KeyTest):
    def test_needs_a_key(self):
        with self.assertRaises(HarnessError) as e:
            gemini.ask("what is BamA", {})
        self.assertIn("smallharness key gemini", str(e.exception))

    def test_no_search_by_default_and_the_key_in_a_header(self):
        keys.put("gemini", "k-123456789")
        seen = {}

        def opener(req, timeout=0):
            seen.update(url=req.full_url, headers=dict(req.header_items()), body=json.loads(req.data))
            return Reply(json.dumps({"candidates": [{"content": {"parts": [
                {"text": "thinking…", "thought": True}, {"text": "BamA folds barrels."}]}}],
                "modelVersion": "gemini-x-001"}).encode())
        a = gemini.ask("what is BamA", {"model": "gemini-x"}, opener)
        self.assertEqual((a.text, a.model, a.searched), ("BamA folds barrels.", "gemini-x-001", False))
        self.assertNotIn("tools", seen["body"])                             # free tier: no Google Search
        self.assertIn("/models/gemini-x:generateContent", seen["url"])
        self.assertNotIn("k-123456789", seen["url"])
        self.assertEqual(seen["headers"]["X-goog-api-key"], "k-123456789")
        self.assertIn("own knowledge", a.markdown())

    def test_search_when_asked(self):
        keys.put("gemini", "k-123456789")
        bodies = []

        def opener(req, timeout=0):
            bodies.append(json.loads(req.data))
            return Reply(json.dumps(GEMINI_REPLY).encode())
        a = gemini.ask("what is BamA", {"model": "m", "search": True}, opener)
        self.assertEqual(bodies[0]["tools"], [{"google_search": {}}])
        self.assertEqual(a.sources, [{"title": "example.org", "url": "https://example.org/bama"}])   # once; no javascript:
        self.assertIn("## Sources", a.markdown())

    def test_a_busy_model_falls_back_to_the_next(self):
        keys.put("gemini", "k-123456789")
        asked = []

        def opener(req, timeout=0):
            model = req.full_url.split("/models/")[1].split(":")[0]
            asked.append(model)
            if model == "busy":
                raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b'{"error": {"message": "high demand"}}'))
            if model == "slow":
                raise TimeoutError()
            return Reply(json.dumps(GEMINI_REPLY).encode())
        a = gemini.ask("q", {"model": "busy", "fallback": ["slow", "ok"]}, opener)
        self.assertEqual((asked, a.model), (["busy", "slow", "ok"], "ok"))
        self.assertEqual(a.notes, ["busy: overloaded", "slow: timed out"])

    def test_errors_say_what_to_do(self):
        keys.put("gemini", "k-123456789")

        def refuse(code, msg):
            def opener(req, timeout=0):
                raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(json.dumps({"error": {"message": msg}}).encode()))
            return opener
        with self.assertRaises(gemini.GeminiError) as e:
            gemini.ask("q", {"model": "old"}, refuse(404, "no such model"))
        self.assertIn("[gemini].model", str(e.exception))
        with self.assertRaises(gemini.GeminiError) as e:
            gemini.ask("q", {"model": "m", "search": True, "fallback": []}, refuse(429, "quota"))
        self.assertIn("[gemini].search = false", str(e.exception))
        with self.assertRaises(gemini.GeminiError):
            gemini.parse("q", {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}})


class PubMed(KeyTest):
    def test_search_asks_for_open_access_by_relevance(self):
        opener = ncbi({("esearch", "pmc"): {"esearchresult": {"idlist": ["6529637"]}},
                       ("esummary", "pmc"): {"result": {"uids": ["6529637"],
                                                        "6529637": summary("6529637", "PMC6529637", "10.1128/mBio.00660-19")}}})
        hits = pubmed.Client({}, opener).search("BamA complex", 5)
        self.assertEqual([(h.pmcid, h.doi, h.year) for h in hits], [("PMC6529637", "10.1128/mBio.00660-19", "2019")])
        params = opener.calls[0][1]
        self.assertEqual((params["term"], params["sort"], params["retmax"]),
                         ("(BamA complex) AND open access[filter]", "relevance", "5"))
        self.assertNotIn("api_key", params)
        self.assertIn("Tata M et al.", hits[0].cite())

    def test_fetch_by_doi_checks_the_article_is_that_doi(self):
        # The search also finds an article that cites the DOI: it is skipped.
        opener = ncbi({("esearch", "pmc"): {"esearchresult": {"idlist": ["111", "6529637"]}},
                       ("esummary", "pmc"): {"result": {"uids": ["111", "6529637"],
                                                        "111": summary("111", "PMC111", "10.9/citing"),
                                                        "6529637": summary("6529637", "PMC6529637", "10.1128/mBio.00660-19")}},
                       ("efetch", "pmc"): lambda p: PMC_XML if p["id"] == "6529637" else b"<x/>"})
        a = pubmed.Client({}, opener).fetch("https://doi.org/10.1128/MBIO.00660-19")
        self.assertTrue(a.full_text)
        self.assertEqual((a.pmcid, a.doi, a.authors), ("PMC6529637", "10.1128/mBio.00660-19", ["Tata M", "Konovalova A"]))
        self.assertIn("## Introduction", a.text)
        self.assertIn("### Detail", a.text)
        self.assertIn("The Bam complex folds beta-barrels.", a.text)
        self.assertNotIn("noise", a.text)                                    # tables left out
        self.assertEqual(a.filename(), "pubmed-PMC6529637.md")

    def test_not_in_pmc_falls_back_to_the_abstract(self):
        opener = ncbi({("esearch", "pmc"): {"esearchresult": {"idlist": []}},
                       ("esearch", "pubmed"): {"esearchresult": {"idlist": ["999"]}},
                       ("efetch", "pubmed"): b"1. Some Journal. 2020.\n\nAn abstract.",
                       ("esummary", "pubmed"): {"result": {"999": {"title": "Closed article", "pubdate": "2020"}}}})
        a = pubmed.Client({}, opener).fetch("10.1000/closed")
        self.assertFalse(a.full_text)
        self.assertIn("Abstract only", a.text)
        self.assertEqual(a.ident, "PMID999")

    def test_unknown_and_bad_identifiers(self):
        opener = ncbi({("esearch", "pmc"): {"esearchresult": {"idlist": []}},
                       ("esearch", "pubmed"): {"esearchresult": {"idlist": []}}})
        with self.assertRaises(NotFound):
            pubmed.Client({}, opener).fetch("10.1000/nothing")
        with self.assertRaises(Invalid):
            pubmed.Client({}, opener).fetch("not an id")

    def test_the_ncbi_key_is_sent_when_set(self):
        keys.put("ncbi", "ncbi-key-1234")
        opener = ncbi({("esearch", "pmc"): {"esearchresult": {"idlist": []}}})
        pubmed.Client({}, opener).search("x")
        self.assertEqual(opener.calls[0][1]["api_key"], "ncbi-key-1234")


class IntoTheKnowledgeBase(KeyTest):
    def test_gemini_answer_attached_then_saved(self):
        keys.put("gemini", "k-123456789")
        h = self.harness()
        r = Research(h)
        with mock.patch("urllib.request.urlopen", lambda req, timeout=0: Reply(json.dumps(GEMINI_REPLY).encode())):
            a = r.gemini("What is BamA?")
        self.assertEqual((a.kind, a.num), ("gemini", 1))
        r.attach(1)                                                          # CAG like any answer
        a, res = r.save(1)
        self.assertEqual(a.saved, ".smallharness/library/gemini-what-is-bama.md")
        self.assertIn(a.saved, h.project_rag.sources)
        self.assertTrue(any("central component" in x.text for x in r.find("Bam complex component")))
        r.save(1)                                                            # again: replaced, not doubled
        self.assertEqual(sum(1 for s in h.project_rag.sources if s.startswith(".smallharness/library/gemini-")), 1)

    def test_article_by_number_from_the_last_search(self):
        h = self.harness()
        r = Research(h)
        opener = ncbi({("esearch", "pmc"): {"esearchresult": {"idlist": ["6529637"]}},
                       ("esummary", "pmc"): {"result": {"uids": ["6529637"],
                                                        "6529637": summary("6529637", "PMC6529637", "10.1128/mBio.00660-19")}},
                       ("efetch", "pmc"): PMC_XML})
        with mock.patch("urllib.request.urlopen", opener):
            r.pubmed("BamA")
            art, source, res = r.add_article("1")
        self.assertEqual(source, ".smallharness/library/pubmed-PMC6529637.md")
        self.assertIn(source, [x["source"] for x in r.library()])
        self.assertTrue(r.find("RcsF jams"))

    def test_chat_gemini_then_kb(self):
        keys.put("gemini", "k-123456789")
        h = self.harness()
        chat = h.chats.new()
        with mock.patch("urllib.request.urlopen", lambda req, timeout=0: Reply(json.dumps(GEMINI_REPLY).encode())):
            added = chat.send("/gemini what is BamA")
        web = added[-1]
        self.assertEqual(web.kind, "gemini")
        self.assertIn("central component", web.seen)                          # the local model sees it
        self.assertEqual(self.backend.prompts, [])                            # no local model call
        done = chat.send(f"/kb {web.num}")[-1]
        self.assertIn(done.meta["source"], h.project_rag.sources)
        self.assertEqual(chat.send("/kb x")[-1].kind, "error")
