# Research

Research works on a **knowledge base**: the files and notes you add, kept in one
index (`.smallharness/rag/project.sqlite`). Code: `research.py`, `context/rag.py`,
`notes.py`.

## Adding files
`/add PATH` (or `smallharness index PATH…`, or the files bar under **Research → Ask**
in the web interface, which also takes uploads) indexes a file or a whole folder. It
skips `.git`, `node_modules`, virtualenvs and files that have not changed. Accepted
are the types in `[rag]`: by default `.txt`, `.md`, `.py`, `.sh`, PDFs and images.

- **Images** are read with OCR (`tesseract`).
- **PDFs** are read page by page. With `pdf_mode = "auto"`, a page keeps its own text
  when it has some, and scanned pages are OCR'd (`pdftoppm`, then `tesseract`);
  `pdf_mode = "always"` OCRs every page. Install poppler and tesseract
  (`brew install tesseract poppler`, or `apt install tesseract-ocr poppler-utils`).
  Without them, PDFs and images are skipped with a message.
- **Notes** are part of the knowledge base too (see [Notes](#notes)).

## Asking
Type a question. The harness searches for the best excerpts (k = `[research].k`) and
the model answers from those alone, in a clean context: the system prompt, the
numbered excerpts, any attached answers, and the question. No history and no tools.
If nothing matches, the model is not asked.

- **References.** The answer cites excerpts as numbers only, like `[3]`. A number means
  the same excerpt for the whole research session, and leads to the session's
  bibliography, a temporary file (`.smallharness/research/<id>.md`) with each
  excerpt's source, line and text. In the terminal, `/bibliography [N]`; on the web,
  the Bibliography pane, or click a `[n]`.
- **Attaching (CAG).** The context stays clear by default. `/attach N` (or Attach on
  the web) keeps answer N in the context of later questions, until `/detach N`.
  Attached answers share `[research].attached_budget` of the window.
- **Scope.** `/scope FILE…` (or the checkboxes next to files and notes on the web)
  limits questions to some files, folders or notes; `/scope all` searches everything
  again. `/find WORDS` shows the excerpts a question would get, without asking the
  model.

## Notes
Notes are Markdown files in `.smallharness/notes/<n>.md`, indexed into the knowledge
base as soon as they are saved. Tags are optional: write `#word` anywhere (`# Heading`
is not a tag). Notes edited or deleted outside the harness are picked up the next time
they are opened.

- `smallharness notes` is note mode without loading a model: type a line to save it;
  `/new`, `/list [#tag]`, `/search WORDS [#tag]`, `/show N`, `/edit N`,
  `/append N TEXT`, `/delete N` and `/tags` do the rest.
- In research, `/note TEXT` saves one and `/notes` opens note mode.
- On the web, the **Notes** tab writes, searches, filters by tag, edits and deletes.

## How the knowledge base is searched
Adding may take a while so that asking is fast.

- **Chunks follow the text's structure.** In code, a new function or class (with its
  decorators) starts a chunk; in Markdown, a heading does. Small sections stay
  together, and other text is split at blank lines.
- **Keywords and meaning.** Every chunk is scored by keywords (BM25, with light
  word-form matching: "deployed" finds "deploy") and, with an embedding model, by
  meaning. The two rankings are merged (Reciprocal Rank Fusion), so exact names still
  match while "how do I undo a release" finds "run the rollback script".
- **Fast searching.** Per-term posting lists and the vectors, packed into one matrix,
  are built once per change. A question touches only the chunks that contain its
  words, plus one matrix multiplication.
- **Embeddings** (`[rag.embed].backend`, default `auto`): fastembed in process if it is
  installed (`pip install "smallharness[embed]"`, about 70 MB for `bge-small-en-v1.5`,
  with its RAM reserved in the RAM check); otherwise the model's own server
  (`/v1/embeddings`; for Ollama, `ollama pull nomic-embed-text`); otherwise keywords
  only, with a warning. `server` and `fastembed` choose one explicitly, `none` turns
  them off. Changing the embedding model re-embeds on the next `/add`.
  - Matches by meaning count only above `min_similarity` (automatic: 0.5 for bge
    models, which score even unrelated text 0.35–0.5, else 0.3) and within
    `similarity_margin` of the best match, so an unrelated question still finds
    nothing. `smallharness check --probe` tests the embedder and suggests a value.
- **Storage.** The index is an SQLite file (standard library). A change writes only
  what changed, and several processes can share it: the terminal, `smallharness notes`
  and the web interface each see the others' changes.
