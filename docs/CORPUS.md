# Corpus

Source: the FAST-NUCES public website (nu.edu.pk), public pages only · Documents: 9 (7 PDFs and
2 HTML pages) · Pages: 279 PDF pages plus 2 web pages · Checked and downloaded on 3 Nov 2026

Nothing from SLATE, Flex, NU Mail or any login is used, and nothing was written by a language
model. The repository does **not** contain the documents: it contains
[`corpus/sources.yaml`](../corpus/sources.yaml) (URLs, `doc_version`, `superseded`) and
[`corpus/MANIFEST.md`](../corpus/MANIFEST.md) (URL, size and SHA-256 of every file).
`python corpus/fetch.py` downloads them one at a time with a pause between requests, and
`python corpus/fetch.py --verify` re-hashes them against the manifest. In the cluster the
indexer Job's init container runs the same script, so a clean clone needs no manual download.

## The documents

| Document | Version | Superseded | Pages | Why it is here |
| --- | --- | --- | ---: | --- |
| Undergraduate Academic Rules (Aug 2023) | 2023-08 | **yes** | 26 | older rules; **scanned** (24 of 26 pages are images) |
| Academic Rules Handbook (Jun 2026) | 2026-06 | no | 58 | current rules, numbered clauses, the Generative AI policy |
| Prospectus 2026: Academic Rules and Regulations | 2026 | no | 5 | the same rules in a 3-column layout with different clause numbers |
| Student Handbook (Jun 2026) | 2026-06 | no | 64 | conduct, discipline and student services |
| Annual Report 2019-20 | 2019-20 | **yes** | 33 | tables by campus; older edition |
| Annual Report 2023-24 | 2023-24 | no | 85 | tables by campus; current edition |
| Fee Structure page (HTML) | 2026-27 | no | 1 | tuition per credit hour, miscellaneous fees, refunds |
| Grading page (HTML) | 2026 | no | 1 | grade points and the CGPA scale |
| Faculty Entrepreneurship and Technical Start-ups Policy | 2022-05 | no | 8 | **scanned**, image-only |

## The seven required properties

| # | Required property | Document(s) that provide it | Evidence or notes |
| --- | --- | --- | --- |
| 1 | At least 6 documents, 40 pages | all 9 documents | 7 PDFs with 279 pages in total, plus 2 HTML pages: well above 6 documents and 40 pages. |
| 2 | Two versions of one policy | **Academic Rules Aug 2023** (superseded) and **Academic Rules Handbook Jun 2026** (current); also Annual Report 2019-20 vs 2023-24 | Recorded as `doc_version` `2023-08` / `2026-06` and `superseded: true` / `false` in `sources.yaml`, copied onto every chunk. They really differ: the 2026 text lets a final-year student take one extra course (clause 4.3) where the 2023 text allows more courses if the student is not "getting ahead of their batch"; the 130-credit-hour requirement (1.25 in 2023) is gone from 2026; the retake policy (section 9) exists only in 2026; and clause numbers shift (graduation CGPA is 1.26 in 2023, 1.25 in 2026). The Prospectus is a third rendering with its own numbering. |
| 3 | Scanned, image-only PDF | **Faculty Entrepreneurship and Technical Start-ups Policy** (8 of 8 pages) and **Academic Rules Aug 2023** (24 of 26 pages) | `pdffonts` on the policy prints an empty font table, and pdfplumber extracts 0 characters from its 8 pages and 176 from all 26 pages of the 2023 rules. Both are OCR'd with Tesseract (`indexer/pipeline/extract.py`) and every OCR'd page is marked `ocr=True` and logged. What OCR could not recover is in `docs/PIPELINE.md`. |
| 4 | A table with real information | **Annual Report 2023-24** and **2019-20** (ruled tables of publications, projects and activities by campus), the **Fee Structure** page and the **Grading** page (HTML tables) | e.g. 2023-24 page 63, "Campus Wise information of Journal Articles": Lahore 160, Islamabad 128, Karachi 62, Chiniot-Faisalabad 57, Peshawar 21, grand total 428. The fee page lists tuition per credit hour (Rs 12,000), the admission fee (Rs 30,000) and a miscellaneous-fees table. The before-and-after of how extraction keeps these columns is in `docs/PIPELINE.md`. |
| 5 | Long definitions | **Academic Rules Handbook Jun 2026**, section 10, "Policy on Generative AI Based Tools" (about 15 pages of numbered sub-sections) | e.g. "Unacceptable Uses of Generative AI Tools": a numbered list whose items run to several lines each, with sub-bullets. A fixed-size chunker cuts them mid-item; the structure-aware chunker keeps a paragraph or list item whole unless it is longer than the chunk size. |
| 6 | A deliberately absent topic | **Hostel and transport rules** | The Prospectus PDF "Transport and Hostel Facilities" is left out on purpose (its URL is in a comment in `sources.yaml`). A student plausibly asks "what is the hostel curfew?". The corpus only says that hostels and transport exist: the annual reports mention hostel facilities in a line or two, and the 2019-20 report (page 32) says buses run on a "self-finance basis" at each campus. There are no hostel rules, curfew, rent or application process, and no bus timetable or fare. That makes `a03` a deliberate near-miss: transport is mentioned, but the asked-for timetable and fee are not. The golden set asks three such questions (`a01` to `a03`) and the gate requires the system to decline all of them. |
| 7 | Exact-term content | Clause numbers (1.21 attendance, 5.4 academic warning, 1.25 graduation CGPA), CGPA thresholds (2.00 to avoid warning, 3.90 for Suma cum laude), fees (Rs 12,000 per credit hour, Rs 30,000 admission, Rs 5,000 late registration) | These are the questions embeddings blur ("clause 1.21" is nearly identical to "clause 1.22" as a vector). They are why retrieval is hybrid (dense plus BM25) and the golden set has an `exact` category with its own recall figure. |

## Traps in this corpus (on purpose)

* The 2023 and 2026 rules answer some questions differently, so retrieval must prefer the
  current one by metadata, not by prompt. A question that names the year ("in the 2023 rules")
  is allowed to reach the superseded document (`api/app/retrieval/versions.py`).
* Two documents are scans and one of them is the older version of the policy: a failed OCR would
  silently remove the old version from the index.
* The handbooks are set in columns. Plain text extraction reads straight across them and
  interleaves unrelated sentences, which is why extraction reads each column separately.

## Licence and attribution

The documents belong to FAST-NUCES and are used here only for coursework, fetched from public
pages. They are not redistributed: only their URLs and checksums are committed.
